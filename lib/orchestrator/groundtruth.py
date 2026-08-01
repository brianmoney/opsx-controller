"""Ground-truth verification: git evidence for change creation and archival.

Independent evidence for whether a change was actually created or archived,
rather than trusting a worker or controller exit code.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from lib.orchestrator import base


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )


def read_controller_state(repo: Path, cfg: dict, cid: str) -> dict | None:
    p = repo / cfg["state_file"].format(change=cid)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"_malformed": True}


def find_archive_dir(repo: Path, cid: str) -> Path | None:
    root = repo / "openspec" / "changes" / "archive"
    if not root.is_dir():
        return None
    for entry in sorted(root.iterdir(), reverse=True):
        if entry.is_dir() and entry.name.endswith(f"-{cid}") and base.ARCHIVE_DIR_RE.match(
            entry.name
        ):
            return entry
    return None


def find_archive_commit(repo: Path, cid: str) -> str:
    res = git(
        repo, "log", "--fixed-strings", f"--grep=archive({cid}):",
        "--format=%H", "-n", "1",
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def archive_dir_ignored(repo: Path) -> bool:
    """True when `openspec/changes/archive/` is gitignored in this repo.

    Decides whether an `archive(<id>):` commit is required evidence. When the
    archive directory is ignored, `openspec archive` moves files git will never
    track, so the archive worker has nothing to stage and legitimately produces
    no commit — its absence is expected, not a failure. When the directory is
    tracked (the default OpenSpec layout), a missing commit means the archive
    was never durably recorded, which must fail the change.
    """
    # --no-index answers purely from the ignore rules. Without it git consults
    # the index and refuses to call a path ignored once anything under it is
    # tracked (e.g. a force-added legacy archive), which would flip the gate
    # even though newly archived files still stage nothing.
    # The trailing slash matters: a directory-only ignore rule
    # (`openspec/changes/archive/`) does not match the bare path when the
    # directory does not exist on disk yet.
    res = git(repo, "check-ignore", "--no-index", "-q", "openspec/changes/archive/")
    return res.returncode == 0


def verify_change_done(repo: Path, cfg: dict, cid: str) -> tuple[bool, str]:
    """A change is done when OpenSpec has archived it.

    Authoritative evidence (required): the change has left openspec/changes/
    and now lives under a dated openspec/changes/archive/ directory. That move
    is exactly what `openspec archive` produces atomically, and is the ground
    truth of completion.

    Corroborating signals (warn, never veto): an `archive(<id>):` commit
    reachable from HEAD, and the controller state file agreeing
    (completed/done/passed). These go stale when a change was archived by hand,
    squashed into another commit, or recovered after a drive error, so they are
    surfaced as notes but no longer fail a change that OpenSpec itself archived.
    """
    cdir = change_dir(repo, cid)
    if cdir.exists():
        return False, f"{cdir.relative_to(repo)} still exists"

    if find_archive_dir(repo, cid) is None:
        return False, "no dated archive directory found"

    warnings: list[str] = []
    if not find_archive_commit(repo, cid):
        warnings.append("no archive(<id>): commit (archived manually or squashed?)")

    cs = read_controller_state(repo, cfg, cid)
    if cs is not None:
        if cs.get("_malformed"):
            warnings.append("controller state file is malformed JSON")
        else:
            if cs.get("status") != "completed" or cs.get("phase") != "done":
                warnings.append(
                    f"controller state stale: status={cs.get('status')} "
                    f"phase={cs.get('phase')}"
                )
            arch = cs.get("archive", {})
            if arch.get("status") != "passed" or not arch.get("commit"):
                warnings.append("controller archive state not passed")

    if warnings:
        base.log(f"  note: {cid} archived but {'; '.join(warnings)}")

    return True, ""


def run_fast_checks(repo: Path, cfg: dict) -> tuple[bool, str]:
    timeout = cfg["check_timeout_minutes"] * 60
    for cmd in cfg["fast_checks"]:
        base.log(f"  check: {cmd}")
        try:
            res = subprocess.run(
                shlex.split(cmd), cwd=repo, capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"check timed out: {cmd}"
        except FileNotFoundError as exc:
            return False, f"check command not found: {exc}"
        if res.returncode != 0:
            tail = (res.stdout + res.stderr).strip().splitlines()[-5:]
            return False, f"check failed: {cmd} :: " + " | ".join(tail)
    return True, ""


def change_dir(repo: Path, cid: str) -> Path:
    direct = repo / "openspec" / "changes" / cid
    if direct.is_dir():
        return direct
    changes_dir = repo / "openspec" / "changes"
    if changes_dir.is_dir():
        for entry in sorted(changes_dir.iterdir(), reverse=True):
            if entry.is_dir() and entry.name.endswith(f"-{cid}"):
                return entry
    return direct


AUTHORED_ARTIFACTS = ("proposal.md", "tasks.md")


def change_authored(repo: Path, cid: str) -> bool:
    """True only when the change dir holds the required artifacts, not just a
    bare `openspec new change` scaffold (`.openspec.yaml`). The scheduler uses
    this — instead of mere directory existence — to decide whether a change
    still needs authoring, so a half-written scaffold no longer silently skips
    the create stage and gets driven as if complete."""
    cdir = change_dir(repo, cid)
    return cdir.is_dir() and all((cdir / a).is_file() for a in AUTHORED_ARTIFACTS)


def scaffold_is_clearable(repo: Path, cid: str) -> bool:
    """True when a change dir is a pure, untracked scaffold the orchestrator may
    remove before re-creating: it exists, holds no authored markdown, and has no
    tracked files. Any `.md` content or any tracked file makes it unsafe to
    delete (it may carry hand-written work), so we refuse and ask the operator."""
    cdir = change_dir(repo, cid)
    if not cdir.is_dir():
        return False
    if any(p.is_file() for p in cdir.rglob("*.md")):
        return False
    tracked = git(repo, "ls-files", "--", str(cdir.relative_to(repo)))
    return not tracked.stdout.strip()


def verify_change_created(
    repo: Path,
    cfg: dict,
    cid: str,
    before_tracked: tuple[str, str, str] | None = None,
) -> tuple[bool, str]:
    """A change counts as created only when independent evidence agrees:
    1. openspec/changes/<id> exists with proposal.md and tasks.md
    2. the configured created_check command (default
       `openspec validate <id> --strict`) exits 0
    3. when a pre-create snapshot is provided, creation touched no tracked files
       (change authoring is additive)
    """
    reasons: list[str] = []
    cdir = change_dir(repo, cid)
    if not cdir.is_dir():
        return False, f"openspec/changes/{cid} does not exist"
    for artifact in ("proposal.md", "tasks.md"):
        if not (cdir / artifact).is_file():
            reasons.append(f"missing {artifact}")

    check = cfg["created_check"].format(change=cdir.name)
    if check.strip():
        try:
            res = subprocess.run(
                shlex.split(check), cwd=repo, capture_output=True, text=True,
                timeout=cfg["check_timeout_minutes"] * 60,
            )
            if res.returncode != 0:
                tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
                reasons.append(f"created_check failed: " + " | ".join(tail))
        except subprocess.TimeoutExpired:
            reasons.append(f"created_check timed out: {check}")
        except FileNotFoundError as exc:
            reasons.append(f"created_check command not found: {exc}")

    if before_tracked is not None and tracked_worktree_snapshot(repo) != before_tracked:
        reasons.append(
            "creation modified tracked files; change authoring must be "
            "additive (review with `git status` before continuing)"
        )
    return (not reasons, "; ".join(reasons))


def tracked_worktree_snapshot(repo: Path) -> tuple[str, str, str]:
    """Return tracked-file state, excluding untracked files.

    Creation runs are allowed to add untracked OpenSpec files, but must not alter
    tracked files. Capturing the full tracked diff before and after the create
    stage avoids falsely failing when unrelated tracked edits already existed.
    """
    pieces: list[str] = []
    for args in (
        ("status", "--porcelain", "--untracked-files=no"),
        ("diff", "--no-ext-diff", "--binary"),
        ("diff", "--cached", "--no-ext-diff", "--binary"),
    ):
        res = git(repo, *args)
        if res.returncode != 0:
            pieces.append(f"git {' '.join(args)} failed: {res.stderr}")
        else:
            pieces.append(res.stdout)
    return pieces[0], pieces[1], pieces[2]


def tracked_tree_clean(repo: Path) -> bool:
    res = git(repo, "status", "--porcelain", "--untracked-files=no")
    if res.returncode != 0:
        return False
    lines = [
        ln for ln in res.stdout.splitlines()
        if ln.strip() and not ln[3:].startswith(".opsx-plan/")
    ]
    return not lines
