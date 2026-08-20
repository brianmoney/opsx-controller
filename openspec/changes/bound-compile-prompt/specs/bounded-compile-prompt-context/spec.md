## Purpose

Keep the `opsx-plan compile` prompt bounded and its delivery safe in
repositories of any age, so compilation stays reliable no matter how large
the plan archive grows.

## ADDED Requirements

### Requirement: Compile prompt respects an explicit size budget

The compiler SHALL assemble the compile prompt under an explicit character
budget of approximately 128,000 characters. The source plan markdown, the
manifest schema guidance, and the compile instructions SHALL always be
included, even when the source plan alone exceeds the budget; in that case
the compiler SHALL log a warning noting the overage. Optional example
material SHALL be included only while it fits within the remaining budget,
and the compiler SHALL log a note whenever an example is omitted for budget
reasons.

#### Scenario: Large archived plans do not inflate the prompt

- **WHEN** a repository contains archived plan files large enough to exceed the budget and an operator compiles a plan
- **THEN** the resulting compile prompt stays within the budget (plus the always-included fixed sections) and contains no archived plan content

#### Scenario: Oversized source plan still compiles

- **WHEN** the source plan markdown alone exceeds the prompt budget
- **THEN** the prompt still contains the full source markdown, schema guidance, and compile instructions, and a warning about the overage is logged

#### Scenario: Omitted examples are logged

- **WHEN** the canonical sample pair or the repository template pair does not fit within the remaining prompt budget
- **THEN** the compiler omits that example and logs a note identifying what was dropped

### Requirement: Template examples follow a fixed priority order

After the always-included sections, the compiler SHALL include optional
examples in this priority order: first the canonical sample plan pair
(installed or checkout copy), then at most one repository template pair.
The repository template pair SHALL be the smallest active `openspec/plans/`
markdown/TOML pair that fits within the remaining budget; when no active
pair fits, none is included. The compiler SHALL NOT include more than one
repository template pair.

#### Scenario: Canonical sample precedes repository template

- **WHEN** the budget can hold only one of the canonical sample pair or a repository template pair
- **THEN** the canonical sample pair is included and the repository template is omitted

#### Scenario: Smallest fitting repository pair is selected

- **WHEN** a repository has several active plan pairs of different sizes and only the smallest fits the remaining budget
- **THEN** the smallest fitting pair is included as the single repository template example

### Requirement: Archived plans are excluded from template context

The compiler SHALL NOT include plans from `openspec/plans/archived/` in the
compile prompt under any circumstances. Template discovery SHALL consider
only top-level `openspec/plans/` markdown files and their matching TOML
manifests.

#### Scenario: Archived pair is never included

- **WHEN** a repository has both an active plan pair and an archived plan pair and an operator compiles a plan
- **THEN** the archived pair's content never appears in the compile prompt, regardless of remaining budget

### Requirement: Compile client argv never carries an oversized inline prompt

Before spawning any compile client, the compiler SHALL verify that no
single argv element exceeds a conservative inline-argument threshold. When
an element would exceed it, the compiler SHALL fail before spawn with an
error that names the adapter and states that the prompt is too large for
argv delivery, rather than surfacing an operating-system argument-list
error.

#### Scenario: Oversized inline prompt fails before spawn

- **WHEN** a compile client invocation would place a prompt larger than the inline-argument threshold directly in argv
- **THEN** compilation raises a clear actionable error naming the adapter and no client process is spawned
