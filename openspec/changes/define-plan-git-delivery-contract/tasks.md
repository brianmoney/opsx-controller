## 1. Capability And Config Contract

- [ ] 1.1 Add a new `plan-git-delivery` capability spec under `openspec/specs/` through this change's spec delta.
- [ ] 1.2 Define the `plan.git_delivery.enabled`, `plan.git_delivery.branch`, `plan.git_delivery.base_ref`, and `plan.git_delivery.create_pull_request` configuration keys and their default-off behavior.
- [ ] 1.3 Define validation behavior for plans that omit the `git_delivery` table or leave optional branch/base fields unset.

## 2. Durable State And Resume Contract

- [ ] 2.1 Define the persisted `git_delivery.base_ref`, `git_delivery.branch_name`, and `git_delivery.delivery_status` state fields.
- [ ] 2.2 Define the clean-tracked-tree precondition for first branch creation.
- [ ] 2.3 Define the fail-closed resume guard that requires `HEAD` to match the recorded branch before any stage dispatch.

## 3. Delivery Semantics

- [ ] 3.1 Define how archive-commit reachability verification interacts with a recorded delivery branch.
- [ ] 3.2 Define the completion condition under which pull-request creation becomes eligible.
- [ ] 3.3 Define that branching, pushing, and pull-request creation are orchestrator responsibilities and remain forbidden to implement, review, and archive workers.

## 4. Verification

- [ ] 4.1 Run `openspec validate define-plan-git-delivery-contract --strict`.
