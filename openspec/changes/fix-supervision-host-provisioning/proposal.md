## Why

The email-plan supervisor service must run under three distinct OS
principals — operator (the human, uid 1000), service (`opsx-supervisor`,
uid 1001), and worker (`opsx-worker`, uid 1002). On current `main` the service
host cannot boot in that posture:

- The operator endpoint resolves its allowed uid from the identity the service
  process runs under (`detect_backend()` uses the invoking user), so a service
  started as `opsx-supervisor` either accepts the service uid as the operator
  or fails; there is no supported way to name the provisioned operator
  principal (`OPSX_SUPERVISOR_OPERATOR_PRINCIPAL`).
- The worker-actions endpoint accepts only the service uid, so the worker
  principal (`opsx-worker`) cannot reach its own scoped verbs.
- Both sockets are bound `0600` service-owned, so with distinct principals the
  operator and the worker cannot even attempt a connection; there is no
  supported operator/worker socket-group provisioning.
- One malformed or truncated client request re-raises out of the serve loop
  and kills the service, and a response write to a vanished peer masks the
  refusal.
- `skills/opsx-supervision/SKILL.md` frontmatter is invalid YAML (an unquoted
  multi-line plain scalar containing `job: read ...`), so the official
  installer ships a skill the client cannot parse and would overwrite a
  corrected project copy with the broken canonical source.

## What Changes

- `lib/supervisor/authority.py`: add `OPERATOR_PRINCIPAL_ENV`
  (`OPSX_SUPERVISOR_OPERATOR_PRINCIPAL`) and `provisioned_operator_name()`;
  operator precedence in `principal_set_from_environment()` becomes explicit
  argument, provisioned env, then invoking user.
- `lib/orchestrator/supervision.py`: resolve the operator endpoint allowlist
  from the provisioned operator principal (never the service uid), fail closed
  when serving as the service without a valid operator, and require the
  operator uid to be distinct from the service and worker uids. The
  worker-actions allowlist becomes service plus provisioned worker, never the
  operator.
- `lib/orchestrator/supervision.py`: support `OPSX_SUPERVISOR_OPERATOR_GROUP`
  and `OPSX_SUPERVISOR_WORKER_GROUP`. A configured group makes the
  service-owned socket `0660` with that group as its group owner; kernel
  `SO_PEERCRED` uid auth remains authoritative. Startup fails closed when the
  operator group admits the worker, when a configured group is missing, or
  when a socket parent (possibly outside the ledger home via the existing
  socket path parameters) is not service-owned and worker-unwritable.
- `lib/supervisor/broker_client.py`: contain per-connection faults — malformed
  JSON/UTF-8, partial requests, disconnects, and failed response writes are
  answered where possible and never tear down the serve loop; deferred
  delivery acknowledgements are not persisted on a failed write. Authority and
  ledger faults are still surfaced, not silenced.
- `skills/opsx-supervision/SKILL.md`: render the `description` frontmatter as a
  folded scalar; body unchanged.

## Impact

- `lib/supervisor/authority.py`, `lib/orchestrator/supervision.py`,
  `lib/supervisor/broker_client.py` — provisioning resolution, socket
  provisioning, and transport fault containment.
- `docs/opsx-supervision-service.md` — document the new endpoint principal and
  socket-group environment scope.
- `skills/opsx-supervision/SKILL.md` and the regenerated
  `adapters/codex-cli/plugin/skills/opsx-supervision/SKILL.md` bundle copy —
  frontmatter syntax fix.
- `tests/supervisor/test_supervision_host_endpoints.py` — focused regression
  tests; `tests/supervisor/test_session_bridge.py` — host field rename.

Out of scope: the parked broad host-activation WIP (idle service, worker
supervisor wrapper, native model-config inheritance) is not imported here.
