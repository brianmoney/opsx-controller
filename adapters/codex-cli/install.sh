#!/usr/bin/env bash
set -euo pipefail

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
      shift
      ;;
  esac
done

install_skill() {
  local src_dir="$1"
  local dest_base="$2"
  local skill_dir="$dest_base/skills/opsx-drive"
  mkdir -p "$skill_dir"
  install -m 0644 "$src_dir/skills/opsx-drive/SKILL.md" "$skill_dir/SKILL.md"
  mkdir -p "$skill_dir/agents"
  install -m 0644 "$src_dir/skills/opsx-drive/agents/openai.yaml" "$skill_dir/agents/openai.yaml"
}

install_agents() {
  local src_dir="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  local file
  for file in "$src_dir/agents/"*.toml; do
    [[ -e "$file" ]] || continue
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
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
  local skills_root="$HOME/.agents"
  local agents_root="$HOME/.codex"
  install_skill "$SCRIPT_DIR" "$skills_root"
  install_agents "$SCRIPT_DIR" "$agents_root/agents"
  install_support_readme "$SCRIPT_DIR" "$agents_root/opsx-controller"
  printf '%s\n' \
    "Installed skill to $skills_root/skills/opsx-drive/" \
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

  local skills_root="$project_dir/.agents"
  local agents_root="$project_dir/.codex"
  install_skill "$SCRIPT_DIR" "$skills_root"
  install_agents "$SCRIPT_DIR" "$agents_root/agents"
  install_support_readme "$SCRIPT_DIR" "$agents_root/opsx-controller"
  ensure_project_gitignore "$project_dir"

  printf '%s\n' \
    "Installed skill to $skills_root/skills/opsx-drive/" \
    "Installed agents to $agents_root/agents/" \
    "Installed support files to $agents_root/opsx-controller/" \
    "Updated $agents_root/.gitignore"
  do_verify
}

install_plugin() {
  local plugin_dir="$SCRIPT_DIR/plugin"
  mkdir -p "$plugin_dir/skills/opsx-drive/agents"
  mkdir -p "$plugin_dir/agents"
  mkdir -p "$plugin_dir/.codex-plugin"

  install -m 0644 "$SCRIPT_DIR/skills/opsx-drive/SKILL.md" "$plugin_dir/skills/opsx-drive/SKILL.md"
  install -m 0644 "$SCRIPT_DIR/skills/opsx-drive/agents/openai.yaml" "$plugin_dir/skills/opsx-drive/agents/openai.yaml"
  local file
  for file in "$SCRIPT_DIR/agents/"*.toml; do
    [[ -e "$file" ]] || continue
    install -m 0644 "$file" "$plugin_dir/agents/$(basename "$file")"
  done

  printf '%s\n' \
    "Plugin bundle created at $plugin_dir" \
    "  $plugin_dir/.codex-plugin/plugin.json" \
    "  $plugin_dir/skills/opsx-drive/" \
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
