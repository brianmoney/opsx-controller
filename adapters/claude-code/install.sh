#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/install-common.sh
source "$SCRIPT_DIR/../../lib/install-common.sh"

usage() {
  printf '%s\n' \
    'Usage:' \
    '  bash adapters/claude-code/install.sh --global' \
    '  bash adapters/claude-code/install.sh --project /path/to/project' \
    '  bash adapters/claude-code/install.sh --global --verify' \
    '  bash adapters/claude-code/install.sh --project /path/to/project --verify'
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

install_skills() {
  local dest_root="$1"
  mkdir -p "$dest_root"
  local skill_dir skill_name
  for skill_dir in "$ROOT_DIR"/adapters/claude-code/skills/*; do
    [[ -d "$skill_dir" ]] || continue
    skill_name="$(basename "$skill_dir")"
    mkdir -p "$dest_root/$skill_name"
    cp -R "$skill_dir"/. "$dest_root/$skill_name/"
  done
}

install_agents() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  local file
  for file in "$ROOT_DIR"/adapters/claude-code/agents/*.md; do
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_support_readme() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$ROOT_DIR/adapters/claude-code/support/opsx-controller-state-README.md" \
    "$dest_dir/README.md"
}

ensure_project_gitignore() {
  local gitignore_path="$1/.claude/.gitignore"
  local ignore_line='opsx-controller/*.json'

  mkdir -p "$1/.claude"
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
  if verify_command_available claude; then
    printf '%s\n' "claude CLI detected. Restart Claude Code to reload skills and agents."
  else
    print_verify_notice claude
  fi
}

install_global() {
  local config_root="$HOME/.claude"
  install_skills "$config_root/skills"
  install_agents "$config_root/agents"
  install_support_readme "$config_root/opsx-controller"
  printf '%s\n' \
    "Installed skills to $config_root/skills" \
    "Installed agents to $config_root/agents" \
    "Installed support files to $config_root/opsx-controller"
  do_verify
}

install_project() {
  local project_dir="$1"
  if [[ ! -d "$project_dir" ]]; then
    printf 'Project directory does not exist: %s\n' "$project_dir" >&2
    exit 1
  fi

  install_skills "$project_dir/.claude/skills"
  install_agents "$project_dir/.claude/agents"
  install_support_readme "$project_dir/.claude/opsx-controller"
  ensure_project_gitignore "$project_dir"

  printf '%s\n' \
    "Installed skills to $project_dir/.claude/skills" \
    "Installed agents to $project_dir/.claude/agents" \
    "Installed support files to $project_dir/.claude/opsx-controller" \
    "Updated $project_dir/.claude/.gitignore"
  do_verify
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

printf '%s\n' 'Restart Claude Code after install so new agents are loaded reliably.'
