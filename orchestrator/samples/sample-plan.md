# Sample Implementation Plan

A realistic phased implementation plan whose structure exercises phases,
dependency edges, and a gate.

## Phase 1 — Foundation

### `add-unit-tests`

Add unit test coverage for the core payment calculation module. This change
establishes the test infrastructure later changes build on.

**Depends on:** none (independent)

### `add-input-validation` (proposed shared-validation capability)

Introduce a shared validation layer for API request payloads, used by every
handler that touches user input. Gate this one — it's the first change in a
new capability that later work inherits.

**Depends on:** none

## Phase 2 — Core Logic

### `fix-tax-calculation`

Correct the rounding error in the tax engine that causes off-by-one-cent
discrepancies in multi-jurisdiction orders. Depends on `add-unit-tests`
because the existing test harness validates the fix.

**Depends on:** `add-unit-tests`

### `add-discount-code-verification`

Add verification logic that validates discount codes against the active
promotions registry before applying them to an order. Independent of the
tax fix and the validation layer — it ships its own self-contained checks.

**Depends on:** none (independent; may proceed in parallel with tax fix)

## Phase 3 — Integration

### `integrate-payment-gateway-v2`

Swap in the v2 payment gateway client, replacing the deprecated v1 client.
This is the first change that exercises both the validation layer and the
discount verification together. Pause here for human review: this change
talks to a real payment processor.

**Depends on:** `add-input-validation`, `add-discount-code-verification`

## Deferred

### `add-subscription-renewal-scheduling` (deferred)

Deferred until the billing team ships the webhook contract. Nothing depends
on it, so it stays out of the enabled set entirely.
