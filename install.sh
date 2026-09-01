#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf '%s\n' \
    'This script must be run with bash, not sourced.' \
    'Usage: bash install.sh --global' >&2
  return 1 2>/dev/null || exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/install-common.sh
source "$SCRIPT_DIR/lib/install-common.sh"

# Fixed, deterministic adapter install order (matches the design).
OPSX_ADAPTERS=(opencode claude-code codex-cli dsh)

usage() {
  printf '%s\n' \
    'Usage:' \
    '  bash install.sh --global' \
    '  bash install.sh --global --verify' \
    '  bash install.sh --global --only opencode,claude-code' \
    '  bash install.sh --project /path/to/project' \
    '  bash install.sh --project /path/to/project --verify' \
    '  bash install.sh --project /path/to/project --only codex-cli' \
    '  bash install.sh --help' \
    '' \
    'Installs every supported adapter (opencode, claude-code, codex-cli, dsh)' \
    'and the shared opsx-plan orchestrator in one invocation. Adapters are' \
    'installed through their own installers, which remain directly runnable.' \
    '' \
    'Flags:' \
    '  --global              Install globally (adapter config under ~/.config,' \
    '                        orchestrator to ~/.local/bin and ~/.local/lib).' \
    '  --project <path>      Install into a single project directory; the shared' \
    '                        orchestrator is installed self-contained under' \
    '                        <path>/.opsx-controller.' \
    '  --verify              Run each adapter installer with its verification' \
    '                        path and report any missing artifacts.' \
    '  --only <adapters>     Comma-separated subset of adapters to install.' \
    '                        Defaults to all adapters when omitted.' \
    '                        Valid adapters: opencode, claude-code, codex-cli, dsh'
}

ROOT_DIR="$SCRIPT_DIR"

MODE=""
PROJECT_DIR=""
VERIFY=false
ONLY=()

# Parse flags. --verify and --only may appear in any order; the mode
# (--global / --project) must be present exactly once.
while (($#)); do
  case "$1" in
    --global)
      [[ -z "$MODE" ]] || { usage >&2; exit 1; }
      MODE="--global"
      ;;
    --project)
      [[ -z "$MODE" ]] || { usage >&2; exit 1; }
      [[ $# -ge 2 ]] || { usage >&2; exit 1; }
      MODE="--project"
      PROJECT_DIR="$2"
      shift
      ;;
    --verify)
      VERIFY=true
      ;;
    --only)
      [[ $# -ge 2 ]] || { usage >&2; exit 1; }
      ONLY+=("$2")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
  shift
done

if [[ -z "$MODE" ]]; then
  usage >&2
  exit 1
fi

# --only must not be combined with other selector-bearing flags; a bare
# `--global --only` without a value is caught above.
if [[ "${#ONLY[@]}" -gt 1 ]]; then
  printf '%s\n' 'Error: --only accepts a single comma-separated value.' >&2
  usage >&2
  exit 1
fi

# Resolve the selected adapters. Default to every adapter when --only is
# omitted. Reject unknown adapter names with a usage error listing the valid
# names so a typo never silently installs nothing.
if [[ "${#ONLY[@]}" -eq 0 ]]; then
  SELECTED=("${OPSX_ADAPTERS[@]}")
else
  IFS=',' read -r -a SELECTED <<< "${ONLY[0]}"
  local_adapter_valid=1
  for name in "${SELECTED[@]}"; do
    local_adapter_valid=0
    for known in "${OPSX_ADAPTERS[@]}"; do
      if [[ "$name" == "$known" ]]; then
        local_adapter_valid=1
        break
      fi
    done
    if [[ "$local_adapter_valid" -ne 1 ]]; then
      printf 'Error: unknown adapter: %s\n' "$name" >&2
      printf 'Valid adapters: %s\n' "${OPSX_ADAPTERS[*]}" >&2
      usage >&2
      exit 1
    fi
  done
fi

MODE_ARGS=()
if [[ "$MODE" == "--global" ]]; then
  MODE_ARGS=(--global)
else
  MODE_ARGS=(--project "$PROJECT_DIR")
fi
if $VERIFY; then
  MODE_ARGS+=(--verify)
fi

# Run each selected adapter in the fixed order, tracking per-adapter success
# or failure so a partial failure reports exactly which adapters completed and
# which failed. `set -e` aborts on the first failing adapter; the reporting
# below runs in an explicit trap so the partial state is still visible.
FAILED_ADAPTERS=()
COMPLETED_ADAPTERS=()
run_adapter() {
  local adapter="$1"
  if bash "$ROOT_DIR/adapters/$adapter/install.sh" "${MODE_ARGS[@]}"; then
    COMPLETED_ADAPTERS+=("$adapter")
    printf '%s\n' "[install] $adapter: OK"
  else
    FAILED_ADAPTERS+=("$adapter")
    printf '%s\n' "[install] $adapter: FAILED" >&2
    return 1
  fi
}

report_partial() {
  if [[ "${#FAILED_ADAPTERS[@]}" -gt 0 ]]; then
    printf '%s\n' '' \
      'Universal install finished with errors.' \
      "Completed adapters: ${COMPLETED_ADAPTERS[*]:-none}" \
      "Failed adapters:    ${FAILED_ADAPTERS[*]}" >&2
    printf '%s\n' \
      'Rerun with --only to retry just the failed adapter(s).' >&2
  fi
}
trap 'report_partial' EXIT

for adapter in "${SELECTED[@]}"; do
  run_adapter "$adapter"
done
