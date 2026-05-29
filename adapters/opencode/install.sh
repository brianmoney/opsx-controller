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
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_support_readme() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$ROOT_DIR/adapters/opencode/support/opsx-controller-state-README.md" \
    "$dest_dir/README.md"
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
  local config_root="$HOME/.config/opencode"
  install_commands "$config_root/commands"
  install_agents "$config_root/agents"
  install_support_readme "$config_root/opsx-controller"
  printf '%s\n' \
    "Installed commands to $config_root/commands" \
    "Installed agents to $config_root/agents" \
    "Installed support files to $config_root/opsx-controller"
}

install_project() {
  local project_dir="$1"
  if [[ ! -d "$project_dir" ]]; then
    printf 'Project directory does not exist: %s\n' "$project_dir" >&2
    exit 1
  fi

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
