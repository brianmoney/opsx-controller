#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf '%s\n' \
    'This script must be run with bash, not sourced.' \
    'Usage: bash install.sh --global' >&2
  return 1 2>/dev/null || exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/install-common.sh
source "$SCRIPT_DIR/../../lib/install-common.sh"

usage() {
  printf '%s\n' \
    'Usage:' \
    '  bash install.sh --global' \
    '  bash install.sh --project <path>' \
    '  bash install.sh --plugin' \
    '  bash install.sh --global --verify' \
    '  bash install.sh --project <path> --verify'
}

ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERIFY=false

# Parse optional flags
for arg in "$@"; do
  case "$arg" in
    --verify)
      VERIFY=true
      ;;
  esac
done

install_skill() {
  local src_dir="$1"
  local dest_base="$2"
  # The opsx-drive skill has been removed; this function is retained as a
  # no-op so global/project install paths do not fail. Supported skills are
  # installed by install_plugin when --plugin is used.
}

install_agents() {
  local src_dir="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  local file
  for file in "$src_dir/agents/"*.toml; do
    [[ -e "$file" ]] || continue
    install_agent "$file" "$dest_dir/$(basename "$file")"
  done
}

install_support_readme() {
  local src_dir="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$src_dir/support/opsx-controller-state-README.md" \
    "$dest_dir/README.md"
}

ensure_project_gitignore() {
  local gitignore_path="$1/.codex/.gitignore"
  local ignore_line='opsx-controller/*.json'

  mkdir -p "$1/.codex"
  if [[ -f "$gitignore_path" ]]; then
    if ! grep -Fxq "$ignore_line" "$gitignore_path"; then
      printf '\n%s\n' "$ignore_line" >> "$gitignore_path"
    fi
  else
    printf '%s\n' "$ignore_line" > "$gitignore_path"
  fi
}

do_verify() {
  if ! $VERIFY; then
    return 0
  fi
  if verify_command_available codex; then
    printf '%s\n' "codex CLI detected. Restart Codex CLI to reload skills and agents."
  else
    print_verify_notice codex
  fi
}

install_global() {
  load_model_env codex-cli

  local skills_root="$HOME/.agents"
  local agents_root="$HOME/.codex"
  # Remove stale opsx-drive skill directory before installing
  rm -rf "$skills_root/skills/opsx-drive"
  install_skill "$SCRIPT_DIR" "$skills_root"
  install_agents "$SCRIPT_DIR" "$agents_root/agents"
  install_support_readme "$SCRIPT_DIR" "$agents_root/opsx-controller"
  bash "$ROOT_DIR/scripts/install-orchestrator.sh" "$ROOT_DIR"

  printf '%s\n' \
    "Installed agents to $agents_root/agents/" \
    "Installed support files to $agents_root/opsx-controller/"
  do_verify
}

install_project() {
  local project_dir="$1"
  if [[ ! -d "$project_dir" ]]; then
    printf 'Project directory does not exist: %s\n' "$project_dir" >&2
    exit 1
  fi

  load_model_env codex-cli

  local skills_root="$project_dir/.agents"
  local agents_root="$project_dir/.codex"
  # Remove stale opsx-drive skill directory before installing
  rm -rf "$skills_root/skills/opsx-drive"
  install_skill "$SCRIPT_DIR" "$skills_root"
  install_agents "$SCRIPT_DIR" "$agents_root/agents"
  install_support_readme "$SCRIPT_DIR" "$agents_root/opsx-controller"
  ensure_project_gitignore "$project_dir"

  printf '%s\n' \
    "Installed agents to $agents_root/agents/" \
    "Installed support files to $agents_root/opsx-controller/" \
    "Updated $project_dir/.codex/.gitignore"
  do_verify
}

install_plugin() {
  local plugin_dir="$SCRIPT_DIR/plugin"
  # Remove stale opsx-drive before regenerating the bundle
  rm -rf "$plugin_dir/skills/opsx-drive"
  mkdir -p "$plugin_dir/agents"
  mkdir -p "$plugin_dir/.codex-plugin"

  local file
  for file in "$SCRIPT_DIR/agents/"*.toml; do
    [[ -e "$file" ]] || continue
    install -m 0644 "$file" "$plugin_dir/agents/$(basename "$file")"
  done

  printf '%s\n' \
    "Plugin bundle created at $plugin_dir" \
    "  $plugin_dir/.codex-plugin/plugin.json" \
    "  $plugin_dir/agents/"
}

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

case "$1" in
  --global)
    if [[ $# -lt 1 || $# -gt 2 ]]; then
      usage
      exit 1
    fi
    install_global
    ;;
  --project)
    if [[ $# -lt 2 || $# -gt 3 ]]; then
      usage
      exit 1
    fi
    install_project "$2"
    ;;
  --plugin)
    if [[ $# -ne 1 ]]; then
      usage
      exit 1
    fi
    install_plugin
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac

printf '%s\n' 'Restart Codex CLI after install so it reloads skills and agents.'
