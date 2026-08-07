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
