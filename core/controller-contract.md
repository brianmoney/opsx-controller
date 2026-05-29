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
