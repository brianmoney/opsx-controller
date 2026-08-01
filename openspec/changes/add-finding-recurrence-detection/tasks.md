## 1. Extend the reviewer output contract

- [ ] 1.1 Add the `findings` array to the reviewer response shape in
  `adapters/opencode/agents/opsx-reviewer.md`, including the locus format
  rule (repo-root-relative path, optional `:<symbol>` suffix) and the
  requirement that the array is present and empty on a passing verdict
- [ ] 1.2 Apply the same contract to
  `adapters/claude-code/agents/opsx-reviewer.md`,
  `adapters/codex-cli/agents/opsx-reviewer.toml`, and
  `plugins/opsx-controller/agents/opsx-reviewer.md`, keeping the four agent
  definitions in sync
- [ ] 1.3 Confirm the prose `fix_prompt` sections are unchanged in all four
  definitions — the array is additive, not a replacement
- [ ] 1.4 Document the `PRIOR_FINDING_LOCI` input field in all four agent
  definitions as advisory naming context

## 2. Normalize loci and compute identity

- [ ] 2.1 Add a locus normalizer to `orchestrator/opsx-plan.py` that trims
  whitespace, backticks, and trailing punctuation, converts separators to
  POSIX form, and splits the optional `:<symbol>` suffix
- [ ] 2.2 Resolve the path portion against tracked files (`git ls-files`)
  when the entry is a unique path suffix of exactly one tracked file; retain
  the trimmed form when the match is ambiguous or absent
- [ ] 2.3 Cache the tracked-file list per run so normalization does not shell
  out once per locus
- [ ] 2.4 Add unit tests covering varying path depth resolving to one
  identity, ambiguous suffixes, unresolvable paths, and exact symbol
  comparison

## 3. Persist findings per round

- [ ] 3.1 In `apply_review_result`, record each returned finding's severity,
  normalized locus set, and statement into the round's history entry
- [ ] 3.2 Leave `finding_counts`, `last_review`, and `latest_fix_prompt`
  untouched in shape and meaning
- [ ] 3.3 Tolerate a missing or malformed `findings` array without failing
  the change; record that the round contributed no recurrence evidence
- [ ] 3.4 Add a test asserting a legacy review payload (verdict and counts,
  no findings array) drives the loop exactly as before

## 4. Add the recurrence ceiling

- [ ] 4.1 Add `finding_recurrence_limit` to `load_plan` with default `0` and
  a negative-value rejection mirroring `_parse_escalation_threshold`
- [ ] 4.2 Emit the key from `render_single_change_manifest` and set it to `0`
  in the synthesized single-change configuration
- [ ] 4.3 Add the key to `build_schema_guidance` and to
  `orchestrator/samples/sample-plan.toml`
- [ ] 4.4 Add a round-trip test asserting a non-zero value survives derived
  manifest serialization and reload

## 5. Detect recurrence and halt

- [ ] 5.1 Compute per-locus recurrence counts over the change's persisted
  round findings, counting distinct rounds and only blocking severities
- [ ] 5.2 Derive "blocking" from the severities that gate the verdict under
  the active configuration, so the rule composes with `skip_warning` and
  `skip_suggestion` rather than duplicating their logic
- [ ] 5.3 Evaluate the ceiling only after a failing review verdict, before
  the `max_rounds` check, and halt with a distinct `last_result` value
- [ ] 5.4 Record a reason naming the offending locus and the rounds in which
  it was cited
- [ ] 5.5 Add the recurrence result to `NO_RETRY_RESULTS`
- [ ] 5.6 Emit a `change_failed` notification consistent with the
  no-progress and max-rounds ceilings

## 6. Feed prior loci into review dispatch

- [ ] 6.1 Supply the previous round's blocking-finding loci as
  `PRIOR_FINDING_LOCI` in the review dispatch input
- [ ] 6.2 Emit the field present-and-empty for a change's first review round
- [ ] 6.3 Confirm recurrence accounting is unchanged when the reviewer
  ignores the field

## 7. Verification

- [ ] 7.1 Add a regression reproducing the observed stall: blocking findings
  citing one locus in rounds 4, 5, 7, and 8, asserting a ceiling of `3`
  halts at round 7 rather than running to `max_rounds`
- [ ] 7.2 Add a regression asserting a locus cited only by non-blocking
  severities never triggers a halt
- [ ] 7.3 Run `python3 -m unittest discover -t . -s tests` and confirm no
  regressions
- [ ] 7.4 Run `openspec validate add-finding-recurrence-detection --strict`
