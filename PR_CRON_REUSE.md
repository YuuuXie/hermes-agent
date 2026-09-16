# PR_CRON_REUSE — opt-in cron-owned reusable sessions

**Branch:** `feat/cron-reusable-session` (worktree `/home/yuxie1/overnight-audit-20260916/pr-cron-reuse`)
**Base:** `783619ffab99b35ead0dd32bf26b9c7a8ab7c686` (reviewed integration HEAD)
**Commit:** `2c88d88953` — `feat(cron): opt-in cron-owned reusable execution sessions`
**Not pushed. No live jobs, profiles, or gateway touched.**

---

## What ships

A per-job `session_mode`:

| mode | behaviour |
|---|---|
| `fresh` (default) | one `cron_<job_id>_<timestamp>` session per tick — **unchanged**, and the key is *absent* from pre-feature job records, not rewritten |
| `reuse` | one stable cron-OWNED `cron_<job_id>` session whose transcript each tick continues |

There is **no** `target_session_id` and no interactive-session injection.

## Candidate comparison (code, not bodies)

| PR | verdict |
|---|---|
| **#60330** `c09fd55a` | The reference. Ported: the `session_mode` vocabulary + aliases, the stable-id branch in `list_cron_job_runs`, the CLI/store surface, the reuse-stable title idea. **Rejected:** `target_session_id` + `_validate_target_session_ownership` (writes cron turns into user sessions — explicitly out of scope), and `_ResolvedSessionAdmission` — a **process-local** `threading.Lock` keyed by session id. That is not exclusion: two gateways / a gateway + `hermes cron run` are different processes, so its history read races. This codebase already has the correct primitive (durable `session_turn_leases` row lease), so I used it instead of adding a second, weaker one. Its scheduler also read the transcript *before* any admission. |
| **#47094** `49a78282` | Same shape, no `hermes_state` run-listing fix, same pre-lease read. Nothing to take that #60330 didn't have. |
| **#88312 / #22044** | Stable id only. Confirms the audit note: it never actually loads prior conversation, and it drags in unrelated kanban churn. Not used. |

## Design decisions

1. **History is loaded inside the existing turn lease, not in the scheduler.**
   `agent/turn_facade_lease.py::admit_durable_turn_lease` already brackets
   load→run→flush for every shared-session caller. A new `_load_durable_history_on_admission`
   flag on the agent reuses that bracket. No new lock. The scheduler resolves only the
   *logical id* (`cron_<job_id>`) and touches no DB.
2. **Compression tip only.** The reload uses `get_compression_tip`, **not**
   `resolve_resume_session_id` — the latter also walks branch/delegate/reset children, which
   could let an unrelated child hijack the cron root. This is what keeps the reused session
   from growing without compaction.
3. **A reuse tick never waits behind a holder** (`wait_seconds=0`). Waiting would run a stale
   captured prompt after the operator edited/paused the job. Refused fire → next scheduled
   fire re-reads the current definition. Stop/latest-prompt amendments honoured.
4. **The reused conversation is not ended or retitled per fire.** Per-run completion stays in
   `executions.db`; ending the shared row each tick would race a successor holder.
   Distinct per-run execution ids are preserved (tested).
5. **Cron-owned only.** `source='cron'` is verified before the read; reuse hard-fails without
   a session store and without compression enabled, rather than silently degrading.
6. **User-owned lane.** `session_mode` is in the store, the CLI, and `cronjob()`'s signature,
   but deliberately **absent from `CRONJOB_SCHEMA` and `_HANDLER_FORWARDED_ARGS`** — same rule
   as `--model`. The agent must not opt its own unattended jobs into a durable, user-visible
   conversation. (Tested.)
7. **Profile isolation** is inherited, not re-implemented: the lease rows and transcripts live
   in the profile-scoped `state.db`.

## Files

| file | change |
|---|---|
| `cron/jobs.py` | `_normalize_session_mode` + create/update normalizer registration; optional-key persistence |
| `cron/scheduler.py` | `cron_session_mode()`, `_cron_execution_session_id()`, reuse arming, reuse-aware finalizer |
| `cron/scheduler_detached_worker.py` | `reuse_session` passthrough |
| `agent/turn_facade_lease.py` | durable-history load under the lease, compression-tip walk, cron-ownership check, no-wait admission |
| `hermes_state_portability.py` | `list_cron_job_runs` matches the stable id (indexed, no wildcard scan) |
| `hermes_cli/{cron,subcommands/cron,tips}.py` | `--session-mode`, display line, tip text |
| `tools/cronjob_{tools,job_args}.py` | programmatic param; schema deliberately untouched |
| `website/docs/user-guide/features/cron.md` | user docs section |
| `tests/cron/test_cron_session_mode_reuse.py` | 6 contract tests |
| `tests/cron/test_reuse_tick_integration.py` | 9 real-`tick()` integration tests |

## Tests (real temp SQLite; only the inference boundary faked)

Runner: `~/.hermes/local-patches/stack/venv/bin/python -m pytest`

- **New suites — 15 passed.** Cover: two sequential real `tick()` calls sharing one
  conversation with tick 2 receiving tick 1's transcript; distinct execution-ledger ids across
  those ticks; compression rotation continuing on the child; compaction-archived rows excluded
  from the model-fed reload; cross-process takeover refused *before* any history read; fresh
  default unchanged and loading no history; profile A→B→A isolation through the real
  `_profile_cron_scope`; store/CLI round-trip; missing-DB and foreign-source fail-closed;
  schema absence.
- **Regression sweep** (`tests/cron/` + turn-lease + cronjob-tool + cron-parser suites):
  **1510 passed, 5 failed, 1 skipped**.
- The 5 failures are all `tests/cron/test_mirror_origin_fallback.py` and **reproduce identically
  on an untouched checkout of the base commit** (verified in `/tmp/cron-baseline`) —
  pre-existing, unrelated to this change.

## CLI example

```bash
hermes cron create "every 6h" \
  "Continue the paper triage; skip anything you already read." \
  --name "paper triage" --session-mode reuse

hermes cron edit <job_id> --session-mode fresh   # switch back any time
hermes cron runs <job_id>                        # stable session + older timestamped runs
```

Verified store round-trip on a throwaway `HERMES_HOME`:
`session_mode='reuse'` persists; a plain job has **no** `session_mode` key;
reuse resolves to `cron_<job_id>`, fresh to `cron_<job_id>_<stamp>`.

## Honest remaining

- Dashboard/web `CronJobCreate|Update` pydantic models were **not** extended (#60330 did).
  The field flows through the store and CLI; the dashboard editor will not show it yet.
- Desktop Bot-Mode routine dialog likewise untouched.
- No end-to-end run against a real model was performed (no real research jobs, per the task).
- The 5 pre-existing `test_mirror_origin_fallback` failures are left as found.

## Coordination note

This worktree was being edited concurrently by another agent during the session; the two
converged on the same design (my helper names and the `_load_durable_history_on_admission`
hook survived, hardened with the ownership check, the no-wait admission and the reuse-aware
finalizer). The committed state is the merged result and is fully green on the new suites.
