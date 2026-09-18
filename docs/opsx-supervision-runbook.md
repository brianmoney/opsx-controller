# opsx-plan supervision runbook

Practical, end-to-end operating procedure for the `opsx-plan` supervised
service. This is the action-oriented companion to:

- `docs/opsx-supervision-service.md` — the manual provisioning contract for
  the Linux systemd user service host,
- `docs/opsx-plan-operator-workflow.md` — the full command surface,
- `core/plan-supervision.md` — the trust model and durable-state contracts,
- `skills/opsx-supervision/SKILL.md` — the frontier primary's bounded loop.

Nothing in the installer activates supervision: the service is deployed
disabled and stays inert until the operator performs the provisioning steps
below.

## Two operating modes

| | Legacy | Supervised |
|---|---|---|
| Trigger | `opsx-plan run` | Registered job + `supervise start` / service |
| Authority | JSON state in repo (`.opsx-plan/`) | Service-owned ledger + OS-authenticated broker |
| Gates | Direct edits by `approve`/`accept` | Durable receipts over the operator socket |
| Recovery | Re-run; residual budget flags | Watchdog reconstitution, reservations, incidents |
| Roles | implement / review / archive | + supervisor, supervised_author, acceptance_reviewer, fixer, verifier |

An unregistered worktree takes the legacy path byte-identically. Nothing below
applies until a job is registered.

## A. One-time host provisioning

Only a Linux host with a systemd user manager and an OpenCode session bridge is
supported. Anything else fails closed with `UnsupportedHostError`; no weaker
posture is substituted. The installer deploys only data (unit template and
provisioning document), never enables or starts anything.

```bash
# 1. Three distinct identities: operator (you), service, and worker.
sudo useradd --create-home --shell /usr/sbin/nologin opsx-supervisor
sudo useradd --create-home --shell /usr/sbin/nologin opsx-worker

# 2. Service-owned authority store: a file, never worker-writable.
sudo -u opsx-supervisor mkdir -p /home/opsx-supervisor/.local/share/opsx-controller/supervisor
sudo -u opsx-supervisor touch /home/opsx-supervisor/.local/share/opsx-controller/supervisor/supervisor.sqlite3

# 3. Keep the service principal's user manager alive without a login, and
#    install the runtime into the service principal's home.
sudo loginctl enable-linger opsx-supervisor
sudo -H -u opsx-supervisor bash /path/to/opsx-controller/install.sh --global

# 4. Render the unit as the service principal (AssertUser= pins identity) and
#    reload; do not enable yet.
export OPSX_SUPERVISE_EXECUTABLE="$SERVICE_HOME/.local/bin/opsx-plan"
export OPSX_SUPERVISE_REPO="/path/to/repo"
export OPSX_SUPERVISE_SERVICE_PRINCIPAL=opsx-supervisor
export OPSX_SUPERVISE_WORKER_PRINCIPAL=opsx-worker
export OPSX_SUPERVISE_STATE_FILE="$SERVICE_HOME/.local/share/opsx-controller/supervisor/supervisor.sqlite3"
envsubst < "$SERVICE_HOME/.local/lib/opsx-controller/systemd/opsx-supervise.service.in" | \
  sudo -u opsx-supervisor tee "$SERVICE_HOME/.config/systemd/user/opsx-supervise.service" >/dev/null
sudo -u opsx-supervisor env XDG_RUNTIME_DIR="$SERVICE_RUNTIME" systemctl --user daemon-reload

# 5. Mandatory fail-closed probe: a real worker-principal process proves its
#    effective uid and its write attempt against the store is denied EACCES.
opsx-plan supervise probe

# 6. Deliberate activation, only after the probe passes.
sudo -u opsx-supervisor env XDG_RUNTIME_DIR="$SERVICE_RUNTIME" \
  systemctl --user enable --now opsx-supervise.service
```

The rendered unit runs `opsx-plan supervise serve` with `Restart=on-failure`.
Re-running an installer later refreshes the template and document but never
activates the service.

## B. Per-plan supervised cycle

1. **Author and preflight as usual** — compile the plan, activate it, run
   `opsx-plan doctor`:

   ```bash
   opsx-plan compile docs/my-plan.md -o plan.toml
   opsx-plan use plan.toml
   opsx-plan doctor
   ```

2. **Register the job** (trust-root step; no live service required):

   ```bash
   opsx-plan supervise register --budget-usd 25 --per-action-usd 3 \
     --budget-minutes 600 --deadline-minutes 480 --max-incident-attempts 3 \
     --primary-session
   ```

   Registration freezes the protected manifest snapshot (with its hash), role
   models, inexpensive allowlist, budgets, and primary-session linkage. A
   second active job for the worktree is refused with `DuplicateJobError`.
   Nothing is written to the worktree or JSON execution state.

3. **Start and drive** — the same run engine as `opsx-plan run`, no new DAG:

   ```bash
   opsx-plan supervise start               # registered -> active, records fence, drives engine
   opsx-plan supervise start --no-drive    # transition only
   ```

4. **Watch it**:

   ```bash
   opsx-plan supervise status [--json]         # read-only capability report
   opsx-plan supervise inspect [--json]        # state, waits, policy, budget, actions, incidents, manual checklist
   opsx-plan status                            # supervised-job block appended when registered
   opsx-plan report / opsx-plan dashboard      # supervision sections
   opsx-plan supervise watchdog --once --json  # live/quiet/stalled/dead/expected_human_wait
   ```

5. **Release gates through the operator path** (peer-uid authenticated, not a
   token; the service must be live):

   ```bash
   opsx-plan approve <change>      # or --all / P3
   opsx-plan accept <change>
   opsx-plan reset <change>        # durable broker transaction
   ```

   Gates with `pause_before_human_only = false` are delegated: only the job's
   scoped service action releases them, never an operator approval. A receipt
   invalidated by an explicit plan or policy revision reports
   `StaleMaterialError` and the gate re-arms.

6. **Stop boundaries**:

   - `supervise pause` — interrupts in-flight actions (marked `uncertain`) and
     enters `paused`.
   - `supervise drain` — forbids new dispatch; in-flight work finishes first.
   - `supervise resume` — revalidates every relied-upon receipt before
     reactivating; a stale receipt keeps the job paused.
   - `supervise cancel` — terminal; ends open waits and frees the worktree for
     a new registration.

7. **Incidents, recovery, and budgets**: dispatch reserves against the pricing
   catalog before running and reconciles afterward. Unknown pricing blocks
   before any side effect (`UnknownPricingError`); exhausted budgets are
   terminal operator-actionable states. `supervise serve` performs the
   boot-scan reconciliation and the watchdog reconstitutes only a genuinely
   `dead`, quiesced job with a prior execution, under bounded backoff. An
   `expected_human_wait` is never acted on.

8. **Completion** is decided only from archive evidence, post-archive fast
   checks, and a clean tracked tree — never a worker or primary claim.
   `supervise inspect` prints the pending `(manual)` operator checklist; those
   items never block completion.

## Primary session

`opsx-plan supervise serve --primary-session` starts or adopts the
`opsx-supervisor` frontier session over the OpenCode bridge (adopt-by-lookup
first, replacement only when the recorded session is gone). To have the
packaged systemd unit host it, append `--primary-session` to the unit's
`ExecStart` when rendering — the stock template runs plain `serve` — and
register the job with `--primary-session` so its linkage is recorded. The
agent receives a bounded briefing from the ledger and can invoke only
`opsx-supervise` worker-actions verbs: `request_action`, `record_evidence`,
`report_status`, `heartbeat`, `release_delegated_gate`, `report_violation`.

It holds no operator authority: it cannot run arbitrary Bash or Task agents,
edit files, approve, or resume. Mechanical repair routes to the pinned `fixer`
and is consumed only after an independent `verifier` validates the real diff;
hard judgments return to the primary.

## Refusal cheat sheet

| Error | Meaning |
|---|---|
| `UnsupportedHostError` | Missing backend, bridge, distinct principals, or trusted store; no weaker posture is substituted |
| `ActivationProbeError` | The mandatory probe could not prove the isolation boundary |
| `BrokerUnavailableError` | Service not running, store missing, or socket unbindable |
| `BrokerMediationError` | Worker/unauthenticated mutation, dispatch outside the supervised execution, or a delegated gate via the operator path |
| `StaleMaterialError` | A relied-upon receipt no longer matches the current material revision |
| `DuplicateJobError` | A second active job for the same worktree |
| `UnknownJobError` / `IllegalTransitionError` / `TerminalJobError` | Lifecycle errors; all exit non-zero |

`doctor`, `status`, `logs`, and `report` acquire no boundary dependency and
keep working unchanged on hosts where the boundary is unavailable.
