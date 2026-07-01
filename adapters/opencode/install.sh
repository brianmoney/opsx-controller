#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage:' \
    '  bash adapters/opencode/install.sh --global' \
    '  bash adapters/opencode/install.sh --project /path/to/project'
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

install_commands() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  local file
  for file in "$ROOT_DIR"/adapters/opencode/commands/*.md; do
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_agents() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"

  local file
  for file in "$ROOT_DIR"/adapters/opencode/agents/*.md; do
    install_agent "$file" "$dest_dir/$(basename "$file")"
  done
}

require_model_envs() {
  require_model_env OPSX_CONTROLLER_MODEL
  require_model_env OPSX_IMPLEMENTER_MODEL
  require_model_env OPSX_REVIEWER_MODEL
  require_model_env OPSX_ARCHIVER_MODEL
}

require_model_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf 'Required model environment variable is not set: %s\n' "$name" >&2
    printf 'Source your opsx-controller .env before installing the OpenCode adapter.\n' >&2
    exit 1
  fi
}

install_agent() {
  local src="$1"
  local dest="$2"
  local tmp
  tmp="$(mktemp)"

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line//\{env:OPSX_CONTROLLER_MODEL\}/${OPSX_CONTROLLER_MODEL}}"
    line="${line//\{env:OPSX_IMPLEMENTER_MODEL\}/${OPSX_IMPLEMENTER_MODEL}}"
    line="${line//\{env:OPSX_REVIEWER_MODEL\}/${OPSX_REVIEWER_MODEL}}"
    line="${line//\{env:OPSX_ARCHIVER_MODEL\}/${OPSX_ARCHIVER_MODEL}}"
    printf '%s\n' "$line"
  done <"$src" >"$tmp"

  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
}

install_support_readme() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$ROOT_DIR/adapters/opencode/support/opsx-controller-state-README.md" \
    "$dest_dir/README.md"
}

install_orchestrator() {
  local dest_dir="$HOME/.local/bin"
  mkdir -p "$dest_dir"
  install -m 0755 \
    "$ROOT_DIR/orchestrator/opsx-plan.py" \
    "$dest_dir/opsx-plan"
}

ensure_project_gitignore() {
  local gitignore_path="$1/.opencode/.gitignore"
  local ignore_line='opsx-controller/*.json'

  mkdir -p "$1/.opencode"
  if [[ -f "$gitignore_path" ]]; then
    if ! grep -Fxq "$ignore_line" "$gitignore_path"; then
      printf '\n%s\n' "$ignore_line" >> "$gitignore_path"
    fi
  else
    printf '%s\n' "$ignore_line" > "$gitignore_path"
  fi
}

ensure_project_config() {
  local project_dir="$1"
  if [[ -f "$project_dir/opencode.json" || -f "$project_dir/opencode.jsonc" || -f "$project_dir/.opencode/opencode.json" ]]; then
    printf '%s\n' \
      'Existing OpenCode config detected.' \
      'Merge adapters/opencode/templates/project/opencode.json.snippet.json manually if needed.'
    return
  fi

  mkdir -p "$project_dir/.opencode"
  install -m 0644 \
    "$ROOT_DIR/adapters/opencode/templates/project/opencode.json.snippet.json" \
    "$project_dir/.opencode/opencode.json"
}

install_global() {
  require_model_envs

  local config_root="$HOME/.config/opencode"
  install_commands "$config_root/commands"
  install_agents "$config_root/agents"
  install_support_readme "$config_root/opsx-controller"
  install_orchestrator
  printf '%s\n' \
    "Installed commands to $config_root/commands" \
    "Installed agents to $config_root/agents" \
    "Installed support files to $config_root/opsx-controller" \
    "Installed opsx-plan to $HOME/.local/bin/opsx-plan"
}

install_project() {
  local project_dir="$1"
  if [[ ! -d "$project_dir" ]]; then
    printf 'Project directory does not exist: %s\n' "$project_dir" >&2
    exit 1
  fi

  require_model_envs

  install_commands "$project_dir/.opencode/commands"
  install_agents "$project_dir/.opencode/agents"
  install_support_readme "$project_dir/.opencode/opsx-controller"
  ensure_project_gitignore "$project_dir"
  ensure_project_config "$project_dir"

  printf '%s\n' \
    "Installed commands to $project_dir/.opencode/commands" \
    "Installed agents to $project_dir/.opencode/agents" \
    "Installed support files to $project_dir/.opencode/opsx-controller" \
    "Updated $project_dir/.opencode/.gitignore"
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

printf '%s\n' 'Restart OpenCode after install so it reloads commands and agents.'
