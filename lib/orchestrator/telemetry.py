"""Telemetry recording and usage/model metadata extraction
(plan-run-observability schema v1).
"""
from __future__ import annotations

import json
import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path

from lib.orchestrator import base
from lib.orchestrator import cost as cost_mod
from lib.orchestrator import state as state_mod


TELEMETRY_SCHEMA_VERSION = 1


def compute_duration_ms(started_at: str, ended_at: str) -> int:
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(ended_at)
    return int((end - start).total_seconds() * 1000)


def build_telemetry_record(
    *,
    plan_name: str,
    run_id: str,
    change_id: str,
    stage: str,
    round_num: int,
    status: str,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    adapter: str,
    worker_command: str,
    timeout_seconds: int,
    retry_attempt: int = 0,
    log_path: str = "",
    stage_status: str | None = None,
    error_message: str | None = None,
    verdict: str | None = None,
    critical_count: int | None = None,
    warning_count: int | None = None,
    note_count: int | None = None,
) -> dict:
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "uid": str(uuid.uuid4()),
        "plan_name": plan_name,
        "run_id": run_id,
        "change_id": change_id,
        "stage": stage,
        "round": round_num,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "invocation": {
            "adapter": adapter,
            "worker_command": worker_command,
            "args_sample": None,
            "timeout_seconds": timeout_seconds,
            "retry_attempt": retry_attempt,
        },
        "model": {
            "provider": None,
            "model_id": None,
            "model_alias": None,
        },
        "result": {
            "log_path": log_path,
            "stage_status": stage_status,
            "error_message": error_message,
            "verdict": verdict,
            "critical_count": critical_count,
            "warning_count": warning_count,
            "note_count": note_count,
        },
        "usage": {
            "usage_available": False,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "usage_source": None,
        },
        "cost": {
            "status": "unavailable",
            "pricing_catalog_version": None,
            "price_snapshot": None,
            "unresolved_reason": None,
            "estimated_cost": None,
        },
    }


def write_telemetry_record(repo: Path, plan_name: str, record: dict) -> None:
    telemetry_dir = repo / ".opsx-plan" / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = telemetry_dir / f"{plan_name}.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(jsonl_path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# Usage / model metadata extraction for direct stage telemetry
# ---------------------------------------------------------------------------

# Recognized token field names mapped to normalized schema keys.
_TOKEN_FIELD_MAP = {
    "input_tokens": "input_tokens",
    "inputTokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "promptTokens": "input_tokens",
    "output_tokens": "output_tokens",
    "outputTokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "completionTokens": "output_tokens",
    "cached_input_tokens": "cached_input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "cache_creation_input_tokens": "cached_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "reasoningTokens": "reasoning_tokens",
    "thinking_tokens": "reasoning_tokens",
    "thinkingTokens": "reasoning_tokens",
    "total_tokens": "total_tokens",
    "totalTokens": "total_tokens",
}

_MODEL_FIELD_MAP = {
    "provider": "provider",
    "model_id": "model_id",
    "modelId": "model_id",
    "model": "model_id",
    "model_alias": "model_alias",
    "modelAlias": "model_alias",
}


def _valid_token_count(value):
    """Only non-negative ``int`` values are accepted. Booleans are rejected."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) and value >= 0


def _extract_token_fields(obj):
    """Return ``(normalized_token_dict, found_any)`` from *obj*.

    Inspects top-level keys and a nested ``usage`` sub-dict when
    present.  Each recognized field is validated as a non-negative
    integer; the first valid value for each normalized key wins.
    """
    result = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    found_any = False

    candidates = [obj]
    usage = obj.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)

    for source in candidates:
        for key, value in source.items():
            norm = _TOKEN_FIELD_MAP.get(key)
            if norm is None:
                continue
            if result[norm] is not None:
                continue  # first source wins
            if _valid_token_count(value):
                result[norm] = int(value)
                found_any = True

    return result, found_any


def _extract_model_fields(obj):
    """Return normalized ``{provider, model_id, model_alias}`` dict.

    Inspects top-level keys and a nested ``model`` sub-dict when
    present.  Only non-empty string values are accepted.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    candidates = [obj]
    model = obj.get("model")
    if isinstance(model, dict):
        candidates.append(model)

    for source in candidates:
        for key, value in source.items():
            norm = _MODEL_FIELD_MAP.get(key)
            if norm is None:
                continue
            if result[norm] is not None:
                continue
            if isinstance(value, str) and value.strip():
                result[norm] = value.strip()

    return result


def _claude_model_usage_tokens(entry) -> int:
    """Sum recognized token fields on one ``modelUsage`` entry, for ranking."""
    if not isinstance(entry, dict):
        return -1
    total = 0
    for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def _extract_claude_envelope_model(envelope: dict) -> dict:
    """Extract model identity from a Claude Code result envelope.

    Prefers the generic top-level/nested ``model`` fields (matching every
    other usage source), then falls back to the envelope's own
    ``modelUsage`` map — a dict keyed by canonical model id, each carrying a
    ``canonicalModel`` and a hosting ``provider`` (e.g. ``"firstParty"``).
    When more than one model billed against a stage (sub-agent delegation),
    the entry with the most combined tokens wins. Provider is normalized to
    ``"anthropic"`` — the envelope's ``provider`` field describes hosting
    infrastructure, not the model vendor, and every model Claude Code can
    dispatch is an Anthropic model regardless of hosting.
    """
    result = _extract_model_fields(envelope)
    if result["provider"] is not None or result["model_id"] is not None:
        return result

    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return result

    best_id, best_entry = max(
        model_usage.items(), key=lambda kv: _claude_model_usage_tokens(kv[1])
    )
    if not isinstance(best_entry, dict):
        return result

    canonical = best_entry.get("canonicalModel")
    model_id = canonical if isinstance(canonical, str) and canonical.strip() else best_id
    if isinstance(model_id, str) and model_id.strip():
        result["provider"] = "anthropic"
        result["model_id"] = model_id.strip()

    return result


def _try_parse_json_line(line):
    """Parse *line* as a JSON object dict, or return ``None``."""
    stripped = line.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _scan_log_for_usage(log_path):
    """Scan every line of *log_path* for JSON objects that carry token fields.

    Returns ``(normalized_token_dict, found_any)``.
    """
    result = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    found_any = False

    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            obj = _try_parse_json_line(line)
            if obj is None:
                continue
            tokens, any_found = _extract_token_fields(obj)
            if not any_found:
                continue
            for key in result:
                if result[key] is None and tokens[key] is not None:
                    result[key] = tokens[key]
                    found_any = True
    except OSError:
        pass

    return result, found_any


def _scan_log_for_model(log_path):
    """Scan every line of *log_path* for JSON objects that carry model fields.

    Returns normalized ``{provider, model_id, model_alias}`` dict.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            obj = _try_parse_json_line(line)
            if obj is None:
                continue
            model = _extract_model_fields(obj)
            for key in result:
                if result[key] is None and model[key] is not None:
                    result[key] = model[key]
    except OSError:
        pass

    return result


def _parse_invocation_model_value(model_value):
    """Parse an invocation-configured model string into normalized fields.

    Recognizes the common ``provider/model_id`` form used by installed
    OpenCode agent configs. When no provider prefix is present, preserves the
    raw value as ``model_id`` and leaves ``provider`` unset.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }
    if not isinstance(model_value, str):
        return result
    value = model_value.strip()
    if not value:
        return result
    if "/" in value:
        provider, model_id = value.split("/", 1)
        provider = provider.strip()
        model_id = model_id.strip()
        if provider and model_id:
            result["provider"] = provider
            result["model_id"] = model_id
            return result
    result["model_id"] = value
    return result


_ADAPTER_AGENT_DIR_PARTS = {
    "opencode": (".config", "opencode", "agents"),
    "claude-code": (".claude", "agents"),
}

_ADAPTER_REPO_AGENT_DIR_PARTS = {
    "opencode": (".opencode", "agents"),
    "claude-code": (".claude", "agents"),
}


def _adapter_agent_dir(adapter: str, repo: Path | None = None) -> list[Path]:
    """Return candidate installed agent directories for *adapter*, in lookup
    order: the repo-local install (when *repo* is given) first, then the
    home-rooted install location. Empty when *adapter* is unknown."""
    candidates: list[Path] = []
    if repo is not None:
        repo_parts = _ADAPTER_REPO_AGENT_DIR_PARTS.get(adapter)
        if repo_parts is not None:
            candidates.append(repo.joinpath(*repo_parts))
    home_parts = _ADAPTER_AGENT_DIR_PARTS.get(adapter)
    if home_parts is not None:
        candidates.append(Path.home().joinpath(*home_parts))
    return candidates


def _best_effort_expand_invoke(invoke: str) -> str:
    """Expand ``$VAR``/``${VAR}`` references in *invoke* for telemetry fallback.

    Unlike the fail-closed expansion in ``invoke_direct_stage``, this never
    raises or blocks: it is read-only best-effort parsing of a command that
    already ran, so an unset variable just leaves that token as-is rather
    than failing telemetry extraction.
    """
    try:
        tokens = shlex.split(invoke)
    except ValueError:
        return invoke
    return shlex.join(os.path.expandvars(token) for token in tokens)


def _extract_invocation_model(worker_command, adapter: str = "opencode", repo: Path | None = None):
    """Return model identity from the configured worker invocation.

    Supports either an explicit ``--model`` argument or an ``--agent``
    reference whose installed agent frontmatter (in *adapter*'s own agent
    directory) declares a ``model:`` value. When *repo* is given, a
    repo-local agent install is checked before the home-rooted one.
    """
    result = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }
    if not isinstance(worker_command, str) or not worker_command.strip():
        return result

    try:
        parts = shlex.split(worker_command)
    except ValueError:
        return result

    agent_name = None
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--model" and i + 1 < len(parts):
            return _parse_invocation_model_value(parts[i + 1])
        if part.startswith("--model="):
            return _parse_invocation_model_value(part.split("=", 1)[1])
        if part == "--agent" and i + 1 < len(parts):
            agent_name = parts[i + 1].strip() or None
            i += 2
            continue
        if part.startswith("--agent="):
            agent_name = part.split("=", 1)[1].strip() or None
        i += 1

    if not agent_name:
        return result

    for agent_dir in _adapter_agent_dir(adapter, repo):
        agent_path = agent_dir / f"{agent_name}.md"
        try:
            lines = agent_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        if not lines or lines[0].strip() != "---":
            return result

        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            if not stripped.startswith("model:"):
                continue
            _, _, raw_value = stripped.partition(":")
            model_value = raw_value.strip()
            if len(model_value) >= 2 and model_value[0] == model_value[-1] and model_value[0] in {'"', "'"}:
                model_value = model_value[1:-1].strip()
            return _parse_invocation_model_value(model_value)

        return result

    return result


# Recognized sidecar token field names.  The plugin emits camelCase keys.
_SIDECAR_TOKEN_FIELD_MAP = {
    "input_tokens": "input_tokens",
    "inputTokens": "input_tokens",
    "output_tokens": "output_tokens",
    "outputTokens": "output_tokens",
    "cached_input_tokens": "cached_input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "reasoningTokens": "reasoning_tokens",
    "thinking_tokens": "reasoning_tokens",
    "thinkingTokens": "reasoning_tokens",
    "total_tokens": "total_tokens",
    "totalTokens": "total_tokens",
}

_SIDECAR_MODEL_FIELD_MAP = {
    "provider": "provider",
    "model_id": "model_id",
    "modelId": "model_id",
    "model": "model_id",
    "model_alias": "model_alias",
    "modelAlias": "model_alias",
}


def _normalize_sidecar_tokens(obj: dict) -> tuple[dict[str, int | None], bool]:
    """Extract and validate token counts from a sidecar record.

    Returns ``(normalized, found_any)``.  Only non-negative ``int`` values
    (not bool) are accepted.  The ``usage`` sub-object is inspected when
    present.
    """
    result: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    found_any = False

    candidates = [obj]
    usage = obj.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)

    for source in candidates:
        for key, value in source.items():
            norm = _SIDECAR_TOKEN_FIELD_MAP.get(key)
            if norm is None:
                continue
            if result[norm] is not None:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                result[norm] = value
                found_any = True

    return result, found_any


def _normalize_sidecar_model(obj: dict) -> dict[str, str | None]:
    """Extract model identity from a sidecar record.

    Returns ``{provider, model_id, model_alias}`` with non-empty string
    values or None.
    """
    result: dict[str, str | None] = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }
    candidates = [obj]
    model = obj.get("model")
    if isinstance(model, dict):
        candidates.append(model)
    for source in candidates:
        for key, value in source.items():
            norm = _SIDECAR_MODEL_FIELD_MAP.get(key)
            if norm is None:
                continue
            if result[norm] is not None:
                continue
            if isinstance(value, str) and value.strip():
                result[norm] = value.strip()
    return result


def _parse_sidecar_timestamp(value):
    """Try to parse *value* as an ISO-8601 datetime.

    Returns a ``datetime`` or ``None``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value)
        # Replace naive datetimes with UTC to avoid comparison failures
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _identity_match(record: dict, plan_name: str, run_id: str,
                    change_id: str, stage: str, round_num: int) -> bool:
    """Return True when *record* identity fields match the stage invocation."""
    return (
        record.get("plan_name") == plan_name
        and record.get("run_id") == run_id
        and record.get("change_id") == change_id
        and record.get("stage") == stage
        and record.get("round") == round_num
    )


def _read_sidecar_usage(
    sidecar_path: Path | None,
    plan_name: str,
    run_id: str,
    change_id: str,
    stage: str,
    round_num: int,
    is_normal_completion: bool,
) -> tuple[dict[str, int | None], dict[str, str | None], bool, str | None]:
    """Read and select the best valid sidecar usage record.

    Returns ``(token_dict, model_dict, selected, usage_source)``.

    *token_dict* and *model_dict* are caller-owned defaults to fill.
    *selected* is ``True`` when usable sidecar data was found.
    *usage_source* is ``"opencode_plugin"`` when sidecar wins.
    """
    tokens: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
    }
    model: dict[str, str | None] = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    if sidecar_path is None:
        return tokens, model, False, None

    # 2.1 -- Read file (handle missing, empty, unreadable)
    try:
        lines = sidecar_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return tokens, model, False, None

    # 2.2 -- Validate each record
    final_records: list[dict] = []
    incremental_records: list[dict] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not (stripped.startswith("{") and stripped.endswith("}")):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue

        # -- schema version
        if record.get("schema_version") != 1:
            continue

        # -- identity match
        if not _identity_match(record, plan_name, run_id, change_id, stage, round_num):
            continue

        # -- event type
        event_type = record.get("event_type")
        if event_type not in ("final", "incremental"):
            continue

        # -- usable timestamp
        emitted_at = _parse_sidecar_timestamp(record.get("emitted_at"))
        if emitted_at is None:
            continue

        # -- conservative numeric fields + model identity
        norm_tokens, tokens_found = _normalize_sidecar_tokens(record)
        norm_model = _normalize_sidecar_model(record)
        model_found = any(v is not None for v in norm_model.values())
        if not tokens_found and not model_found:
            continue

        entry = {
            "record": record,
            "emitted_at": emitted_at,
            "tokens": norm_tokens,
            "model": norm_model,
        }

        if event_type == "final":
            final_records.append(entry)
        else:
            incremental_records.append(entry)

    # 2.3 -- Select latest valid final record
    if final_records:
        best = max(final_records, key=lambda e: e["emitted_at"])
        tokens = best["tokens"]
        model = best["model"]
        return tokens, model, True, "opencode_plugin"

    # 2.4 & 2.5 -- Incremental records
    if incremental_records and not is_normal_completion:
        best = max(incremental_records, key=lambda e: e["emitted_at"])
        tokens = best["tokens"]
        model = best["model"]
        return tokens, model, True, "opencode_plugin"

    return tokens, model, False, None


def extract_usage_and_model(
    payload,
    log_path,
    sidecar_path=None,
    plan_name: str = "",
    run_id: str = "",
    change_id: str = "",
    stage: str = "",
    round_num: int = 0,
    is_normal_completion: bool = True,
    envelope=None,
):
    """Extract usage and model metadata for a completed stage invocation.

    **Precedence:**
    1. Usage & model from parsed worker JSON (*payload*) are preferred.
    2. When *payload* carries no token usage, the selected Claude Code
       result *envelope* (if any) is consulted.
    3. When neither worker JSON nor the envelope provides token usage, the
       stage log is scanned for recognizable token metadata.
    4. When none of the above provide token usage, the OpenCode plugin
       sidecar is consulted.
    5. Model identity follows the same order: worker JSON, then envelope,
       then log scan, then sidecar. A lower-precedence source never
       supplements a partial higher-precedence model.

    Returns ``(usage_dict, model_dict)`` where *usage_dict* includes
    every normalised token field (int or None), ``usage_available``, and
    ``usage_source``.
    """
    usage = {
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "usage_available": False,
        "usage_source": None,
    }
    model = {
        "provider": None,
        "model_id": None,
        "model_alias": None,
    }

    worker_usage_found = False
    worker_model_found = False

    # 1. Worker JSON -------------------------------------------------------
    if isinstance(payload, dict):
        tokens, wu_found = _extract_token_fields(payload)
        if wu_found:
            worker_usage_found = True
            for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                         "reasoning_tokens", "total_tokens"):
                if tokens[key] is not None:
                    usage[key] = tokens[key]
            usage["usage_available"] = True
            usage["usage_source"] = "worker_json"

        wm = _extract_model_fields(payload)
        for key in ("provider", "model_id", "model_alias"):
            if wm[key] is not None:
                model[key] = wm[key]
                worker_model_found = True

    # 2. Claude Code result envelope ---------------------------------------
    envelope_usage_found = False
    envelope_model_found = False
    if isinstance(envelope, dict):
        if not worker_usage_found:
            env_tokens, eu_found = _extract_token_fields(envelope)
            if eu_found:
                envelope_usage_found = True
                for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                             "reasoning_tokens", "total_tokens"):
                    if env_tokens[key] is not None:
                        usage[key] = env_tokens[key]
                usage["usage_available"] = True
                usage["usage_source"] = "claude_result_json"

        if not worker_model_found:
            env_model = _extract_claude_envelope_model(envelope)
            for key in ("provider", "model_id", "model_alias"):
                if env_model[key] is not None:
                    model[key] = env_model[key]
                    envelope_model_found = True

    # 3. Log fallback ------------------------------------------------------
    log_usage_found = False
    if log_path is not None:
        if not worker_usage_found and not envelope_usage_found:
            log_tokens, log_found = _scan_log_for_usage(log_path)
            if log_found:
                log_usage_found = True
                for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                             "reasoning_tokens", "total_tokens"):
                    if log_tokens[key] is not None:
                        usage[key] = log_tokens[key]
                usage["usage_available"] = True
                usage["usage_source"] = "log_metadata"

        if not worker_model_found and not envelope_model_found:
            log_model = _scan_log_for_model(log_path)
            for key in ("provider", "model_id", "model_alias"):
                if model[key] is None and log_model[key] is not None:
                    model[key] = log_model[key]

    # 4. OpenCode plugin sidecar fallback ----------------------------------
    # Always consult the sidecar for model identity when no higher source
    # exists, independently of whether usage was provided by a higher source.
    if sidecar_path is not None:
        sidecar_tokens, sidecar_model, sidecar_selected, sidecar_source = (
            _read_sidecar_usage(
                sidecar_path, plan_name, run_id, change_id, stage, round_num,
                is_normal_completion,
            )
        )
        # Sidecar token usage only when no higher-precedence source found
        if not worker_usage_found and not envelope_usage_found and not log_usage_found and sidecar_selected:
            sidecar_token_found = False
            for key in ("input_tokens", "output_tokens", "cached_input_tokens",
                         "reasoning_tokens", "total_tokens"):
                if sidecar_tokens[key] is not None:
                    usage[key] = sidecar_tokens[key]
                    sidecar_token_found = True
            if sidecar_token_found:
                usage["usage_available"] = True
                usage["usage_source"] = "opencode_plugin"

        # Sidecar model identity supplements only when no higher source
        if not worker_model_found and not envelope_model_found:
            for key in ("provider", "model_id", "model_alias"):
                if model[key] is None and sidecar_model[key] is not None:
                    model[key] = sidecar_model[key]

    return usage, model


def get_or_create_run_id(repo: Path, cfg: dict, state: dict) -> str:
    run_id = state.get("run_id", "")
    if run_id:
        return run_id
    started_at = state.get("started_at", "")
    if started_at:
        # Derive stable run_id from plan started_at timestamp
        run_id = started_at.replace(":", "").replace("-", "").replace("T", "-")
    else:
        # First run: generate UUID, persist started_at and run_id
        now = base.utcnow()
        state["started_at"] = now
        run_id = now.replace(":", "").replace("-", "").replace("T", "-")
    state["run_id"] = run_id
    state_mod.save_state(repo, cfg["name"], state)
    return run_id


def _record_stage_telemetry(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    stage: str,
    round_num: int,
    started_at: str,
    ended_at: str,
    duration_ms: int,
    telemetry_status: str,
    error_message: str | None,
    payload: dict | None,
    log_path: Path,
    sidecar_path: Path | None = None,
    envelope: dict | None = None,
) -> None:
    run_id = get_or_create_run_id(repo, cfg, state)
    plan_name = cfg["name"]
    is_normal = telemetry_status == "completed"
    stage_status = payload.get("status") if isinstance(payload, dict) else None
    verdict = None
    critical_count = None
    warning_count = None
    note_count = None
    if isinstance(payload, dict) and stage == "review":
        verdict = payload.get("verdict")
        counts = payload.get("finding_counts")
        if isinstance(counts, dict):
            critical_count = counts.get("critical")
            warning_count = counts.get("warning")
            note_count = counts.get("note")
    rel_log_path = str(log_path.relative_to(repo)) if log_path else ""

    record = build_telemetry_record(
        plan_name=cfg["name"],
        run_id=run_id,
        change_id=cid,
        stage=stage,
        round_num=round_num,
        status=telemetry_status,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        adapter=cfg["adapter"],
        worker_command=cfg[f"{stage}_invoke"],
        timeout_seconds=int(cfg["changes"][cid]["timeout_minutes"] * 60),
        log_path=rel_log_path,
        stage_status=stage_status,
        error_message=error_message,
        verdict=verdict,
        critical_count=critical_count,
        warning_count=warning_count,
        note_count=note_count,
    )
    # Populate usage and model metadata when a payload was parsed
    # (extraction is best-effort; never fail telemetry write).
    try:
        usage, model = extract_usage_and_model(
            payload, log_path,
            sidecar_path=sidecar_path,
            plan_name=plan_name,
            run_id=run_id,
            change_id=cid,
            stage=stage,
            round_num=round_num,
            is_normal_completion=is_normal,
            envelope=envelope,
        )
        if model["provider"] is None and model["model_id"] is None:
            expanded_invoke = _best_effort_expand_invoke(cfg[f"{stage}_invoke"])
            invocation_model = _extract_invocation_model(expanded_invoke, cfg["adapter"], repo)
            for key in ("provider", "model_id", "model_alias"):
                if model[key] is None and invocation_model[key] is not None:
                    model[key] = invocation_model[key]
        record["usage"].update(usage)
        record["model"].update(model)
    except Exception:
        pass

    # Attempt cost estimation (best-effort; never fail telemetry write).
    try:
        cost = cost_mod.estimate_stage_cost(record["usage"], record["model"], repo=repo)
        record["cost"].update(cost)
    except Exception:
        pass

    write_telemetry_record(repo, cfg["name"], record)
    state_mod.rec(state, cid)["telemetry"] = {"latest_telemetry": record["uid"]}

