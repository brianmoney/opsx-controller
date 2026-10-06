# opsx-plan autopilot runbook

Step-by-step procedure for running a plan unattended under
`opsx-plan autopilot` — the lightweight successor to the dormant supervision
stack. Commands are for a Linux machine with a systemd **user** session.

Reference tables (CLI flags, failure classification, config keys, unit
commands, veto mechanism) live in
[`opsx-plan-operator-workflow.md` → "Autopilot (unattended runs)"](opsx-plan-operator-workflow.md#autopilot-unattended-runs).
This runbook only sequences the operator actions; it does not restate those
tables.

## 1. Prerequisites

```bash
# Installed runtime is current (run from the opsx-controller repo root).
bash install.sh --global --verify

# A systemd user session is available (must print manager state, not
# "Failed to connect to bus").
systemctl --user status

# Unattended operation needs the user manager to survive logout; must print
# "Linger=yes" (enable once with `sudo loginctl enable-linger "$USER"`).
# Without lingering the unit dies with your session.
loginctl show-user "$USER" -p Linger

# The plan manifest exists and loads.
opsx-plan status openspec/plans/my-plan.toml
```

`opsx-plan` must resolve on `PATH` (`~/.local/bin/opsx-plan`). These checks run
in your shell, which has your full `PATH`; the unit runs with the systemd user
manager's bare `PATH` instead, so also verify the toolchain requirement in
step 3.

## 2. One-time notification setup

Pick an unguessable ntfy.sh topic name and subscribe to it in the ntfy app
(`https://ntfy.sh/<topic>`). Then write the config file:

```bash
mkdir -p ~/.config/opsx-controller
cat > ~/.config/opsx-controller/autopilot.toml <<'EOF'
# Minimal complete autopilot config. Other keys exist with defaults:
# veto_window_minutes, max_auto_resets, reset_spacing_seconds, poll_seconds.
ntfy_topic = "opsx-7f3c1a9e5b2d4c8f"
EOF
```

`OPSX_AUTOPILOT_NTFY_TOPIC` overrides `ntfy_topic`. With no topic from either
source, pushes are skipped but escalation digests are still written to
`.opsx-plan/escalations.jsonl`.

## 3. One-time unit setup

Bind the installed unit template to this repository and plan with one command,
run from the plan repository (`--repo <path>` overrides the cwd):

```bash
opsx-plan autopilot install --plan openspec/plans/my-plan.toml

# Preview exactly what will be written, touching nothing:
opsx-plan autopilot install --plan openspec/plans/my-plan.toml --print
```

`install` resolves the plan with the same precedence as `run` (explicit
`--plan`, `OPSX_PLAN`, active-plan pointer), reads the installed-data templates
from `~/.local/lib/opsx-controller/systemd/`, and writes
`~/.config/systemd/user/opsx-autopilot.service` plus
`~/.config/systemd/user/opsx-autopilot.service.d/plan.conf`. It derives an
`Environment=PATH=` line from your shell's toolchain — the directories
providing `openspec`, the adapter client (e.g. `opencode`), and node, followed
by the systemd default directories — so the unit's bare `PATH` stays complete.
It then runs `systemctl --user daemon-reload` and `enable`, and re-running it is
idempotent (the managed files are rewritten with a generated header).
It never `start`s the unit. `--unit-name <name>` writes an alternate binding
(for example a per-repo unit) without touching the default one, and
`--no-enable` writes the files without enabling.

`enable` starts nothing; the unit stays down until you `start` it (step 6).
To use the repository's active-plan pointer instead of pinning a manifest,
remove the `Environment=OPSX_PLAN=` line from `plan.conf` (then
`daemon-reload`) and select the plan with `opsx-plan use <plan.toml>`.

<details>
<summary>Manual rendering fallback (when <code>opsx-plan autopilot install</code> is unavailable)</summary>

Render the installed-data templates by hand. The installed data lives at
`~/.local/lib/opsx-controller/systemd/`.

```bash
RUNTIME="$HOME/.local/lib/opsx-controller"
UNIT_DIR="$HOME/.config/systemd/user/opsx-autopilot.service.d"
mkdir -p "$UNIT_DIR"

# Main unit has no placeholders; copy it verbatim.
cp "$RUNTIME/systemd/opsx-autopilot.service.in" \
   "$HOME/.config/systemd/user/opsx-autopilot.service"

# Drop-in: substitute the two placeholders (envsubst ships with gettext).
# Scope the variable list so only the placeholders are expanded, not the
# template's comment text.
export OPSX_AUTOPILOT_REPO="$PWD"                      # absolute repo path
export OPSX_AUTOPILOT_PLAN="openspec/plans/my-plan.toml"
envsubst '${OPSX_AUTOPILOT_REPO} ${OPSX_AUTOPILOT_PLAN}' \
  < "$RUNTIME/systemd/opsx-autopilot.service.d/plan.conf.in" \
  > "$UNIT_DIR/plan.conf"

systemctl --user daemon-reload
systemctl --user enable opsx-autopilot
```

Systemd user units get a bare `PATH` (`/usr/local/bin`, `/usr/bin`, ...), not
your shell's. If `openspec`, your adapter client (e.g. `opencode`), or node
live under `$HOME` — check with `which openspec opencode node` — add a PATH
line to `plan.conf` and `daemon-reload` again:

```ini
Environment=PATH=%h/.npm-global/bin:%h/.opencode/bin:%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

Without it the unit exits 2 on preflight with "OpenSpec CLI not found
repo-locally or on PATH", and `Restart=on-failure` retry-loops it. (The
`opsx-plan autopilot install` command above derives this line for you.)

</details>

## 4. Per-plan preparation

Manifest guidance for unattended runs:

- `review_created = true` — the controller assesses authored changes; keep it
  on. Each orchestrator-created change then parks at an acceptance gate until
  you `opsx-plan accept <change-id>` (step 7).
- `pause_before = true` — think before using it. Each gate is a notification
  plus a veto window, then auto-approval (step 7).
- `require_clean_tracked = true` — refuse to start on a dirty tracked tree.
- `reuse_fix_sessions = true` — opencode adapter only; warm fix rounds, cheaper.
- Budget circuit breakers — the engine's `--budget-usd` (`budget_usd`) and
  `--budget-minutes` (`budget_minutes`) flags. A budget pause is classified
  `budget`: autopilot notifies and stops instead of looping. The shipped unit
  does not pass these flags; use them on hand-run passes.
- `escalate_after_review_fails` — leave off unless wanted; autopilot already
  escalates on `max_rounds_reached`.

## 5. Preflight + smoke

```bash
git status --short          # must be clean for require_clean_tracked
opsx-plan status            # know pending / failed / gated changes
opsx-plan doctor            # models resolve, client on PATH, plan loads

# Single-pass smoke from the repo. With no --plan the active plan is used;
# against an already-complete plan this only emits a completion push. To
# exercise one real pass, target a plan with pending work and shorten the
# gate window:
opsx-plan autopilot --once --plan openspec/plans/my-plan.toml \
  --veto-window-minutes 2 --poll-seconds 10
```

A healthy pass: clean preflight, the engine advances (or completes) pending
changes, the command exits 0, and no new line is appended to
`.opsx-plan/escalations.jsonl`. A `--once` pass can run as long as one
`opsx-plan run`, and waits out a `pause_before` veto window if it reaches one
(auto-approving at the end; the next pass does the actual work).

## 6. Launch + monitor

```bash
systemctl --user start opsx-autopilot

journalctl --user -u opsx-autopilot -f      # decisions and engine output
opsx-plan status                            # per-change state
opsx-plan autopilot status --plan openspec/plans/my-plan.toml  # read-only pause/state snapshot
tail -f .opsx-plan/autopilot-events.jsonl   # structured autopilot events
```

## 7. While it runs

- **Gate notification** (title `opsx-plan gate: <change-id>`) — do nothing and
  it auto-approves after the veto window (default 30 min). To approve early:
  `opsx-plan approve <change-id>`. To veto, create the marker during the
  window:

  ```bash
  mkdir -p .opsx-plan/veto
  touch .opsx-plan/veto/<change-id>
  ```

  A veto escalates the change as `human_veto` and stops the unit.
- **Acceptance prompt** (title `opsx-plan accept: <change-id>`) — an
  orchestrator-created change needs review before it is implemented. Review
  `openspec/changes/<change-id>/`, then run `opsx-plan accept <change-id>`;
  autopilot resumes on its own. There is no auto-accept and no unit restart
  (a `--once` pass announces and exits instead of waiting).
- **Escalation push** (title `opsx-plan escalate: <class>`, high priority) —
  follow the playbook below.
- **Completion push** (title `opsx-plan complete: <plan>`) — the unit exits 0
  and stays down; nothing further to do.

## 8. Escalation playbook

Read the newest digest and follow its `suggested_action`:

```bash
tail -n 1 .opsx-plan/escalations.jsonl
```

Record fields: `ts`, `plan`, `change_id`, `class`, `last_result`, `reason`,
`loci`, `attempts`, `log_path`, `suggested_action`.

| Class | Typical fix |
|---|---|
| `transient_exhausted` | Check provider status / model id, then `opsx-plan reset <change-id>` |
| `permanent_provider` (billing/quota) | Top up the provider balance or fix the model id |
| `permission` | Fix opencode permissions for the worker |
| `finding_recurrence_exceeded`, `max_rounds_reached`, `no_progress` | Inspect `loci`; manual fix or trusted-model dispatch per the `opsx-plan-ops` skill |
| `archive_failed`, `archive_invalid` | Fix the DELTA, never the canonical spec |
| `deterministic` (environment pause) | Clean the tracked tree / commit archive output, fix plan resolution, or restore the `opsx-plan` executable; then run `autopilot resume` |
| `environment` (transient/retryable) | Inspect execution-lock contention or unclassified engine/status/approve/reset errors |
| `human_veto` | You vetoed the gate; resolve it by hand |
| `no_forward_progress` | Autopilot's own guard (3 quick passes, identical statuses); inspect `opsx-plan status` and the stage logs |
| `unknown` | Inspect the `log_path` stage log; the classifier could not place the failure |

For a deterministic environment failure, inspect the durable pause and recheck
preflight after fixing its cause. Use the **same repo, plan selection, and PATH
as the unit** (a drop-in's `OPSX_PLAN` is not automatically set in your shell):

```bash
opsx-plan autopilot status --plan openspec/plans/my-plan.toml
opsx-plan autopilot resume --plan openspec/plans/my-plan.toml
```

`status` shows class, reason, creation time, suggested action, and recorded
plan/change state without reconciling or writing it. Pause details remain
available even if the plan cannot load. `resume` checks plan resolution,
`require_clean_tracked`, and the child executable; it exits 2 and leaves the
marker untouched if a check still fails. On success it clears
`.opsx-plan/autopilot-paused.json` and exits 0, but does **not** start the unit.
The marker is repository-wide: switching plans or starting the unit again
(including `--once`) does not bypass it. Paused starts append only a `paused`
event, not another digest or push. To debug without autopilot, run the engine
directly with `opsx-plan run`.

Then restart (also the normal recovery for a change-level escalation):

```bash
systemctl --user start opsx-autopilot
```

Change-level escalations and deterministic environment pauses exit 0, so
`Restart=on-failure` leaves the unit **down** for the operator. A pause records
exactly one digest and sends at most one push. Only known deterministic reasons
pause: dirty tracked worktrees (including archive output), unresolvable plans,
or a missing `opsx-plan` executable. Transient `environment` escalations still
exit 2: execution-lock contention, unclassified engine exits 2, invalid
`status --json`, or failed `approve`/`reset` subprocesses. Systemd retries them
under `RestartSec=30` until `StartLimitBurst` (5 starts / 600s) trips. If that
lands the unit in `failed`, clear it with
`systemctl --user reset-failed opsx-autopilot` before starting it. Resetting
systemd's failed state alone does not clear an autopilot pause.

## 9. Stopping / switching plans / teardown

```bash
# Stop (in-flight work is interrupted; a re-start resumes from state).
systemctl --user stop opsx-autopilot

# Switch plans: re-bind with the new plan
# (opsx-plan autopilot install --plan <new.toml>), or select the plan via the
# active-plan pointer.
opsx-plan autopilot install --plan openspec/plans/other-plan.toml
systemctl --user start opsx-autopilot

# Full teardown.
systemctl --user stop opsx-autopilot
systemctl --user disable opsx-autopilot
rm -f "$HOME/.config/systemd/user/opsx-autopilot.service" \
      "$HOME/.config/systemd/user/opsx-autopilot.service.d/plan.conf"
rmdir "$HOME/.config/systemd/user/opsx-autopilot.service.d" 2>/dev/null || true
systemctl --user daemon-reload
```

## 10. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| Unit won't start | `journalctl --user -u opsx-autopilot -e`; verify `WorkingDirectory` / `OPSX_PLAN` rendered in `plan.conf` | Correct `plan.conf`, `daemon-reload`, `start` |
| Unit exits 0 immediately, even on explicit start or `--once` | `opsx-plan autopilot status --plan <unit-plan.toml>`; `.opsx-plan/autopilot-paused.json` | Fix the reported deterministic check, run `opsx-plan autopilot resume --plan <unit-plan.toml>` in the unit's repo/environment, then `start`; do not just delete the marker |
| `StartLimitBurst` tripped (5 starts / 600s) | `systemctl --user status opsx-autopilot` | `systemctl --user reset-failed opsx-autopilot` then `start` |
| Unit exits 2, journal says "OpenSpec CLI not found repo-locally or on PATH" | Systemd user `PATH` lacks your toolchain dirs (`which openspec opencode node`) | Re-run `opsx-plan autopilot install` (it derives `Environment=PATH=`, step 3), then `start`; `reset-failed` if the retry loop tripped |
| No pushes | Topic unset in `autopilot.toml` / `OPSX_AUTOPILOT_NTFY_TOPIC`; digests still in `.opsx-plan/escalations.jsonl` | Set the topic; test with `curl -d test ntfy.sh/<topic>` |
| `worktree ... execution lock is already held ...; refusing to proceed` | A hand-run `opsx-plan run` / `reset` is racing the unit | Don't hand-run mutating commands while the unit is up (`approve` / `status` are safe); stop the unit first |
| Config changes not taking effect | Running unit still has the old config | Restart the unit; CLI flags override the file, env overrides the topic |
