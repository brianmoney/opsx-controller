#!/usr/bin/env bash
set -euo pipefail

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf '%s\n' \
    'This script must be run with bash, not sourced.' \
    'Usage: bash adapters/opencode/install.sh --global' >&2
  return 1 2>/dev/null || exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/install-common.sh
source "$SCRIPT_DIR/../../lib/install-common.sh"

usage() {
  printf '%s\n' \
    'Usage:' \
    '  bash adapters/opencode/install.sh --global' \
    '  bash adapters/opencode/install.sh --project /path/to/project' \
    '  bash adapters/opencode/install.sh --global --verify' \
    '  bash adapters/opencode/install.sh --project /path/to/project --verify'
}

ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Parse optional flags
VERIFY=false
args=()
for arg in "$@"; do
  case "$arg" in
    --verify)
      VERIFY=true
      ;;
    *)
      args+=("$arg")
      ;;
  esac
done
set -- "${args[@]}"

install_skills() {
  local dest_root="$1"
  mkdir -p "$dest_root"
  local skill_dir skill_name
  for skill_dir in "$ROOT_DIR/skills"/*; do
    [[ -d "$skill_dir" ]] || continue
    skill_name="$(basename "$skill_dir")"
    mkdir -p "$dest_root/$skill_name"
    cp -R "$skill_dir"/. "$dest_root/$skill_name/"
  done
}

install_commands() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  # Remove stale legacy command files from previous installations
  local legacy cmd
  for legacy in opsx-author.md opsx-archive-no-prompt.md opsx-verify-auto.md opsx-review.md opsx-drive.md; do
    rm -f "$dest_dir/$legacy"
  done
  local file
  for file in "$ROOT_DIR"/adapters/opencode/commands/*.md; do
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_agents() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  # Remove stale nested controller agent from previous installations
  rm -f "$dest_dir/opsx-controller.md"

  local file
  for file in "$ROOT_DIR"/adapters/opencode/agents/*.md; do
    install_agent "$file" "$dest_dir/$(basename "$file")"
  done
}

install_support_readme() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$ROOT_DIR/adapters/opencode/support/opsx-controller-state-README.md" \
    "$dest_dir/README.md"
}

install_plan_authoring_reference() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  install -m 0644 \
    "$ROOT_DIR/core/plan-authoring.md" \
    "$dest_dir/plan-authoring.md"
}

install_plugins() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  local module_manifest="$ROOT_DIR/adapters/opencode/plugins/package.json"
  if [[ -f "$module_manifest" ]]; then
    install -m 0644 "$module_manifest" "$dest_dir/package.json"
  fi
  local file
  for file in "$ROOT_DIR"/adapters/opencode/plugins/*.js; do
    [[ -e "$file" ]] || continue
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_orchestrator() {
  bash "$ROOT_DIR/scripts/install-orchestrator.sh" "$ROOT_DIR"
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

do_verify() {
  if ! $VERIFY; then
    return 0
  fi
  if verify_command_available opencode; then
    printf '%s\n' "opencode CLI detected. Restart OpenCode to reload commands and agents."
  else
    print_verify_notice opencode
  fi
}

verify_plugin_deployed() {
  local plugins_dir="$1"
  local plugin_name="opsx-usage-emitter.js"
  if [[ -f "$plugins_dir/$plugin_name" && -f "$plugins_dir/package.json" ]]; then
    printf '%s\n' "Verify: usage emitter plugin deployed at $plugins_dir/$plugin_name"
    return 0
  else
    printf '%s\n' "Verify: usage emitter plugin MISSING from $plugins_dir/$plugin_name" >&2
    return 1
  fi
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

install_global() {
  load_model_env opencode

  local config_root="$HOME/.config/opencode"
  install_skills "$config_root/skills"
  install_commands "$config_root/commands"
  install_agents "$config_root/agents"
  install_plugins "$config_root/plugins"
  install_support_readme "$config_root/opsx-controller"
  install_plan_authoring_reference "$config_root/opsx-controller"
  install_orchestrator
  printf '%s\n' \
    "Installed skills to $config_root/skills" \
    "Installed commands to $config_root/commands" \
    "Installed agents to $config_root/agents" \
    "Installed plugins to $config_root/plugins" \
    "Installed support files to $config_root/opsx-controller" \
    "Installed plan-authoring reference to $config_root/opsx-controller/plan-authoring.md" \
    "Installed opsx-plan runtime libraries to $HOME/.local/lib/opsx-controller" \
    "Installed opsx-plan to $HOME/.local/bin/opsx-plan" \
    "Installed opsx-run to $HOME/.local/bin/opsx-run" \
    "Installed opsx-watch-plan to $HOME/.local/bin/opsx-watch-plan"
  do_verify
  verify_plugin_deployed "$config_root/plugins"
  verify_plan_authoring_reference "$config_root/opsx-controller"
}

install_project() {
  local project_dir="$1"
  if [[ ! -d "$project_dir" ]]; then
    printf 'Project directory does not exist: %s\n' "$project_dir" >&2
    exit 1
  fi

  load_model_env opencode

  install_skills "$project_dir/.opencode/skills"
  install_commands "$project_dir/.opencode/commands"
  install_agents "$project_dir/.opencode/agents"
  install_plugins "$project_dir/.opencode/plugins"
  install_support_readme "$project_dir/.opencode/opsx-controller"
  install_plan_authoring_reference "$project_dir/.opencode/opsx-controller"
  ensure_project_gitignore "$project_dir"
  ensure_project_config "$project_dir"

  printf '%s\n' \
    "Installed skills to $project_dir/.opencode/skills" \
    "Installed commands to $project_dir/.opencode/commands" \
    "Installed agents to $project_dir/.opencode/agents" \
    "Installed plugins to $project_dir/.opencode/plugins" \
    "Installed support files to $project_dir/.opencode/opsx-controller" \
    "Installed plan-authoring reference to $project_dir/.opencode/opsx-controller/plan-authoring.md" \
    "Updated $project_dir/.opencode/.gitignore"
  do_verify
  verify_plugin_deployed "$project_dir/.opencode/plugins"
  verify_plan_authoring_reference "$project_dir/.opencode/opsx-controller"
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
  -h|--help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac

printf '%s\n' 'Restart OpenCode after install so it reloads commands, agents, and plugins.'
