#!/usr/bin/env python3
"""Isolated real-Git tests for ffadopt.py. No network: the 'remote' is a local
bare fixture repo, pinned only through the test config. Nothing under
/home/yuxie1/.hermes is read or written.

The healthy-receipt fixture is a LITERAL transcription of a real integration.py
receipt (evidence/first-upgrade-receipt.json), NOT a fixture generated from
ffadopt's own constants -- otherwise a schema drift between the verifier and
the adopter would be invisible here.
"""
from __future__ import annotations

import fcntl, hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ffadopt  # noqa: E402


def run_git(cwd, *a):
    p = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)
    assert p.returncode == 0, f"git {a}: {p.stderr}"
    return p.stdout.strip()


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}

#: Step names, in order, exactly as a real successful integration dry-run emits
#: them. Transcribed from a real receipt; note there is NO ``verify:2`` on a
#: healthy first pass -- label 2 only exists when the bounded repair ran.
REAL_STEPS_BEFORE_VERIFY = [
    "repo_lock", "preflight", "backup_ref", "clone_candidate",
    "git_metadata_baseline", "merge_seed", "guard_selector",
    "trusted_harness_frozen", "trusted_harness_seed_drift", "fetch_upstream",
    "upstream_frozen", "trusted_shallow_boundary",
    "git_metadata_audit:before_untrusted_execution", "merge_upstream",
    "candidate_clean_pre_verify", "guard_set_intact",
    "trusted_harness_intact:pre_verify",
]
REAL_STEPS_AFTER_VERIFY = [
    "git_metadata_audit:post_verify_before_git", "candidate_clean_post_verify",
    "git_metadata_audit:pre_promote", "trusted_harness_intact:pre_promote",
    "no_stray_processes",
]


def verify_step(label="1", *, ok=True, rc=0, timed_out=False,
                selector_rc=0, extra="ran", extra_rc=0, extra_files=3):
    """A real ``verify:<label>`` step, with the real two-phase ``phases`` list."""
    phases = [{"phase": "selector", "returncode": selector_rc,
               "timed_out": False, "selector": "/srv/guard/verify_reliability.py"}]
    if extra == "ran":
        phases.append({"phase": "extra", "returncode": extra_rc, "timed_out": timed_out,
                       "files": [f"tests/t{i}.py" for i in range(extra_files)],
                       "file_count": extra_files})
    elif extra == "skipped":
        phases.append({"phase": "extra", "skipped": "no extra_patterns configured"})
    # extra == "absent" -> selector phase only
    return {"step": f"verify:{label}", "ok": ok, "at": "2026-09-22T00:00:00+00:00",
            "returncode": rc, "timed_out": timed_out, "phases": phases,
            "sandboxed": True, "log": "/srv/x.verify-1.log", "tail": "ok"}


class TempGitRepo:
    """Live repo + bare fixture remote + one extra candidate commit."""

    def __init__(self, root: Path, dep_change: bool = False):
        self.root = root
        self.live = root / "live"
        self.remote = root / "remote.git"
        self.state = root / "state"
        self.state.mkdir(parents=True)
        src = root / "src"
        src.mkdir()
        run_git(root, "init", "-q", "-b", "main", str(src))
        (src / "app.py").write_text("v=1\n")
        (src / "pyproject.toml").write_text("[project]\nname='x'\n")
        run_git(src, "add", "-A")
        run_git(src, "commit", "-qm", "base")
        self.base_sha = run_git(src, "rev-parse", "HEAD")
        (src / "app.py").write_text("v=2\n")
        if dep_change:
            # a REAL packaging-manifest change in the candidate commit
            (src / "pyproject.toml").write_text("[project]\nname='x'\ndeps=['evil']\n")
        run_git(src, "add", "-A")
        run_git(src, "commit", "-qm", "candidate")
        self.cand_sha = run_git(src, "rev-parse", "HEAD")
        self.cand_tree = run_git(src, "rev-parse", "HEAD^{tree}")
        run_git(root, "clone", "-q", "--bare", str(src), str(self.remote))
        run_git(root, "clone", "-q", str(src), str(self.live))
        run_git(self.live, "checkout", "-q", self.base_sha)
        run_git(self.live, "checkout", "-qB", "main", self.base_sha)


def make_receipt(path: Path, *, cand_sha, cand_tree, base_sha, manifest,
                 dep_changed=(), dep_ok=True, dep_omit_changed=False,
                 dep_gate_absent=False, extra_gates=(),
                 fail_step=None, status="dry-run", exit_code=0,
                 verifies=None, code_updated=False, schema=2,
                 mode="integration"):
    def plain(names):
        out = []
        for s in names:
            d = {"step": s, "ok": s != fail_step, "at": "2026-09-22T00:00:00+00:00"}
            # real integration.py records the exact sha/tree on both clean steps
            if s == "candidate_clean_pre_verify":
                d.update(sha=cand_sha, tree=cand_tree)
            elif s == "candidate_clean_post_verify":
                d.update(sha=cand_sha, tree=cand_tree,
                         note="exact tree recorded pre AND post verification")
            out.append(d)
        return out

    steps = plain(REAL_STEPS_BEFORE_VERIFY)
    if not dep_gate_absent:
        gate = {"step": "dependency_gate:main", "ok": dep_ok,
                "at": "2026-09-22T00:00:00+00:00"}
        if not dep_omit_changed:
            gate["changed"] = list(dep_changed)
        steps.append(gate)
    for g in extra_gates:
        steps.append(g)
    steps.extend(verifies if verifies is not None else [verify_step("1")])
    steps.extend(plain(REAL_STEPS_AFTER_VERIFY))
    rec = {"run_id": "abc123", "schema": schema, "mode": mode, "status": status,
           "exit_code": exit_code, "code_updated": code_updated,
           "runtime_restart_pending": False,
           "manifest": str(manifest),
           "repo": "/srv/live", "candidate_sha": cand_sha,
           "live_head_before": base_sha,
           "candidate_tree_pre_verify": cand_tree, "verified_tree": cand_tree,
           "steps": steps}
    path.write_text(json.dumps(rec, indent=2))
    return rec


class AdopterTest(unittest.TestCase):
    dep_change = False

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ffadopt-test-"))
        os.environ.update(ENV)
        self.g = TempGitRepo(self.root, dep_change=self.dep_change)
        self.verifier = self.root / "integration.py"
        self.verifier.write_text("# trusted staged verifier stand-in\n")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text('{"trusted": "manifest"}\n')
        self.receipt_root = self.root / "receipts"
        self.receipt_root.mkdir()
        self.receipt = self.receipt_root / "receipt.json"
        self.legacy = self.root / "legacy_updater.py"
        self.legacy.write_text(
            "import sys, json, pathlib\n"
            "a = sys.argv\n"
            "sf = pathlib.Path(a[a.index('--state-file') + 1])\n"
            "sha = a[a.index('--sha') + 1]\n"
            "st = json.loads(sf.read_text()) if sf.exists() else None\n"
            "if st is None: print('nothing to clear'); sys.exit(0)\n"
            "if st.get('target_sha') != sha: print('mismatch'); sys.exit(11)\n"
            "sf.unlink(); print('cleared'); sys.exit(0)\n")
        self.make_receipt()
        self.cfg_path = self.root / "config.json"
        self.ready_path = self.root / "ready.json"
        self.state_file = self.g.state / "restart-needed.json"
        self.journal = self.g.state / "ffadopt-journal.json"
        self.write_cfg()
        self.write_ready()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_receipt(self, **over):
        kw = dict(cand_sha=self.g.cand_sha, cand_tree=self.g.cand_tree,
                  base_sha=self.g.base_sha, manifest=self.manifest)
        kw.update(over)
        return make_receipt(self.receipt, **kw)

    def write_cfg(self, **over):
        cfg = {"repo": str(self.g.live), "live_branch": "main",
               "restart_state_file": str(self.state_file),
               "applied_journal": str(self.journal),
               "ready_file": str(self.ready_path),
               "receipt_root": str(self.receipt_root),
               "remote_url": str(self.g.remote), "remote_ref": "refs/heads/main",
               "verifier_path": str(self.verifier),
               "verifier_sha256": sha256(self.verifier.read_bytes()),
               "manifest_path": str(self.manifest),
               "manifest_sha256": sha256(self.manifest.read_bytes()),
               "require_extra": True,
               "python": sys.executable, "legacy_updater": str(self.legacy)}
        cfg.update(over)
        self.cfg_path.write_text(json.dumps(cfg))
        return cfg

    def write_ready(self, **over):
        r = {"schema": 2, "status": "ready",
             "candidate_sha": self.g.cand_sha, "candidate_tree": self.g.cand_tree,
             "base_sha": self.g.base_sha, "receipt_path": str(self.receipt),
             "receipt_sha256": sha256(self.receipt.read_bytes()),
             "verifier_path": str(self.verifier),
             "verifier_sha256": sha256(self.verifier.read_bytes()),
             "manifest_path": str(self.manifest),
             "manifest_sha256": sha256(self.manifest.read_bytes()),
             "remote_url": str(self.g.remote), "remote_ref": "refs/heads/main"}
        r.update(over)
        self.ready_path.write_text(json.dumps(r))
        return r

    def resync(self):
        """Re-pin the receipt hash in ready.json after rewriting the receipt."""
        self.write_ready(receipt_sha256=sha256(self.receipt.read_bytes()))

    def apply(self):
        return ffadopt.main(["apply", str(self.cfg_path)])

    def clear(self, sha=None):
        return ffadopt.main(["restart-clear", "--state-file", str(self.state_file),
                             "--config", str(self.cfg_path),
                             "--sha", sha or self.g.cand_sha])

    def head(self):
        return run_git(self.g.live, "rev-parse", "HEAD")

    # -- preserved coverage (1-15) -------------------------------------------

    def test_01_no_ready_is_noop_exit0(self):
        self.ready_path.unlink()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.head(), self.g.base_sha)
        self.assertFalse(self.state_file.exists())

    def test_02_success_exact_ff_marker_and_readback(self):
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertEqual(self.head(), self.g.cand_sha)
        st = json.loads(self.state_file.read_text())
        self.assertEqual(st["schema"], 1)
        self.assertEqual(st["target_sha"], self.g.cand_sha)
        self.assertEqual(st["previous_sha"], self.g.base_sha)
        self.assertEqual(st["first_pending_sha"], self.g.base_sha)
        refs = run_git(self.g.live, "for-each-ref", "--format=%(refname)",
                       "refs/ffadopt-backup")
        self.assertIn("refs/ffadopt-backup", refs)

    def test_03_second_run_is_idempotent_noop_keeps_marker(self):
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        first = json.loads(self.state_file.read_text())
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertEqual(self.head(), self.g.cand_sha)
        self.assertEqual(json.loads(self.state_file.read_text())["target_sha"],
                         first["target_sha"])

    def test_04_marker_recovery_after_crash(self):
        """Promotion crashed between the ff and the marker: journal shows the
        intent, marker is gone -> must still report restart pending."""
        run_git(self.g.live, "merge", "--ff-only", self.g.cand_sha)
        ffadopt.Journal(self.journal).record_intent(
            self.g.cand_sha, self.g.base_sha, "crashrun")
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertEqual(json.loads(self.state_file.read_text())["target_sha"],
                         self.g.cand_sha)

    def test_05_stale_base_refused(self):
        run_git(self.g.live, "commit", "-q", "--allow-empty", "-m", "live drift")
        drifted = self.head()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), drifted)
        self.assertFalse(self.state_file.exists())

    def test_06_divergent_target_not_ff_refused(self):
        run_git(self.g.live, "checkout", "-qB", "main", self.g.base_sha)
        run_git(self.g.live, "commit", "-q", "--allow-empty", "-m", "divergent")
        div = self.head()
        self.make_receipt(base_sha=div)
        self.write_ready(base_sha=div, receipt_sha256=sha256(self.receipt.read_bytes()))
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), div)

    def test_07_failed_receipt_step_refused(self):
        self.make_receipt(fail_step="guard_set_intact")
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_08_mutated_receipt_hash_mismatch_refused(self):
        self.receipt.write_text(self.receipt.read_text() + "\n")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_09_dirty_live_repo_refused(self):
        (self.g.live / "app.py").write_text("locally edited\n")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_10_remote_sha_moved_refused(self):
        run_git(self.g.remote, "update-ref", "refs/heads/main", self.g.base_sha)
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_11_dependency_change_refused(self):
        self.make_receipt(dep_changed=["pyproject.toml"])
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_12_unbound_verifier_or_remote_refused(self):
        self.write_ready(verifier_path="/tmp/evil.py")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.write_ready(remote_url="/tmp/evil.git")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_13_verifier_on_disk_changed_refused(self):
        self.verifier.write_text("# tampered\n")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_14_malformed_and_stale_ready_refused(self):
        self.ready_path.write_text("{not json")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.write_ready(schema=99)
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.write_ready(candidate_sha="deadbeef")
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_15_receipt_tree_or_status_mismatch_refused(self):
        self.make_receipt(status="aborted", exit_code=1)
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    # -- REGRESSION 1: healthy first-pass receipts (verify:1 only) -----------

    def test_16_healthy_first_pass_receipt_without_verify2_is_adopted(self):
        """A real healthy run emits ONLY verify:1. Requiring verify:2 rejected
        every good receipt and only accepted repaired ones."""
        self.make_receipt(verifies=[verify_step("1")])
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertEqual(self.head(), self.g.cand_sha)

    def test_17_repaired_run_latest_verify_wins_and_last_failure_rejected(self):
        # 1 failed, 2 succeeded (real repair path) -> adopt
        self.make_receipt(verifies=[verify_step("1", ok=False, rc=1, extra_rc=1),
                                    verify_step("2")])
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertEqual(self.head(), self.g.cand_sha)
        # ... and the reverse order (good then failed) must be REFUSED
        run_git(self.g.live, "checkout", "-qB", "main", self.g.base_sha)
        self.state_file.unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)
        self.make_receipt(verifies=[verify_step("1"),
                                    verify_step("2", ok=False, rc=1, extra_rc=1)])
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_18_verify_phase_integrity(self):
        """Phases must have genuinely executed: no skipped extra when the config
        requires it, no missing selector phase, integer rc 0 (not "0")."""
        cases = [
            dict(verifies=[verify_step("1", extra="skipped")]),
            dict(verifies=[verify_step("1", extra="absent")]),
            dict(verifies=[verify_step("1", extra_files=0)]),
            dict(verifies=[verify_step("1", selector_rc="0")]),
            dict(verifies=[verify_step("1", extra_rc="0")]),
            dict(verifies=[verify_step("1", timed_out=True)]),
            dict(verifies=[dict(verify_step("1"), ok="yes")]),
            dict(verifies=[]),
        ]
        for i, kw in enumerate(cases):
            with self.subTest(case=i):
                self.make_receipt(**kw)
                self.resync()
                self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
                self.assertEqual(self.head(), self.g.base_sha)

    def test_19_require_extra_false_accepts_skipped_extra(self):
        self.write_cfg(require_extra=False)
        self.make_receipt(verifies=[verify_step("1", extra="skipped")])
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)

    # -- REGRESSION 2: stable cleared no-op ---------------------------------

    def test_20_cleared_deployment_is_a_stable_noop(self):
        """apply -> 11, restart-clear (delegated, verified) -> 0, then the next
        DAILY apply must be exit 0 with NO marker recreated."""
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertTrue(self.state_file.exists())
        self.assertEqual(self.clear(), 0)
        self.assertFalse(self.state_file.exists())
        for _ in range(3):  # three more daily runs
            self.assertEqual(self.apply(), ffadopt.EXIT_OK)
            self.assertFalse(self.state_file.exists())
        self.assertEqual(self.head(), self.g.cand_sha)

    def test_21_clear_failure_does_not_acknowledge(self):
        """A legacy clear that refuses (runtime not on target) must NOT mark the
        journal cleared; the next apply still reports restart pending."""
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        rc = self.clear(sha="0" * 40)
        self.assertNotEqual(rc, 0)
        self.assertTrue(self.state_file.exists())
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        # and if the marker is then lost without a verified clear, recover it
        self.state_file.unlink()
        self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        self.assertTrue(self.state_file.exists())

    def test_22_legacy_argv_without_config_uses_env_binding(self):
        os.environ["FFADOPT_CONFIG"] = str(self.cfg_path)
        try:
            self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
            rc = ffadopt.main(["restart-clear", "--state-file", str(self.state_file),
                               "--sha", self.g.cand_sha])
            self.assertEqual(rc, 0)
            self.assertEqual(self.apply(), ffadopt.EXIT_OK)
        finally:
            os.environ.pop("FFADOPT_CONFIG", None)

    # -- REGRESSION 3: real dependency check --------------------------------

    def test_23_absent_or_failed_dependency_gate_refused(self):
        for kw in (dict(dep_gate_absent=True), dict(dep_ok=False),
                   dict(dep_omit_changed=True)):
            with self.subTest(**kw):
                self.make_receipt(**kw)
                self.resync()
                self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
                self.assertEqual(self.head(), self.g.base_sha)

    def test_24_post_agent_gate_with_changes_refused(self):
        """Every dependency_gate:* is inspected, not just :main."""
        self.make_receipt(extra_gates=[
            {"step": "dependency_gate:post_agent", "ok": True,
             "changed": ["scripts/whatsapp-bridge/package.json"]}])
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    # -- REGRESSION 5: bounded commands / repo lock -------------------------

    def test_25_held_repo_lock_refuses(self):
        lock = ffadopt.repo_lock_path(Path(self.g.live), timeout=10)
        with open(lock, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)
        self.assertFalse(self.state_file.exists())

    def test_26_live_drift_after_object_import_refused(self):
        """The lock-held recheck happens AFTER objects are imported and
        immediately before the ff: a drift there must abort, not ff."""
        orig = ffadopt.import_objects

        def drifting(repo, scratch, deadline):
            orig(repo, scratch, deadline)
            run_git(repo, "commit", "-q", "--allow-empty", "-m", "external drift")

        ffadopt.import_objects = drifting
        try:
            self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        finally:
            ffadopt.import_objects = orig
        self.assertNotEqual(self.head(), self.g.cand_sha)
        self.assertFalse(self.state_file.exists())

    def test_27_git_commands_are_bounded_and_noninteractive(self):
        seen = {}
        orig = subprocess.run

        def spy(argv, **kw):
            if argv and argv[0] == "git":
                seen.setdefault("timeouts", []).append(kw.get("timeout"))
                seen.setdefault("prompt", []).append(
                    (kw.get("env") or {}).get("GIT_TERMINAL_PROMPT"))
            return orig(argv, **kw)

        subprocess.run = spy
        try:
            self.assertEqual(self.apply(), ffadopt.EXIT_RESTART_PENDING)
        finally:
            subprocess.run = orig
        self.assertTrue(seen["timeouts"])
        self.assertTrue(all(isinstance(t, (int, float)) and 0 < t <= 120
                            for t in seen["timeouts"]), seen["timeouts"])
        self.assertTrue(all(p == "0" for p in seen["prompt"]), seen["prompt"])

    # -- REGRESSION 4: readiness / manifest binding -------------------------

    def test_28_ready_status_and_manifest_binding(self):
        for over in ({"status": "draft"}, {"status": None},
                     {"manifest_path": "/tmp/evil-manifest.json"},
                     {"manifest_sha256": "0" * 64}):
            with self.subTest(**over):
                self.write_ready(**over)
                self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.write_ready()
        self.manifest.write_text('{"tampered": true}\n')  # config pins old digest
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_29_receipt_schema_mode_and_code_updated(self):
        for over in ({"schema": 1}, {"mode": "patchstack"},
                     {"code_updated": True}, {"status": "ok-code-updated-restart-pending"}):
            with self.subTest(**over):
                self.make_receipt(**over)
                self.resync()
                self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
                self.assertEqual(self.head(), self.g.base_sha)

    def test_30_receipt_outside_configured_root_refused(self):
        outside = self.root / "elsewhere.json"
        shutil.copy(self.receipt, outside)
        self.write_ready(receipt_path=str(outside),
                         receipt_sha256=sha256(outside.read_bytes()))
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_31_pre_and_post_verify_tree_must_match(self):
        r = self.make_receipt()
        r["candidate_tree_pre_verify"] = "b" * 40
        self.receipt.write_text(json.dumps(r, indent=2))
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)

    def test_32_clean_step_sha_tree_must_match_final_receipt(self):
        """candidate_clean_post_verify records the exact sha/tree; if that
        disagrees with the receipt body, the metadata is inconsistent (e.g. a
        pre-repair tree) and we refuse rather than invent a transition."""
        r = self.make_receipt()
        for s in r["steps"]:
            if s["step"] == "candidate_clean_post_verify":
                s["sha"], s["tree"] = self.g.cand_sha, "c" * 40
        self.receipt.write_text(json.dumps(r, indent=2))
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)


class DependencyDiffTest(AdopterTest):
    """The candidate REALLY changes pyproject.toml; the receipt claims no
    dependency change. The adopter must find it with its own git diff."""
    dep_change = True

    def test_90_lying_receipt_caught_by_independent_diff(self):
        self.make_receipt(dep_changed=[])   # receipt lies: nothing changed
        self.resync()
        self.assertEqual(self.apply(), ffadopt.EXIT_REFUSED)
        self.assertEqual(self.head(), self.g.base_sha)
        self.assertFalse(self.state_file.exists())

    # the inherited cases either still pass or are about other gates; keep only
    # the ones whose meaning does not change under a dependency-bearing diff.
    for _n in list(vars(AdopterTest)):
        if _n.startswith("test_") and _n not in (
                "test_01_no_ready_is_noop_exit0",
                "test_08_mutated_receipt_hash_mismatch_refused",
                "test_09_dirty_live_repo_refused",
                "test_10_remote_sha_moved_refused",
                "test_11_dependency_change_refused",
                "test_12_unbound_verifier_or_remote_refused",
                "test_13_verifier_on_disk_changed_refused",
                "test_14_malformed_and_stale_ready_refused",
                "test_15_receipt_tree_or_status_mismatch_refused"):
            locals()[_n] = None
    del _n


if __name__ == "__main__":
    unittest.main(verbosity=2)
