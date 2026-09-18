# Provisioning the opsx supervision service

This document is the manual provisioning contract for the Linux supervision
service host packaged with opsx-controller. It is shipped as read-only data by
every global install (adapter installers and the universal installer) alongside
the versioned unit template:

- `systemd/opsx-supervise.service.in` — the systemd **user** unit template
- this document

Installing those artifacts is **not** provisioning. The installer deploys the
template and this document into the installed runtime tree
(`~/.local/lib/opsx-controller/systemd/` and
`~/.local/lib/opsx-controller/docs/` for a global install) and then stops. It
never writes a unit into a service-manager directory, never runs `systemctl`,
never enables or starts the service, and never creates or modifies an
operating-system account or the authority store. The service is deployed
**disabled by default** and stays inert until the operator performs the steps
below.

Only a Linux host with the systemd user manager and an OpenCode session bridge
is supported. Non-Linux service managers (launchd, Windows services) and
non-OpenCode session bridges are unsupported and fail closed: there is no
silent fallback and no weaker activation path.

## Prerequisites

- A Linux host with a systemd user manager and no unprivileged access to the
  service account.
- Privileges to create the two operating-system accounts and to enable a user
  manager (typically `sudo`).
- The OpenCode CLI available to the service principal for the service session
  bridge (or `OPSX_SESSION_SERVER_COMMAND` pinned to an equivalent command).

A systemd **user** unit runs under whichever user manager loads it, so the
service's effective identity is the user whose manager enables it — an
environment variable is not an identity. The packaged template pins the
identity with an `AssertUser=` assertion: the unit's start job fails loudly
unless it is loaded by the configured service principal's manager. Render,
reload, and enable it **as the service principal**; enabling it under any other
user is not a supported posture.

## Manual operator steps

These steps are deliberately manual. Nothing in the installer or in
`opsx-plan` performs them.

### 1. Create the service and worker accounts

Create a dedicated unprivileged **service principal** that owns the supervision
runtime and authority store, and a distinct unprivileged **worker principal**
that executes model work. The two accounts must be different from each other and
from the operator, and the worker must not have write access to the service
account's home or store:

```bash
sudo useradd --create-home --shell /usr/sbin/nologin opsx-supervisor
sudo useradd --create-home --shell /usr/sbin/nologin opsx-worker
```

The default principal names are `opsx-supervisor` and `opsx-worker`; the
boundary requires three distinct identities and refuses a collapsed
configuration.

### 2. Provision the authority store

Create the service-owned authority-store **file** and make its mutable parent
chain unwritable to the worker principal. The default location follows the
service principal's home:

```bash
sudo -u opsx-supervisor mkdir -p /home/opsx-supervisor/.local/share/opsx-controller/supervisor
sudo -u opsx-supervisor touch /home/opsx-supervisor/.local/share/opsx-controller/supervisor/supervisor.sqlite3
```

When the service principal does not exist yet, detection falls back to the
root-owned `/var/lib/opsx-controller/supervisor/supervisor.sqlite3`. The
canonical location can be pinned explicitly with `OPSX_SUPERVISOR_STATE_FILE`.
Ownership and the parent chain are validated by the boundary; a worker-writable
store or parent is reported `unprovisioned` and activation is refused.

### 3. Enable the service principal's user manager

The unit must run from the service principal's own user manager, so provision
that manager first and install the orchestrator runtime into the service
principal's home. The service principal is typically a `nologin` account, so
linger keeps its user manager alive without an interactive login:

```bash
sudo loginctl enable-linger opsx-supervisor
sudo -H -u opsx-supervisor bash /path/to/opsx-controller/install.sh --global
```

From here on, every `systemctl --user` command runs as the service principal
with its runtime directory set:

```bash
SERVICE_USER="opsx-supervisor"
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
SERVICE_RUNTIME="/run/user/$(id -u "$SERVICE_USER")"
```

### 4. Render the unit template as the service principal

Render every `${...}` placeholder in the installed template and install the
result into the **service principal's** user unit directory. The template's
`AssertUser=` assertion requires that directory and the manager that loads it
to belong to the configured service principal. Do **not** enable or start it
yet:

```bash
export OPSX_SUPERVISE_EXECUTABLE="$SERVICE_HOME/.local/bin/opsx-plan"
export OPSX_SUPERVISE_REPO="/path/to/supervised/repo"
export OPSX_SUPERVISE_SERVICE_PRINCIPAL="$SERVICE_USER"
export OPSX_SUPERVISE_WORKER_PRINCIPAL="opsx-worker"
export OPSX_SUPERVISE_STATE_FILE="$SERVICE_HOME/.local/share/opsx-controller/supervisor/supervisor.sqlite3"

sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" \
  "$SERVICE_HOME/.config/systemd/user"
envsubst < "$SERVICE_HOME/.local/lib/opsx-controller/systemd/opsx-supervise.service.in" \
  | sudo -u "$SERVICE_USER" tee \
    "$SERVICE_HOME/.config/systemd/user/opsx-supervise.service" >/dev/null
sudo -u "$SERVICE_USER" env XDG_RUNTIME_DIR="$SERVICE_RUNTIME" \
  systemctl --user daemon-reload
```

Rendering and reloading alone does not enable the service: the unit remains
disabled until step 6. Enabling the rendered unit from any other user manager
fails closed on the `AssertUser=` assertion.

### 5. Run the activation probe

The mandatory activation probe spawns a real process under the worker principal
and requires it to prove its identity before a write attempt against the
authority store is refused with `EACCES`. Run it and confirm it passes:

```bash
opsx-plan supervise probe
```

A failing or un-runnable probe is a named error and supervision is not enabled.
An unsupported host fails closed with the named unsupported-host error; no
weaker posture is substituted. The composed gate refuses a host **without a
supported systemd user manager** (a trusted `systemctl` and a live
`$XDG_RUNTIME_DIR/systemd/private` user-manager socket) or **without an OpenCode
session bridge** (a resolvable `opencode` CLI or a pinned
`OPSX_SESSION_SERVER_COMMAND`), in addition to the authority-boundary checks
(non-Linux, no peer credentials, missing or collapsed principals, untrusted
store location).

### 6. Enable the service

Only after the probe passes, perform the deliberate activation step from the
service principal's user manager:

```bash
sudo -u "$SERVICE_USER" env XDG_RUNTIME_DIR="$SERVICE_RUNTIME" \
  systemctl --user enable --now opsx-supervise.service
```

Enabling is a separate, explicit operator action. Re-running an installer later
replaces the template and this document but never enables, starts, or activates
the service.

## Verifying the deployment

After any global install, `--verify` checks that the template and this document
were deployed and match the repository copies byte-for-byte; a missing or stale
artifact is a verification failure.

`opsx-plan doctor` reports the packaged service state read-only, without
enabling, starting, or provisioning anything:

- whether the unit template and provisioning document are installed,
- whether a rendered service unit is present,
- the supervisor ledger schema version when a ledger is present, and
- the isolation-backend capability status.

An absent or unsupported service state is reported plainly and does not fail the
check for an operator who has not enabled supervision; legacy unsupervised
installations stay green.
