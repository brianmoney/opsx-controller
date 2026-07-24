#!/usr/bin/env python3
"""Audit an opsx-plan TOML manifest before running it.

This checks two things that ``opsx-plan status`` does not, and that are easy to
get wrong when authoring a manifest by hand:

1. **Unknown keys.** ``load_plan()`` reads an explicit allowlist of keys and
   drops everything else with no warning and no error. A manifest can therefore
   configure behavior the controller does not implement, load clean, and read
   as "configured and working" in review. ``escalate_after_review_fails`` is the
   known instance of this.

2. **Change-id reality.** The manifest drives *unarchived* OpenSpec changes. An
   id that is already archived is finished work and does not belong in the
   graph. An id with no directory at all is only valid when the plan has a
   ``create_invoke`` authoring stage to produce it.

Graph validation (duplicate ids, unknown dependencies, cycles) is already done
by ``load_plan()``, so run ``opsx-plan status <manifest>`` for that rather than
duplicating it here.

Exit codes: 0 clean, 1 findings, 2 usage/IO error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        print(
            "error: needs Python 3.11+ for tomllib, or `pip install tomli`",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

ARCHIVE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<cid>.+)$")

# Keys that `opsx-plan compile` writes as human-facing documentation. The loader
# ignores these too, but they are inert by design, so they are summarized rather
# than reported one by one -- otherwise they bury the keys that look behavioral
# and silently do nothing, which is the finding that actually matters.
DECORATIVE_PLAN_KEYS = {
    "title", "doc_type", "status", "owner", "updated", "source", "purpose",
    "planning_principles", "current_risks", "phase_count", "change_count",
    "schema_version",
}
DECORATIVE_CHANGE_KEYS = {
    "purpose", "scope", "out_of_scope", "capabilities", "success_parameters",
    "review", "pause_reason", "proposed_capability", "rationale", "title",
}


def find_controller(explicit: str | None) -> Path:
    """Locate orchestrator/opsx-plan.py, which is the schema's source of truth."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("KF_OPSX_CONTROLLER") or os.environ.get("OPSX_CONTROLLER")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / "opsx-controller")

    for base in candidates:
        base = base.expanduser()
        direct = base if base.name == "opsx-plan.py" else base / "orchestrator" / "opsx-plan.py"
        if direct.is_file():
            return direct
    raise SystemExit(
        "error: could not find orchestrator/opsx-plan.py. Pass --controller "
        "or set KF_OPSX_CONTROLLER."
    )


def _function_source(source: str, name: str) -> str:
    """Slice one top-level function body out of the module text.

    Text slicing rather than import: loading the controller module would execute
    it, and this only needs to read which keys the loader looks up.
    """
    start = source.find(f"def {name}(")
    if start == -1:
        return ""
    rest = source[start:]
    # End at the next top-level def/class, which is column-zero in this module.
    end = re.search(r"\n(?=(?:def |class )\w)", rest)
    return rest[: end.start()] if end else rest


def known_keys(controller_py: Path) -> tuple[set[str], set[str], set[str]]:
    """Extract the key allowlists the loader actually reads.

    Derived from source rather than hardcoded so this tracks whatever controller
    version is installed instead of drifting from it.
    """
    source = controller_py.read_text(encoding="utf-8")
    load_plan = _function_source(source, "load_plan")
    git_delivery = _function_source(source, "_parse_git_delivery_config")

    if not load_plan:
        raise SystemExit(f"error: could not locate load_plan() in {controller_py}")

    plan_keys = set(re.findall(r'plan\.get\(\s*["\'](\w+)["\']', load_plan))
    change_keys = set(re.findall(r'c\.get\(\s*["\'](\w+)["\']', load_plan))
    delivery_keys = set(re.findall(r'raw\.get\(\s*["\'](\w+)["\']', git_delivery))

    # `git_delivery` is consumed as a nested table, not a scalar plan key.
    plan_keys.add("git_delivery")
    return plan_keys, change_keys, delivery_keys


def classify_change(repo: Path, cid: str) -> str:
    """Return 'active', 'archived', or 'missing' for one change id."""
    changes = repo / "openspec" / "changes"
    if (changes / cid).is_dir():
        return "active"
    archive = changes / "archive"
    if archive.is_dir():
        for entry in archive.iterdir():
            match = ARCHIVE_DIR_RE.match(entry.name)
            if entry.is_dir() and match and match.group("cid") == cid:
                return "archived"
    return "missing"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("manifest", help="path to the plan TOML")
    parser.add_argument("--repo", default=".", help="repo root holding openspec/ (default: cwd)")
    parser.add_argument("--controller", default=None, help="opsx-controller checkout or opsx-plan.py path")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).expanduser()
    repo = Path(args.repo).expanduser().resolve()
    if not manifest_path.is_file():
        print(f"error: no such manifest: {manifest_path}", file=sys.stderr)
        return 2

    try:
        raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
        print(f"error: {manifest_path} is not valid TOML: {exc}", file=sys.stderr)
        return 2

    controller_py = find_controller(args.controller)
    plan_keys, change_keys, delivery_keys = known_keys(controller_py)

    findings: list[str] = []
    decorative: list[str] = []
    plan = raw.get("plan", {})

    for key in sorted(set(plan) - plan_keys):
        if key in DECORATIVE_PLAN_KEYS:
            decorative.append(f"[plan] {key}")
            continue
        findings.append(
            f"[plan] unknown key {key!r} is silently ignored by load_plan(); "
            f"remove it or implement it in the controller"
        )

    delivery = plan.get("git_delivery", {})
    if isinstance(delivery, dict):
        for key in sorted(set(delivery) - delivery_keys):
            findings.append(f"[plan.git_delivery] unknown key {key!r} is silently ignored")

    changes = raw.get("changes", [])
    if not isinstance(changes, list) or not changes:
        findings.append("no [[changes]] entries: the manifest would drive nothing")
        changes = []

    has_create = bool(str(plan.get("create_invoke", "")).strip())

    for entry in changes:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        for key in sorted(set(entry) - change_keys):
            if key in DECORATIVE_CHANGE_KEYS:
                decorative.append(f"[[changes]] {cid or '<no id>'}.{key}")
                continue
            findings.append(f"[[changes]] {cid or '<no id>'}: unknown key {key!r} is silently ignored")
        if not cid:
            findings.append("[[changes]] entry is missing 'id'")
            continue

        state = classify_change(repo, cid)
        enabled = entry.get("enabled", True)
        if state == "archived":
            findings.append(
                f"{cid}: already archived in openspec/changes/archive/ -- finished work "
                f"does not belong in the graph; remove the entry"
            )
        elif state == "missing" and not (has_create or str(entry.get("create_invoke", "")).strip()):
            level = "note" if not enabled else "error"
            findings.append(
                f"{cid}: no openspec/changes/{cid}/ and no create_invoke to author it "
                f"({level}: the plan cannot drive a change that does not exist)"
            )

    label = f"{manifest_path} against {repo}"

    def print_decorative() -> None:
        if not decorative:
            return
        print(f"\n  {len(decorative)} inert documentation key(s) ignored by the loader (harmless):")
        preview = ", ".join(decorative[:8])
        suffix = ", ..." if len(decorative) > 8 else ""
        print(f"    {preview}{suffix}")

    if findings:
        print(f"audit: {len(findings)} finding(s) in {label}\n")
        for item in findings:
            print(f"  - {item}")
        print_decorative()
        return 1

    print(f"audit: clean -- {label}")
    print(f"  schema source: {controller_py}")
    print(f"  {len(changes)} change(s), all reconciled against openspec/")
    print_decorative()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
