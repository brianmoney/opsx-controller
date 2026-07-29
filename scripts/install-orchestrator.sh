#!/usr/bin/env bash
# Shared orchestrator runtime installer for opsx-controller adapters.
#
# Copies the client-neutral `opsx-plan` and `opsx-run` executables to
# ~/.local/bin and the required runtime packages to
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
  rm -rf "$runtime_dir/lib"
  mkdir -p "$runtime_dir/lib"

  cp -R "$repo_root/lib/metrics"   "$runtime_dir/lib/"
  cp -R "$repo_root/lib/pricing"   "$runtime_dir/lib/"
  cp -R "$repo_root/lib/models"    "$runtime_dir/lib/"

  install -m 0755 \
    "$repo_root/orchestrator/opsx-plan.py" \
    "$dest_dir/opsx-plan"
  install -m 0755 \
    "$repo_root/orchestrator/opsx-plan.py" \
    "$dest_dir/opsx-run"

  printf '%s\n' \
    "Installed opsx-plan runtime libraries to $runtime_dir" \
    "Installed opsx-plan to $dest_dir/opsx-plan" \
    "Installed opsx-run to $dest_dir/opsx-run"
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
