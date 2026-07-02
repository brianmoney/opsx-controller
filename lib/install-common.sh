#!/usr/bin/env bash
# Common installer utilities for opsx-controller adapters.
# Source this file from adapter install.sh scripts.
set -euo pipefail

_OPSX_install_common_sourced=1

OPSX_CONTROLLER_ROOT="${OPSX_CONTROLLER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# ---------------------------------------------------------------------------
# Model environment helpers
# ---------------------------------------------------------------------------

require_model_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf 'Required model environment variable is not set: %s\n' "$name" >&2
    printf 'Source your opsx-controller .env before installing.\n' >&2
    exit 1
  fi
}

require_model_envs() {
  require_model_env OPSX_CONTROLLER_MODEL
  require_model_env OPSX_IMPLEMENTER_MODEL
  require_model_env OPSX_REVIEWER_MODEL
  require_model_env OPSX_ARCHIVER_MODEL
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
