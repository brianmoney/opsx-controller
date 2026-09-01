"""``opsx-plan archive-plan`` command handler.

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint, together
with the private ``_git_mv_or_rename`` helper only it uses (design D2).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from lib.orchestrator import planref


def cmd_archive_plan(args: argparse.Namespace) -> int:
    """opsx-plan archive-plan <manifest> — archive a plan manifest pair."""
    repo = Path(args.repo).resolve()
    plan_arg = args.plan

    # Resolve to absolute path relative to repo
    plan_path = (repo / plan_arg).resolve()
    if not plan_path.is_file():
        print(f"error: manifest not found: {plan_arg}", file=sys.stderr)
        return 2

    # Refuse targets already under archived/
    try:
        plan_rel = str(plan_path.relative_to(repo))
    except ValueError:
        print(f"error: manifest must be inside the repository: {plan_path}", file=sys.stderr)
        return 2

    if "openspec/plans/archived/" in plan_rel:
        print(f"error: manifest is already under openspec/plans/archived/: {plan_rel}", file=sys.stderr)
        return 2

    if not plan_rel.startswith("openspec/plans/"):
        print(f"error: manifest must be under openspec/plans/: {plan_rel}", file=sys.stderr)
        return 2

    # Determine the sibling .md path
    md_path = plan_path.with_suffix(".md")
    has_md = md_path.is_file()

    archived_dir = repo / "openspec" / "plans" / "archived"
    archived_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []

    # Move the .toml
    toml_dst = archived_dir / plan_path.name
    moved.append(str(plan_path.relative_to(repo)))
    _git_mv_or_rename(repo, plan_path, toml_dst)

    # Move the sibling .md when present
    if has_md:
        md_dst = archived_dir / md_path.name
        moved.append(str(md_path.relative_to(repo)))
        _git_mv_or_rename(repo, md_path, md_dst)

    # Clear the active-plan pointer when it referenced the archived plan
    active = planref.read_active_plan(repo)
    if active == plan_rel:
        pointer = planref.active_plan_pointer_path(repo)
        if pointer.is_file():
            pointer.unlink()

    print(f"Archived:")
    for path in moved:
        print(f"  {path} -> openspec/plans/archived/{Path(path).name}")
    if active == plan_rel:
        print(f"  Cleared active-plan pointer (was: {plan_rel})")
    print(f"  Move still needs committing; archive-plan does not create a commit.")
    return 0


def _git_mv_or_rename(repo: Path, src: Path, dst: Path) -> None:
    """Move *src* to *dst* using ``git mv`` for tracked files, plain rename otherwise."""
    try:
        rel_src = str(src.relative_to(repo))
    except ValueError:
        rel_src = str(src)
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel_src],
        cwd=repo, capture_output=True, text=True,
    )
    if res.returncode == 0:
        subprocess.run(
            ["git", "mv", rel_src, str(dst.relative_to(repo))],
            cwd=repo, check=True, capture_output=True,
        )
    else:
        os.rename(src, dst)
