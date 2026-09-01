#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf '%s\n' \
    'This script must be run with bash, not sourced.' \
    'Usage: bash adapters/dsh/install.sh --global' >&2
  return 1 2>/dev/null || exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/install-common.sh
source "$SCRIPT_DIR/../../lib/install-common.sh"

usage() {
  printf '%s\n' \
    'Usage:' \
    '  bash adapters/dsh/install.sh --global' \
    '  bash adapters/dsh/install.sh --project /path/to/project' \
    '  bash adapters/dsh/install.sh --global --verify' \
    '  bash adapters/dsh/install.sh --project /path/to/project --verify'
}

ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERIFY=false

# Parse optional flags
declare -a _verify_filtered=()
for arg in "$@"; do
  case "$arg" in
    --verify)
      VERIFY=true
      ;;
    *)
      _verify_filtered+=("$arg")
      ;;
  esac
done
set -- "${_verify_filtered[@]}"

install_shim() {
  local dest_bin="$1"
  mkdir -p "$dest_bin"
  install -m 0755 \
    "$SCRIPT_DIR/bin/opsx-dsh-worker" \
    "$dest_bin/opsx-dsh-worker"
}

install_role_files() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  local file
  for file in "$SCRIPT_DIR"/agents/*.md; do
    [[ -e "$file" ]] || continue
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_support_readme() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$SCRIPT_DIR/support/opsx-controller-state-README.md" \
    "$dest_dir/README.md"
}

install_plan_authoring_reference() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$ROOT_DIR/core/plan-authoring.md" \
    "$dest_dir/plan-authoring.md"
}

verify_plan_authoring_reference() {
  local support_dir="$1"
  local ref="$support_dir/plan-authoring.md"
  if [[ -f "$ref" ]]; then
    if cmp -s "$ROOT_DIR/core/plan-authoring.md" "$ref"; then
      printf '%s\n' "Verify: plan-authoring reference deployed and matches source at $ref"
      return 0
    else
      printf '%s\n' "Verify: plan-authoring reference at $ref differs from $ROOT_DIR/core/plan-authoring.md" >&2
      return 1
    fi
  else
    printf '%s\n' "Verify: plan-authoring reference MISSING from $ref" >&2
    return 1
  fi
}

verify_shim_deployed() {
  local shim_path="$1"
  if [[ -f "$shim_path" && -x "$shim_path" ]]; then
    printf '%s\n' "Verify: dsh worker shim deployed at $shim_path"
    return 0
  else
    printf '%s\n' "Verify: dsh worker shim MISSING from $shim_path" >&2
    return 1
  fi
}

# Warn (never fail) when the host cannot run dsh: Node without TypeScript
# type-stripping silently breaks every dsh tool call, and the pinned npx
# fallback needs npx present. The warning names the exact checks so an
# operator knows what to verify.
warn_node_typescript() {
  if command -v node >/dev/null 2>&1; then
    if ! node -e 'process.exit(process.features && process.features.typescript ? 0 : 1)' 2>/dev/null; then
      printf '%s\n' \
        'Warning: Node.js is present but process.features.typescript is falsy.' \
        'dsh tool calls will fail with this Node build; install a Node build' \
        'with TypeScript type-stripping enabled.' >&2
    fi
  fi
  if ! command -v dsh >/dev/null 2>&1 && ! command -v npx >/dev/null 2>&1; then
    printf '%s\n' \
      'Warning: neither dsh nor npx is detectable on PATH; dsh will be run via' \
      'the pinned npx fallback (npx --yes @deepseek-ai/dsh@0.1.0-rc.7), which' \
      'requires npx. Install dsh or npx, or set DSH_BINARY at dispatch time.' >&2
  fi
}

do_verify() {
  if ! $VERIFY; then
    return 0
  fi
  if verify_command_available dsh; then
    printf '%s\n' "dsh CLI detected on PATH."
  else
    print_verify_notice dsh
  fi
}

install_global() {
  load_model_env dsh

  local config_root="$HOME/.config/opsx-controller/dsh"
  install_shim "$HOME/.local/bin"
  install_role_files "$config_root/agents"
  install_support_readme "$config_root"
  install_plan_authoring_reference "$config_root"
  bash "$ROOT_DIR/scripts/install-orchestrator.sh" "$ROOT_DIR" --global
  warn_node_typescript

  printf '%s\n' \
    "Installed dsh worker shim to $HOME/.local/bin/opsx-dsh-worker" \
    "Installed role instruction files to $config_root/agents" \
    "Installed support files to $config_root" \
    "Installed plan-authoring reference to $config_root/plan-authoring.md" \
    "Installed opsx-plan runtime libraries to $HOME/.local/lib/opsx-controller" \
    "Installed opsx-plan to $HOME/.local/bin/opsx-plan" \
    "Installed opsx-run to $HOME/.local/bin/opsx-run" \
    "Installed opsx-watch-plan to $HOME/.local/bin/opsx-watch-plan"
  do_verify
  verify_shim_deployed "$HOME/.local/bin/opsx-dsh-worker"
  verify_plan_authoring_reference "$config_root"
}

install_project() {
  local project_dir="$1"
  if [[ ! -d "$project_dir" ]]; then
    printf 'Project directory does not exist: %s\n' "$project_dir" >&2
    exit 1
  fi

  load_model_env dsh

  local support_dir="$project_dir/.opsx-controller/dsh"
  install_role_files "$support_dir/agents"
  install_support_readme "$support_dir"
  install_plan_authoring_reference "$support_dir"
  bash "$ROOT_DIR/scripts/install-orchestrator.sh" "$ROOT_DIR" --project "$project_dir"
  warn_node_typescript

  printf '%s\n' \
    "Installed role instruction files to $support_dir/agents" \
    "Installed support files to $support_dir" \
    "Installed plan-authoring reference to $support_dir/plan-authoring.md" \
    "Installed opsx-plan runtime libraries to $project_dir/.opsx-controller/lib" \
    "Installed opsx-plan to $project_dir/.opsx-controller/bin/opsx-plan" \
    "Installed opsx-run to $project_dir/.opsx-controller/bin/opsx-run" \
    "Installed opsx-watch-plan to $project_dir/.opsx-controller/bin/opsx-watch-plan"
  do_verify
  verify_plan_authoring_reference "$support_dir"
}

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

case "$1" in
  --global)
    if [[ $# -ne 1 ]]; then
      usage
      exit 1
    fi
    install_global
    ;;
  --project)
    if [[ $# -ne 2 ]]; then
      usage
      exit 1
    fi
    install_project "$2"
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac
