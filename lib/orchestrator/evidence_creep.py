"""Evidence-creep advisory for ``opsx-plan doctor``.

An evidence obligation is self-justifying: proving the claim needs a harness,
the harness needs a binding, the binding needs a manifest, the manifest needs an
auditor. Nothing in that chain is bounded by the product's value, so one
evidence parameter can cost more than the feature it was meant to validate. It
is also unfalsifiable at authoring time -- the plan cannot know whether the
observation is even obtainable.

This module scans a markdown plan's ``Success parameters`` blocks and reports
parameters that assert the existence, integrity, or provenance of evidence
rather than the behavior of the change. It is ADVISORY: it never fails a run
and never blocks a plan. ``core/plan-authoring.md`` carries the normative rule;
this is the cheap mechanical nudge that surfaces it at authoring time.
"""

from __future__ import annotations

import re
from pathlib import Path

# Phrases that mark a parameter as being about evidence rather than behavior.
# Deliberately conservative: each pattern below appeared in a real plan that
# consumed days of run time proving a claim instead of shipping the feature.
_EVIDENCE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d+\s+screenshots?\b", re.I), "screenshot count"),
    (re.compile(r"\bscreenshot set\b", re.I), "screenshot set"),
    (re.compile(r"\bevidence packet\b", re.I), "evidence packet"),
    (re.compile(r"\btraceability ledger\b", re.I), "traceability ledger"),
    (re.compile(r"\bmanifest\b", re.I), "manifest"),
    (re.compile(r"\bsha-?256\b", re.I), "hash binding"),
    (re.compile(r"\breconciled (runtime|source|binding)", re.I), "runtime binding"),
    (re.compile(r"\bfrozen (before|inputs?|bytes)", re.I), "frozen inputs"),
    (re.compile(r"\bno .{0,20}retagg", re.I), "no-retagging clause"),
    (re.compile(r"\bexact[- ]HEAD\b", re.I), "exact-HEAD binding"),
    (re.compile(r"\bp9[059]\b", re.I), "p95/p99 measurement"),
    (re.compile(r"\bbenchmark\b", re.I), "benchmark"),
    (re.compile(r"\b\d[\d,]{2,}\s+requests?\b", re.I), "request-count campaign"),
    (re.compile(r"\blifecycle (manifest|binding)\b", re.I), "lifecycle binding"),
    (re.compile(r"\bprovenance\b", re.I), "provenance"),
)

# A parameter may legitimately name a behavior whose subject is one of these
# words, e.g. "the static asset manifest is served". Requiring a proof shape
# (a count, a binding verb, an integrity verb) keeps those out of the report.
_BEHAVIOR_ESCAPE = re.compile(
    r"\b(renders?|serves?|returns?|rejects?|accepts?|displays?|lists?|"
    r"emits?|refuses?|is absent|is present)\b",
    re.I,
)

_PARAM_SPLIT = re.compile(r"(?:^|\n|;)\s*\(?(\d{1,2})[.)]\s+")
_PARAM_BLOCK = re.compile(
    r"\*\*Success parameters:\*\*(.*?)(?=\n\s*\*\*[A-Z]|\n#{2,}|\n\s*\n|\Z)", re.S
)


def _params(block: str) -> list[str]:
    """Split a success-parameters block into individual parameter strings."""
    pieces = _PARAM_SPLIT.split(block)
    # With a capturing split the shape is [pre, num, text, num, text, ...].
    params = [pieces[i] for i in range(2, len(pieces), 2)]
    if not params:
        single = block.strip()
        if single:
            params = [single]
    return params


def scan_plan_text(text: str) -> list[tuple[str, str, str]]:
    """Return ``(change, marker, parameter_excerpt)`` for each suspected hit."""
    hits: list[tuple[str, str, str]] = []
    change = "(plan)"
    # Walk headings so each hit is attributed to its change.
    for raw_line in text.splitlines():
        m = re.match(r"^###\s+Change:\s*`([^`]+)`", raw_line)
        if m:
            change = m.group(1)

    for block in _PARAM_BLOCK.finditer(text):
        body = block.group(1)
        # Attribute the block to the nearest preceding change heading.
        preceding = text[: block.start()]
        names = re.findall(r"^###\s+Change:\s*`([^`]+)`", preceding, re.M)
        block_change = names[-1] if names else change
        for param in _params(body):
            flat = " ".join(param.split())
            if not flat:
                continue
            matched = [label for pat, label in _EVIDENCE_PATTERNS if pat.search(flat)]
            if not matched:
                continue
            # A behavior verb plus no integrity verb is a product claim.
            if _BEHAVIOR_ESCAPE.search(flat) and not re.search(
                r"\b(bind|bound|hash|frozen|reconcile|retag|manifest|provenance)\b",
                flat,
                re.I,
            ):
                continue
            hits.append((block_change, matched[0], flat[:140]))
    return hits


def _resolve_markdown_plan(repo: Path, plan_src: str | None) -> Path | None:
    """Find the markdown plan a ``plan_src`` refers to.

    ``opsx-plan`` resolves plans to the TOML manifest, but the evidence rule is
    about the authored markdown's success parameters. Accept either: use the
    markdown directly, or the ``.md`` sibling of a ``.toml`` manifest.
    """
    if not plan_src:
        return None
    path = Path(plan_src)
    if not path.is_absolute():
        path = repo / path
    if path.suffix == ".md":
        return path if path.is_file() else None
    candidate = path.with_suffix(".md")
    if candidate.is_file():
        return candidate
    # Archived plans live beside their toml; nothing to scan if absent.
    return None


def check_evidence_creep(repo: Path, plan_src: str | None) -> tuple[bool, str, str]:
    """Doctor check: advise when success parameters are evidence obligations."""
    label = "Plan success parameters avoid evidence obligations"
    md = _resolve_markdown_plan(repo, plan_src)
    if md is None:
        return True, label, ""

    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return True, label, ""

    hits = scan_plan_text(text)
    if not hits:
        return True, label, ""

    by_change: dict[str, list[tuple[str, str]]] = {}
    for change, marker, excerpt in hits:
        by_change.setdefault(change, []).append((marker, excerpt))

    total = len(hits)
    summary = ", ".join(f"{c} ({len(v)})" for c, v in by_change.items())
    remediation = (
        f"ADVISORY: {total} success parameter(s) look like evidence obligations "
        f"in {summary}.\n"
        "    A success parameter asserts behavior or a gate result, never the "
        "existence, integrity,\n"
        "    or provenance of evidence produced to prove another parameter. "
        "Every hit above costs run time\n"
        "    and can be impossible to discharge. See core/plan-authoring.md -> "
        "*Executable Success Parameters*.\n"
        "    Detail:\n"
        + "\n".join(
            f"      - {change}: [{marker}] {excerpt}"
            for change, marker, excerpt in hits[:8]
        )
        + (f"\n      ... and {total - 8} more" if total > 8 else "")
    )
    # Advisory: return True so the check never fails a doctor run.
    return True, f"Plan success parameters avoid evidence obligations ({total} advisory)", remediation
