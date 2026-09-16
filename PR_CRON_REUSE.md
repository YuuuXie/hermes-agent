# PR_CRON_REUSE — historical report (feature retired)

> **Retired:** the cron-owned reuse implementation and its default-on setting have been removed.
> This report preserves historical design, attribution and test evidence; it is NOT current
> setup guidance and its commands must not be replayed. Native `/goal`, `/loop` and
> `/heartbeat` are the session-automation options. Old reuse job definitions are kept paused
> and fail closed if fired without an explicit maintainer migration. Their transcripts
> remain readable; unrelated reliability fixes and ordinary turn leases remain in place.

## Delivery and commits

- Worktree: `/home/yuxie1/overnight-audit-20260916/pr-cron-reuse`
- Branch: `feat/cron-reusable-session`
- Exact base: `783619ffab99b35ead0dd32bf26b9c7a8ab7c686`
- Implementation: `2c88d8895373731015a088b95799d70487cb0ee9` — opt-in cron-owned reusable execution sessions.
- Initial report: `126f13d0a502f2f89e7501b74358934b151b9faf`.
- The follow-up commit containing this revision corrects the initial report's attribution, candidate comparison, CLI example and verification. Locate it with `git log -1 -- PR_CRON_REUSE.md`.
- Local only: not pushed, not installed into a gateway. No production research jobs were run, created or changed. A toy CLI job was created **paused** in a temporary home and remained paused after editing.

A concurrent worker committed this shared worktree before the final commit attempt. The tested production/test files were verified byte-identical to that implementation commit (`git diff --exit-code`); its older report was not treated as verification evidence. In particular, its claim of five remaining baseline failures is superseded by the clean hermetic run below.

## Credit and code comparison

Credit for the reuse-only port goes to **konsisumer <der@konsi.org>**, author of [#60330](https://github.com/NousResearch/hermes-agent/pull/60330), source commit `c09fd55aa092cfd72b46a0b2e5c73e11dc0232ce`. This is an adaptation of its mode/store/CLI and cron-owned-root approach to the newer decomposed codebase, **not** a wholesale cherry-pick. Interactive targeting and its local admission lock were deliberately excluded.

The following conclusions come from downloaded diffs, not PR bodies. Diffs are preserved in `/home/yuxie1/overnight-audit-20260916/receipts/cron-reuse/`.

| Candidate | Code observed | Decision |
|---|---|---|
| #60330 — `c09fd55aa092cfd72b46a0b2e5c73e11dc0232ce` | Adds `session_mode`, stable `cron_<job_id>` reuse, actual transcript loading, store/CLI/tool/dashboard surfaces and run listing. Also adds `target_session_id` and a process-local keyed `threading.Lock` before history loading. | Preferred reuse-only reference. Its lock is real admission **within one process**, not cross-process exclusion. Replace that boundary with the existing durable turn lease; do not port interactive targets, mode `target`, or the new local lock. |
| #47094 — `49a7828221b74774f1c04dba453e3b579191747f` (Song Luo / rrrrrredy) | Accepts a configurable `session` ID; follows compression tip, validates root/tip source as `cron`, loads history and reopens it. Loading happens in the scheduler without an exclusive turn-lease bracket. | Useful ownership check, but arbitrary session naming is wider than one stable logical session per job. Do not adopt that surface. |
| #88312 — `b0745b16907893770d0b7073b1279cbadf23e37e` (will / zhao0112) | Replaces timestamped IDs with `cron_<job_id>_latest` unconditionally. No history loading or opt-in. Includes unrelated kanban parser changes. | Reject: changing the ID alone is not conversation continuation and changes the fresh default. No kanban changes ported. |

## Precise implemented scope

1. **Opt-in store and CLI.** `create_job(..., session_mode="reuse")`, updates, and `hermes cron create/edit --session-mode {fresh,reuse}`. Unset jobs keep the old absent-key storage shape and fresh behavior. The formatted job-response whitelist exposes the stored mode in CLI output. Existing `continuity` still means previous-output injection via `context_from`; it is not renamed or repurposed.
2. **One logical cron conversation per job and profile.** The scheduler computes `cron_<job_id>` without opening a second DB. It uses the normal profile-scoped session DB and passes a private admission flag to its ephemeral agent. There is **no `target_session_id`**, shared interactive/main-session injection, arbitrary session selector, or process-local-only locking mechanism.
3. **Admit before reading.** The existing `admit_durable_turn_lease` acquires the SQLite lineage-root lease even on the first fire, when the root has no session row yet. Only then does it resolve the compression tip, verify cron ownership, reopen an existing tip, and load history. The ordinary loop retains first-row creation and full metadata persistence. The existing lease refresher, interrupt handling and fenced writes remain the mechanism.
4. **Actual active-history replay.** The facade forwards loaded conversation history to the inner loop. Replay uses `repair_alternation=True` and `include_row_ids=True`, with default exclusion of inactive/compacted display rows and ancestors. `get_compression_tip` is deliberately narrower than the general resume resolver, which also considers eligible non-compression continuations; the general resolver already excludes branch/delegate/reset children and was not mischaracterized as following them here.
5. **Compaction remains enabled.** Reuse refuses missing SQLite, unsupported turn-lease persistence, disabled persistence or disabled automatic compression. It follows rotated tips and does not regrow archived history after in-place compaction. It uses existing compression/context-limit behavior rather than adding a second summarizer. This bounds model context through existing compaction; it does **not** promise bounded disk archives or guaranteed success of a compression-model call.
6. **Refuse contention rather than queue stale prompts.** Reuse uses `wait_seconds=0`. A busy fire receives the existing `session_busy` failure result instead of loading another holder's transcript. A later scheduled fire re-reads the job. Existing pause/disable/interrupt gates remain; edits become the new prompt for subsequent fires, not a rewrite of old transcript messages. No new cross-chat stop propagation is claimed.
7. **Execution ledger stays distinct.** Every fire still receives its own UUID execution row and status. A shared conversation is not a run row. Reuse cleanup does not end or retitle the conversation after the facade has released the lease, including refused contenders; that would race a successor. DB release remains bounded, and a timed-out worker keeps its handle until its future completes. Fresh finalization remains unchanged.
8. **Session browsing, not ledger rewriting.** `list_cron_job_runs` adds the reused root's compression tip to the existing fresh-ID prefix query; tips need not share the root's textual prefix. `hermes cron runs <job_id>` still lists execution ledger entries, not conversation rows.
9. **Narrow UI surface.** CLI and the Python `cronjob()` parameter support the opt-in. No new model-facing schema field was added (`CRONJOB_SCHEMA` / `_HANDLER_FORWARDED_ARGS` unchanged), and no dashboard/Desktop editor was extended.

## Verified tests

Final command, run with clean credentials/environment and per-file isolation:

```bash
HERMES_PYTHON=/home/yuxie1/.hermes/local-patches/stack/venv/bin/python \
HERMES_TEST_WORKERS=6 HERMES_TEST_FILE_RETRIES=0 \
scripts/run_tests.sh \
  tests/cron/ \
  tests/agent/test_turn_facade_lease.py \
  tests/agent/test_cross_process_turn_lease.py \
  tests/hermes_state/test_session_turn_lease.py \
  tests/tools/test_cronjob_tools.py \
  tests/hermes_cli/test_cron_parser_builder.py
```

**127 files; 1,517 passed; 0 failed; 1 skipped; exit 0.** The skipped test is Windows-only on this Linux host. `git diff --check` also passed.

Evidence: `/home/yuxie1/overnight-audit-20260916/receipts/cron-reuse/final-tests.log`.

The new acceptance suites execute **17 cases**:

- `tests/cron/test_cron_session_mode_reuse.py`: **7 passed**. Store/parser/display contracts; real separate-interpreter lease contention before history reads, both existing and absent root; compression/compaction active-tip replay with a non-prefix child; pre-admission stop and fresh no-load; missing persistence/foreign-source refusal; cleanup does not mutate a foreign holder's session.
- `tests/cron/test_reuse_tick_integration.py`: **10 passed**. Two real `scheduler.tick()` calls through `run_job` and the real `TurnFacadeMixin`/lease, with tick 2 receiving tick 1 history; distinct **completed** execution IDs; default fresh sessions; compression rotation; in-place compaction; edited next prompt; pause and disable; fail-closed missing persistence/compression; **same-job-ID A → B → A** through the actual `_profile_cron_scope` and separate SQLite files.

These are real scheduler/facade/SQLite tests, **not live inference tests**. The inner conversation loop is replaced by a recorder writing test turns; provider resolution, environment loading and delivery are stubbed. Production provider behavior, automatic LLM summarization and the full transcript-flush algorithm are not claimed to have been exercised by this integration harness. The existing lease/persistence regression suites were included separately.

Earlier launcher notifications were not test receipts: `nohup ... &` can exit successfully even when pytest later fails. An initial broad run also found a detached-finalizer call-shape regression, which was fixed before the final all-green run.

## CLI example and actual smoke check

```bash
hermes cron create "every 6h" \
  "Check the local queue; report only new items." \
  --name "queue check" --session-mode reuse --deliver local --paused

hermes cron edit <job_id> --session-mode fresh
hermes cron runs <job_id>  # per-fire execution ledger
```

Actual subprocess CLI smoke used `/tmp/cron-reuse-cli-verified-gka27cjx` as both HOME and HERMES_HOME with credentials omitted. It created toy job `244b5a35bc8f` and printed `Session mode: reuse`; JSON readback confirmed `session_mode="reuse"`, `enabled=false`. Editing printed `Session mode: fresh`; a second JSON readback confirmed `session_mode="fresh"`, still `enabled=false`. Both CLI processes exited 0. No tick or provider call was run for this toy job.

## Honest remaining / intentionally excluded

- No real-model run or production deployment; no real research jobs used.
- No dashboard/Desktop mode editor or model-facing tool schema expansion.
- No arbitrary target session, interactive injection, new retry queue, or cross-chat stop propagation.
- A refused busy occurrence is recorded as a failed fire; there is no immediate replay after the holder exits. A later scheduled fire uses current job state.
- Archived audit/history rows may accumulate on disk; no retention/deletion policy was added.
- This was longer than the requested eight-minute checkpoint. The delivered checkpoint is a tested local implementation plus these explicit boundaries, not a claim of live deployment.
