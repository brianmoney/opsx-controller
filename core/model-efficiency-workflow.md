# Model Efficiency Workflow

How to use `opsx-plan` telemetry, cost estimation, reporting, and dashboards to
benchmark OPSX model choices and answer: *which model combination is measurably
better for my workload?*

This document does **not** prescribe a single model. It describes the data
collection and comparison methodology and lets you apply your own criteria
(reliability, speed, rework, cost) to your own plans.

---

## Quick Start

The benchmarking loop has six steps. Model selection uses `OPSX_*_MODEL`
environment variables, but these are **install-time** inputs, not per-run
exports. The installer bakes them into OpenCode agent configs as concrete
`model:` values (see `lib/install-common.sh` and `~/.config/opencode/agents/`).
Changing models requires re‑running the installer.

```bash
# 0. Record the baseline commit (required for clean re-runs)
git rev-parse HEAD > .baseline-commit

# 1. Set model environment variables and install agents with the target models
set -a
source .env
set +a
# Verify the .env contains values for OPSX_CONTROLLER_MODEL,
# OPSX_IMPLEMENTER_MODEL, OPSX_REVIEWER_MODEL, and OPSX_ARCHIVER_MODEL.
bash adapters/opencode/install.sh --global --verify

# 2. Compile a markdown plan to TOML (one-time)
opsx-plan compile docs/my-plan.md -o plan.toml

# 3. Dry-run to review the DAG and gate config
opsx-plan run plan.toml --dry-run

# 4. Run the plan
opsx-plan run plan.toml

# 5. Inspect results
opsx-plan report plan.toml              # human-readable tables
opsx-plan report plan.toml --json       # machine-readable JSON

# 6. Generate a static dashboard
opsx-plan dashboard plan.toml           # -> .opsx-plan/dashboards/<plan_name>.html
```

In these examples, replace `<plan_name>` with the plan's actual name: the
`[plan].name` value from the manifest, or the TOML filename stem when `name`
is omitted.

To produce a second comparable data point you must first restore a clean
baseline: a completed plan run leaves archive commits and orchestrator
state that prevent a straightforward re-run (see `orchestrator/README.md`).
Reset the worktree to the recorded baseline, update the model values in
`.env`, re-install the agents, and re-run the same plan:

```bash
# Restore repo to the pre-run baseline
git reset --hard $(cat .baseline-commit)
git clean -fd
rm -f .opsx-plan/<plan_name>.state.json

# Edit .env with a second model set, then re-install
set -a
source .env
set +a
bash adapters/opencode/install.sh --global --verify

# Re-run with the second model set
opsx-plan run plan.toml
```

By default, `opsx-plan report` and `opsx-plan dashboard` aggregate a single
run: the latest `run_id` in `.opsx-plan/telemetry/<plan_name>.jsonl`. To
compare two runs, generate separate outputs for each run id:

```bash
opsx-plan report plan.toml --run-id <run_id_a> > report.<run_id_a>.txt
opsx-plan report plan.toml --run-id <run_id_b> > report.<run_id_b>.txt

opsx-plan dashboard plan.toml --run-id <run_id_a> \
  --output .opsx-plan/dashboards/<plan_name>.<run_id_a>.html
opsx-plan dashboard plan.toml --run-id <run_id_b> \
  --output .opsx-plan/dashboards/<plan_name>.<run_id_b>.html
```

---

## Configuring Model Sets

### Model selection is install-time, not per-run

The `OPSX_*_MODEL` environment variables are **install-time** inputs. The
OpenCode adapter installer (`adapters/opencode/install.sh` via
`lib/install-common.sh`) reads these variables and substitutes `{env:OPSX_*_MODEL}`
placeholders with concrete `provider/model` values in the agent YAML frontmatter
(`~/.config/opencode/agents/`). OpenCode dispatches each agent to the model
specified in its `model:` field — there is no further runtime resolution or
fallback inside `opsx-plan`.

| Variable | Baked into agent | Agent role |
|---|---|---|
| `OPSX_CONTROLLER_MODEL` | `opsx-controller.md` | plan‑drive orchestrator |
| `OPSX_IMPLEMENTER_MODEL` | `opsx-implementer.md` | implement phase |
| `OPSX_REVIEWER_MODEL` | `opsx-reviewer.md` | review phase |
| `OPSX_ARCHIVER_MODEL` | `opsx-archiver.md` | archive phase |

The baked model value is captured in telemetry as `model.model_id` (and
`model.provider` when the identifier follows `provider/model-id` convention).
It flows into aggregation, leaderboard grouping, and cost‑estimation lookups.

**To change models**, update the variables in your `.env` file and re‑run the
installer:

```bash
# Edit .env with new model ids, then:
set -a
source .env
set +a
bash adapters/opencode/install.sh --global --verify
```

### Comparing two model sets

To compare two model combinations, run the **same plan** twice, restoring a
clean worktree between runs so each model set starts from an identical repo
state. Two mutating plan‑stage runs in one worktree is a known failure mode
(see `orchestrator/README.md`), so re‑installing agents and re‑running is not
enough — you must reset the repo as well.

```bash
# Record the baseline commit first
git rev-parse HEAD > .baseline-commit

# Configure model set A in .env, then install and run
# .env: OPSX_IMPLEMENTER_MODEL="deepseek/deepseek-v4-pro"
#       OPSX_REVIEWER_MODEL="anthropic/claude-sonnet-4-20250514"
set -a
source .env
set +a
bash adapters/opencode/install.sh --global --verify
opsx-plan run plan.toml

# Restore a clean baseline before model set B
# A completed plan run leaves archive commits and orchestrator
# state; the worktree must match the starting state.
git reset --hard $(cat .baseline-commit)
git clean -fd
rm -f .opsx-plan/<plan_name>.state.json

# Configure model set B in .env, then re-install and run
# .env: OPSX_IMPLEMENTER_MODEL="openai/gpt-4o"
#       OPSX_REVIEWER_MODEL="openai/gpt-4o"
set -a
source .env
set +a
bash adapters/opencode/install.sh --global --verify
opsx-plan run plan.toml
```

Telemetry is per-plan, not per-run. Multiple runs for the same plan append to
the same JSONL file at `.opsx-plan/telemetry/<plan_name>.jsonl` (see
`openspec/specs/plan-run-observability/spec.md`). By default,
`opsx-plan report` and `opsx-plan dashboard` select the latest `run_id`.
`--run-id` selects one specific run, not multiple runs:

```bash
opsx-plan report plan.toml --run-id <run-id>
opsx-plan dashboard plan.toml --run-id <run-id>
```

To compare run A and run B, render each run separately and review the two
outputs side by side:

```bash
opsx-plan report plan.toml --run-id <run_id_a>
opsx-plan report plan.toml --run-id <run_id_b>
```

---

## Running Comparable Plans

A model comparison is only valid when the **same work** was performed under each
model set. The following requirements define comparability.

### What makes plan runs comparable

| Criterion | Requirement |
|---|---|
| Plan content | Identical changes in identical order, same DAG |
| Toolchain | Same adapter (e.g. OpenCode), same orchestrator version |
| Change complexity | Same accepted changes — not a bug‑fix against one model set and a feature‑build against another |
| Environment | Same repo state at start‑of‑run (checked out commit) |

When these hold, any difference in metrics between two runs is attributable to
model choice rather than to different work.

### What breaks comparability (invalid comparisons)

- **Complexity bias:** Running *run‑A* against a plan that contains a
  single‑file typo fix and *run‑B* against a plan that contains a multi‑file
  refactor with new specs. The second plan is harder by construction — more
  tokens, more rounds, more cost — and those differences are **not** the
  model's "fault."

  **Mitigation:** use the **exact same plan** (`plan.toml`) for every model
  set. The plan document should be authored once, compiled once, and reused
  across runs.

- **Plan content mismatch:** Authoring different plans for different model sets
  (e.g. a 3‑change plan for model A and a 5‑change plan for model B). The
  extra changes add tokens, rounds, and cost independent of model performance.

  **Mitigation:** keep a single blessed plan and never edit it between runs.

- **Mixed billing without context:** Comparing a per‑token model
  (`billing_mode = "per_token"`) against a subscription‑amortized model
  (`billing_mode = "subscription"`) without accounting for the subscription
  denominator assumption. The subscription estimate depends on an
  operator‑configured denominator; comparing the raw `$0.02` amortized cost
  against a `$2.50` per‑token cost without this context is misleading.

  **Mitigation:** when comparing models with different billing modes, note the
  denominator used for subscription amortization and consider the total
  subscription cost alongside per‑run estimates.

- **Different adapter or orchestrator versions:** A change in the adapter
  (OpenCode vs. Claude Code) or orchestrator version between runs can affect
  worker behavior, token usage reporting, and model‑identity extraction. The
  comparison is then between *toolchain versions* as much as between models.

  **Mitigation:** pin the opsx‑controller version and adapter for the duration
  of a benchmarking cycle. Record the version in your run notes.

### Practical guidance for a comparison run

```bash
# 1. Author a plan once (the "comparison plan")
#    Include 3–5 realistic changes of similar complexity.
#
# 2. Compile to TOML
opsx-plan compile docs/comparison-plan.md -o plan.toml

# 3. Dry-run to verify
opsx-plan run plan.toml --dry-run

# 4. Record the baseline commit (required for clean re-runs)
git rev-parse HEAD > .baseline-commit

# 5. Install model set A and run
#    Update .env with model set A values, then:
set -a
source .env
set +a
bash adapters/opencode/install.sh --global --verify
opsx-plan run plan.toml

# 6. Restore a clean baseline for model set B
#    A completed plan run leaves archive commits and orchestrator
#    state.  Resetting only the orchestrator (`opsx-plan reset`) is
#    not enough — the worktree must match the starting state.
git reset --hard $(cat .baseline-commit)
git clean -fd
rm -f .opsx-plan/<plan_name>.state.json

# 7. Install model set B and run
#    Update .env with model set B values, then:
set -a
source .env
set +a
bash adapters/opencode/install.sh --global --verify
opsx-plan run plan.toml

# 8. Compare specific runs
opsx-plan report plan.toml --run-id <run_id_a>
opsx-plan report plan.toml --run-id <run_id_b>
opsx-plan dashboard plan.toml --run-id <run_id_a> \
  --output .opsx-plan/dashboards/<plan_name>.<run_id_a>.html
opsx-plan dashboard plan.toml --run-id <run_id_b> \
  --output .opsx-plan/dashboards/<plan_name>.<run_id_b>.html
```

---

## Maintaining the Pricing Catalog

Cost estimation depends on `lib/pricing/catalog.toml`. Models not listed there
produce `unresolved` costs in reports and dashboards.

### Adding a new model entry

Append a `[[entries]]` block to `lib/pricing/catalog.toml`. Required fields for
every entry:

| Field | Type | Description |
|---|---|---|
| `provider` | string | API provider name (e.g. `"openai"`, `"anthropic"`) |
| `model_id` | string | Canonical model identifier (e.g. `"gpt-4o"`) |
| `display_name` | string | Human‑readable name for reports (e.g. `"GPT-4o"`) |
| `billing_mode` | string | `"per_token"` or `"subscription"` |
| `currency` | string | ISO 4217 code (e.g. `"USD"`) |
| `effective_date` | string | ISO‑8601 date when this pricing became effective |

**Per‑token models** also require:

| Field | Required | Description |
|---|---|---|
| `input_price_per_mtok` | **yes** | USD per million input tokens |
| `output_price_per_mtok` | no | USD per million output tokens |
| `cached_input_price_per_mtok` | no | USD per million cached input tokens |
| `reasoning_price_per_mtok` | no | USD per million reasoning tokens |

**Subscription models** also require:

| Field | Required | Description |
|---|---|---|
| `subscription_period` | **yes** | `"monthly"` or `"yearly"` |
| `subscription_price` | **yes** | Price in the specified currency |

Optional: `notes` (freeform operator text).

Example per‑token entry:

```toml
[[entries]]
provider = "openai"
model_id = "gpt-4o"
display_name = "GPT-4o"
billing_mode = "per_token"
currency = "USD"
input_price_per_mtok = 2.50
output_price_per_mtok = 10.00
cached_input_price_per_mtok = 1.25
effective_date = "2025-01-01"
```

Example subscription entry:

```toml
[[entries]]
provider = "github"
model_id = "copilot"
display_name = "GitHub Copilot"
billing_mode = "subscription"
currency = "USD"
subscription_period = "monthly"
subscription_price = 10.00
effective_date = "2025-01-01"
```

### Updating a pricing rate

**Do not edit existing entries in place.** Add a new `[[entries]]` block with
the same `provider` and `model_id` and a later `effective_date`. The loader
always picks the latest‑effective entry for a given `(provider, model_id)`
pair. This preserves historical pricing so past telemetry records remain
reproducible.

```toml
# Old entry (kept for history)
[[entries]]
provider = "openai"
model_id = "gpt-4o"
display_name = "GPT-4o"
billing_mode = "per_token"
currency = "USD"
input_price_per_mtok = 2.50
effective_date = "2025-01-01"

# New entry (becomes active 2025-07-01)
[[entries]]
provider = "openai"
model_id = "gpt-4o"
display_name = "GPT-4o (reduced)"
billing_mode = "per_token"
currency = "USD"
input_price_per_mtok = 2.00
effective_date = "2025-07-01"
```

### What happens when a model is missing from the catalog

When telemetry captures a `model_id` with no matching catalog entry, cost
estimation produces `cost.status = "unresolved"` with `cost.unresolved_reason`
set to `"unknown model"` (or `"unknown provider"` when the provider itself has
no entries). In reports:

- **Table mode**: renders as `"unresolved"` in the cost column.
- **JSON mode**: `cost_status` is `"unresolved"`, `estimated_cost` is `null`.
- **Dashboard**: amber `(unresolved)` label.

Unresolved costs do **not** contribute to total or average cost calculations.
The change is still counted in `unresolved_cost_changes`.

---

## Interpreting Cost Estimates

### Cost statuses

Every telemetry record carries a `cost.status` field (requirement: *Cost
estimation placeholders reserve billing mode slots* in the
`plan-run-observability` spec):

| Status | Meaning | Rendering |
|---|---|---|
| `estimated` | A cost was computed from usage and pricing data. | `$X.XX` (table), `estimated_cost` field (JSON) |
| `unresolved` | Estimation attempted but required inputs were missing (unknown model, no usage data, missing subscription denominator). | `unresolved` (table), `null` (JSON) |
| `unavailable` | Cost estimation has never been attempted (records before Phase 2, or records from runs where estimation was disabled). | `—` (table), `null` (JSON) |

**Zero estimated cost** (`$0.00`) is distinct from unresolved: a stage that
used zero tokens with a known model produces a legitimate zero‑cost estimate.

### Per‑token (list‑price‑equivalent) cost

For models with `billing_mode = "per_token"`, the estimate is the sum of priced
token categories:

```
cost = (input_tokens / 1_000_000) * input_price_per_mtok
     + (output_tokens / 1_000_000) * output_price_per_mtok
     + (cached_input_tokens / 1_000_000) * cached_input_price_per_mtok
     + (reasoning_tokens / 1_000_000) * reasoning_price_per_mtok
```

Null token categories contribute zero. A category with observed usage but a
null catalog rate makes the whole estimate unresolved.

### Subscription‑amortized cost

For models with `billing_mode = "subscription"`, the effective stage cost is:

```
cost = subscription_price * (stage_usage_units / subscription_usage_denominator)
```

This requires an operator‑configured denominator (tokens per subscription
period). Without a configured denominator, the cost is unresolved.

### Reading cost in reports

`opsx-plan report <plan>` (table mode) shows:

- **Plan summary**: `total_estimated_cost` (sum), plus breakdown counts
  (`estimated_cost_changes`, `unresolved_cost_changes`, `unknown_cost_changes`).
- **Per‑change table**: cost column renders `$X.XX` / `unresolved` /
  `unavailable` / `—` per change.
- **Leaderboard**: `average_cost` computed from only the changes with at least
  one estimated‑cost stage.

### Reading cost in dashboards

`opsx-plan dashboard <plan>` includes a **Cost Breakdown** section with a CSS
bar chart showing the proportion of estimated, unresolved, and unknown costs.
The **Per‑Change Table** uses color coding: normal text for `$X.XX`, amber for
`(unresolved)`, gray for `—` / no data.

---

## Comparing Model Combinations

Once you have two or more runs against the same plan, compare one selected run
at a time. By default, both `opsx-plan report` and `opsx-plan dashboard`
aggregate only the latest `run_id`; use `--run-id` to generate separate outputs
for each run you want to compare.

### The leaderboard

The **Model Leaderboard** table groups changes by the full
`(implementer_model, reviewer_model, archiver_model)` combination from the
selected run and reports:

| Column | Description |
|---|---|
| Implementer Model | `model.provider:model.model_id` from implement stages |
| Reviewer Model | `model.provider:model.model_id` from review stages |
| Archiver Model | `model.provider:model.model_id` from archive stages |
| Change Count | How many changes used this combination in the selected run |
| Success Rate | `completed / change_count` |
| First‑Pass Rate | Fraction of changes that passed review on round 1 |
| Avg Rounds | Average implement‑review cycles per change |
| Avg Duration | Average wall‑clock time per change |
| Avg Tokens | Average token consumption per change |
| Avg Cost | Average estimated cost per change |

Unknown identities are grouped as `unknown`. The leaderboard includes all
changes in the selected run, including completed, failed, blocked, and
incomplete changes.

### Interpreting the columns

- **Success Rate + First‑Pass Rate**: proxies for *reliability*. A model set
  with higher first‑pass rate spends fewer rounds on review‑driven rework.
- **Avg Rounds**: proxies for *rework*. Higher rounds → more review failures →
  the implementer needed more attempts to satisfy the reviewer.
- **Avg Duration**: proxies for *speed*. Careful: if model A is faster but
  fails review more often, its total duration may still be higher because of
  extra rounds.
- **Avg Tokens + Avg Cost**: proxies for *resource consumption*. Per‑token
  models expose cost directly; subscription models expose amortized cost.

No single column is the "right" metric. The right metric depends on your
priorities: a reviewer‑constrained team may value first‑pass rate above all; a
budget‑constrained team may optimize for cost.

### Filtering for focused analysis

```bash
# Compare only implementer model performance
opsx-plan report plan.toml --stage implement

# Focus on one change's performance across runs
opsx-plan report plan.toml --change add-gardening-suggestions

# Isolate one run
opsx-plan report plan.toml --run-id <run-id>

# Filter leaderboard by model substring
opsx-plan report plan.toml --model gpt-4o
```

The dashboard accepts `--change` and `--run-id` filters for the same purpose.

### Using the dashboard

`opsx-plan dashboard plan.toml` produces a self‑contained static HTML file with
seven sections:

1. **Plan Summary Header**: overview of all changes.
2. **Model Leaderboard Table**: ranked by success rate, best values highlighted.
3. **Per‑Change Table**: one row per change with color‑coded status badges.
4. **Failure Breakdown**: lists failed changes and their failure reasons.
5. **Cost Breakdown**: CSS bar chart of estimated / unresolved / unknown costs.
6. **Rounds Histogram**: distribution of round counts across completed changes.
7. **Stage Timeline**: every stage invocation sorted by `started_at`.

The dashboard is a convenient artifact to share with a team or archive alongside
run results.

---

## Limitations and Caveats

The telemetry and cost estimation pipeline has the following known limitations.
Each is by design and documented here so operators can interpret results
correctly.

### Costs are list‑price‑equivalent only

All per‑token cost estimates use **published list prices** from
`lib/pricing/catalog.toml`. They do **not** account for:

- Volume discounts, committed‑use discounts, or reserved capacity pricing.
- Free tier allowances or promotional credits.
- Provider billing‑side rounding, tax, or currency conversion fees.

The pipeline computes a reproducible estimate from catalog rates and observed
token counts. It does **not** reconcile with a provider invoice.

### No live provider pricing or billing reconciliation

The pricing catalog is a static operator‑maintained file. It does not query
provider APIs for current pricing, does not pull usage from provider dashboards,
and does not reconcile estimates against bills. When prices change, you must
update the catalog yourself (following the add‑new‑entry convention).

### Subscription cost amortization depends on operator configuration

Subscription‑model cost estimates require a configured **subscription usage
denominator** (e.g., `50_000_000` tokens per month). Without this denominator,
subscription costs are `unresolved`. The denominator is an operator judgment
about expected usage; different denominators produce different amortized costs.

Compare subscription‑model costs against per‑token‑model costs with caution:
the per‑token estimate is deterministic from observed tokens, while the
subscription estimate depends on a denominator you choose.

### Unknown model identity produces unresolvable costs

When the telemetry extractor cannot determine `model.provider` or
`model.model_id` from worker output (e.g., the worker uses a model alias the
extractor does not recognize, or the adapter does not expose model metadata),
cost estimation cannot look up pricing and produces `cost.status = "unresolved"`
with reason `"model identity unavailable"`.

These changes still contribute to `unresolved_cost_changes`. In the leaderboard,
missing model identities are rendered as `unknown` in the affected role column.

### Comparable plans must use identical changes

As described in **Running Comparable Plans**: any difference in change
complexity, plan content, adapter version, or environment state between two
runs makes a direct metric comparison invalid. The pipeline does not
automatically detect invalid comparisons — you must ensure comparability
yourself by controlling the plan, environment, and toolchain.

### Correlating model choice with change complexity requires operator judgment

The pipeline measures *what happened*, not *how hard the work was*. A change
that is inherently more complex (more files, more design decisions) will
consume more tokens, rounds, and cost regardless of which model processes it.
The pipeline does not embed a difficulty metric. When comparing across
different plans or change sets, apply your own complexity adjustment.

### Catalog versioning and reproducibility

Historical telemetry records store `cost.price_snapshot` inline, so old
estimates remain reproducible even after the catalog is updated. However, if
you re‑aggregate old telemetry against a new catalog version, the estimates
will differ. The catalog version recorded in telemetry
(`cost.pricing_catalog_version`) identifies which catalog was current when the
estimate was computed.

---

## Reference

- **Telemetry schema**: `openspec/specs/plan-run-observability/spec.md` —
  requirements for telemetry records, cost estimation, pricing catalog, report,
  and dashboard.
- **Pricing catalog**: `lib/pricing/catalog.toml` — the operator‑maintained
  pricing entries file (format specified in the pricing catalog requirement
  within the plan-run-observability spec).
- **Report command spec**: `openspec/specs/plan-run-observability/spec.md` —
  requirements under *Report command reads telemetry and state without
  mutation*, *Default output is human‑readable tables*, *JSON output is stable
  and complete*, and *Filters narrow displayed data without affecting plan
  summary*.
- **Dashboard command spec**: `openspec/specs/plan-run-observability/spec.md` —
  requirements under *Dashboard command reads telemetry and state without
  mutation*, *Dashboard output is a self‑contained static HTML file*, and
  *Dashboard contains seven required sections*.
- **Orchestrator usage**: `orchestrator/README.md` — subcommand reference and
  execution model.
- **Plan compile**: `orchestrator/README.md` — The compile stage section
  documents the `opsx-plan compile` workflow.
