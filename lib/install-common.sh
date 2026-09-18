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
  printf '  implementer_escalation = "openai/gpt-5.2"\n' >&2
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

# Role names as they appear in OPSX_<ROLE>_MODEL. The four required roles plus
# the optional supervised roles that carry installed OpenCode agents; the
# optional roles are unresolved on an unsupervised machine, so substitution
# must tolerate their absence (`${var:-}`) rather than tripping `set -u`.
OPSX_MODEL_ROLES=(
  CONTROLLER IMPLEMENTER REVIEWER ARCHIVER
  SUPERVISOR ACCEPTANCE_REVIEWER FIXER VERIFIER
)

# Built-in reasoning-variant defaults per role, used when no
# OPSX_<ROLE>_VARIANT is resolved from models.toml or the environment.
# These match the historical hardcoded `variant:` frontmatter values.
OPSX_VARIANT_DEFAULT_CONTROLLER=high
OPSX_VARIANT_DEFAULT_IMPLEMENTER=high
OPSX_VARIANT_DEFAULT_REVIEWER=xhigh
OPSX_VARIANT_DEFAULT_ARCHIVER=high
# Supervised roles fall back to the same `high` default the other OpenCode
# roles use; only the reviewer keeps its historical `xhigh` default.
OPSX_VARIANT_DEFAULT_SUPERVISOR=high
OPSX_VARIANT_DEFAULT_ACCEPTANCE_REVIEWER=high
OPSX_VARIANT_DEFAULT_FIXER=high
OPSX_VARIANT_DEFAULT_VERIFIER=high

# Line-based {env:OPSX_<ROLE>_MODEL} / {env:OPSX_<ROLE>_VARIANT}
# substitution. Works for any text agent format (OpenCode's .md frontmatter,
# Codex's .toml) since it only ever rewrites matching placeholder tokens on
# each line. An unset variant resolves to the role's built-in default so the
# installed file always carries a concrete value. An unset *model* for an
# optional role substitutes the empty string here, which is why callers skip
# an optional-role artifact whose model is unconfigured before rendering it
# (see the OpenCode adapter's supervised-agent handling).
install_agent() {
  local src="$1"
  local dest="$2"
  local tmp
  tmp="$(mktemp)"

  while IFS= read -r line || [[ -n "$line" ]]; do
    local role var variant_var default_var variant_value
    for role in "${OPSX_MODEL_ROLES[@]}"; do
      var="OPSX_${role}_MODEL"
      line="${line//\{env:${var}\}/${!var:-}}"
      variant_var="OPSX_${role}_VARIANT"
      default_var="OPSX_VARIANT_DEFAULT_${role}"
      variant_value="${!variant_var:-${!default_var:-}}"
      line="${line//\{env:${variant_var}\}/${variant_value}}"
    done
    printf '%s\n' "$line"
  done <"$src" >"$tmp"

  install -m 0644 "$tmp" "$dest"
  rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# Supervised agent / skill rendering and verification helpers
# ---------------------------------------------------------------------------
#
# The supervised agents are optional-role artifacts: an unsupervised machine
# has no OPSX_<SUPERVISED_ROLE>_MODEL resolved, so the adapter renders them
# only when every role they need is configured and reports the rest as
# unconfigured instead of installing an artifact with an empty model. The
# rendering is the same line-wise substitution `install_agent` performs, so
# re-rendering from source with the current environment is a valid
# comparison for installer verification.

# Role env-var suffixes (OPSX_<SUFFIX>_MODEL) that gate a supervised agent
# artifact. The agent file name is `opsx-<kebab-role>.md`.
OPSX_SUPERVISED_AGENT_ROLES=(
  SUPERVISOR ACCEPTANCE_REVIEWER FIXER VERIFIER
)

# Map an agent basename (e.g. opsx-acceptance-reviewer) to its role suffix
# (e.g. ACCEPTANCE_REVIEWER). Prints nothing for an unsupervised agent.
opsx_supervised_role_for_agent() {
  local agent="$1"
  local role kebab
  for role in "${OPSX_SUPERVISED_AGENT_ROLES[@]}"; do
    kebab="$(printf '%s' "$role" | tr '[:upper:]_' '[:lower:]-')"
    if [[ "$agent" == "opsx-${kebab}" ]]; then
      printf '%s' "$role"
      return 0
    fi
  done
  return 1
}

# Install the supervised agents from *src_dir* into *dest_dir*, skipping any
# whose role model is unresolved and reporting each skipped file by name.
# Legacy (unsupervised) agents are always installed. Returns 0 either way:
# an unconfigured supervised role is a reported state, not a failure.
install_supervised_agents() {
  local src_dir="$1"
  local dest_dir="$2"
  mkdir -p "$dest_dir"
  local file agent role var
  for file in "$src_dir"/*.md; do
    [[ -e "$file" ]] || continue
    agent="$(basename "$file" .md)"
    if role="$(opsx_supervised_role_for_agent "$agent")"; then
      var="OPSX_${role}_MODEL"
      if [[ -z "${!var:-}" ]]; then
        printf '%s\n' \
          "Supervised agent $agent is unconfigured ($var is unset); not installed" >&2
        continue
      fi
    fi
    install_agent "$file" "$dest_dir/$agent.md"
  done
}

# Verify the installed supervised agents against a re-render of the
# repository source with the currently resolved environment, byte-compare the
# installed supervision skill and worker shell wrapper with `cmp -s`, and
# report every missing or differing file by name. Returns non-zero when any is
# found. On a machine with no supervised roles configured the agents are
# reported as unconfigured rather than as failures.
verify_supervised_agents_and_skill() {
  local agents_dir="$1"
  local skills_dir="$2"
  local repo_root="$3"
  local failed=0
  local file agent role var installed tmp cmp_ok
  local bin_dir="${4:-}"

  for file in "$repo_root"/adapters/opencode/agents/opsx-*.md; do
    [[ -e "$file" ]] || continue
    agent="$(basename "$file" .md)"
    if ! role="$(opsx_supervised_role_for_agent "$agent")"; then
      continue
    fi
    var="OPSX_${role}_MODEL"
    if [[ -z "${!var:-}" ]]; then
      printf '%s\n' \
        "Verify: supervised agent $agent is unconfigured ($var is unset); skipped"
      continue
    fi
    installed="$agents_dir/$agent.md"
    if [[ ! -f "$installed" ]]; then
      printf '%s\n' \
        "Verify: supervised agent $agent is MISSING from $installed" >&2
      failed=1
      continue
    fi
    tmp="$(mktemp)"
    install_agent "$file" "$tmp"
    cmp_ok=0
    cmp -s "$tmp" "$installed" || cmp_ok=1
    rm -f "$tmp"
    if (( cmp_ok == 0 )); then
      printf '%s\n' \
        "Verify: supervised agent $agent deployed and matches source at $installed"
    else
      printf '%s\n' \
        "Verify: supervised agent $agent at $installed differs from $file (re-run the installer)" >&2
      failed=1
    fi
  done

  local skill_source="$repo_root/skills/opsx-supervision"
  local skill_installed="$skills_dir/opsx-supervision"
  if [[ ! -d "$skill_installed" ]]; then
    printf '%s\n' \
      "Verify: supervision skill is MISSING from $skill_installed" >&2
    failed=1
  elif [[ -f "$skill_source/SKILL.md" ]] \
    && cmp -s "$skill_source/SKILL.md" "$skill_installed/SKILL.md"; then
    printf '%s\n' \
      "Verify: supervision skill deployed and matches source at $skill_installed"
  else
    printf '%s\n' \
      "Verify: supervision skill at $skill_installed differs from $skill_source (re-run the installer)" >&2
    failed=1
  fi

  if [[ -n "$bin_dir" ]]; then
    local shim source
    for shim in opsx-supervise opsx-worker-exec; do
      source="$repo_root/adapters/opencode/bin/$shim"
      installed="$bin_dir/$shim"
      if [[ ! -f "$installed" ]]; then
        printf '%s\n' \
          "Verify: supervised service tool $shim is MISSING from $installed" >&2
        failed=1
      elif cmp -s "$source" "$installed"; then
        printf '%s\n' \
          "Verify: supervised service tool $shim deployed and matches source at $installed"
      else
        printf '%s\n' \
          "Verify: supervised service tool $shim at $installed differs from $source (re-run the installer)" >&2
        failed=1
      fi
    done
  fi

  return "$failed"
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

# Verify the supervision service packaging (the versioned systemd user unit
# template and its provisioning document) that scripts/install-orchestrator.sh
# deploys into the installed runtime tree. *runtime_dir* is the installed
# runtime root (e.g. ~/.local/lib/opsx-controller or
# <project>/.opsx-controller) and *repo_root* is the repository checkout. Every
# missing or differing artifact is reported and makes the helper return
# non-zero. This is read-only: the service is never enabled, started, or
# provisioned by verification.
verify_supervision_service_packaging() {
  local runtime_dir="$1"
  local repo_root="$2"
  local failed=0
  local rel src installed
  for rel in \
    "systemd/opsx-supervise.service.in" \
    "docs/opsx-supervision-service.md"; do
    src="$repo_root/$rel"
    installed="$runtime_dir/$rel"
    if [[ ! -f "$installed" ]]; then
      printf '%s\n' \
        "Verify: supervision service artifact MISSING from $installed" >&2
      failed=1
    elif cmp -s "$src" "$installed"; then
      printf '%s\n' \
        "Verify: supervision service artifact deployed and matches source at $installed"
    else
      printf '%s\n' \
        "Verify: supervision service artifact at $installed differs from $src (re-run the installer)" >&2
      failed=1
    fi
  done
  return "$failed"
}

print_verify_notice() {
  local client="$1"
  printf '\n%s\n' "Verification: $client CLI not found in PATH. Skipping post-install verification."
}
