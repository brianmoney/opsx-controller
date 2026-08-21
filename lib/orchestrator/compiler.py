"""Compile helpers: ``opsx-plan compile`` pipeline.

Owns the compile-client registry, source / output resolution, prompt
construction, client invocation, and TOML extraction.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from lib.orchestrator import base

# Character budget for the assembled compile prompt.
#
# The compile prompt is built in fixed priority order: the source plan
# markdown, schema guidance, and compile instructions are always included
# whole; the canonical sample pair and at most one active repository
# template pair are included only while they fit within this budget.  The
# value (~128k) is roughly a 4x reduction from the ~559k prompt observed
# in a mature repository while leaving ample room for a large source plan
# plus the fixed sections.  A constant, not a config knob: a single value
# keeps behavior predictable across repositories.
COMPILE_PROMPT_BUDGET_CHARS = 128_000

# Maximum acceptable length for a single compile-client argv element.
#
# Any argv element longer than this fails before spawn with a named
# ``PlanError`` instead of surfacing an opaque OS "Argument list too long"
# error.  The threshold is far below ``ARG_MAX``; it only guards against
# future regressions to an inline-argv prompt transport (today OpenCode
# uses a workspace-local file and Claude Code uses stdin).
MAX_INLINE_ARG_CHARS = 100_000

COMPILE_CLIENTS: dict[str, dict] = {
    "opencode": {
        "executable": "opencode",
        "supported": True,
        "prompt_transport": "file",
        "argv_template": ["{executable}", "run", "--model", "{model}", "{prompt}"],
    },
    "claude-code": {
        "executable": "claude",
        "supported": True,
        "prompt_transport": "stdin",
        "argv_template": ["{executable}", "-p", "--model", "{model}"],
    },
    "codex-cli": {
        "executable": "codex",
        "supported": False,
    },
    "dsh": {
        "executable": "dsh",
        "supported": False,
    },
}


def resolve_compile_source(repo: Path, source: str) -> Path:
    """Resolve a compile source path relative to *repo*.

    Returns the absolute ``Path`` or raises ``PlanError`` when the source
    does not exist or is not a ``.md`` file.
    """
    p = (repo / source).resolve()
    if not p.is_file():
        raise base.PlanError(f"source not found: {p}")
    if p.suffix.lower() != ".md":
        raise base.PlanError(f"source must be a markdown file (.md): {p}")
    return p


def resolve_compile_output(repo: Path, output: str, force: bool) -> Path:
    """Resolve compile output path, refusing overwrite unless *force*.

    Returns the absolute ``Path`` for the output file.  The parent
    directory is created if it does not already exist.
    """
    p = (repo / output).resolve()
    if p.exists() and not force:
        raise base.PlanError(
            f"output exists: {p}  (use --force to overwrite)"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def check_controller_model(repo: Path | None = None,
                           adapter: str = "opencode") -> tuple[str, str | None]:
    """Return the controller ``(model, variant)`` resolved for *adapter*.

    ``variant`` is the optional controller reasoning-effort label
    (``None`` when no ``controller_variant`` source is set — the compile
    invocation then omits the flag entirely).

    Raises ``PlanError`` when the ``controller`` role cannot be resolved
    for *adapter*, or when the resolved identifier violates the adapter's
    model syntax rules (e.g. a provider-prefixed identifier for Claude).
    """
    try:
        from lib.models.resolver import ModelConfigError
        from lib.models.resolver import resolve as resolve_models
        from lib.models.resolver import validate as validate_models
        from lib.models.types import ROLES

        resolved = resolve_models(adapter, repo=repo)
    except ModelConfigError as exc:
        raise base.PlanError(str(exc)) from exc
    model = resolved["controller"].model
    if not model:
        raise base.PlanError(
            f"controller model is not configured for the {adapter} adapter; "
            f"compile requires a controller model to invoke the selected client "
            f"(run `opsx-plan models show --adapter {adapter}` to inspect, "
            f"or `opsx-plan models init` to seed a configuration file)"
        )

    # Reject an identifier whose syntax is invalid for the selected adapter
    # before any process spawn (the opencode adapter expects provider/model,
    # claude-code rejects provider prefixes, etc.).
    warnings = validate_models(adapter, resolved)
    controller_warnings = [w for w in warnings if w.startswith("controller:")]
    if controller_warnings:
        raise base.PlanError(
            f"controller model '{model}' is not valid for the {adapter} adapter: "
            f"{controller_warnings[0]}\n"
            f"Run `opsx-plan models show --adapter {adapter}` to inspect "
            f"resolved models, or `opsx-plan models init` to seed a "
            f"configuration file."
        )

    variant = resolved["controller"].variant
    return model, variant


def discover_template_pairs(repo: Path) -> list[tuple[Path, Path | None]]:
    """Find repository template plan pairs (md + matching toml).

    Lists only top-level ``openspec/plans/`` pairs, without recursing.
    Pairs under ``openspec/plans/archived/`` are excluded unconditionally:
    archived plans are historical records, not conventions to imitate, and
    they are what historically inflated the compile prompt without bound.
    Returns a list of ``(md_path, toml_path_or_None)`` tuples.
    """
    plans_dir = repo / "openspec" / "plans"
    pairs: list[tuple[Path, Path | None]] = []
    if plans_dir.is_dir():
        for md_path in sorted(plans_dir.glob("*.md")):
            toml_path = md_path.with_suffix(".toml")
            pairs.append((md_path, toml_path if toml_path.is_file() else None))
    return pairs


def _render_repo_pair(repo: Path, md: Path,
                      toml: Path | None) -> str:
    """Render one repository template pair (md + optional toml) as text.

    The rendered text is exactly what ``build_compile_prompt`` appends for
    the pair, so budget selection can rely on its length.
    """
    parts = ["## Repository template plans\n"]
    rel = md.relative_to(repo)
    parts.append(f"### Template: `{rel}`\n")
    try:
        parts.append(md.read_text(encoding="utf-8"))
    except OSError:
        pass
    if toml is not None:
        rel_toml = toml.relative_to(repo)
        parts.append(f"### Template manifest: `{rel_toml}`\n")
        try:
            parts.append(toml.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(parts)


def _render_sample_pair(sample_md: Path, sample_toml: Path) -> str:
    """Render the canonical sample pair (md + toml) as text.

    The rendered text is exactly what ``build_compile_prompt`` appends for
    the pair, so budget selection can rely on its length.
    """
    parts = ["## Sample plan (canonical)\n"]
    try:
        parts.append(sample_md.read_text(encoding="utf-8"))
    except OSError:
        pass
    parts.append("### Sample manifest (canonical)\n")
    try:
        parts.append(sample_toml.read_text(encoding="utf-8"))
    except OSError:
        pass
    return "\n".join(parts)


def _select_repo_template_pair(repo: Path,
                               available_chars: int) -> tuple[Path, Path | None] | None:
    """Select the smallest active repository template pair that fits.

    Returns the smallest ``(md, toml)`` pair from top-level
    ``openspec/plans/`` whose combined rendered size is
    <= *available_chars*, or ``None`` when no active pair fits.  Archived
    pairs are never considered (see ``discover_template_pairs``).
    """
    best: tuple[Path, Path | None] | None = None
    best_size: int | None = None
    for md, toml in discover_template_pairs(repo):
        size = len(_render_repo_pair(repo, md, toml))
        if size > available_chars:
            continue
        if best_size is None or size < best_size:
            best = (md, toml)
            best_size = size
    return best


def resolve_sample_plan_pair() -> tuple[Path, Path | None] | None:
    """Resolve the canonical sample plan pair.

    Probes ``~/.local/lib/opsx-controller/samples`` (installed) first, then
    falls back to ``<checkout>/orchestrator/samples``.  The installed copy
    takes precedence because it represents the version that was actually
    deployed — the checkout copy is only a fallback for development.

    Returns ``(md_path, toml_path)`` or ``None`` when neither location
    exists.
    """
    # 1. Installed samples (authoritative copy).
    installed = Path.home() / ".local" / "lib" / "opsx-controller" / "samples"
    md_path = installed / "sample-plan.md"
    toml_path = installed / "sample-plan.toml"
    if md_path.is_file() and toml_path.is_file():
        return md_path, toml_path

    # 2. Checkout fallback (mirrors _SCRIPT_ROOT / _RUNTIME_ROOTS).
    for root in base._RUNTIME_ROOTS:
        samples_dir = root / "orchestrator" / "samples"
        md_path = samples_dir / "sample-plan.md"
        toml_path = samples_dir / "sample-plan.toml"
        if md_path.is_file() and toml_path.is_file():
            return md_path, toml_path

    return None


def _escape_toml_value(value: str) -> str:
    """Escape *value* for safe inclusion inside a TOML basic (double-quoted) string.

    Only backslash and double-quote require escaping in the TOML basic-string
    grammar; other characters (``$``, ``{``, ``}``, etc.) are literal.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_schema_guidance(adapter: str = "opencode") -> str:
    """Build manifest schema guidance derived from ``load_plan()`` behavior.

    Covers ``[plan]`` fields, ``[[changes]]`` entries, dependency edges,
    gate defaults, adapter defaults, and fields consumed by the parser.

    *adapter* selects the compile client and determines the ``[plan]``
    defaults rendered in the prompt — every field shown reflects the
    selected adapter's own defaults, not a generic template.
    """
    default_adapter = adapter
    defaults = base.ADAPTER_DEFAULTS[default_adapter]
    state_file = defaults.get("state_file", "")
    impl_invoke = defaults.get("implement_invoke", "")
    review_invoke = defaults.get("review_invoke", "")
    archive_invoke = defaults.get("archive_invoke", "")
    return (
        "## Expected TOML manifest shape\n"
        "\n"
        "The manifest is a TOML document with a ``[plan]`` table and\n"
        "one or more ``[[changes]]`` entries.\n"
        "\n"
        f"### ``[plan]`` table fields (all optional; the {adapter} defaults shown)\n"
        "\n"
        "| Field | Type | Default | Description |\n"
        "|-------|------|---------|-------------|\n"
        "| name | string | stems from filename | plan display name |\n"
        f"| adapter | string | ``\"{default_adapter}\"`` | adapter key (``ADAPTER_DEFAULTS``) |\n"
        f"| state_file | string | ``{state_file}`` | controller state path |\n"
        f"| implement_invoke | string | ``{impl_invoke}`` | direct implement command |\n"
        f"| review_invoke | string | ``{review_invoke}`` | direct review command |\n"
        f"| archive_invoke | string | ``{archive_invoke}`` | direct archive command |\n"
        "| timeout_minutes | float | ``90`` | per-change stage timeout |\n"
        "| max_rounds | int | ``5`` | implement-review loop ceiling |\n"
        "| no_progress_limit | int | ``2`` | consecutive no-progress rounds before failing |\n"
        "| escalate_after_review_fails | int | ``0`` | promote implement to escalation model after N failed reviews; round=(N+1) first escalates; 0 disables |\n"
        "| finding_recurrence_limit | int | ``0`` | halt a change when one locus is cited by a blocking finding in this many distinct rounds; 0 disables |\n"
        "| invalid_output_retries | int | ``2`` | in-place retries when a stage ends without its final JSON envelope (contract miss, transient provider error); 0 disables |\n"
        "| fast_checks | list[str] | ``[]`` | post-archive CLI checks |\n"
        "| check_timeout_minutes | float | ``15`` | fast-check timeout |\n"
        "| require_clean_tracked | bool | ``true`` | refuse to run when tracked tree is dirty |\n"
        "| skip_warning | bool | ``false`` | warning and note findings do not gate the review verdict |\n"
        "| skip_suggestion | bool | ``false`` | note findings do not gate the review verdict |\n"
        "| plan_doc | string | ``\"\"`` | path to the source markdown plan for ``create_invoke`` |\n"
        "| create_invoke | string | ``\"\"`` | authoring command for auto-creating changes |\n"
        "| create_timeout_minutes | float | ``30`` | create stage timeout |\n"
        "| create_max_attempts | int | ``2`` | create retry ceiling |\n"
        "| review_created | bool | ``true`` | require operator ``accept`` before driving created changes |\n"
        "| created_check | string | ``\"openspec validate {change} --strict\"`` | post-create validation command |\n"
        "\n"
        "### ``[[changes]]`` entry fields\n"
        "\n"
        "| Field | Type | Default | Description |\n"
        "|-------|------|---------|-------------|\n"
        "| id | string | **required** | unique change identifier (slug) |\n"
        "| phase | int | ``None`` | phase number (e.g. 1, 2, 3) |\n"
        "| depends_on | list[str] | ``[]`` | ids of changes that must complete first |\n"
        "| pause_before | bool | ``false`` | wait for ``opsx-plan approve`` before running |\n"
        "| enabled | bool | ``true`` | set ``false`` to defer a change |\n"
        "| timeout_minutes | float | plan-level timeout | per-change stage timeout override |\n"
        "| create_invoke | string | ``\"\"`` | per-change authoring command override |\n"
        "| create_max_attempts | int | plan-level value | per-change create attempt override |\n"
        "\n"
        "### Dependency semantics\n"
        "\n"
        "- ``depends_on`` lists only canonical change ids (slugs). Each id must\n"
        "  appear as another ``[[changes]]`` entry.\n"
        "- A change cannot depend on itself (no self-loops).\n"
        "- The orchestrator validates that every dependency id is present and\n"
        "  that the resulting DAG has no cycles.\n"
        "- ``depends_on = []`` means no dependencies.\n"
        "- Backticked known change ids from the source doc become edges.\n"
        "- ``Phase N`` references expand to that phase's changes.\n"
        "- Text starting with ``None`` or containing independence wording\n"
        "  (\"independent\", \"in parallel\", \"may proceed\") produces no\n"
        "  edges even when other changes are mentioned.\n"
        "\n"
        "### Gate manual defaults\n"
        "\n"
        "- First change of each capability marked ``(proposed`` in the source\n"
        "  gets ``pause_before = true``.\n"
        "- ``deferred`` wording sets ``enabled = false``.\n"
        "- Manual phase-exit gates (``pause_before = true``) are added by the\n"
        "  operator; the compiler records but does not invent them.\n"
        "\n"
        f"### Adapter defaults ({adapter})\n"
        "\n"
        "```toml\n"
        f"[plan]\n"
        f"adapter = \"{default_adapter}\"\n"
        f"state_file = \"{_escape_toml_value(defaults['state_file'])}\"\n"
        f"implement_invoke = \"{_escape_toml_value(defaults['implement_invoke'])}\"\n"
        f"review_invoke = \"{_escape_toml_value(defaults['review_invoke'])}\"\n"
        f"archive_invoke = \"{_escape_toml_value(defaults['archive_invoke'])}\"\n"
        "```\n"
    )


def build_compile_prompt(source_content: str, source_path: Path,
                         repo: Path, adapter: str = "opencode") -> str:
    """Build the complete compile prompt for the selected adapter.

    The prompt is assembled under ``COMPILE_PROMPT_BUDGET_CHARS`` in fixed
    priority order: the source plan markdown, adapter-aware schema
    guidance, and compile instructions are always included whole — when
    the source plan alone exceeds the budget a warning is logged and the
    source is still included, because it is the input, not an example.
    Optional examples follow while they fit the remaining budget: first
    the canonical sample plan pair, then at most one repository template
    pair (the smallest active ``openspec/plans/`` pair that fits, selected
    by ``_select_repo_template_pair``).  An optional example omitted for
    budget reasons is logged with a note.
    """
    try:
        rel_source = str(source_path.resolve().relative_to(repo.resolve()))
    except ValueError:
        rel_source = str(source_path)

    budget = COMPILE_PROMPT_BUDGET_CHARS

    source_part = f"## Source plan markdown\n{source_content}"
    guidance_part = build_schema_guidance(adapter)

    if len(source_content) > budget:
        base.log(
            f"  warning: source plan markdown alone ({len(source_content)} "
            f"chars) exceeds the compile prompt budget ({budget} chars); "
            "it is included whole regardless, and optional examples are omitted"
        )

    instructions_part = (
        "## Compile instructions\n"
        "\n"
        "Convert the source plan markdown above into a valid opsx-plan TOML "
        "manifest that can be loaded by `opsx-plan status` and "
        "`opsx-plan run`. Follow these rules:\n"
        "\n"
        "1. **Output only TOML.** Do not include any prose, explanation, "
        "markdown headers, or commentary outside the TOML payload. "
        "Output raw TOML or a single fenced ```toml block.\n"
        "2. **Emit a `[plan]` table** with at least `name`, `adapter` "
        f"(\"{adapter}\"), and `plan_doc` set to exactly "
        f"\"{rel_source}\".\n"
        "3. **The `adapter` field MUST equal exactly** "
        f"\"{adapter}\".\n"
        "4. **Emit one `[[changes]]` entry per change** described in the "
        "source plan, in phase order.\n"
        "5. **Preserve dependency semantics:** backticked known change ids "
        "in the source doc become `depends_on` entries. Independence wording "
        "(\"independent\", \"in parallel\", \"may proceed\") means no "
        "dependency edge. Deferred wording means `enabled = false`.\n"
        "6. **Preserve manual gates:** `pause_before = true` for any change "
        "that introduces a proposed capability (marked with `(proposed` "
        "in the source) or has an explicit gate note.\n"
        "7. **Preserve phase numbers** as `phase` fields on each change.\n"
        "8. **Every change id must be unique** and every `depends_on` id "
        "must reference another change in the manifest.\n"
        "9. **The DAG must have no cycles.**\n"
    )

    # 1. Fixed sections — always included whole.
    parts: list[str] = [source_part, guidance_part, instructions_part]
    # Two "\n" join separators between the three fixed parts, plus one more
    # per optional part appended below.
    fixed_size = len(source_part) + len(guidance_part) + len(instructions_part) + 2
    available = max(budget - fixed_size, 0)

    # 2. Canonical sample pair — included only while it fits the budget.
    sample_pair = resolve_sample_plan_pair()
    if sample_pair is not None:
        sample_text = _render_sample_pair(*sample_pair)
        if available >= len(sample_text) + 1:
            parts.append(sample_text)
            available -= len(sample_text) + 1
        else:
            base.log(
                f"  note: omitting canonical sample plan pair: it does not "
                f"fit within the remaining compile prompt budget "
                f"({len(sample_text) + 1} chars needed, {available} available)"
            )

    # 3. One repository template pair — the smallest active pair that fits.
    repo_pair = _select_repo_template_pair(repo, available)
    if repo_pair is not None:
        parts.append(_render_repo_pair(repo, *repo_pair))
    elif discover_template_pairs(repo):
        base.log(
            "  note: omitting repository template plans: no active "
            "openspec/plans pair fits within the remaining compile prompt "
            f"budget ({available} chars available)"
        )

    return "\n".join(parts)


def _build_compile_argv(adapter: str, model: str, prompt: str,
                        prompt_file: Path | None = None,
                        variant: str | None = None) -> list[str]:
    """Build the compile client argv for *adapter*.

    For OpenCode, a resolved *variant* is appended as ``--variant
    <variant>`` immediately after ``--model <model>``; when *variant* is
    ``None`` the flag is omitted entirely so the client's built-in default
    applies. Claude Code has no reasoning-variant flag, so *variant* is
    always ignored for it.

    Raises ``PlanError`` when the adapter is unsupported for compilation.
    """
    entry = COMPILE_CLIENTS.get(adapter)
    if entry is None:
        raise base.PlanError(f"unknown adapter '{adapter}'; "
                        f"known adapters: {', '.join(sorted(COMPILE_CLIENTS))}")
    if not entry.get("supported", False):
        raise base.PlanError(
            f"compilation through the {adapter} adapter is not supported "
            f"in this release; select a supported adapter "
            f"({'opencode'} or {'claude-code'})"
        )
    executable = entry["executable"]
    if adapter == "opencode" and prompt_file is not None:
        argv = [
            executable, "run", "--model", model,
            "Follow the complete compile instructions in the attached file. Output only TOML.",
            "--file", str(prompt_file),
        ]
        if variant:
            model_index = argv.index("--model")
            argv[model_index + 2:model_index + 2] = ["--variant", variant]
        return argv

    # Use a template-style argv construction so we compose the full command
    # from the registry without relying on a shared argv template format.
    tmpl = entry["argv_template"]
    # Replace {executable} first so later replacements do not interfere.
    argv: list[str] = []
    for part in tmpl:
        argv.append(
            part.replace("{executable}", executable)
                .replace("{model}", model)
                .replace("{prompt}", prompt)
        )
    if adapter == "opencode" and variant:
        model_index = argv.index("--model")
        argv[model_index + 2:model_index + 2] = ["--variant", variant]
    return argv


def _repo_compile_prompt_dir(repo: Path) -> Path:
    """Return the workspace-local directory for compile prompt files.

    OpenCode resolves ``--file`` attachments through its permission system,
    which auto-rejects reads of files outside the working tree
    (``external_directory`` — e.g. the system ``/tmp``).  The compile prompt
    file must therefore live inside the repository so the sandbox can read it
    without prompting.  It is stored under ``.opsx-plan/compile/``; the
    ``.opsx-plan/`` root is self-ignored by git, so a leftover prompt file
    never pollutes the working tree.
    """
    dot_dir = repo / ".opsx-plan"
    dot_dir.mkdir(parents=True, exist_ok=True)
    gi = dot_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    prompt_dir = dot_dir / "compile"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    return prompt_dir


def run_compile_client(repo: Path, adapter: str, model: str,
                       prompt: str,
                       variant: str | None = None,
                       timeout_minutes: float = 10.0) -> tuple[str, str]:
    """Invoke the selected compile client non-interactively.

    *variant* is the optional controller reasoning variant. OpenCode
    appends ``--variant <variant>`` to its invocation when set; Claude
    Code ignores it.

    The prompt is delivered according to the adapter's
    ``prompt_transport``: ``"file"`` writes it to a workspace-local file
    attached via ``--file`` (OpenCode, unchanged), ``"stdin"`` pipes it
    through the client's standard input (Claude Code) so prompt size is
    never limited by the OS argument-list limit.  After argv construction
    a pre-spawn guard rejects any single argv element over
    ``MAX_INLINE_ARG_CHARS`` with an adapter-naming ``PlanError`` instead
    of an opaque OS error.

    *timeout_minutes* bounds the client invocation (default 10.0 minutes,
    preserving the historical 600-second timeout); a timeout failure names
    the ``--timeout-minutes`` option in its diagnostic.

    Returns ``(stdout, stderr)`` as a tuple.  Raises ``PlanError`` on
    spawn failure, timeout, or unsupported adapter.
    """
    entry = COMPILE_CLIENTS[adapter]
    executable = entry["executable"]
    transport = entry.get("prompt_transport", "file")
    prompt_file: Path | None = None
    prompt_dir: Path | None = None
    if transport == "file":
        # OpenCode receives attached files as prompt parts. Keeping the full
        # prompt out of argv avoids the OS argument-size limit for large plans.
        # The file is written inside the workspace (not /tmp) because opencode
        # auto-rejects external_directory reads of the attachment.
        prompt_dir = _repo_compile_prompt_dir(repo)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="opsx-compile-", suffix=".md",
            dir=str(prompt_dir), delete=False,
        ) as handle:
            handle.write(prompt)
            prompt_file = Path(handle.name)

    argv = _build_compile_argv(adapter, model, prompt, prompt_file, variant)
    try:
        # Pre-spawn guard: no single argv element may carry an oversized
        # inline prompt (would surface as an opaque OS "Argument list too
        # long" error after spawn).
        for element in argv:
            if len(element) > MAX_INLINE_ARG_CHARS:
                raise base.PlanError(
                    f"compile prompt is too large for argv delivery to the "
                    f"{adapter} adapter client ({executable}): an argv "
                    f"element is {len(element)} chars, over the "
                    f"{MAX_INLINE_ARG_CHARS} char inline-argument limit"
                )
        timeout_s = timeout_minutes * 60
        run_kwargs = {
            "cwd": repo,
            "capture_output": True,
            "text": True,
            "timeout": timeout_s,
        }
        if transport == "stdin":
            run_kwargs["input"] = prompt
        proc = subprocess.run(argv, **run_kwargs)
    except FileNotFoundError:
        raise base.PlanError(
            f"could not spawn {executable}; is it installed and on PATH?"
        )
    except OSError as exc:
        raise base.PlanError(f"could not spawn {executable}: {exc}") from exc
    except subprocess.TimeoutExpired:
        raise base.PlanError(
            f"{executable} compile invocation timed out after "
            f"{timeout_s:g}s (--timeout-minutes {timeout_minutes:g})"
        )
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)
            assert prompt_dir is not None
            try:
                prompt_dir.rmdir()
            except OSError:
                pass
    if proc.returncode != 0:
        raise base.PlanError(
            f"{executable} exited with code {proc.returncode}\n"
            f"stderr: {proc.stderr[:500]}"
        )
    return proc.stdout, proc.stderr


def _strip_claude_envelope(output: str) -> str:
    """Strip one known Claude CLI result envelope, if present.

    Claude ``-p`` output may wrap the actual response in one of a small set
    of stable formats.  This function removes exactly one recognised envelope
    so the remaining payload can be passed to the standard TOML extractor.

    Returns the input unchanged when no known envelope is detected.
    """
    stripped = output.strip()

    # JSON result envelope: {"result": "...", ...}
    # Claude may return a JSON object with a "result" key containing the
    # actual model output.  Remove exactly one such outer wrapper.
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            if isinstance(data, dict) and "result" in data:
                inner = data["result"]
                if isinstance(inner, str) and inner.strip():
                    return inner
    return output


def extract_toml(output: str, adapter: str = "opencode") -> str:
    """Extract a TOML payload from raw model output.

    Accepts a single fenced ``toml`` block — possibly surrounded by
    short model preamble / trailing prose, which is logged and ignored —
    or a bare TOML payload.  Raises ``PlanError`` for ambiguous output:
    multiple fenced blocks, or no TOML content at all.

    *adapter* is used in error messages and may trigger client-specific
    envelope handling.
    """
    if adapter == "claude-code":
        output = _strip_claude_envelope(output)

    stripped = output.strip()
    if not stripped:
        client = COMPILE_CLIENTS.get(adapter, {}).get("executable", adapter)
        raise base.PlanError(f"{client} returned empty output; no TOML to compile")

    fenced_matches = list(re.finditer(r"```(?:toml)?\s*\n(.*?)```", stripped, re.DOTALL))
    if len(fenced_matches) > 1:
        raise base.PlanError(
            "ambiguous model output: multiple fenced TOML blocks found; "
            "expected a single clean TOML payload"
        )
    if len(fenced_matches) == 1:
        match = fenced_matches[0]
        before = stripped[:match.start()].strip()
        after = stripped[match.end():].strip()
        if before or after:
            base.log(
                f"  ignoring {len(before.splitlines())} leading and "
                f"{len(after.splitlines())} trailing prose line(s) around "
                "the fenced TOML payload"
            )
        return match.group(1).strip()

    if "[" in stripped:
        return stripped

    client = COMPILE_CLIENTS.get(adapter, {}).get("executable", adapter)
    raise base.PlanError(
        f"could not extract TOML from {client} output; "
        "output does not contain a fenced toml block or bare TOML"
    )
