# Controller Contract

The controller owns one OpenSpec change per run.

Required behavior:

- accept exactly one change id
- initialize or resume durable state for that change
- run phases in order: implement, review, archive
- loop back from review to implement when review reports any blocking findings,
  supplying the failed review's complete corrective handoff as
  `LATEST_FIX_PROMPT` so the next implementer receives every finding,
  corrective guideline, and verification requirement without loss
- treat any critical, warning, or note finding as blocking
- classify each task in the change tasks file as manual (line ends in
  `(manual)`) or automatable; never advance an `implemented` round with
  unchecked automatable tasks, re-entering implement with a corrective prompt
  naming them instead and failing the change (naming the task ids) when the
  round budget is exhausted
- archive only after a fresh clean review, exempting unchecked `(manual)`
  tasks from the fail-closed gate and surfacing them as an operator checklist
- stop after a bounded number of failed review rounds or repeated no-progress
  implementation rounds
- fail closed when change status, phase output, or archive scope is ambiguous

Required external inputs:

- repository guidance from `AGENTS.md`
- live OpenSpec status for the active change
- live OpenSpec instructions for the active change
- current change task list and change artifacts

Adapter responsibilities:

- expose an entrypoint for starting or resuming the controller
- map client-specific commands, agents, or skills onto the three phases
- install any client-specific files into the locations that client expects
- preserve the durable state contract and strict review/archive gates

## Evidence-based completion and accepted deferrals

Implementation and review use the chain **requirement -> execution path ->
observation source -> applicable verification**. Mark a task complete only when
the required work is implemented and appropriately verified. Synthetic fixtures
and test doubles establish only the evidence scope they actually exercise;
scaffolding, caller assertions, and synthetic-only coverage cannot establish
live, integration, or candidate proof when that proof is required. Missing
execution prerequisites do not establish implementation or verification.

An unimplemented requirement is assessed by impact, not automatically treated
as a blocking finding. A gap blocks acceptance when it is essential to the
current agreed acceptance scope, correct operation of implemented features, or
a required security or correctness guarantee. Other gaps may be explicitly
accepted for deferral with a brief reason, impact, and follow-up. Cite the
current agreement or scope artifact that accepts the deferral; workers must not
invent acceptance or silently narrow the scope.

Reflect an accepted deferral in the current scope/specs and replace its active
task checkbox with an identified plain follow-up entry retaining the requirement
or task id, reason, impact, follow-up, and acceptance reference. Never check
unimplemented work as complete or relabel it `(manual)` to evade the gate.
Active unchecked automatable tasks still block advancement and archive.

Accepted deferrals do not trigger another implementation round. Disclose them
in existing change artifacts and the phase result's existing `summary`, not in
review `finding_counts`, `findings`, or `fix_prompt`, including as a `note`.
Completion claims cover the implemented and verified accepted scope; they do
not imply that deferred work is complete or that unproven guarantees hold.

Reviewers independently inspect the execution path and evidence rather than
trusting implementer claims. Each blocking finding cites the violated
requirement and a concrete failing case with expected versus observed behavior.
A recurring critical requires reassessing why the previous correction failed
before another patch; the reviewer independently checks closure and retains the
locus for the same defect. These rules refine handoff criteria without adding
phase statuses or overriding the existing task, review, and archive gates.
