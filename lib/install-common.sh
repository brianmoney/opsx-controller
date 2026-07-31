#!/usr/bin/env bash
# Common installer utilities for opsx-controller adapters.
# Source this file from adapter install.sh scripts.
set -euo pipefail

_OPSX_install_common_sourced=1

OPSX_CONTROLLER_ROOT="${OPSX_CONTROLLER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ---------------------------------------------------------------------------
# Model environment helpers
# ---------------------------------------------------------------------------

# Resolve models for <adapter> through the resolver (reached via the
# controller source tree, not PATH, so installing one adapter never depends
# on another adapter's orchestrator install) and export OPSX_*_MODEL into
# the current shell. When no configuration exists, auto-init from ambient
# environment variables and retry. Exits non-zero with actionable guidance
# only when a config exists but leaves roles unresolved, so no artifact is
# ever installed with an empty model value.
load_model_env() {
  local adapter="$1"
  local output
  local config_path="$HOME/.config/opsx-controller/models.toml"

  if output="$(python3 "$OPSX_CONTROLLER_ROOT/orchestrator/opsx-plan.py" models env --adapter "$adapter" 2>&1)"; then
    eval "$output"
    return 0
  fi

  if [[ ! -f "$config_path" ]]; then
    printf 'No model configuration found. Seeding %s from environment…\n' "$config_path" >&2
    python3 "$OPSX_CONTROLLER_ROOT/orchestrator/opsx-plan.py" models init >&2 || true
    if output="$(python3 "$OPSX_CONTROLLER_ROOT/orchestrator/opsx-plan.py" models env --adapter "$adapter" 2>&1)"; then
      eval "$output"
      return 0
    fi
  fi

  printf 'Could not resolve model configuration for adapter: %s\n' "$adapter" >&2
  printf '%s\n' "$output" >&2
  printf '\nEdit %s to configure your models.\n' "$config_path" >&2
  printf 'Example:\n' >&2
  printf '  [defaults]\n' >&2
  printf '  controller = "openai/gpt-5.2"\n' >&2
  printf '  implementer = "openai/gpt-5.2"\n' >&2
  printf '  reviewer = "openai/gpt-5.2"\n' >&2
  printf '  archiver = "openai/gpt-5.2"\n' >&2
  printf '\nThen re-run the installer.\n' >&2
  exit 1
}

# ---------------------------------------------------------------------------
# File installation helpers
# ---------------------------------------------------------------------------

install_files() {
  local src_dir="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  local file
  for file in "$src_dir"/*; do
    [[ -e "$file" ]] || continue
    install -m 0644 "$file" "$dest_dir/$(basename "$file")"
  done
}

install_support_readme() {
  local src="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  install -m 0644 "$src" "$dest_dir/README.md"
}

# ---------------------------------------------------------------------------
# Agent installation with model substitution (OpenCode specific)
# ---------------------------------------------------------------------------

install_agents_with_models() {
  local src_dir="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  local file
  for file in "$src_dir"/*.md; do
    [[ -e "$file" ]] || continue
    install_agent "$file" "$dest_dir/$(basename "$file")"
  done
}

# Role names as they appear in OPSX_<ROLE>_MODEL, matching lib/models/types.py ROLES.
OPSX_MODEL_ROLES=(CONTROLLER IMPLEMENTER REVIEWER ARCHIVER)

# Line-based {env:OPSX_<ROLE>_MODEL} substitution. Works for any text agent
# format (OpenCode's .md frontmatter, Codex's .toml) since it only ever
# rewrites matching placeholder tokens on each line.
install_agent() {
  local src="$1"
  local dest="$2"
  local tmp
  tmp="$(mktemp)"

  while IFS= read -r line || [[ -n "$line" ]]; do
    local role var
    for role in "${OPSX_MODEL_ROLES[@]}"; do
      var="OPSX_${role}_MODEL"
      line="${line//\{env:${var}\}/${!var}}"
    done
    printf '%s\n' "$line"
  done <"$src" >"$tmp"

  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# .gitignore helpers
# ---------------------------------------------------------------------------

sure_gitignore() {
  local gitignore_path="$1"
  local ignore_line="$2"

  mkdir -p "$(dirname "$gitignore_path")"
  if [[ -f "$gitignore_path" ]]; then
    if ! grep -Fxq "$ignore_line" "$gitignore_path"; then
      printf '\n%s\n' "$ignore_line" >> "$gitignore_path"
    fi
  else
    printf '%s\n' "$ignore_line" > "$gitignore_path"
  fi
}

# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

verify_command_available() {
  local cmd="$1"
  if command -v "$cmd" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

print_verify_notice() {
  local client="$1"
  printf '\n%s\n' "Verification: $client CLI not found in PATH. Skipping post-install verification."
}
