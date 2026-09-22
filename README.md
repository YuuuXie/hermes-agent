# Hermes split preparation / backup tooling

This is a **tooling archive branch**, not a Hermes product release. Do not merge
or fast-forward a production Hermes checkout to this branch.

- Product checkpoint: `automation/update-candidate` in this fork.
- Accepted product pointer, when genuinely available: `automation/update-ready`.
- This branch: `automation/maintenance-tools`, preserving the supporting source
  and regression tests separately from the tested product tree.

## Components

- `ffadopt.py`: receipt-only, exact-base/SHA/tree, fast-forward-only consumer.
  Never merges conflicts, repairs code, runs tests, or installs dependencies.
- `publish_ready.py`: checks a full successful verifier receipt and the real
  remote commit/tree before atomic READY publication. There is no caller-supplied
  success flag.
- `daily_dispatch.py`: no READY -> pure backup; READY -> existing external
  maintenance controller with the separately configured receipt consumer.
  Adoption preflight failure does not block the backup fallback.
- `daily-backup-prepared.py`: invokes the existing backup program and checks its
  exit code plus remote-verification marker. It never changes production code.

The existing installation's `maintenance.py` continues to own backup verification,
maintenance locking, idle-safe reload requests and gateway runtime read-back.
No replacement scheduler, model provider or dependency installer is included here.

## Configuration and trust

Runtime configuration and state are intentionally **not committed**. They remain
in the active profile's private maintenance directory and its normal backups.
This source is deployment-specific (some scripts contain host-local paths); adapt
and review those paths before using it elsewhere.

The consumer configuration binds the production repo/branch, pending marker and
journal, READY/receipt root, exact fork ref, verifier and manifest paths+digests,
legacy marker-clear command and time budget. `config.json` is deployed beside
`ffadopt.py` for the controller's legacy `restart-clear` argv.

Trust is same-user controller-owned files, not a cryptographic signature chain
or a hostile-agent sandbox. All dependency/packaging file changes are conservatively
refused, even if the producer approved them. Such updates need a separate reviewed
transition/environment workflow; do not weaken this gate from a cron agent.

## Evidence scope

The adopter/publisher/dispatcher/protocol suites passed 59 tests, using real
isolated Git repositories. Backup transport, notifier and gateway runtime in the
controller bridge tests are explicit fixtures. The pure-backup wrapper passed
9 further isolated tests. A real read-only fetch from the GitHub fork verified
its checkpoint SHA/tree. These do **not** claim a production backup, adoption,
reload or the first naturally scheduled end-to-end run succeeded.

The source and parent corrections passed independent pre-wiring review. The
current product checkpoint remains pending full acceptance; storing its code in
GitHub does not make it a READY release.
