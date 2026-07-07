## 1. Cost Estimation Contract

- [x] 1.1 Define a small helper or function that accepts normalized telemetry `usage`, normalized telemetry `model`, and a resolved pricing entry and returns either an estimated cost payload or an unresolved reason.
- [x] 1.2 Define the persisted `price_snapshot` shape for both `per_token` and `subscription` billing modes, including subscription denominator fields.
- [x] 1.3 Define the set of unresolved reasons surfaced through `cost.unresolved_reason`.

## 2. Per-Token Estimation

- [x] 2.1 Calculate estimated cost from per-million token rates using `input_tokens`, `output_tokens`, `cached_input_tokens`, and `reasoning_tokens` when those fields are present.
- [x] 2.2 Treat `null` token counts as unavailable, `0` token counts as known zero, and positive usage with a missing applicable rate as unresolved.
- [x] 2.3 Copy the resolved catalog fields and catalog version into `cost.price_snapshot` and `cost.pricing_catalog_version`.

## 3. Subscription Estimation

- [x] 3.1 Add a minimal configuration path for subscription usage denominators.
- [x] 3.2 Compute effective amortized stage cost from `subscription_price` and the configured denominator when the telemetry record has usable stage usage units.
- [x] 3.3 Mark subscription estimates unresolved when denominator configuration is missing or invalid.

## 4. Orchestrator Integration

- [x] 4.1 Integrate cost estimation into direct-stage telemetry record construction without changing stage success or failure semantics.
- [x] 4.2 Ensure completed direct-stage records written by the updated path end with `cost.status` equal to `"estimated"` or `"unresolved"`, not the legacy default `"unavailable"`.
- [x] 4.3 Preserve the legacy default `"unavailable"` behavior for historical records or any writer path that does not attempt estimation.

## 5. Unit Tests

- [x] 5.1 Test a known per-token model with input and output usage produces `cost.status="estimated"`, correct `estimated_cost`, and a populated per-token `price_snapshot`.
- [x] 5.2 Test cached input tokens contribute to the estimate when `cached_input_price_per_mtok` is present.
- [x] 5.3 Test usage unavailable produces `cost.status="unresolved"` with a missing-usage reason and `estimated_cost = null`.
- [x] 5.4 Test missing model identity produces `cost.status="unresolved"`.
- [x] 5.5 Test unknown pricing produces `cost.status="unresolved"` with an unknown-provider or unknown-model reason.
- [x] 5.6 Test a token category with positive usage but no applicable rate produces `cost.status="unresolved"`.
- [x] 5.7 Test a subscription-priced model with valid denominator configuration produces `cost.status="estimated"` and a subscription `price_snapshot` that includes denominator fields.
- [x] 5.8 Test a subscription-priced model without denominator configuration produces `cost.status="unresolved"`.

## 6. Verification

- [x] 6.1 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 6.2 Run `openspec validate estimate-stage-token-costs --strict`.
- [x] 6.3 Run `bash adapters/opencode/install.sh --global --verify` after implementation because this change updates orchestrator runtime behavior.
