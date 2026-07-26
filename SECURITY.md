# Security

`opsx-controller` shells out to LLM agents (implementer, reviewer, archiver)
that read and write files in your repository and run arbitrary commands you
configure. Workers run with **your** permissions — there is no sandboxing
beyond what your coding CLI itself provides.

Before running `opsx-plan run` or `/opsx-drive` against a repository:

- Review the OpenSpec change manifest (`openspec/changes/<id>/`) you're about
  to drive. The orchestrator executes whatever the implement/review/archive
  workers decide, within the scope that change describes.
- Treat `fast_checks` and `create_invoke`/`implement_invoke`/`review_invoke`/
  `archive_invoke` plan configuration as trusted, executable input — don't run
  a plan file from a source you don't trust.

## Reporting a vulnerability

Open a GitHub issue, or if the report shouldn't be public, contact the
maintainer directly through their GitHub profile.
