## 1. Endpoint principal provisioning

- [x] 1.1 Add `OPERATOR_PRINCIPAL_ENV`
  (`OPSX_SUPERVISOR_OPERATOR_PRINCIPAL`) and `provisioned_operator_name()` in
  `lib/supervisor/authority.py`, and make
  `principal_set_from_environment()` prefer an explicit argument, then the
  provisioned env, then the invoking user.
- [x] 1.2 In `lib/orchestrator/supervision.py`, resolve the operator endpoint
  allowlist from the provisioned operator principal; fail closed when serving
  as the service principal without a valid operator; require the operator uid
  to be distinct from the service and worker uids.
- [x] 1.3 Make the worker-actions allowlist the service principal plus the
  provisioned worker principal, never the operator.
- [x] 1.4 Add focused tests for env precedence, the fail-closed service
  posture, collapsed principals, and the two endpoint allowlists in
  `tests/supervisor/test_supervision_host_endpoints.py`.

## 2. Socket groups and secure parents

- [x] 2.1 Support `OPSX_SUPERVISOR_OPERATOR_GROUP` and
  `OPSX_SUPERVISOR_WORKER_GROUP`: a configured group makes the service-owned
  socket `0660` with that group as its group owner; kernel peer-uid
  authentication remains the authoritative accept decision.
- [x] 2.2 Fail closed at startup on a configured-but-missing group, an
  operator group that admits the worker, an unsafe socket parent (not
  service-owned/root-owned, worker-writable by mode or ACL, or
  world-writable), and a world-accessible socket mode; allow sockets outside
  the ledger home through the existing socket path settings.
- [x] 2.3 Add focused tests for `0600`/`0660` modes, group reachability
  not bypassing the uid check, secure/untrusted/world-writable parents, stale
  socket replacement, and non-socket path refusal.

## 3. Connection fault containment

- [x] 3.1 Contain malformed JSON/UTF-8, partial requests, disconnects, and
  failed response writes per connection in `lib/supervisor/broker_client.py`
  and the serve loop, closing the bad peer and continuing to serve; keep
  authority and ledger faults surfaced; persist a deferred acknowledgement
  only after a successful write.
- [x] 3.2 Add focused tests for the malformed-request loop survival, a
  mid-request disconnect, a connection reset during read, a failed refusal
  write, a failed success write not acknowledging the delivery, and an
  unsilenced ledger fault.

## 4. Canonical supervision skill frontmatter

- [x] 4.1 Render the `description` in
  `skills/opsx-supervision/SKILL.md` as a folded scalar so the frontmatter is
  valid YAML; body unchanged; regenerate the committed codex-cli plugin
  bundle copy so it carries the same valid frontmatter.
- [x] 4.2 Add focused tests asserting an explicit scalar, a successful YAML
  parse where PyYAML is available, and an unchanged body.

## 5. Validation and documentation

- [x] 5.1 Document `OPSX_SUPERVISOR_OPERATOR_PRINCIPAL`,
  `OPSX_SUPERVISOR_OPERATOR_GROUP`, and `OPSX_SUPERVISOR_WORKER_GROUP` (and
  the socket path/parent rules) in `docs/opsx-supervision-service.md`.
- [x] 5.2 Run `python3 -m unittest discover -t . -s tests` —
  2207 tests OK (2 skipped).
- [x] 5.3 Run `node tests/opencode/test-opsx-usage-emitter.js` —
  48 passed, 0 failed.
- [x] 5.4 Run `openspec validate fix-supervision-host-provisioning --strict`
  and `openspec validate --all`.
- [x] 5.5 Run `python3 scripts/generate-phase-agents.py --check` (no
  agent body or template touched; expected no drift).
