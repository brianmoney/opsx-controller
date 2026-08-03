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
