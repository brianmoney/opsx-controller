# Controller Contract

The controller owns one OpenSpec change per run.

Required behavior:

- accept exactly one change id
- initialize or resume durable state for that change
- run phases in order: implement, review, archive
- loop back from review to implement when review reports any blocking findings
- treat any critical, warning, or note finding as blocking
- stop after a bounded number of failed review rounds or repeated no-progress
  implementation rounds
- archive only after a fresh clean review
- fail closed when change status, phase output, or archive scope is ambiguous

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
