## MODIFIED Requirements

### Requirement: Operator authority and worker actions use separate endpoints

The service SHALL expose a separate operator endpoint and a restricted
worker-actions endpoint. The operator endpoint SHALL be authenticated by OS
peer credentials, so only a process running under the operator's OS identity
can invoke it. The worker-actions endpoint SHALL carry no operator authority:
it SHALL expose only the scoped job-service actions a worker may request.

Operator credentials SHALL NOT be exposed to a model session: no token,
credential, or capability that can invoke the operator endpoint SHALL be
present in a worker's environment, filesystem domain, or transport. An
authority scheme that a worker process can reach — a flag, a TTY check, a
token in the worker environment, or a same-UID permission bit — SHALL NOT
satisfy this requirement.

The service host SHALL resolve the operator endpoint's accepted identity from
the explicitly provisioned `OPSX_SUPERVISOR_OPERATOR_PRINCIPAL`, never from
the identity the service process itself runs under. A service started as the
service principal without a valid provisioned operator principal SHALL fail
closed. The resolved operator uid SHALL be distinct from the service and
worker uids, so the operator allowlist never admits the service or the worker.
The worker-actions endpoint SHALL accept the service principal that hosts it
plus the provisioned worker principal, and SHALL NOT accept the operator.

Endpoint socket access SHALL be provisioned through the optional named groups
`OPSX_SUPERVISOR_OPERATOR_GROUP` and `OPSX_SUPERVISOR_WORKER_GROUP`. With no
group the service-owned socket SHALL be owner-only; with a configured group
the socket SHALL be service-owned with mode `0660` and that group as its group
owner, so group members can connect. A group SHALL NOT replace the
kernel-reported peer-uid decision: it only makes the socket reachable enough
to attempt the check. Socket paths MAY be provisioned outside the ledger home
through the existing socket path settings, and the service SHALL validate the
socket's parent directory as service-owned (or root-owned) and
worker-unwritable — evaluated against the POSIX ACL as well as the mode bits —
with traversal allowed for the provisioned group members. A world-writable or
worker-writable parent, a configured-but-missing group, or an operator group
that admits the worker principal SHALL fail closed at startup before any
socket is bound, and no endpoint mode SHALL be world-accessible.

#### Scenario: A worker principal cannot invoke the operator endpoint

- **WHEN** a process running under the worker identity attempts to connect to
  the operator endpoint
- **THEN** the OS peer-credential check rejects the connection before any
  operation is evaluated

#### Scenario: The worker endpoint carries no operator authority

- **WHEN** a worker process invokes the worker-actions endpoint
- **THEN** only the scoped job-service actions are available, and no
  operator-only operation is reachable through it

#### Scenario: No operator credential exists in the worker domain

- **WHEN** a worker session's environment, filesystem domain, and transport
  are inspected
- **THEN** no credential capable of invoking the operator endpoint is present

#### Scenario: The operator principal is provisioned explicitly

- **WHEN** the service host boots with
  `OPSX_SUPERVISOR_OPERATOR_PRINCIPAL` naming the operator account
- **THEN** the operator endpoint accepts only that account's uid, the
  worker-actions endpoint accepts the service and provisioned worker uids, and
  the operator uid is accepted by no other endpoint

#### Scenario: Serving as the service without a provisioned operator fails closed

- **WHEN** the service process runs under the service principal and no valid
  `OPSX_SUPERVISOR_OPERATOR_PRINCIPAL` is configured
- **THEN** the service host refuses to boot with the named broker-unavailable
  error instead of accepting the service uid as the operator

#### Scenario: A group socket is service-owned and group-reachable

- **WHEN** `OPSX_SUPERVISOR_OPERATOR_GROUP` names the operator's group and the
  service host binds the operator socket
- **THEN** the socket is owned by the service principal with mode `0660` and
  the configured group, and a member of that group still has to pass the
  kernel peer-uid check to have any request served

#### Scenario: An operator group that admits the worker fails closed

- **WHEN** the provisioned operator group contains the worker principal (by
  membership or as the worker's primary group) and the service host boots
- **THEN** the service host refuses to bind the operator socket with the named
  broker-unavailable error, and no operator socket is published

#### Scenario: An insecure socket parent fails closed

- **WHEN** a provisioned socket path places its socket under a parent
  directory that is world-writable, owned by neither the service principal nor
  root, or writable by the worker principal
- **THEN** the service host refuses to bind that endpoint with the named
  broker-unavailable error and tears the session down

### Requirement: Concrete supervised agents and the supervision skill are installed and verified

The supervised roles `supervisor`, `acceptance_reviewer`, `fixer`, and
`verifier` SHALL each have a concrete, installed agent definition, and the
primary session SHALL have an installed supervision skill that briefs it on
its bounded loop. Each agent definition SHALL pin its model through the
registered role resolution so the installed agent cannot drift from the job
policy's pinned identity, and SHALL carry a least-privilege permission
contract for that role. Installation SHALL be verified: a missing or
differing installed agent or skill SHALL be reported by the installer's
verification step rather than silently serving a stale contract. The legacy
`opsx-implementer`, `opsx-reviewer`, and `opsx-archiver` agents SHALL NOT be
modified by this contract.

The canonical supervision skill's YAML frontmatter SHALL be valid YAML: the
`description` SHALL be an explicit scalar (folded or quoted) rather than a
multi-line plain scalar that a colon-space terminates, so every installing
client can parse the skill and an installer run over a corrected copy is not
replaced by a source that cannot be parsed. Adapter-bundled copies of the
skill SHALL be regenerated from the canonical source, so a shipped bundle
never carries the invalid frontmatter.

#### Scenario: A supervised role dispatches under its concrete agent

- **WHEN** a supervised job dispatches the `supervisor`,
  `acceptance_reviewer`, `fixer`, or `verifier` role
- **THEN** the session runs under that role's installed concrete agent
  definition, with the model resolved from the registered role pin

#### Scenario: A stale or missing installed agent is reported

- **WHEN** installer verification runs and an installed supervised agent or
  the supervision skill is missing or differs from the repository source
- **THEN** verification reports the differing file by name instead of
  treating the installation as current

#### Scenario: Legacy agents are untouched

- **WHEN** the supervised agents and skill are installed
- **THEN** the installed `opsx-implementer`, `opsx-reviewer`, and
  `opsx-archiver` agent definitions are byte-identical to their prior
  installed form

#### Scenario: The supervision skill's frontmatter parses

- **WHEN** the canonical `skills/opsx-supervision/SKILL.md` frontmatter is
  parsed as YAML
- **THEN** it parses into a mapping whose `description` is the intended
  briefing text, and the skill body is unchanged by the frontmatter syntax fix

## ADDED Requirements

### Requirement: The broker endpoint transport contains per-connection faults

The service endpoint transport SHALL contain faults to the connection that
caused them. A malformed request (invalid JSON, invalid UTF-8, a non-object
payload, or an unexposed verb), a partial request, a peer disconnect, or a
failed response write SHALL be answered where the peer can still receive a
response and otherwise closed, and SHALL NOT tear down the serve loop: after a
bad connection the service SHALL keep accepting and serving other requests.
The bad peer SHALL be closed and its failure named in the outcome rather than
silently ignored.

Authority and ledger faults SHALL NOT be silenced by this containment: a
handler refusal SHALL still travel to the client as its named error where
possible, and a ledger fault SHALL still be surfaced rather than being
reported as success. A deferred delivery acknowledgement (notification
high-water) SHALL be persisted only after the response has actually been
delivered, so a failed write leaves the acknowledgement pending for a later
retry instead of recording a successful delivery that never happened.

#### Scenario: A malformed request does not kill the serve loop

- **WHEN** a client sends a request that is not a valid JSON object and then a
  second client sends a valid request
- **THEN** the malformed request is closed with a named endpoint error, the
  service keeps serving, and the second request is dispatched normally

#### Scenario: A disappearing peer is contained

- **WHEN** a client disconnects, resets the connection, or vanishes before its
  response can be written
- **THEN** the connection is closed, the failure is reported for that
  connection only, and the serve loop continues

#### Scenario: A ledger fault is not reported as success

- **WHEN** the service-owned ledger raises during a request
- **THEN** the failure is surfaced as the named ledger error rather than a
  successful response, and no deferred delivery acknowledgement is persisted
  for the failed request
