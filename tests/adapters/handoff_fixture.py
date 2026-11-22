"""Throwaway git fixture for the plan-level Phase Transition Gate.

The plan-level orchestrator delegates the handoff audit to a read-only agent
(`opsx-integration-auditor`) and any repair to a scoped writer
(`opsx-repo-hygiene`). Both are prompt surfaces, so a unit test cannot dispatch
them directly. This module models the *protocol* they implement against a real
temporary git repository, so the tests can assert the observable contract:

* an archive-only commit does not satisfy the gate;
* the audit is read-only and machine-readable;
* an explicit-pathspec commit repairs the handoff and never stages a
  user-owned file;
* the next change is selected only after ``handoff_ready: true``.

Nothing here ships to an adapter. It exists so the fixture is deterministic,
stdlib-only, and exercises real git behavior instead of mocked state.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CHANGE = "sample-change"
NEXT_CHANGE = "next-change"
ARCHIVE_DATE = "2026-11-22"
ARCHIVED_CHANGE_REL = f"openspec/changes/archive/{ARCHIVE_DATE}-{CHANGE}"

# Implementation files that exist only in the working tree when the fixture is
# built: the exact failure the gate exists to catch.
IMPLEMENTATION_PATHS = (
    "src/api/cache.py",
    "src/api/handlers.py",
)
PLAN_PATH = "docs/plans/sample-plan.md"
PERF_SCRIPT_PATH = "scripts/perf-check.sh"
FOCUSED_CHECK_PATH = "scripts/focused-check.sh"
USER_OWNED_PATH = "notes/local.md"
RUN_STATE_DIR = ".opsx-run/2026-11-22-sample-plan"
MANIFEST_PATH = f"{RUN_STATE_DIR}/acceptance.json"
CHECKPOINT_PATH = f"{RUN_STATE_DIR}/checkpoint.md"

#: The perf script's committed default, which points at the pre-archive change
#: directory and therefore recreates an archived change path when run at HEAD.
PERF_SCRIPT_BODY = """#!/usr/bin/env bash
# Retained benchmark entrypoint for the change.
set -eu
OUT="${1:-openspec/changes/sample-change/evidence}"
mkdir -p "$OUT"
printf 'perf ok\\n'
"""

#: The repaired default: retained evidence belongs in run-local state, not in
#: the archived change tree.
PERF_SCRIPT_REPAIRED_BODY = """#!/usr/bin/env bash
# Retained benchmark entrypoint for the change.
set -eu
OUT="${1:-.opsx-run/sample-perf/evidence}"
mkdir -p "$OUT"
printf 'perf ok\\n'
"""

FOCUSED_CHECK_BODY = """#!/usr/bin/env bash
# Focused check that must pass from a fresh checkout of HEAD.
set -eu
test -f openspec/specs/sample-capability/spec.md
printf 'focused ok\\n'
"""


@dataclass
class Fixture:
    """A built fixture repository and the paths the tests assert on."""

    root: Path
    run_id: str = "2026-11-22-sample-plan"
    change: str = CHANGE
    next_change: str = NEXT_CHANGE
    accepted: tuple[str, ...] = IMPLEMENTATION_PATHS
    user_owned: tuple[str, ...] = (USER_OWNED_PATH,)
    archive_commit: str = ""
    archive_implementation_paths: tuple[str, ...] = field(default_factory=tuple)


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> str:
    proc = _run(["git", *args], repo)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _git_bytes(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def _git_ok(repo: Path, *args: str) -> bool:
    return _run(["git", *args], repo).returncode == 0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def status_lines(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return [line for line in out.splitlines() if line.strip()]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(root: Path) -> Fixture:
    """Build the archive-only fixture repository under *root*.

    Resulting state, in order:

    1. a baseline commit with the change artifacts, the next change's intake
       proposal, the perf script, and a user-owned note;
    2. an archive-only commit that moves the change directory and syncs one
       benchmark spec, carrying zero implementation paths;
    3. a dirty working tree: accepted implementation and plan uncommitted, run
       state untracked and unignored, and the user-owned note modified.
    """

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    if _run(["git", "rev-parse", "--verify", "-q", "HEAD"], root).returncode != 0:
        # Unborn HEAD is expected; make the branch name deterministic anyway.
        _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Handoff Fixture")
    _git(root, "config", "commit.gpgsign", "false")

    # --- baseline commit -------------------------------------------------
    _write(
        root / "openspec/changes" / CHANGE / "proposal.md",
        f"# Proposal: {CHANGE}\n",
    )
    _write(
        root / "openspec/changes" / CHANGE / "tasks.md",
        "- [x] 1.1 done\n",
    )
    _write(
        root / "openspec/changes" / NEXT_CHANGE / "proposal.md",
        f"# Proposal: {NEXT_CHANGE}\n",
    )
    _write(root / PERF_SCRIPT_PATH, PERF_SCRIPT_BODY)
    _write(root / FOCUSED_CHECK_PATH, FOCUSED_CHECK_BODY)
    _write(root / USER_OWNED_PATH, "local notes\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feat: baseline fixture")

    # --- archive-only commit --------------------------------------------
    archive_dir = root / "openspec/changes/archive" / f"{ARCHIVE_DATE}-{CHANGE}"
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(
        root,
        "mv",
        f"openspec/changes/{CHANGE}",
        str(archive_dir.relative_to(root)),
    )
    _write(
        root / "openspec/specs/sample-capability/spec.md",
        "# sample-capability Specification\n\n## Purpose\n\nFixture capability.\n",
    )
    _git(root, "add", "-A")
    _git(
        root,
        "commit",
        "-q",
        "-m",
        f"archive({CHANGE}): archive completed OpenSpec change",
    )

    # --- dirty working tree left by the plan run -------------------------
    _write(
        root / "src/api/handlers.py",
        "def handle():\n    return {'status': 'ok'}\n",
    )
    _write(
        root / "src/api/cache.py",
        "CACHE = {}\n\n\ndef get(key):\n    return CACHE.get(key)\n",
    )
    _write(root / PLAN_PATH, "# Sample plan\n\n1. " + CHANGE + "\n2. " + NEXT_CHANGE + "\n")
    _write(
        root / CHECKPOINT_PATH,
        "stage: handoff\nchange: " + CHANGE + "\nhandoff_ready: unknown\n",
    )
    _write(root / USER_OWNED_PATH, "local notes, edited by the operator\n")

    archive_commit = head_sha(root)
    changed = [
        line
        for line in _git(
            root, "show", "--name-only", "--pretty=format:", archive_commit
        ).splitlines()
        if line.strip()
    ]
    implementation = tuple(
        path for path in changed if path in IMPLEMENTATION_PATHS
    )

    manifest = {
        "change": CHANGE,
        "run_id": "2026-11-22-sample-plan",
        "accepted": [
            {
                "path": path,
                "sha256": sha256_path(root / path),
            }
            for path in IMPLEMENTATION_PATHS
        ],
        "plan": {"path": PLAN_PATH, "sha256": sha256_path(root / PLAN_PATH)},
        "evidence": [{"path": "openspec/specs/sample-capability/spec.md"}],
        "no_push_waiver": "fixture repository has no remote; recorded by the operator",
    }
    _write(root / MANIFEST_PATH, json.dumps(manifest, indent=2) + "\n")

    return Fixture(
        root=root,
        archive_commit=archive_commit,
        archive_implementation_paths=implementation,
    )


def load_manifest(fixture: Fixture) -> dict:
    return json.loads((fixture.root / MANIFEST_PATH).read_text(encoding="utf-8"))


def _paths_from_status(fixture: Fixture) -> dict[str, str]:
    """Map porcelain path -> status code for the tracked+untracked tree."""

    result: dict[str, str] = {}
    for line in status_lines(fixture.root):
        code = line[:2].strip()
        path = line[2:].strip().strip('"')
        if " -> " in path:  # rename
            path = path.split(" -> ", 1)[1]
        result[path] = code
    return result


def _is_run_state(path: str, fixture: Fixture) -> bool:
    return path.startswith(".opsx-run/") or f"/{fixture.run_id}/" in path


def audit(fixture: Fixture) -> dict:
    """Model the auditor's read-only checks against the fixture repository.

    Returns the machine-readable handoff report documented in the change's
    spec: ``status``, ``handoff_ready``, ``change``, ``head``, ``checks[]``,
    ``blockers[]``, and ``next_action``.
    """

    repo = fixture.root
    manifest = load_manifest(fixture)
    head = head_sha(repo)
    checks: list[dict] = []
    blockers: list[dict] = []

    def check(
        check_id: str,
        result: str,
        command: str,
        evidence: str,
        sha: str = "",
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "result": result,
                "command": command,
                "evidence": evidence,
                "sha256": sha,
            }
        )

    # 1. tracked tree clean except explicitly allowed user-owned or ignored
    #    paths.
    dirty = _paths_from_status(fixture)
    allowed = set(fixture.user_owned)
    unclassified = sorted(
        path
        for path in dirty
        if path not in allowed and not _is_run_state(path, fixture)
    )
    if unclassified:
        check(
            "tracked-tree-clean",
            "failed",
            "git status --porcelain --untracked-files=all",
            ", ".join(unclassified),
        )
    else:
        check(
            "tracked-tree-clean",
            "passed",
            "git status --porcelain --untracked-files=all",
            "only allowed user-owned or ignored paths remain dirty",
            sha256_bytes("\n".join(status_lines(repo)).encode()),
        )

    # 2. every accepted source-manifest path exists at HEAD with the accepted
    #    content.
    missing_source: list[str] = []
    for entry in manifest["accepted"]:
        path = entry["path"]
        if not _git_ok(repo, "cat-file", "-e", f"HEAD:{path}"):
            missing_source.append(path)
            continue
        if sha256_bytes(_git_bytes(repo, "cat-file", "-p", f"HEAD:{path}")) != entry["sha256"]:
            missing_source.append(path)
    if missing_source:
        blockers.append(
            {
                "id": "implementation-uncommitted",
                "severity": "blocking",
                "location": ", ".join(missing_source),
                "evidence": "git cat-file -e HEAD:<path>",
                "repair": {
                    "action": "commit-pathspecs",
                    "pathspecs": missing_source,
                    "operator_decision": "none",
                },
            }
        )
        check(
            "source-manifest-at-head",
            "failed",
            "git cat-file -p HEAD:<path> | sha256sum",
            ", ".join(missing_source),
        )
    else:
        check(
            "source-manifest-at-head",
            "passed",
            "git cat-file -p HEAD:<path> | sha256sum",
            f"{len(manifest['accepted'])} accepted source path(s) verified at HEAD",
        )

    # 3. the plan the next change will read exists at HEAD.
    plan_path = manifest["plan"]["path"]
    plan_sha = manifest["plan"]["sha256"]
    if (
        not _git_ok(repo, "cat-file", "-e", f"HEAD:{plan_path}")
        or sha256_bytes(_git_bytes(repo, "cat-file", "-p", f"HEAD:{plan_path}"))
        != plan_sha
    ):
        blockers.append(
            {
                "id": "plan-uncommitted",
                "severity": "blocking",
                "location": plan_path,
                "evidence": f"git cat-file -e HEAD:{plan_path}",
                "repair": {
                    "action": "commit-pathspecs",
                    "pathspecs": [plan_path],
                    "operator_decision": "none",
                },
            }
        )
        check(
            "plan-at-head",
            "failed",
            f"git cat-file -e HEAD:{plan_path}",
            plan_path,
        )
    else:
        check(
            "plan-at-head",
            "passed",
            f"git cat-file -e HEAD:{plan_path}",
            plan_path,
            plan_sha,
        )

    # 4. the archive lineage contains the accepted implementation paths. The
    #    archive commit itself may carry them; when it does not, a later
    #    handoff repair commit on the same lineage may, because the requirement
    #    is that the implementation is reproducible from HEAD, not that one
    #    particular commit object holds it.
    changed: list[str] = []
    for sha in _git(
        repo, "rev-list", f"{fixture.archive_commit}..HEAD"
    ).split() + [fixture.archive_commit]:
        changed.extend(
            line.strip()
            for line in _git(
                repo, "show", "--name-only", "--pretty=format:", sha
            ).splitlines()
            if line.strip()
        )
    archive_changed = [
        line.strip()
        for line in _git(
            repo,
            "show",
            "--name-only",
            "--pretty=format:",
            fixture.archive_commit,
        ).splitlines()
        if line.strip()
    ]
    implementation_paths = [
        path for path in dict.fromkeys(changed) if path in fixture.accepted
    ]
    if not implementation_paths:
        blockers.append(
            {
                "id": "archive-commit-implementation-paths",
                "severity": "blocking",
                "location": fixture.archive_commit,
                "evidence": "implementation_paths: 0",
                "repair": {
                    "action": "commit-pathspecs",
                    "pathspecs": list(fixture.accepted),
                    "operator_decision": "authorize implementation commit",
                },
            }
        )
        check(
            "archive-commit-implementation-paths",
            "failed",
            "git show --name-only --pretty=format: " + fixture.archive_commit,
            "implementation_paths: 0",
        )
    else:
        in_archive_commit = [p for p in implementation_paths if p in archive_changed]
        evidence = (
            ", ".join(implementation_paths)
            if in_archive_commit
            else (
                "implementation_paths: 0 in the archive commit; landed in a "
                "post-archive handoff commit on the archive lineage: "
                + ", ".join(implementation_paths)
            )
        )
        check(
            "archive-commit-implementation-paths",
            "passed",
            "git show --name-only --pretty=format: " + fixture.archive_commit,
            evidence,
        )

    # 5. run-local state is ignored or outside the repository.
    if _git_ok(repo, "check-ignore", "-q", CHECKPOINT_PATH):
        check(
            "run-state-ignored",
            "passed",
            f"git check-ignore -q {CHECKPOINT_PATH}",
            ".opsx-run/ is ignored",
        )
    else:
        blockers.append(
            {
                "id": "run-state-not-ignored",
                "severity": "blocking",
                "location": ".opsx-run/",
                "evidence": f"git check-ignore -q {CHECKPOINT_PATH} exited 1",
                "repair": {
                    "action": "ignore-run-state",
                    "entry": ".opsx-run/",
                    "pathspecs": [".gitignore"],
                    "operator_decision": "none",
                },
            }
        )
        check(
            "run-state-ignored",
            "failed",
            f"git check-ignore -q {CHECKPOINT_PATH}",
            "untracked run state would dirty every checkout",
        )

    # 6. committed evidence manifests resolve at HEAD.
    dead_evidence = [
        entry["path"]
        for entry in manifest.get("evidence", [])
        if not _git_ok(repo, "cat-file", "-e", f"HEAD:{entry['path']}")
    ]
    if dead_evidence:
        check(
            "evidence-at-head",
            "unqualified",
            "git cat-file -e HEAD:<path>",
            ", ".join(dead_evidence),
        )
    else:
        check(
            "evidence-at-head",
            "passed",
            "git cat-file -e HEAD:<path>",
            f"{len(manifest.get('evidence', []))} evidence path(s) resolve at HEAD",
        )

    # 7. perf/evidence scripts run from HEAD without recreating an archived
    #    change directory. The static default-path check stands in for running
    #    the script, so the audit itself mutates nothing.
    script_at_head = _git_bytes(repo, "cat-file", "-p", f"HEAD:{PERF_SCRIPT_PATH}").decode(
        "utf-8"
    )
    recreated: list[str] = []
    for tail in script_at_head.split("openspec/changes/")[1:]:
        token = tail.split()[0].strip("\"'${}") if tail.split() else ""
        if not token or token.startswith("archive/"):
            continue
        candidate = f"openspec/changes/{token}"
        if candidate not in recreated:
            recreated.append(candidate)
    recreated.sort()
    if recreated:
        blockers.append(
            {
                "id": "perf-script-recreates-archived-change",
                "severity": "blocking",
                "location": PERF_SCRIPT_PATH,
                "evidence": ", ".join(recreated),
                "repair": {
                    "action": "retarget-script",
                    "path": PERF_SCRIPT_PATH,
                    "pathspecs": [PERF_SCRIPT_PATH],
                    "operator_decision": "none",
                },
            }
        )
        check(
            "perf-script-from-head",
            "failed",
            f"git cat-file -p HEAD:{PERF_SCRIPT_PATH}",
            ", ".join(recreated),
        )
    else:
        check(
            "perf-script-from-head",
            "passed",
            f"git cat-file -p HEAD:{PERF_SCRIPT_PATH}",
            "no default under openspec/changes/ outside archive/",
        )

    # 8. the next change's required intake artifacts exist, with no active
    #    slug collision.
    next_proposal = f"openspec/changes/{fixture.next_change}/proposal.md"
    collision = (repo / "openspec/changes" / fixture.next_change).is_dir() and _git_ok(
        repo, "cat-file", "-e", f"HEAD:{next_proposal}"
    )
    if not _git_ok(repo, "cat-file", "-e", f"HEAD:{next_proposal}"):
        blockers.append(
            {
                "id": "next-change-intake-missing",
                "severity": "blocking",
                "location": next_proposal,
                "evidence": f"git cat-file -e HEAD:{next_proposal}",
                "repair": {
                    "action": "operator-decision",
                    "pathspecs": [next_proposal],
                    "operator_decision": "author the next change intake before continuing",
                },
            }
        )
        check(
            "next-change-intake",
            "failed",
            f"git cat-file -e HEAD:{next_proposal}",
            next_proposal,
        )
    else:
        check(
            "next-change-intake",
            "passed",
            f"git cat-file -e HEAD:{next_proposal}",
            f"{fixture.next_change} intake present; active-slug-collision={collision}",
        )

    # 9. the branch is committed with an upstream, or a no-push waiver is
    #    recorded verbatim.
    upstream_ok = _git_ok(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    waiver = manifest.get("no_push_waiver")
    if upstream_ok:
        check(
            "branch-upstream",
            "passed",
            "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
            "upstream configured",
        )
    elif waiver:
        check(
            "branch-upstream",
            "waived",
            "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
            str(waiver),
        )
    else:
        blockers.append(
            {
                "id": "branch-upstream-missing",
                "severity": "blocking",
                "location": "HEAD",
                "evidence": "no upstream and no recorded no-push waiver",
                "repair": {
                    "action": "operator-decision",
                    "pathspecs": [],
                    "operator_decision": "record a no-push waiver or set an upstream",
                },
            }
        )
        check(
            "branch-upstream",
            "failed",
            "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
            "no upstream",
        )

    # 10. a fresh checkout can run the change's focused checks.
    fresh = _fresh_checkout_check(repo)
    if fresh["result"] == "passed":
        check(
            "fresh-checkout-checks",
            "passed",
            fresh["command"],
            fresh["evidence"],
            fresh["sha256"],
        )
    else:
        check(
            "fresh-checkout-checks",
            fresh["result"],
            fresh["command"],
            fresh["evidence"],
        )

    ready = not blockers and all(
        check["result"] in {"passed", "waived"} for check in checks
    )
    return {
        "status": "ready" if ready else "blocked",
        "handoff_ready": ready,
        "change": fixture.change,
        "head": head,
        "checks": checks,
        "blockers": blockers,
        "next_action": "continue" if ready else "repair",
    }


def _fresh_checkout_check(repo: Path) -> dict:
    """Detach a worktree at HEAD and run the focused check there."""

    import tempfile

    with tempfile.TemporaryDirectory(prefix="handoff-checkout-") as tmp:
        checkout = Path(tmp) / "checkout"
        add = _run(
            ["git", "worktree", "add", "--detach", "--quiet", str(checkout), "HEAD"],
            repo,
        )
        if add.returncode != 0:
            return {
                "result": "unqualified",
                "command": "git worktree add --detach <tmp> HEAD",
                "evidence": add.stderr.strip(),
                "sha256": "",
            }
        try:
            proc = _run(["bash", FOCUSED_CHECK_PATH], checkout)
        finally:
            _run(["git", "worktree", "remove", "--force", str(checkout)], repo)
        if proc.returncode != 0:
            return {
                "result": "failed",
                "command": f"bash {FOCUSED_CHECK_PATH} (fresh worktree at HEAD)",
                "evidence": (proc.stdout + proc.stderr).strip(),
                "sha256": "",
            }
        return {
            "result": "passed",
            "command": f"bash {FOCUSED_CHECK_PATH} (fresh worktree at HEAD)",
            "evidence": proc.stdout.strip() or "exit 0",
            "sha256": sha256_bytes(proc.stdout.encode()),
        }


def apply_blockers(fixture: Fixture, blockers: list[dict]) -> dict:
    """Model `opsx-repo-hygiene`: repair exactly the named blockers.

    Only the actions the blocker list names are applied. The commit is built
    from explicit pathspecs, and the staged set is re-inspected before the
    commit so an unrelated or user-owned path cannot slip in.
    """

    repo = fixture.root
    pathspecs: list[str] = []
    actions: list[str] = []

    for blocker in blockers:
        repair = blocker.get("repair") or {}
        action = repair.get("action")
        if action == "operator-decision":
            return {
                "status": "blocked",
                "reason": f"operator decision required: {blocker['id']}",
                "blocker": blocker["id"],
                "operator_decision": repair.get("operator_decision"),
                "commit": "",
                "staged_paths": [],
            }
        if action == "commit-pathspecs":
            pathspecs.extend(repair.get("pathspecs") or [])
            actions.append(action)
        elif action == "ignore-run-state":
            gitignore = repo / ".gitignore"
            entry = repair.get("entry", ".opsx-run/")
            existing = (
                gitignore.read_text(encoding="utf-8").splitlines()
                if gitignore.is_file()
                else []
            )
            if entry not in existing:
                existing.append(entry)
                _write(gitignore, "\n".join(existing) + "\n")
            pathspecs.append(".gitignore")
            actions.append(action)
        elif action == "retarget-script":
            script = repo / repair["path"]
            script.write_text(PERF_SCRIPT_REPAIRED_BODY, encoding="utf-8")
            pathspecs.extend(repair.get("pathspecs") or [repair["path"]])
            actions.append(action)
        else:
            return {
                "status": "blocked",
                "reason": f"unknown repair action for {blocker['id']}",
                "blocker": blocker["id"],
                "commit": "",
                "staged_paths": [],
            }

    pathspecs = sorted(dict.fromkeys(pathspecs))
    if not pathspecs:
        return {
            "status": "blocked",
            "reason": "no explicit implementation scope",
            "blocker": "missing-implementation-scope",
            "commit": "",
            "staged_paths": [],
        }

    user_owned_before = {
        path: (repo / path).read_bytes() for path in fixture.user_owned
    }

    _git(repo, "add", "--", *pathspecs)
    staged = [
        line.strip()
        for line in _git(repo, "diff", "--cached", "--name-only").splitlines()
        if line.strip()
    ]
    unexpected = sorted(set(staged) - set(pathspecs))
    if unexpected:
        _git(repo, "reset", "-q", "--", *staged)
        return {
            "status": "blocked",
            "reason": f"staged set outside explicit scope: {', '.join(unexpected)}",
            "blocker": "staged-set-outside-scope",
            "commit": "",
            "staged_paths": [],
        }
    leaked_user_owned = sorted(set(staged) & set(fixture.user_owned))
    if leaked_user_owned:
        _git(repo, "reset", "-q", "--", *staged)
        return {
            "status": "blocked",
            "reason": f"user-owned paths in scope: {', '.join(leaked_user_owned)}",
            "blocker": "user-owned-in-scope",
            "commit": "",
            "staged_paths": [],
        }

    _git(
        repo,
        "commit",
        "-q",
        "-m",
        f"chore(handoff): commit accepted implementation for {fixture.change}",
    )
    commit = head_sha(repo)
    unstaged = [
        {"path": path, "reason": "user-owned; left untouched"}
        for path in fixture.user_owned
        if (repo / path).read_bytes() != user_owned_before.get(path)
        or path in _paths_from_status(fixture)
    ]
    return {
        "status": "repaired",
        "commit": commit,
        "staged_paths": staged,
        "actions": actions,
        "unstaged": unstaged,
        "next_audit_input": f"git rev-parse HEAD -> {commit}",
        "blocker": "",
    }


def gate_decision(audit_report: dict) -> str:
    """Return the orchestrator decision for one audit report."""

    if audit_report.get("handoff_ready") is True:
        return "continue"
    if audit_report.get("next_action") == "repair" and audit_report.get("blockers"):
        return "repair"
    return "stop"


def next_change(audit_report: dict, plan_changes: tuple[str, ...], current: str) -> str | None:
    """Return the change to select next, or ``None`` when the gate blocks."""

    if gate_decision(audit_report) != "continue":
        return None
    index = plan_changes.index(current)
    remaining = plan_changes[index + 1 :]
    return remaining[0] if remaining else None
