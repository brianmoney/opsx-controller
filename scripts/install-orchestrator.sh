#!/usr/bin/env bash
# Shared orchestrator runtime installer for opsx-controller adapters.
#
# Copies the client-neutral `opsx-plan`, `opsx-run`, and `opsx-watch-plan`
# executables to ~/.local/bin and the required runtime packages to
# ~/.local/lib/opsx-controller/lib.  The installed layout is identical
# regardless of which adapter installed it.
#
# Usage: bash scripts/install-orchestrator.sh <repo-root>
set -euo pipefail

install_orchestrator() {
  local repo_root="$1"
  local dest_dir="$HOME/.local/bin"
  local runtime_dir="$HOME/.local/lib/opsx-controller"

  mkdir -p "$dest_dir"
  rm -rf "$runtime_dir/lib" "$runtime_dir/samples"
  mkdir -p "$runtime_dir/lib" "$runtime_dir/samples"

  cp -R "$repo_root/lib/metrics"      "$runtime_dir/lib/"
  cp -R "$repo_root/lib/pricing"      "$runtime_dir/lib/"
  cp -R "$repo_root/lib/models"       "$runtime_dir/lib/"
  cp -R "$repo_root/lib/orchestrator" "$runtime_dir/lib/"
  install -m 0644 \
    "$repo_root/orchestrator/samples/sample-plan.md" \
    "$repo_root/orchestrator/samples/sample-plan.toml" \
    "$runtime_dir/samples/"

  install -m 0644 \
    "$repo_root/core/plan-authoring.md" \
    "$runtime_dir/plan-authoring.md"

  install -m 0755 \
    "$repo_root/orchestrator/opsx-plan.py" \
    "$dest_dir/opsx-plan"
  install -m 0755 \
    "$repo_root/orchestrator/opsx-plan.py" \
    "$dest_dir/opsx-run"
  install -m 0755 \
    "$repo_root/scripts/opsx-watch-plan" \
    "$dest_dir/opsx-watch-plan"

  printf '%s\n' \
    "Installed opsx-plan runtime libraries to $runtime_dir" \
    "Installed opsx-plan samples to $runtime_dir/samples" \
    "Installed plan-authoring reference to $runtime_dir/plan-authoring.md" \
    "Installed opsx-plan to $dest_dir/opsx-plan" \
    "Installed opsx-run to $dest_dir/opsx-run" \
    "Installed opsx-watch-plan to $dest_dir/opsx-watch-plan"
}

if [[ $# -ne 1 ]]; then
  printf 'Usage: bash scripts/install-orchestrator.sh <repo-root>\n' >&2
  exit 1
fi

REPO_ROOT="$1"
if [[ ! -d "$REPO_ROOT" ]]; then
  printf 'Repository root does not exist: %s\n' "$REPO_ROOT" >&2
  exit 1
fi

install_orchestrator "$REPO_ROOT"
