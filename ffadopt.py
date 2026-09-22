#!/usr/bin/env python3
"""Fail-closed, receipt-only fast-forward adopter (consumer side).

Contract (deliberately tiny):
  * It NEVER runs tests, never resolves conflicts, never creates a new merge
    commit (fast-forward only), never repairs, never retries, never calls an
    LLM, and never executes any code from the candidate. All verification work
    has already happened elsewhere and is attested by a receipt file produced
    by the trusted staged verifier.
  * Every doubt is fatal: missing/malformed/stale readiness, a receipt whose
    bytes do not hash to the pinned digest, a verifier or manifest identity
    that is not the config-bound one, a live repo that is dirty or not exactly
    on the tested base, a remote whose pinned ref has moved, or any
    dependency/packaging manifest change -> refuse, exit non-zero, touch no
    production code.
  * The only mutation it can ever perform on the live repo is
    ``git merge --ff-only <exact candidate sha>`` plus a backup ref, and then
    an atomic restart-needed marker in the legacy patchstack schema (exit 11).

CLI is compatible with the existing maintenance updater protocol:
    ffadopt.py apply <config.json>
    ffadopt.py restart-clear --state-file F --sha SHA   (forwarded to the
                                                         trusted legacy updater)

Security note: the receipt is a *controller-owned file*, not a cryptographic
proof. The trust boundary is "same user, same machine": this module gives no
protection against an attacker who can already write the controller's own
trusted directories. It is not a sandbox.
"""
from __future__ import annotations

import argparse, fcntl, fnmatch, hashlib, json, os, re, shutil, subprocess, sys
import tempfile, time
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_REFUSED = 30           # fail-closed refusal; production untouched
EXIT_RESTART_PENDING = 11   # legacy patchstack "code moved / restart pending"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Steps a *healthy* integration dry-run always emits. Verification is checked
#: separately: a healthy FIRST PASS emits only ``verify:1``; ``verify:2`` exists
#: only when the bounded repair attempt ran. Requiring both rejected every good
#: receipt and accepted only repaired ones.
REQUIRED_STEPS = ("candidate_clean_pre_verify", "candidate_clean_post_verify",
                  "no_stray_processes", "guard_set_intact",
                  "trusted_harness_intact:pre_promote")

#: Dependency-bearing filenames, borrowed verbatim from the trusted legacy
#: detector (patchstack.py DEPS_BASENAMES / DEPS_BASENAME_GLOBS). Matching is on
#: the basename so nested packages and nested lockfiles are covered. This is a
#: copy on purpose: we never import code from the candidate or from a path the
#: readiness file chooses.
DEPS_BASENAMES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock",
    "Pipfile", "Pipfile.lock", "constraints.txt", "environment.yml",
    "environment.yaml", "conda-lock.yml", "pdm.lock",
    "package.json", "package-lock.json", "npm-shrinkwrap.json",
    "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
})
DEPS_BASENAME_GLOBS = ("requirements*.txt", "requirements*.in", "*.lock")

#: No single git command may exceed this, and the whole apply is additionally
#: bounded by one global deadline. There is no hidden retry anywhere.
GIT_CMD_CAP = 120
APPLY_DEADLINE = 120
LOCK_TIMEOUT = 0            # non-blocking: a held mutex is an immediate refusal


class Refused(Exception):
    pass


class PostMergeFailure(Refused):
    """Raised only AFTER the live branch was fast-forwarded: the log must not
    claim production was untouched."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def refuse(reason: str, **kw):
    raise Refused(json.dumps({"reason": reason, **kw}, sort_keys=True))


# --- bounded, non-interactive git -------------------------------------------

class Deadline:
    """One global budget for the whole run; every git call is capped by both
    the remaining budget and a per-command ceiling."""

    def __init__(self, seconds: float = APPLY_DEADLINE):
        self.end = time.monotonic() + seconds
        self.total = seconds

    def slice(self) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            refuse("deadline_exceeded", budget_seconds=self.total)
        return min(left, GIT_CMD_CAP)


def _git_env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",   # never block on credential prompts
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    return env


def git_raw(repo: Path, *args: str, deadline: Deadline):
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True,
                              env=_git_env(), timeout=deadline.slice())
    except subprocess.TimeoutExpired:
        refuse("git_timed_out", argv=list(args), cap_seconds=GIT_CMD_CAP)


def git(repo: Path, *args: str, deadline: Deadline, check=True) -> str:
    p = git_raw(repo, *args, deadline=deadline)
    if check and p.returncode != 0:
        refuse("git_failed", argv=list(args), rc=p.returncode,
               stderr=p.stderr.strip()[:400])
    return p.stdout.strip()


def repo_lock_path(repo: Path, timeout: float = 10) -> Path:
    """The SAME per-repository mutex the legacy patchstack takes, so the two
    tools serialise against each other: ``<git-common-dir>/patchstack.lock``."""
    d = Deadline(timeout)
    common = git(repo, "rev-parse", "--git-common-dir", deadline=d, check=False) or ".git"
    lock_dir = Path(common) if os.path.isabs(common) else repo / common
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / "patchstack.lock"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_json(path: Path, what: str) -> dict:
    try:
        d = json.loads(path.read_text())
    except FileNotFoundError:
        refuse(f"{what}_missing", path=str(path))
    except json.JSONDecodeError as e:
        refuse(f"{what}_malformed", path=str(path), error=str(e))
    except OSError as e:
        refuse(f"{what}_unreadable", path=str(path), error=str(e))
    if not isinstance(d, dict):
        refuse(f"{what}_malformed", path=str(path), error="not an object")
    return d


def _int0(v) -> bool:
    """A genuine integer zero return code. ``True`` and ``"0"`` are not."""
    return isinstance(v, int) and not isinstance(v, bool) and v == 0


def _is_true(v) -> bool:
    return v is True


# --- readiness + receipt ----------------------------------------------------

READY_SCHEMA = 2
READY_FIELDS = ("candidate_sha", "candidate_tree", "base_sha", "receipt_path",
                "receipt_sha256", "verifier_path", "verifier_sha256",
                "manifest_path", "manifest_sha256", "remote_url", "remote_ref")

CONFIG_FIELDS = ("repo", "live_branch", "restart_state_file", "applied_journal",
                 "ready_file", "receipt_root", "remote_url", "remote_ref",
                 "verifier_path", "verifier_sha256", "manifest_path",
                 "manifest_sha256", "python", "legacy_updater")


def check_config(cfg: dict) -> None:
    for f in CONFIG_FIELDS:
        if not cfg.get(f):
            refuse("config_field_missing", field=f)


def _bound_file(cfg: dict, ready: dict, kind: str) -> None:
    """The readiness file may not point us at a file of its own choosing: path
    AND content digest must be the config-bound ones, and the file on disk must
    still hash to that digest right now."""
    pkey, skey = f"{kind}_path", f"{kind}_sha256"
    if str(Path(ready[pkey])) != str(Path(cfg[pkey])):
        refuse(f"{kind}_path_not_config_bound", ready=ready[pkey], config=cfg[pkey])
    if ready[skey] != cfg[skey]:
        refuse(f"{kind}_identity_mismatch")
    p = Path(cfg[pkey])
    actual = sha256_file(p) if p.is_file() else None
    if actual != cfg[skey]:
        refuse(f"{kind}_on_disk_changed", expected=cfg[skey], observed=actual)


def check_ready(cfg: dict, ready: dict) -> None:
    if ready.get("schema") != READY_SCHEMA:
        refuse("ready_schema_unsupported", schema=ready.get("schema"),
               supported=READY_SCHEMA)
    if ready.get("status") != "ready":
        refuse("ready_status_not_ready", status=ready.get("status"))
    for f in READY_FIELDS:
        if not ready.get(f):
            refuse("ready_field_missing", field=f)
    for f in ("candidate_sha", "candidate_tree", "base_sha"):
        if not SHA_RE.match(str(ready[f])):
            refuse("ready_field_not_sha40", field=f)
    if ready["candidate_sha"] == ready["base_sha"]:
        refuse("ready_candidate_equals_base")
    if ready["remote_url"] != cfg["remote_url"]:
        refuse("remote_not_config_bound", ready=ready["remote_url"],
               config=cfg["remote_url"])
    if ready["remote_ref"] != cfg["remote_ref"]:
        refuse("remote_ref_not_config_bound", ready=ready["remote_ref"],
               config=cfg["remote_ref"])
    # the verifier that produced the receipt AND the immutable manifest it ran
    # against must both be the config-bound, unchanged ones.
    _bound_file(cfg, ready, "verifier")
    _bound_file(cfg, ready, "manifest")


def _receipt_path(cfg: dict, ready: dict) -> Path:
    root = Path(cfg["receipt_root"]).resolve()
    rp = Path(ready["receipt_path"]).resolve()
    try:
        rp.relative_to(root)
    except ValueError:
        refuse("receipt_outside_configured_root", path=str(rp), root=str(root))
    return rp


def check_verification(r: dict, steps: list, cfg: dict) -> dict:
    """The LATEST verification attempt must have genuinely succeeded.

    Real ``run_verification`` labels attempts "1" (initial) and "2" (bounded
    repair). Each step carries a ``phases`` list: a ``selector`` phase and an
    ``extra`` phase which is either executed (returncode/file_count) or
    explicitly ``skipped``. We require the last attempt to be ok with an
    integer rc 0, no timeout, a genuinely executed selector phase, and -- when
    the config demands it -- a genuinely EXECUTED extra phase. A skipped extra
    is never accepted as a pass under ``require_extra``.
    """
    attempts = [s for s in steps if str(s.get("step", "")).startswith("verify:")]
    if not attempts:
        refuse("receipt_no_verification_attempt")
    last = attempts[-1]
    label = last["step"]
    if not _is_true(last.get("ok")):
        refuse("receipt_latest_verification_failed", step=label, ok=last.get("ok"))
    if not _int0(last.get("returncode")):
        refuse("receipt_verification_returncode_not_int_zero", step=label,
               returncode=last.get("returncode"))
    if last.get("timed_out") is not False:
        refuse("receipt_verification_timed_out_flag", step=label,
               timed_out=last.get("timed_out"))

    phases = last.get("phases")
    if not isinstance(phases, list) or not phases:
        refuse("receipt_verification_phases_missing", step=label)
    by_phase = {}
    for ph in phases:
        if not isinstance(ph, dict) or not ph.get("phase"):
            refuse("receipt_verification_phase_malformed", step=label)
        by_phase[ph["phase"]] = ph

    sel = by_phase.get("selector")
    if sel is None:
        refuse("receipt_selector_phase_absent", step=label)
    if "skipped" in sel or not _int0(sel.get("returncode")) or sel.get("timed_out") is not False:
        refuse("receipt_selector_phase_did_not_pass", step=label, phase=sel)
    if not sel.get("selector"):
        refuse("receipt_selector_phase_has_no_selector_path", step=label)

    extra = by_phase.get("extra")
    require_extra = bool(cfg.get("require_extra", True))
    if require_extra:
        if extra is None:
            refuse("receipt_extra_phase_absent_but_required", step=label)
        if "skipped" in extra:
            refuse("receipt_extra_phase_skipped_but_required", step=label,
                   skipped=extra.get("skipped"))
        if not _int0(extra.get("returncode")) or extra.get("timed_out") is not False:
            refuse("receipt_extra_phase_did_not_pass", step=label, phase=extra)
        fc, files = extra.get("file_count"), extra.get("files")
        if not (isinstance(fc, int) and not isinstance(fc, bool) and fc >= 1):
            refuse("receipt_extra_phase_executed_nothing", step=label, file_count=fc)
        if not isinstance(files, list) or not files:
            refuse("receipt_extra_phase_no_files", step=label)
    elif extra is not None and "skipped" not in extra:
        if not _int0(extra.get("returncode")) or extra.get("timed_out") is not False:
            refuse("receipt_extra_phase_did_not_pass", step=label, phase=extra)
    return last


def check_dependency_gates(steps: list) -> None:
    """EVERY dependency_gate:* step (``main`` and ``post_agent``) must exist,
    be ok, carry an explicit ``changed`` list, and that list must be empty.
    An absent or malformed gate is NOT an empty one."""
    gates = [s for s in steps if str(s.get("step", "")).startswith("dependency_gate:")]
    if not gates:
        refuse("receipt_dependency_gate_absent")
    for g in gates:
        name = g["step"]
        if not _is_true(g.get("ok")):
            refuse("receipt_dependency_gate_failed", step=name)
        if "changed" not in g or not isinstance(g["changed"], list):
            refuse("receipt_dependency_gate_has_no_changed_list", step=name)
        if g["changed"]:
            # Conservative by policy: no installer and no dependency-approval
            # bypass exists here by design, not even for approved changes.
            refuse("dependency_manifest_changed", step=name,
                   changed=[str(p) for p in g["changed"][:20]],
                   count=len(g["changed"]))


def _clean_step(steps_by_name: dict, name: str, sha: str | None, tree: str) -> None:
    s = steps_by_name[name]
    if sha is not None and s.get("sha") != sha:
        refuse("receipt_clean_step_sha_inconsistent", step=name,
               step_sha=s.get("sha"), receipt_sha=sha)
    if s.get("tree") != tree:
        refuse("receipt_clean_step_tree_inconsistent", step=name,
               step_tree=s.get("tree"), receipt_tree=tree)


def check_receipt(cfg: dict, ready: dict) -> dict:
    """Read the receipt EXACTLY ONCE (bytes -> hash -> parse) so there is no
    TOCTOU window between the digest check and the content we act on."""
    rp = _receipt_path(cfg, ready)
    try:
        raw = rp.read_bytes()
    except FileNotFoundError:
        refuse("receipt_missing", path=str(rp))
    except OSError as e:
        refuse("receipt_unreadable", path=str(rp), error=str(e))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != ready["receipt_sha256"]:
        refuse("receipt_content_hash_mismatch", expected=ready["receipt_sha256"],
               observed=digest)
    try:
        r = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        refuse("receipt_malformed", path=str(rp), error=str(e))
    if not isinstance(r, dict):
        refuse("receipt_malformed", path=str(rp), error="not an object")

    if r.get("schema") != 2:
        refuse("receipt_schema_unsupported", schema=r.get("schema"))
    if r.get("mode") != "integration":
        refuse("receipt_mode_unexpected", mode=r.get("mode"))
    if r.get("status") != "dry-run" or not _int0(r.get("exit_code")):
        refuse("receipt_not_successful_dry_run", status=r.get("status"),
               exit_code=r.get("exit_code"))
    if r.get("code_updated") is not False:
        refuse("receipt_code_updated_must_be_false", code_updated=r.get("code_updated"))
    # the receipt must attest the SAME immutable manifest the config binds
    if str(Path(str(r.get("manifest")))) != str(Path(cfg["manifest_path"])):
        refuse("receipt_manifest_not_config_bound", receipt=r.get("manifest"),
               config=cfg["manifest_path"])

    if r.get("candidate_sha") != ready["candidate_sha"]:
        refuse("receipt_candidate_sha_mismatch")
    if r.get("verified_tree") != ready["candidate_tree"]:
        refuse("receipt_verified_tree_mismatch")
    if r.get("candidate_tree_pre_verify") != r.get("verified_tree"):
        refuse("receipt_tree_moved_during_verify")
    if r.get("live_head_before") != ready["base_sha"]:
        refuse("receipt_tested_base_mismatch", tested=r.get("live_head_before"))

    raw_steps = r.get("steps")
    if not isinstance(raw_steps, list):
        refuse("receipt_steps_malformed")
    steps = [s for s in raw_steps if isinstance(s, dict) and s.get("step")]
    by_name = {}
    for s in steps:
        by_name[s["step"]] = s
    for name in REQUIRED_STEPS:
        s = by_name.get(name)
        if s is None:
            refuse("receipt_required_step_absent", step=name)
        if not _is_true(s.get("ok")):
            refuse("receipt_required_step_failed", step=name, ok=s.get("ok"))
    # the tree the verifier recorded around verification must be the tree the
    # receipt says it promoted; if the metadata disagrees the receipt may be
    # describing a pre-repair tree and we refuse rather than invent a transition
    _clean_step(by_name, "candidate_clean_pre_verify", None,
                r.get("candidate_tree_pre_verify"))
    _clean_step(by_name, "candidate_clean_post_verify", r.get("candidate_sha"),
                r.get("verified_tree"))

    check_verification(r, steps, cfg)
    check_dependency_gates(steps)
    return r


# --- dependency detection (independent of the receipt) ----------------------

def is_dep_path(path: str) -> bool:
    base = os.path.basename(path)
    if base in DEPS_BASENAMES:
        return True
    return any(fnmatch.fnmatch(base, g) for g in DEPS_BASENAME_GLOBS)


def changed_dependency_files(repo: Path, a: str, b: str, deadline: Deadline) -> list[str]:
    """Our OWN diff of the two commits. A receipt that claims no dependency
    change is not believed: we look. A git failure here is a refusal, never an
    empty result."""
    p = git_raw(repo, "diff", "--name-only", "-z", a, b, deadline=deadline)
    if p.returncode != 0:
        refuse("independent_dependency_diff_failed", rc=p.returncode,
               stderr=p.stderr.strip()[:300])
    return sorted({q for q in p.stdout.split("\0") if q and is_dep_path(q)})


# --- git side ---------------------------------------------------------------

def fetch_pinned(cfg: dict, ready: dict, scratch: Path, deadline: Deadline) -> str:
    """Fetch the pinned ref into a FRESH scratch repo. The candidate's own
    mutable .git is never trusted or consulted."""
    scratch.mkdir(parents=True, exist_ok=True)
    git(scratch, "init", "--bare", "-q", deadline=deadline)
    git(scratch, "fetch", "--no-tags", cfg["remote_url"],
        f"+{cfg['remote_ref']}:refs/adopt/pinned", deadline=deadline)
    got = git(scratch, "rev-parse", "refs/adopt/pinned", deadline=deadline)
    if got != ready["candidate_sha"]:
        refuse("remote_ref_moved", pinned_ref=cfg["remote_ref"],
               expected=ready["candidate_sha"], observed=got)
    tree = git(scratch, "rev-parse", "refs/adopt/pinned^{tree}", deadline=deadline)
    if tree != ready["candidate_tree"]:
        refuse("remote_tree_mismatch", expected=ready["candidate_tree"], observed=tree)
    return got


def import_objects(repo: Path, scratch: Path, deadline: Deadline) -> None:
    """Import the candidate objects into the live repo WITHOUT moving any
    branch: only refs/adopt/candidate is created."""
    git(repo, "fetch", "--no-tags", str(scratch),
        "refs/adopt/pinned:refs/adopt/candidate", deadline=deadline)


# --- applied journal --------------------------------------------------------

class Journal:
    """A durable, tiny record of what this adopter has applied and whether the
    runtime has since been verified to run it.

    Without it, a successfully cleared deployment looked identical to a crashed
    promotion (head == candidate, marker absent) and every daily run recreated
    the restart marker forever.
    """

    SCHEMA = 1

    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return d if isinstance(d, dict) and d.get("schema") == self.SCHEMA else None

    def _write(self, **fields) -> dict:
        prior = self.read() or {}
        state = {"schema": self.SCHEMA, **prior, **fields, "written_at": _now()}
        atomic_write_json(self.path, state)
        return state

    def record_intent(self, target: str, previous: str, run_id: str) -> dict:
        """Written BEFORE the fast-forward. If we crash between the ff and the
        marker, this is what proves a promotion was in flight."""
        return self._write(target_sha=target, previous_sha=previous,
                           run_id=run_id, applied=False, cleared=False)

    def record_applied(self, target: str) -> dict:
        return self._write(target_sha=target, applied=True, cleared=False)

    def record_cleared(self, target: str) -> dict:
        """After the maintenance caller verified runtime and its trusted
        legacy clear removed the matching pending marker."""
        return self._write(target_sha=target, applied=True, cleared=True,
                           cleared_at=_now())

    def is_cleared(self, target: str) -> bool:
        st = self.read()
        return bool(st and st.get("target_sha") == target and st.get("cleared") is True)


def write_marker(path: Path, repo: Path, branch: str, target: str,
                 previous: str, run_id: str) -> dict:
    prior = {}
    if path.exists():
        try:
            prior = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            prior = {}
    if not isinstance(prior, dict):
        prior = {}
    state = {
        "schema": 1,
        "repo": str(repo),
        "live_branch": branch,
        "target_sha": target,
        "previous_sha": previous,
        "first_pending_sha": prior.get("first_pending_sha", previous),
        "written_at": _now(),
        "run_id": run_id,
        "reason": "code on disk advanced; gateway restart required to run it",
    }
    atomic_write_json(path, state)
    return state


# --- the adoption -----------------------------------------------------------

def already_at_target(cfg: dict, ready: dict, journal: Journal, run_id: str,
                      log) -> int:
    """head == candidate. Decide between 'restart still pending' and a stable,
    cleared no-op. We never DROP a pending marker; we only decline to recreate
    one that a verified clear already retired."""
    state_file = Path(cfg["restart_state_file"])
    cand = ready["candidate_sha"]
    if state_file.exists():
        log(f"already at {cand[:12]}; restart still pending")
        return EXIT_RESTART_PENDING
    if journal.is_cleared(cand):
        log(f"already at {cand[:12]} and the runtime was verified on it; nothing to do")
        return EXIT_OK
    # marker gone without a verified clear: either an interrupted promotion or
    # a marker someone deleted by hand. Conservatively restore it.
    write_marker(state_file, Path(cfg["repo"]), cfg["live_branch"], cand,
                 (journal.read() or {}).get("previous_sha", ready["base_sha"]), run_id)
    journal.record_applied(cand)
    log("recovered missing restart marker for already-applied target "
        "(no verified runtime clear on record)")
    return EXIT_RESTART_PENDING


def adopt(cfg: dict, ready: dict, scratch: Path, journal: Journal, run_id: str,
          deadline: Deadline, log) -> int:
    repo = Path(cfg["repo"])
    branch = cfg["live_branch"]
    state_file = Path(cfg["restart_state_file"])
    cand = ready["candidate_sha"]
    base = ready["base_sha"]

    if git(repo, "status", "--porcelain", deadline=deadline):
        refuse("live_repo_dirty")
    head = git(repo, "rev-parse", "HEAD", deadline=deadline)
    if head != base:
        refuse("live_base_not_exact_tested_base", tested_base=base, live_head=head)

    # Import objects first, then RE-CHECK the live repo immediately before the
    # fast-forward: the import is the longest operation in the critical section.
    import_objects(repo, scratch, deadline)

    cur_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", deadline=deadline)
    head2 = git(repo, "rev-parse", "HEAD", deadline=deadline)
    if cur_branch != branch:
        refuse("live_branch_drifted_after_object_import", expected=branch,
               observed=cur_branch)
    if head2 != base:
        refuse("live_head_drifted_after_object_import", tested_base=base,
               observed=head2)
    if git(repo, "status", "--porcelain", deadline=deadline):
        refuse("live_repo_dirty_after_object_import")

    p = git_raw(repo, "merge-base", "--is-ancestor", head2, cand, deadline=deadline)
    if p.returncode != 0:
        refuse("candidate_does_not_descend_from_live_head")

    # Our own dependency verdict, computed from real objects, not the receipt.
    changed = changed_dependency_files(repo, head2, cand, deadline)
    if changed:
        refuse("independent_dependency_manifest_change", changed=changed[:20],
               count=len(changed),
               note="the receipt's dependency gate disagreed or was silent; "
                    "the adopter's own diff wins and it is conservative")

    backup_ref = f"refs/ffadopt-backup/{run_id}/{branch}"
    git(repo, "update-ref", backup_ref, head2, deadline=deadline)
    log(f"backup ref {backup_ref} -> {head2[:12]}")

    # durable intent BEFORE the mutation, so a crash between ff and marker is
    # still recoverable as "restart pending" rather than a silent no-op.
    journal.record_intent(cand, head2, run_id)

    try:
        # Even a timeout/nonzero exit here may occur after Git changed HEAD.
        # Never describe any subsequent failure as "production untouched".
        git(repo, "merge", "--ff-only", cand, deadline=deadline)
        # Persist pending state before optional Git readback can exhaust the
        # remaining budget. The pre-merge journal survives marker I/O failure.
        write_marker(state_file, repo, branch, cand, head2, run_id)
        journal.record_applied(cand)
        after = git(repo, "rev-parse", "HEAD", deadline=deadline)
        tree_after = git(repo, "rev-parse", "HEAD^{tree}", deadline=deadline)
        if after != cand or tree_after != ready["candidate_tree"]:
            raise PostMergeFailure(json.dumps(
                {"reason": "post_merge_state_unexpected", "head": after,
                 "tree": tree_after, "backup_ref": backup_ref}, sort_keys=True))
    except PostMergeFailure:
        raise
    except Exception as exc:
        raise PostMergeFailure(json.dumps(
            {"reason": "fast_forward_or_post_write_failure", "error": str(exc),
             "target_sha": cand, "backup_ref": backup_ref,
             "journal": str(journal.path), "restart_state": str(state_file)},
            sort_keys=True)) from exc
    log(f"{branch}: {head2[:12]} -> {after[:12]} (tree {tree_after[:12]})")
    log(f"restart marker: {state_file}")
    log(f"  clear with: restart-clear --state-file {state_file} --sha {after}")
    return EXIT_RESTART_PENDING


# --- commands ---------------------------------------------------------------

def cmd_apply(config_path: str) -> int:
    log = lambda m: print(f"[ffadopt] {m}", flush=True)
    run_id = os.urandom(6).hex()
    try:
        cfg = load_json(Path(config_path), "config")
        check_config(cfg)
    except Refused as e:
        print(f"[ffadopt] REFUSED (production untouched): {e}", flush=True)
        return EXIT_REFUSED

    ready_file = Path(cfg["ready_file"])
    if not ready_file.exists():
        # The cheap daily path: no git, no lock, no remote contact.
        log("no ready.json — nothing prepared; skipping (production untouched)")
        return EXIT_OK

    repo = Path(cfg["repo"])
    journal = Journal(Path(cfg["applied_journal"]))
    deadline = Deadline(float(cfg.get("apply_deadline_seconds", APPLY_DEADLINE)))
    scratch = None
    try:
        lock_path = repo_lock_path(repo, timeout=10)
    except Refused as e:
        log(f"REFUSED (production untouched): {e}")
        return EXIT_REFUSED

    with open(lock_path, "a+") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("REFUSED (production untouched): another adopter or the legacy "
                f"patchstack holds {lock_path}")
            return EXIT_REFUSED
        try:
            ready = load_json(ready_file, "ready")
            check_ready(cfg, ready)
            check_receipt(cfg, ready)

            head = git(repo, "rev-parse", "HEAD", deadline=deadline)
            cur_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD", deadline=deadline)
            if cur_branch != cfg["live_branch"]:
                refuse("live_branch_unexpected", expected=cfg["live_branch"],
                       observed=cur_branch)
            if head == ready["candidate_sha"]:
                return already_at_target(cfg, ready, journal, run_id, log)

            scratch = Path(tempfile.mkdtemp(prefix="ffadopt-scratch-"))
            fetch_pinned(cfg, ready, scratch, deadline)
            log(f"receipt + pinned remote agree on {ready['candidate_sha'][:12]}")
            return adopt(cfg, ready, scratch, journal, run_id, deadline, log)
        except PostMergeFailure as e:
            log(f"FAILURE AT/AFTER FAST-FORWARD — production MAY have changed: {e}")
            return EXIT_REFUSED
        except Refused as e:
            log(f"REFUSED (production untouched): {e}")
            return EXIT_REFUSED
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)


def _resolve_config(explicit: str | None) -> Path:
    """restart-clear/status are invoked by maintenance.py with the LEGACY argv
    (no --config), so fall back to the env var then to the adapter's sibling
    config. Fail closed if none is found."""
    for cand in (explicit, os.environ.get("FFADOPT_CONFIG"),
                 str(Path(__file__).resolve().parent / "config.json")):
        if cand and Path(cand).exists():
            return Path(cand)
    refuse("config_not_found", tried=[explicit, os.environ.get("FFADOPT_CONFIG")])


def cmd_delegate(cfg: dict, sub: str, argv: list[str]) -> int:
    """Forward verbatim to the trusted legacy updater; own no such logic."""
    legacy = Path(cfg["legacy_updater"])
    if not legacy.is_file():
        refuse("legacy_updater_missing", path=str(legacy))
    return subprocess.run([cfg["python"], str(legacy), sub, *argv], timeout=120).returncode


def cmd_restart_clear(cfg: dict, state_file: str, sha: str) -> int:
    """The maintenance caller verifies runtime first. The legacy updater only
    validates/removes its matching marker; it does not probe the runtime."""
    if Path(state_file).expanduser().resolve() != Path(cfg["restart_state_file"]).expanduser().resolve():
        refuse("state_file_not_config_bound", given=state_file,
               config=cfg["restart_state_file"])
    if not SHA_RE.match(str(sha or "")):
        refuse("sha_not_sha40", sha=sha)
    journal = Journal(Path(cfg["applied_journal"]))
    if not Path(state_file).exists():
        if journal.is_cleared(sha):
            return EXIT_OK
        refuse("no_pending_marker_to_acknowledge", sha=sha)
    marker = load_json(Path(state_file), "restart_state")
    if marker.get("target_sha") != sha:
        refuse("restart_marker_target_mismatch", expected=sha, observed=marker.get("target_sha"))
    rc = cmd_delegate(cfg, "restart-clear", ["--state-file", state_file, "--sha", sha])
    if rc == EXIT_OK and not Path(state_file).exists():
        Journal(Path(cfg["applied_journal"])).record_cleared(sha)
        print(f"[ffadopt] runtime verified on {sha[:12]}; recorded in the applied "
              "journal — future runs will not recreate the marker", flush=True)
    else:
        print(f"[ffadopt] legacy clear returned {rc}; NOT acknowledging "
              "(restart remains pending)", flush=True)
        if rc == EXIT_OK:
            return EXIT_REFUSED
    return rc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="ffadopt.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply"); a.add_argument("config")
    r = sub.add_parser("restart-clear")
    r.add_argument("--state-file", required=True)
    r.add_argument("--sha", required=True)
    r.add_argument("--config")
    s = sub.add_parser("restart-status")
    s.add_argument("--state-file")
    s.add_argument("--config")
    try:
        ns = ap.parse_args(argv)
        if ns.cmd == "apply":
            return cmd_apply(ns.config)
        cfg = load_json(_resolve_config(ns.config), "config")
        check_config(cfg)
        if ns.cmd == "restart-clear":
            return cmd_restart_clear(cfg, ns.state_file, ns.sha)
        fwd = ["--state-file", ns.state_file] if ns.state_file else []
        return cmd_delegate(cfg, ns.cmd, fwd)
    except Refused as e:
        print(f"[ffadopt] REFUSED: {e}", flush=True)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
