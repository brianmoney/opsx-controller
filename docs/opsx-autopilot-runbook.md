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

`opsx-plan` must resolve on `PATH` (`~/.local/bin/opsx-plan`).

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

Render the installed-data templates into the user unit directory. The
installed data lives at `~/.local/lib/opsx-controller/systemd/`.

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

`enable` starts nothing; the unit stays down until you `start` it (step 6).
To use the repository's active-plan pointer instead of pinning a manifest,
remove the `Environment=OPSX_PLAN=` line from `plan.conf` (then
`daemon-reload`) and select the plan with `opsx-plan use <plan.toml>`.

## 4. Per-plan preparation

Manifest guidance for unattended runs:

- `review_created = true` — the controller assesses authored changes; keep it
  on.
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
| `environment` | Clean tracked tree / stale execution lock |
| `human_veto` | You vetoed the gate; resolve it by hand |
| `no_forward_progress` | Autopilot's own guard (3 quick passes, identical statuses); inspect `opsx-plan status` and the stage logs |
| `unknown` | Inspect the `log_path` stage log; the classifier could not place the failure |

Then restart:

```bash
systemctl --user start opsx-autopilot
```

Change-level escalations exit 0, so the unit's `Restart=on-failure` leaves it
**down** and restarting is always your explicit act. `environment` escalations
are the exception: autopilot exits 2 (invalid `status --json`, engine exit 2, a
failed `approve`/`reset` subprocess), so systemd retries under `RestartSec=30`
until `StartLimitBurst` (5 starts / 600s) trips and the unit lands in
`failed`; clear that with `systemctl --user reset-failed opsx-autopilot`
before starting it.

## 9. Stopping / switching plans / teardown

```bash
# Stop (in-flight work is interrupted; a re-start resumes from state).
systemctl --user stop opsx-autopilot

# Switch plans: re-render plan.conf with the new OPSX_AUTOPILOT_PLAN
# (repeat step 3), or select the plan via the active-plan pointer.
systemctl --user daemon-reload
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
| `StartLimitBurst` tripped (5 starts / 600s) | `systemctl --user status opsx-autopilot` | `systemctl --user reset-failed opsx-autopilot` then `start` |
| No pushes | Topic unset in `autopilot.toml` / `OPSX_AUTOPILOT_NTFY_TOPIC`; digests still in `.opsx-plan/escalations.jsonl` | Set the topic; test with `curl -d test ntfy.sh/<topic>` |
| `worktree ... execution lock is already held ...; refusing to proceed` | A hand-run `opsx-plan run` / `reset` is racing the unit | Don't hand-run mutating commands while the unit is up (`approve` / `status` are safe); stop the unit first |
| Config changes not taking effect | Running unit still has the old config | Restart the unit; CLI flags override the file, env overrides the topic |
