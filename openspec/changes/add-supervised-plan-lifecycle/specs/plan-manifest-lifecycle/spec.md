## ADDED Requirements

### Requirement: Supervised registration binds the job to the canonical plan manifest

Registering a supervised job for a plan SHALL capture the protected manifest
snapshot from the plan's canonical manifest content and record it with the
job at registration. Gate and dispatch decisions for the job SHALL be
evaluated against that protected snapshot, not against repo-writable copies.

A manifest that changes after registration SHALL NOT be silently adopted:
adopting a changed manifest SHALL require an explicit operator decision — a
new registration or an explicit revision — and dispatch against stale
material SHALL be blocked with the named stale-material error under the
existing revalidation requirement.

A supervised job's completion and plan retirement SHALL consume the same
manifest ground truth as an unsupervised run: the existing archive evidence
and the existing completed-plan retirement semantics. Supervision SHALL
introduce no separate plan-completion or plan-retirement authority.

#### Scenario: Registration snapshots the canonical manifest

- **WHEN** an operator registers a supervised job for a plan
- **THEN** the protected manifest snapshot is captured from the plan's
  canonical manifest content and recorded with the job at registration

#### Scenario: A changed manifest is not silently adopted

- **WHEN** the plan manifest changes after registration and the supervised
  job is about to dispatch
- **THEN** dispatch is blocked with the named stale-material error until the
  operator explicitly adopts the change through a new registration or an
  explicit revision

#### Scenario: Supervised completion uses the same plan ground truth

- **WHEN** a supervised job's plan completes
- **THEN** completion is determined from the same archive evidence as an
  unsupervised run, and the completed plan retires under the existing
  retirement semantics unchanged
