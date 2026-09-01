#!/usr/bin/env bash
# Shared orchestrator runtime installer for opsx-controller adapters.
#
# Copies the client-neutral `opsx-plan`, `opsx-run`, and `opsx-watch-plan`
# executables and the required runtime packages to the target location:
#
#   global:   ~/.local/bin and ~/.local/lib/opsx-controller
#   project:  <project>/.opsx-controller/bin and <project>/.opsx-controller/lib
#
# The installed layout is identical regardless of which adapter installed it,
# and the runtime is fully self-contained: the installed executables resolve
# their runtime packages by location, never by importing from a repository
# checkout.
#
# Usage: bash scripts/install-orchestrator.sh <repo-root> [--global|--project <path>]
set -euo pipefail

install_orchestrator() {
  local repo_root="$1"
  local mode="${2:---global}"
  local dest_dir runtime_dir

  if [[ "$mode" == "--global" ]]; then
    dest_dir="$HOME/.local/bin"
    runtime_dir="$HOME/.local/lib/opsx-controller"
  else
    local project_dir="$3"
    if [[ ! -d "$project_dir" ]]; then
      printf 'Project directory does not exist: %s\n' "$project_dir" >&2
      exit 1
    fi
    dest_dir="$project_dir/.opsx-controller/bin"
    runtime_dir="$project_dir/.opsx-controller"
  fi

  mkdir -p "$dest_dir"
  rm -rf "$runtime_dir/lib" "$runtime_dir/samples"
  mkdir -p "$runtime_dir/lib" "$runtime_dir/samples"

  if [[ "$mode" == "--project" ]]; then
    # Project-scoped runtime is machine-local state: keep the installed lib,
    # bin, and samples out of the project's git history, and keep per-change
    # controller state files (.opsx-controller/<change>.json) ignored too.
    # The dsh adapter's tracked support files (.opsx-controller/dsh/) remain
    # untracked by this file.
    local gi="$runtime_dir/.gitignore"
    mkdir -p "$runtime_dir"
    if [[ ! -f "$gi" ]]; then
      printf '%s\n' \
        '# opsx-controller project runtime (installed by install.sh)' \
        'lib/' \
        'bin/' \
        'samples/' \
        '*.json' > "$gi"
    else
      local line
      for line in 'lib/' 'bin/' 'samples/' '*.json'; do
        if ! grep -Fxq "$line" "$gi"; then
          printf '%s\n' "$line" >> "$gi"
        fi
      done
    fi
  fi

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

if [[ $# -lt 1 || $# -gt 3 ]]; then
  printf 'Usage: bash scripts/install-orchestrator.sh <repo-root> [--global|--project <path>]\n' >&2
  exit 1
fi

REPO_ROOT="$1"
if [[ ! -d "$REPO_ROOT" ]]; then
  printf 'Repository root does not exist: %s\n' "$REPO_ROOT" >&2
  exit 1
fi

MODE="${2:---global}"
PROJECT_DIR="${3:-}"

if [[ "$MODE" == "--global" ]]; then
  install_orchestrator "$REPO_ROOT" "--global"
elif [[ "$MODE" == "--project" ]]; then
  [[ -n "$PROJECT_DIR" ]] || {
    printf 'Usage: bash scripts/install-orchestrator.sh <repo-root> --project <path>\n' >&2
    exit 1
  }
  install_orchestrator "$REPO_ROOT" "--project" "$PROJECT_DIR"
else
  printf 'Unknown mode: %s\n' "$MODE" >&2
  printf 'Usage: bash scripts/install-orchestrator.sh <repo-root> [--global|--project <path>]\n' >&2
  exit 1
fi
