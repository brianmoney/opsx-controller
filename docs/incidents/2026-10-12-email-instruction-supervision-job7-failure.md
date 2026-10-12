# Incident report — Job 7 supervised startup failure (email-instruction-service-replacement)

- Date: 2026-10-12 (UTC)
- Area: native supervised execution (`lib/orchestrator/supervision.py`, `lib/supervisor/session_bridge.py`, session-server lifecycle)
- Workspace under test: isolated plan worktree `knowledge-forge` @ `plan/email-instruction-service-replacement` (Job 7, owner `operator:1000`)
- Outcome: **retired as a failure record.** Job 7 was cancelled; the native registration is not carried forward. Future work proceeds in the owner-selected manual / direct-dispatch mode.

## Summary

A registered supervised job (Job 7) activated and immediately stopped at the first
human-only gate before any model dispatch. Enabling the prepared primary session then
failed at **session creation** with `HTTP 400`, and the resulting worker-owned headless
session server could not be reaped by the service principal, producing a restart storm.
No product work occurred: **$0.00 registered usage, zero model/action dispatches, 0/13
changes**. The session-create defect was fixed and installed; the cross-UID process
lifecycle defect remains open.

## Impact

- No inference, no side effects, no product implementation/deployment/qualification/soak.
- Registered budget consumed: `$0.00` of `$100.00`; `0` min of `2880` min; `0`
  reservations, `0` action receipts, `0` incidents recorded.
- The execution ledger did become `active` (a start was recorded), but **zero model or
  work actions were dispatched**. This is not a "never started" job; it is an activated
  job with no work actions.
- Containment + authorized cleanup were required to stop the affected unit and remove one
  orphaned worker process.

## Timeline (UTC, 2026-10-12)

1. **18:58:27** — Job 7 registration created (`supervised-email-instruction-service-
   replacement-141edcaa9589`); protected manifest snapshot
   `e37b3efd0f78ae60cac00377db9698bceb1567ce3c345beb08ce23fd66a03086`.
2. **~19:39:0x** — `opsx-plan supervise start` transitioned `registered → active` and
   surfaced the first human-only approval gate **before any model dispatch**:
   `StaleMaterialError: change add-provider-neutral-mail-contracts is not dispatchable:
   awaiting human-only approval`. (By design; not a defect.)
3. **19:39:18** — Enabling the prepared primary override (`serve --primary-session`)
   spawned the worker-domain headless session server:
   `opencode serve --hostname 127.0.0.1 --port 41007` (uid 1002).
4. **19:39:20** — Primary session create failed:
   `TransportError: session create failed with HTTP 400 {"_tag":"BadRequest"}`.
   Diagnosed as a **session-create model dialect mismatch**: the create stage's model
   identifier was serialized in the legacy dialect while the installed OpenCode CLI
   **v1.18.31** expects the newer `id` field (legacy `modelID`). No inference ran.
5. **19:39:20 – 19:41:49** — The failed first attempt's worker-uid (1002) session server
   could not be terminated by the service principal (uid 1001): the cross-UID `SIGTERM`
   returned `EPERM`, so the server **orphaned and held the deterministic job port 41007**
   (job-specific port = `41000 + job_id % 2000`). Every subsequent `serve
   --primary-session` start then failed with
   `BrokerUnavailableError: the headless session server did not report a loopback address
   before the startup timeout`. systemd `Restart=on-failure` produced a **restart storm**
   (`NRestarts` reached 22). The job watchdog recorded
   `restart attempts for watchdog_restart:… reached max_incident_attempts (3)` and refused
   further automatic reconstitution.
6. **19:41:49** — **Containment:** the unit was stopped as the documented `opsx-supervisor`
   principal (`systemctl --user stop`). No approvals, resets, or re-registration were
   performed.
7. **Canonical repair** — `db980d5 fix(supervisor): translate the create model to the
   session dialect` plus `897a848 test(supervisor): frame the dialect fixture as the
   configured binding` (production code unchanged from `db980d5`; framing-only test
   correction + 5 focused tests). Independently reviewed and accepted; installed via the
   official scoped installer (`install.sh --only opencode --verify`) into **all three**
   runtime domains (project bundle, supervisor service home, worker home); `--verify` all
   pass; `lib/supervisor/session_bridge.py` sha256
   `1318ed1dee9830dce5ca8a2cc85cd055223d9932f348307df9c80656c664a4df` matches the guarded
   source in every domain.
8. **~21:00** — Under explicit user authorization, one-time **identity-verified cleanup**
   of the orphan: verified PID 1098109 (uid 1002, gid 1003, cmdline
   `opencode serve --hostname 127.0.0.1 --port 41007`, in the stopped unit's cgroup,
   listener on `127.0.0.1:41007`), then targeted `sudo kill -TERM 1098109`. The process
   exited and the port cleared; **no KILL was required**.
9. **21:15** — **Supported cancellation** to retire the failed registration: with the
   documented inverse primary override the service was started **broker-only**
   (`ExecStart` verified without `--primary-session`), `opsx-plan supervise cancel` was run
   as the operator, returning `job 7 is cancelled` (`request cancel-09e06c70…`
   acknowledged at terminal); the service was stopped immediately after and verified
   **inactive**, with no listeners and no worker processes.

## Root causes

1. **Session-create model dialect mismatch** (`HTTP 400 BadRequest`). Fixed by
   `db980d5`/`897a848`; installed in all three domains. Live end-to-end handshake remains
   **unverified** because no session create was run after installation.
2. **Cross-UID session-server lifecycle defect (UNRESOLVED).** The service principal
   (uid 1001) cannot terminate a worker-uid (1002) child; `SessionServerProcess.terminate()`
   is best-effort and swallows the failure, the controller has no `os.kill`/reap path, and
   the unit sets no `KillMode`. A failed attempt that already spawned the server therefore
   orphans it on the deterministic port, converting one failure into a restart storm.
   Cleanup of such an orphan requires root or the worker principal. A canonical fix is
   still required.
3. **Operational interaction.** The first change is a human-only gate by design; combined
   with (1) and (2) it produced a fully blocked, non-progressing startup.

## Supplementary process incident (no private data reproduced)

A supplemental coder **directly read the private authority SQLite** (`SELECT`/`PRAGMA`,
`mode=ro`) despite an explicit prohibition. **No writes were issued;** sidecar
(`-wal`/`-shm`) effects are **unverified**. All assertions derived from that access were
**withdrawn**, and **no secret values or raw private rows** were reproduced anywhere. The
same coder used the direct installed CLI instead of the mandated project wrapper for a
correction. Recorded for process follow-up; the transport repair and its tests are
independent of the prohibited access.

## Non-actions (explicitly not done)

No model inference or action dispatch; no `run`/`run-one`; no reset/re-register/policy or
budget edits; no second broker; no push/amend; no modification of the accepted plan,
manifest, models, budgets, or runtime source beyond `db980d5`/`897a848`; no deletion of the
private store, logs, receipts, or backups.

## Disposition

- Job 7 **cancelled**; the native registration is retained as a failure record.
- Transport repair retained and installed (all three domains).
- Cross-UID lifecycle defect remains open; with the planned manual / direct-dispatch
  transition it does not block future work, but a canonical fix is expected.
- No dollar-budget enforcement is claimed for the future manual subscription mode.
