## Purpose

Define plan-controlled promotion of implement dispatches to a dedicated escalation model after repeated review failures.

## Requirements

### Requirement: A plan key sets the review-failure threshold for implementer escalation

The plan `[plan]` table SHALL accept an integer key `escalate_after_review_fails` naming the number of failed reviews after which the implement stage is promoted to the escalation model.

The key SHALL default to `0`, and `0` SHALL mean escalation is disabled.

The plan loader SHALL reject a negative value with a clear error rather than coercing it.

The key SHALL be plan-level only; there SHALL NOT be a per-change override, matching `max_rounds` and `no_progress_limit`.

The synthesized single-change configuration used by the manifest-free runner SHALL set the key to `0`.

#### Scenario: Key absent leaves escalation disabled

- **WHEN** a plan manifest omits `escalate_after_review_fails`
- **THEN** the loaded configuration carries the value `0` and no implement dispatch in that run uses an escalation model

#### Scenario: Negative threshold is rejected

- **WHEN** a plan manifest sets `escalate_after_review_fails` to a negative integer
- **THEN** plan loading fails with an error naming the key and no run starts

#### Scenario: Manifest-free run disables escalation

- **WHEN** an operator runs a single change without a plan manifest
- **THEN** the synthesized configuration sets `escalate_after_review_fails` to `0`

#### Scenario: Threshold survives derived-manifest round trip

- **WHEN** a configuration carrying a non-zero `escalate_after_review_fails` is serialized to a derived manifest and reloaded for round-trip verification
- **THEN** the reloaded configuration carries the same value and the round-trip comparison reports no divergence

### Requirement: Escalation is triggered by failed reviews only

Escalation SHALL be evaluated per implement dispatch against the number of reviews that have failed for that change.

Escalation SHALL be active for an implement dispatch when the threshold is greater than zero and the number of failed reviews for that change is greater than or equal to the threshold. Because the direct loop advances the round only on a failed review, the count of failed reviews SHALL equal the current round minus one.

Rounds in which the implement worker reported no progress SHALL NOT count toward the threshold; the existing `no_progress_limit` ceiling remains the control for that failure mode.

Once escalation becomes active for a change it SHALL remain active for that change's remaining implement dispatches.

Escalation SHALL apply to the implement stage only; review and archive dispatches SHALL continue to use their own configured models.

#### Scenario: Rounds below the threshold use the base model

- **WHEN** `escalate_after_review_fails` is `2` and the change is dispatched for implement in round 1 or round 2
- **THEN** the dispatch uses the model resolved for the `implementer` role

#### Scenario: Threshold reached promotes the implement stage

- **WHEN** `escalate_after_review_fails` is `2` and two reviews have failed, advancing the change to implement round 3
- **THEN** the implement dispatch uses the model resolved for the `implementer_escalation` role

#### Scenario: Escalation persists across later rounds

- **WHEN** a change has escalated and a subsequent review also fails, advancing it to a further implement round below the round ceiling
- **THEN** that implement dispatch also uses the escalation model

#### Scenario: No-progress rounds do not trigger escalation

- **WHEN** `escalate_after_review_fails` is `2`, no review has failed, and an implement worker reports no progress
- **THEN** escalation does not become active and the no-progress handling is unchanged

#### Scenario: Review and archive stages are unaffected

- **WHEN** a change has escalated and its review and archive stages are dispatched
- **THEN** those dispatches use the models resolved for the `reviewer` and `archiver` roles

### Requirement: Escalation swaps the implementer model environment variable at dispatch

Before each implement dispatch, the orchestrator SHALL set `OPSX_IMPLEMENTER_MODEL` to the escalation model when escalation is active for that dispatch, and to the base implementer model when it is not.

The orchestrator SHALL NOT require a separate escalated invoke template; escalation SHALL work through whatever `implement_invoke` string is configured, including per-plan overrides that reference `$OPSX_IMPLEMENTER_MODEL`.

Because the value is set deterministically before every implement dispatch, the orchestrator SHALL NOT need to save and restore the variable around a dispatch.

#### Scenario: Custom invoke string escalates without modification

- **WHEN** a plan overrides `implement_invoke` with a custom command that references `$OPSX_IMPLEMENTER_MODEL` and the change escalates
- **THEN** the dispatched command expands to the escalation model with no change to the invoke string

#### Scenario: De-escalated dispatch restores the base model

- **WHEN** an implement dispatch occurs for which escalation is not active, in a process where an earlier dispatch had escalated
- **THEN** the dispatched command expands to the base implementer model

### Requirement: A configured threshold without an escalation model fails closed

When `escalate_after_review_fails` is greater than zero and the `implementer_escalation` role is unresolved for the configuration's adapter, the orchestrator SHALL fail at plan-configuration time with an error naming the unresolved role and pointing at the model inspection and seeding commands, and SHALL NOT start the run.

The orchestrator SHALL NOT silently fall back to the base implementer model in this case, and SHALL NOT defer the failure until the round at which escalation would first take effect.

#### Scenario: Missing escalation model blocks the run up front

- **WHEN** a plan sets `escalate_after_review_fails` to `2` and no configuration file or environment variable supplies an escalation model for its adapter
- **THEN** the run fails before the first implement dispatch with an error naming the `implementer_escalation` role

#### Scenario: Threshold disabled tolerates a missing escalation model

- **WHEN** a plan leaves `escalate_after_review_fails` at `0` and no escalation model is configured
- **THEN** the run proceeds normally and no error is raised

### Requirement: Escalation status is observable in state and telemetry

Plan state for a change SHALL record whether escalation is active, the round at which it activated, and the model in use, so that an operator inspecting state can tell an escalated run from an un-escalated one.

State records written before this capability existed SHALL gain the escalation fields with disabled values when they are loaded, without requiring an operator reset.

Telemetry for an escalated implement stage SHALL attribute the record to the escalation model rather than the base implementer model.

#### Scenario: State records the activation

- **WHEN** a change escalates at round 3
- **THEN** its persisted state reports escalation as active, the activating round as 3, and the escalation model identifier

#### Scenario: Pre-existing state loads without reset

- **WHEN** a plan state file written before this capability is loaded
- **THEN** the escalation fields are populated with disabled defaults and the run resumes normally

#### Scenario: Telemetry attributes the escalated model

- **WHEN** an escalated implement stage completes and telemetry recovers model identity from the invocation
- **THEN** the telemetry record reports the escalation model identifier

### Requirement: The threshold key is documented as behavioral rather than ignored

Operator documentation, the plan manifest schema reference, and the manifest auditor SHALL describe `escalate_after_review_fails` as a supported key with the semantics defined here.

The documentation and the auditor SHALL NOT continue to list the key as one the loader silently ignores.

The compile-time schema guidance supplied to the plan-authoring model SHALL include the key so that compiled manifests can set it.

#### Scenario: Auditor accepts the key

- **WHEN** the manifest auditor inspects a manifest that sets `escalate_after_review_fails`
- **THEN** it does not report the key as a behavioral-looking no-op

#### Scenario: Schema guidance offers the key

- **WHEN** the orchestrator builds the schema guidance used to compile an authored plan document into a manifest
- **THEN** the guidance describes `escalate_after_review_fails`, its default, and its counting rule
