# Plan Watch Handoff Visibility

## Purpose

Make reviewer verdicts and worker handoffs visible while preserving the
followed stage log exactly as emitted.

## Requirements

### Requirement: The watcher renders a review verdict banner

`opsx-watch-plan` SHALL render a banner summarizing the reviewer verdict
whenever the followed stage log emits a reviewer verdict payload. The banner
SHALL include the review verdict, the round, the critical/warning/note finding
counts, the review summary, and the declared next phase.

When the payload carries a non-empty `fix_prompt`, the banner SHALL include it
in word-wrapped form rather than as a single unwrapped line.

The banner SHALL be rendered in addition to the payload line itself, never in
place of it.

#### Scenario: A failing review verdict produces a banner

- **WHEN** the followed log emits a line containing a reviewer payload with
  `"status":"reviewed"`, `"verdict":"fail"`, finding counts, a summary, and
  a `fix_prompt`
- **THEN** the watcher emits a banner containing the verdict, the finding
  counts, the summary text, and the fix prompt text

#### Scenario: A passing review verdict produces a banner without a fix prompt

- **WHEN** the followed log emits a reviewer payload with `"verdict":"pass"`
  and an empty or absent `fix_prompt`
- **THEN** the watcher emits a banner containing the verdict and finding counts
- **AND** the banner contains no fix-prompt section

#### Scenario: The verdict payload line is still emitted verbatim

- **WHEN** the watcher renders a review verdict banner for a payload line
- **THEN** the original payload line is also present in the watcher output,
  byte-for-byte unchanged

### Requirement: The watcher renders a stage handoff banner on log switch

`opsx-watch-plan` SHALL render a banner identifying the change id, the round,
and the stage whenever it begins following a stage log whose header contains an
`OPSX WORKER INPUT` block.

When that header carries a `LATEST_FIX_PROMPT` value other than the literal
`none`, the banner SHALL include that value in word-wrapped form. When the value
is `none` or absent, the banner SHALL omit the fix-prompt section entirely.

#### Scenario: A log carrying a corrective handoff shows the prompt

- **WHEN** the watcher switches to a stage log whose `OPSX WORKER INPUT` header
  declares a `LATEST_FIX_PROMPT` with corrective guidance text
- **THEN** the watcher emits a banner containing the change id, the round, the
  stage, and the corrective guidance text

#### Scenario: A first-round log shows no fix prompt

- **WHEN** the watcher switches to a stage log whose `OPSX WORKER INPUT` header
  declares `LATEST_FIX_PROMPT: none`
- **THEN** the watcher emits a banner containing the change id, the round, and
  the stage
- **AND** the banner contains no fix-prompt section

#### Scenario: A log without a worker input header still streams

- **WHEN** the watcher switches to a stage log that contains no
  `OPSX WORKER INPUT` block
- **THEN** no stage handoff banner is emitted
- **AND** the log content is followed and emitted as normal

### Requirement: Fix prompts are wrapped on their section markers

When rendering a fix prompt, the watcher SHALL place the `FINDINGS`,
`CORRECTIVE GUIDANCE`, and `VERIFY` sections on separate lines, and SHALL place
each individual finding marker (`- [critical]`, `- [warning]`, `- [note]`) at
the start of its own line.

Text within each section SHALL be word-wrapped to the banner width without
splitting words.

A fix prompt containing none of these markers SHALL be word-wrapped as a single
paragraph rather than emitted as one long line.

#### Scenario: A structured fix prompt is split into sections

- **WHEN** the watcher renders a fix prompt containing `FINDINGS:`, two finding
  markers, `CORRECTIVE GUIDANCE:`, and `VERIFY:`
- **THEN** each section label begins a new line in the rendered banner
- **AND** each finding marker begins a new line in the rendered banner

#### Scenario: An unstructured fix prompt is still wrapped

- **WHEN** the watcher renders a fix prompt longer than the banner width that
  contains no section markers
- **THEN** the rendered banner spans multiple lines
- **AND** no word is split across a line boundary

### Requirement: Banner rendering degrades for width and non-TTY output

Banner width SHALL be derived from the terminal width when one is available and
SHALL fall back to a fixed default when it is not.

When the available width is below a minimum threshold, the watcher SHALL render
banners in a plain prefixed form rather than a bordered box.

When standard output is not a terminal, the watcher SHALL render banners using
ASCII characters only, so that redirected or piped output remains readable.

#### Scenario: Non-TTY output avoids box-drawing characters

- **WHEN** the watcher's standard output is a pipe rather than a terminal
- **THEN** rendered banners contain no non-ASCII box-drawing characters

#### Scenario: A narrow width drops the box border

- **WHEN** the available terminal width is below the minimum box threshold
- **THEN** banners are rendered in the plain prefixed form
- **AND** the banner content is still present in the output

### Requirement: Banner rendering is additive to the followed stream

Banner rendering SHALL NOT suppress, reorder, or rewrite any line of the
followed log. Every line the watcher reads from a followed log SHALL be written
to standard output unchanged, including its original ANSI escape sequences.

Pattern matching used to detect banner-worthy content SHALL operate on a
separate copy of the line, leaving the emitted line unmodified.

#### Scenario: ANSI escapes survive the reader

- **WHEN** the followed log contains lines carrying ANSI escape sequences
- **THEN** those lines appear in the watcher output with their escape sequences
  intact

#### Scenario: A malformed verdict payload does not lose the line

- **WHEN** the followed log emits a line that resembles a reviewer payload but
  cannot be fully parsed
- **THEN** the line is still emitted unchanged
- **AND** the watcher continues following the log

### Requirement: A followed log is emitted exactly once

`opsx-watch-plan` SHALL write each line of a stage log's existing content to
standard output exactly once when it begins following a log it has not
previously followed.

#### Scenario: A newly appeared log is not duplicated

- **WHEN** a stage log containing a distinctive line appears and the watcher
  begins following it
- **THEN** that distinctive line appears exactly once in the watcher output
