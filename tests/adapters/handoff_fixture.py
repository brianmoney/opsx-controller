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
* an unauthored next change is not a blocker; a different change occupying the
  planned slug fails readiness, with an orchestrator-owned
  `resolve-slug-collision` repair when that occupant is reachable from the plan
  and an operator-decision stop when it is out-of-plan; the planned change
  already being active is not a collision, and an archived same-slug directory
  is informational;
* local-only delivery passes with no remote upstream, and an existing upstream
  is informational;
* an evidence retention gap is a scoped repair when the artifact is readable, an
  ``unqualified`` carry-forward when it is gone but independently and
  claim-scoped supported, and a human-only stop when it is the sole basis of an
  accepted claim;
* a malformed or unreadable evidence manifest is an evidence check result inside
  the report, never an escaping exception and never a silent pass, while a
  repository with no evidence manifest at all passes the evidence check;
* ``human_gates`` resolves from explicit input, then plan frontmatter, and a
  gated change is never started;
* a cap override permits another repair round and a cap alone never stops;
* the next change is selected only after ``handoff_ready: true``.

Nothing here ships to an adapter. It exists so the fixture is deterministic,
stdlib-only, and exercises real git behavior instead of mocked state.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CHANGE = "sample-change"
NEXT_CHANGE = "next-change"
ARCHIVE_DATE = "2026-11-22"
ARCHIVED_CHANGE_REL = f"openspec/changes/archive/{ARCHIVE_DATE}-{CHANGE}"
NEXT_CHANGE_REL = f"openspec/changes/{NEXT_CHANGE}"

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
#: The evidence manifest is committed repository content, not run state: the
#: contract reads it from ``HEAD``. Keeping it outside ``.opsx-run/`` makes the
#: committed-at-HEAD behavior testable and keeps run-state ignore separate.
MANIFEST_PATH = "docs/evidence/acceptance-manifest.json"
CHECKPOINT_PATH = f"{RUN_STATE_DIR}/checkpoint.md"
EVIDENCE_PATH = "docs/evidence/bench.txt"
EVIDENCE_SUPERSEDE_PATH = "docs/evidence/bench.v2.txt"
GONE_EVIDENCE_PATH = "docs/evidence/retired-bench.txt"
EVIDENCE_BODY = "benchmark: 12.5 ms median\n"

#: A support path an accepted claim may declare without it ever being committed.
#: Support counts only when it resolves at ``HEAD``, so this declaration proves
#: nothing and must leave the missing artifact as the claim's sole basis.
UNCOMMITTED_SUPPORT_PATH = "docs/evidence/uncommitted-support.txt"

#: A support path that exists only in the ignored run-state directory: readable
#: in the worktree, absent from ``HEAD``. It distinguishes a HEAD check from a
#: worktree-existence check without dirtying the tracked tree.
WORKTREE_ONLY_SUPPORT_PATH = f"{RUN_STATE_DIR}/worktree-only-support.txt"

#: The accepted claim the missing-artifact scenarios hang on. Its evidence can
#: either be the only support the claim has (a human-only adjudication) or be
#: backed by an independent committed artifact (a recordable carry-forward).
GONE_CLAIM_ID = "release-digest-binding"
GONE_CLAIM_STATEMENT = "the release image was measured on a clean host"

#: A committed artifact whose content actually substantiates the accepted claim:
#: it names the claim id. Resolving at ``HEAD`` is not enough on its own.
SUPPORT_PATH = "docs/evidence/release-digest-binding.txt"
SUPPORT_BODY = (
    f"claim: {GONE_CLAIM_ID}\n"
    f"support: {GONE_CLAIM_STATEMENT}\n"
    "host: clean-instance-3\n"
)

#: A committed artifact that says nothing about the accepted claim. Declaring it
#: as support must not qualify: its content does not substantiate the claim even
#: though it resolves at ``HEAD`` with a matching hash.
UNRELATED_SUPPORT_PATH = EVIDENCE_PATH

#: A different change whose directory occupies the planned next-change slug in
#: the collision fixtures. It is deliberately **not** one of the plan's changes
#: or dependencies, so it is an out-of-plan occupant: the orchestrator may not
#: archive, rename, or rewrite it, and the collision is a human-only stop.
OCCUPYING_CHANGE = "legacy-cache-rewrite"

#: The change order the continuation tests use.
PLAN_CHANGES = (CHANGE, NEXT_CHANGE)
#: A plan with one operator-gated change third and a later change after it.
GATED_CHANGE = "gated-change"
GATED_PLAN_CHANGES = (CHANGE, NEXT_CHANGE, GATED_CHANGE, "after-gate")

#: The next change depends on the change just archived.
PLAN_DEPENDENCIES = {NEXT_CHANGE: (CHANGE,)}

#: The recorded operator authority restriction used by the human-only tests.
ARCHIVE_ONLY_WAIVER = (
    "The operator waived committing the accepted source for this change; the "
    "archive-only scope stands and no baseline commit is authorized."
)

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

#: A plan whose frontmatter declares the plan's one operator-gated change.
PLAN_BODY = """---
human_gates:
  - gated-change
---
# Sample plan

1. sample-change
2. next-change
3. gated-change
4. after-gate
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
    deferred: tuple[str, ...] = ()
    dependencies: dict = field(default_factory=lambda: dict(PLAN_DEPENDENCIES))
    plan_changes: tuple[str, ...] = PLAN_CHANGES
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


def is_ancestor(fixture: Fixture, older: str, newer: str) -> bool:
    """True when commit *older* is an ancestor of commit *newer*."""

    return _git_ok(
        fixture.root, "merge-base", "--is-ancestor", older, newer
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def plan_text(fixture: Fixture) -> str:
    """Return the plan the next change will read, from the worktree."""

    return (fixture.root / PLAN_PATH).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# build and small state readers
# ---------------------------------------------------------------------------


def build(root: Path) -> Fixture:
    """Build the archive-only fixture repository under *root*.

    Resulting state, in order:

    1. a baseline commit with the change artifacts, the perf script, the
       committed evidence artifact, and a user-owned note. The next change is
       deliberately **unauthored**: its authoring stage runs after handoff;
    2. an archive-only commit that moves the change directory and syncs one
       benchmark spec, carrying zero implementation paths;
    3. a dirty working tree: accepted implementation and plan uncommitted, run
       state untracked and unignored, and the user-owned note modified;
    4. a commit of the evidence manifest alone, whose accepted entries still
       point at the uncommitted implementation paths.
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
    _write(root / PERF_SCRIPT_PATH, PERF_SCRIPT_BODY)
    _write(root / FOCUSED_CHECK_PATH, FOCUSED_CHECK_BODY)
    _write(root / EVIDENCE_PATH, EVIDENCE_BODY)
    _write(root / SUPPORT_PATH, SUPPORT_BODY)
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
    _write(root / PLAN_PATH, PLAN_BODY)
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
        "evidence": [
            {"path": EVIDENCE_PATH, "sha256": sha256_path(root / EVIDENCE_PATH)}
        ],
    }
    _write(root / MANIFEST_PATH, json.dumps(manifest, indent=2) + "\n")

    # The evidence manifest is committed content: the audit reads it from
    # ``HEAD``, so the fixture must commit it before any audit runs. Commit only
    # the manifest; the accepted implementation and plan stay dirty, which is the
    # archive-only failure the gate exists to catch.
    _git(root, "add", "--", MANIFEST_PATH)
    _git(root, "commit", "-q", "-m", "docs(evidence): record the acceptance manifest")

    return Fixture(
        root=root,
        archive_commit=archive_commit,
        archive_implementation_paths=implementation,
    )


def load_manifest(fixture: Fixture) -> dict:
    """Return the committed evidence manifest, parsed from ``HEAD``."""

    result = read_manifest(fixture)
    if result["state"] != "ok" or result["manifest"] is None:
        raise ValueError(
            f"no usable committed manifest at {MANIFEST_PATH}: {result['reason']}"
        )
    return result["manifest"]


def _read_manifest_text(repo: Path, path: str) -> str:
    """Read the committed evidence manifest as text from ``HEAD``.

    Isolated behind one name so a test can drive the ``PermissionError`` branch
    deterministically by patching this callable — never all of ``open``, which
    would break the rest of the fixture.
    """

    return _git_bytes(repo, "cat-file", "-p", f"HEAD:{path}").decode("utf-8")


def _manifest_entry_error(manifest: dict) -> str:
    """Return a structural error for the entries the audit indexes, else ``""``.

    A top-level type check is not enough: the audit indexes ``entry["path"]`` and
    ``entry["sha256"]``, so a manifest that parses but carries a malformed member
    such as ``accepted: [{}]`` must be classified as unreadable here instead of
    raising ``KeyError`` later. The same holds for an evidence entry's
    ``superseded_by``/``superseded_sha256`` pointer, which the evidence check
    joins into a ``HEAD`` path and a checksum comparison: a non-string there must
    not reach path arithmetic.
    """

    accepted = manifest.get("accepted", [])
    for index, entry in enumerate(accepted):
        if not isinstance(entry, dict):
            return f"accepted[{index}] is not a JSON object"
        for key in ("path", "sha256"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                return f"accepted[{index}].{key} is not a non-empty string"
    plan = manifest.get("plan", {})
    for key in ("path", "sha256"):
        value = plan.get(key)
        if not isinstance(value, str) or not value:
            return f"plan.{key} is not a non-empty string"
    evidence = manifest.get("evidence", [])
    if not isinstance(evidence, list):
        return "field 'evidence' is not a JSON list"
    for index, entry in enumerate(evidence):
        if not isinstance(entry, dict):
            return f"evidence[{index}] is not a JSON object"
        for key in ("path", "sha256"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                return f"evidence[{index}].{key} is not a non-empty string"
        for key in ("superseded_by", "superseded_sha256"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                return f"evidence[{index}].{key} is not a string"
    claims = manifest.get("accepted_claims", [])
    if not isinstance(claims, list):
        return "field 'accepted_claims' is not a JSON list"
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            return f"accepted_claims[{index}] is not a JSON object"
    return ""


def _worktree_manifest_present(fixture: Fixture) -> bool:
    return (fixture.root / MANIFEST_PATH).exists()


def read_manifest(fixture: Fixture) -> dict:
    """Read the committed evidence manifest and classify the result.

    Returns ``{"state": "ok"|"absent"|"uncommitted"|"unreadable",
    "manifest": dict|None, "reason": str, "worktree_present": bool}``.

    * ``absent`` — there is no committed evidence manifest at ``HEAD`` and no
      worktree copy. This is the contract's no-manifest case, which passes.
    * ``uncommitted`` — a manifest exists only in the worktree. It is not
      committed content, so it cannot prove evidence at ``HEAD``; the audit
      reports it as a blocked/unqualified result, never a pass.
    * ``unreadable`` — a committed manifest exists but cannot be read, decoded,
      parsed, or structurally used: permission denial, non-blob content,
      undecodable bytes, invalid JSON, or a shape that does not declare the
      entries the audit indexes.
    * ``ok`` — the committed manifest is structurally usable. A committed
      manifest whose worktree copy is missing is still ``ok``: ``HEAD`` is the
      authoritative source, and absence from the worktree is not a missing
      artifact.

    A read failure is never an escaping exception and never a pass.
    """

    repo = fixture.root
    worktree_present = _worktree_manifest_present(fixture)
    if not _git_ok(repo, "cat-file", "-e", f"HEAD:{MANIFEST_PATH}"):
        if worktree_present:
            return {
                "state": "uncommitted",
                "manifest": None,
                "reason": (
                    f"{MANIFEST_PATH} exists only in the worktree; the audit "
                    "reads committed evidence manifests at HEAD"
                ),
                "worktree_present": True,
            }
        return {
            "state": "absent",
            "manifest": None,
            "reason": f"no committed evidence manifest at {MANIFEST_PATH}",
            "worktree_present": False,
        }

    object_type = _git(repo, "cat-file", "-t", f"HEAD:{MANIFEST_PATH}").strip()
    if object_type != "blob":
        return {
            "state": "unreadable",
            "manifest": None,
            "reason": (
                f"unreadable bytes: {MANIFEST_PATH} at HEAD is a {object_type}, "
                "not a regular file"
            ),
            "worktree_present": worktree_present,
        }

    try:
        raw = _read_manifest_text(repo, MANIFEST_PATH)
    except PermissionError:
        return {
            "state": "unreadable",
            "manifest": None,
            "reason": f"permission denied: {MANIFEST_PATH}",
            "worktree_present": worktree_present,
        }
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "state": "unreadable",
            "manifest": None,
            "reason": (
                f"unreadable bytes: {MANIFEST_PATH} "
                f"({type(exc).__name__}: {exc})"
            ),
            "worktree_present": worktree_present,
        }
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "state": "unreadable",
            "manifest": None,
            "reason": (
                f"invalid JSON: {MANIFEST_PATH} "
                f"({exc.msg} at line {exc.lineno} column {exc.colno})"
            ),
            "worktree_present": worktree_present,
        }
    if not isinstance(manifest, dict):
        return {
            "state": "unreadable",
            "manifest": None,
            "reason": (
                f"malformed manifest shape: {MANIFEST_PATH} is not a JSON object"
            ),
            "worktree_present": worktree_present,
        }
    for key, expected in (("accepted", list), ("plan", dict)):
        if not isinstance(manifest.get(key), expected):
            return {
                "state": "unreadable",
                "manifest": None,
                "reason": (
                    f"malformed manifest shape: {MANIFEST_PATH} field '{key}' is "
                    f"not a JSON {expected.__name__}"
                ),
                "worktree_present": worktree_present,
            }
    structural = _manifest_entry_error(manifest)
    if structural:
        return {
            "state": "unreadable",
            "manifest": None,
            "reason": (
                f"malformed manifest shape: {MANIFEST_PATH} {structural}"
            ),
            "worktree_present": worktree_present,
        }
    return {
        "state": "ok",
        "manifest": manifest,
        "reason": "",
        "worktree_present": worktree_present,
    }


def _commit_manifest(fixture: Fixture, message: str) -> None:
    """Stage and commit the manifest when it actually changed."""

    repo = fixture.root
    _git(repo, "add", "--", MANIFEST_PATH)
    if not _git_ok(repo, "diff", "--cached", "--quiet", "--", MANIFEST_PATH):
        _git(repo, "commit", "-q", "-m", message)


def write_manifest(fixture: Fixture, manifest: dict, *, commit: bool = True) -> None:
    _write(fixture.root / MANIFEST_PATH, json.dumps(manifest, indent=2) + "\n")
    if commit:
        _commit_manifest(fixture, "docs(evidence): update the acceptance manifest")


def archived_slugs(fixture: Fixture) -> dict[str, str]:
    """Map an archived change slug to its archive directory (repo-relative)."""

    root = fixture.root / "openspec/changes/archive"
    if not root.is_dir():
        return {}
    found: dict[str, str] = {}
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        name = path.name
        looks_dated = (
            len(name) > 11
            and name[4] == "-"
            and name[7] == "-"
            and name[10] == "-"
        )
        slug = name[11:] if looks_dated else name
        found.setdefault(slug, f"openspec/changes/archive/{name}")
    return found


def _declared_change_id(repo: Path, directory: str) -> str:
    """Return the change id an active change directory declares for itself.

    The declaration is read from ``HEAD`` when it is committed, and from the
    worktree only when it is not — the same source the auditor inspects. The
    directory's ``proposal.md`` H1 is the declaration: ``# Proposal: <id>`` and
    ``# <id>`` are both accepted, and the first token is the id. An empty string
    means the directory makes no declaration, so slug identity is all there is to
    go on.
    """

    proposal_rel = f"{directory}/proposal.md"
    if _git_ok(repo, "cat-file", "-e", f"HEAD:{proposal_rel}"):
        text = _git_bytes(repo, "cat-file", "-p", f"HEAD:{proposal_rel}").decode(
            "utf-8", "replace"
        )
    else:
        proposal = repo / proposal_rel
        if not proposal.is_file():
            return ""
        text = proposal.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        text_line = line.strip()
        if not text_line.startswith("# "):
            continue
        declared = text_line[2:].strip()
        if declared.lower().startswith("proposal:"):
            declared = declared.split(":", 1)[1].strip()
        tokens = declared.split()
        return tokens[0].strip("`*_") if tokens else ""
    return ""


def next_change_slug_state(fixture: Fixture) -> dict:
    """Describe the planned next-change slug in one of four states.

    * ``state == "active-planned"`` — the active directory at the planned slug
      declares the planned next change itself, so the next change is already
      authored and in progress. This is **not** a collision: readiness passes and
      nothing is started or mutated;
    * ``state == "active-collision"`` — the active directory at the planned slug
      declares a *different* change, so the planned slug is occupied and
      authoring the next change would write into another change's directory.
      The occupant's reachability decides the owner: a plan-reachable occupant
      (one of the plan's changes or declared dependencies) is an
      orchestrator-owned `resolve-slug-collision` repair; an out-of-plan
      occupant is a human-only stop with an operator decision to resolve or
      rename the external change;
    * ``state == "archived-only"`` — only an archive directory carries the slug.
      The auditor contract reports that as informational context, so readiness
      passes;
    * ``state == "unauthored"`` — nothing occupies the slug; the proposal has not
      been written yet, which is expected *after* handoff and is never a
      blocker.

    ``collision`` is true only for ``active-collision``: the planned change being
    active, or an archived directory, is never a collision. ``plan_reachable``
    distinguishes an occupant the plan can drive to archive from an
    ``external_collision`` the orchestrator may not mutate. ``authored`` reports
    whether a proposal is already committed at ``HEAD`` so a caller can tell an
    already-authored directory from an empty one.
    """

    repo = fixture.root
    slug = fixture.next_change
    planned_rel = f"openspec/changes/{slug}"
    planned = repo / planned_rel
    active = planned.is_dir() and any(planned.iterdir())
    authored = _git_ok(repo, "cat-file", "-e", f"HEAD:{planned_rel}/proposal.md")
    archived = archived_slugs(fixture).get(slug, "")
    declared = _declared_change_id(repo, planned_rel) if active else ""
    same_planned_change = active and declared in ("", slug)
    different_change = active and not same_planned_change
    plan_reachable_changes = set(fixture.plan_changes)
    for dependency_chain in fixture.dependencies.values():
        plan_reachable_changes.update(dependency_chain)
    plan_reachable = different_change and declared in plan_reachable_changes
    external_collision = different_change and not plan_reachable
    if different_change:
        state = "active-collision"
    elif same_planned_change:
        state = "active-planned"
    elif archived:
        state = "archived-only"
    else:
        state = "unauthored"
    if state == "archived-only":
        informational = (
            f"{archived} already carries the {slug} slug in the archive; that is "
            "informational context, not a blocker"
        )
    elif external_collision:
        informational = (
            f"{declared} occupies {planned_rel} but is not a plan change or "
            "dependency; the orchestrator may not archive, rename, or rewrite it"
        )
    else:
        informational = ""
    return {
        "slug": slug,
        "planned_path": planned_rel,
        "active_directory": planned_rel if active else "",
        "active": active,
        "authored": authored,
        "declared_change_id": declared,
        "same_planned_change": same_planned_change,
        "different_change": different_change,
        "occupying_change": declared if different_change else "",
        "plan_reachable": plan_reachable,
        "external_collision": external_collision,
        "archived_directory": archived,
        "archived_only": state == "archived-only",
        "collision": state == "active-collision",
        "collision_directory": planned_rel if different_change else "",
        "state": state,
        "informational": informational,
        "proposal_required": False,
    }


def dependencies_state(fixture: Fixture, dependencies: dict | None = None) -> dict:
    """Report whether the next change's dependencies are archived or deferred."""

    plan_dependencies = fixture.dependencies if dependencies is None else dependencies
    required = tuple(plan_dependencies.get(fixture.next_change, ()))
    archived = archived_slugs(fixture)
    deferred = tuple(fixture.deferred)
    unmet = tuple(
        slug for slug in required if slug not in archived and slug not in deferred
    )
    return {
        "dependencies": required,
        "archived": tuple(slug for slug in required if slug in archived),
        "deferred": tuple(slug for slug in required if slug in deferred),
        "unmet": unmet,
        "ready": not unmet,
    }


def delivery_state(fixture: Fixture) -> dict:
    """Report local-only delivery for the committed ``HEAD``.

    A configured upstream is informational: it is reported verbatim and never
    changes the result. No no-push waiver is requested or accepted here.
    """

    repo = fixture.root
    proc = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        repo,
    )
    upstream = proc.stdout.strip() if proc.returncode == 0 else ""
    head = head_sha(repo)
    informational = []
    if upstream:
        informational.append(
            f"upstream {upstream} is configured; it is informational and does not "
            "affect the handoff result"
        )
    evidence = (
        f"local HEAD {head} carries the accepted work; delivery is local, so no "
        "push, remote branch, or pull request is required"
    )
    if informational:
        evidence = f"{evidence}; {informational[0]}"
    return {
        "head": head,
        "upstream": upstream,
        "local_only": not upstream,
        "informational": informational,
        "evidence": evidence,
        "sha256": sha256_bytes(head.encode()),
    }


def evidence_state(fixture: Fixture, manifest: dict | None = None) -> dict:
    """Classify every committed evidence manifest entry.

    * ``resolved`` — the path and its recorded checksum agree at ``HEAD``;
    * ``supersedable`` — the artifact is readable, so a scoped writer can add a
      superseding version and update the checksum;
    * ``gone`` — the artifact exists nowhere the writer can read, so the entry
      must be recorded as ``unqualified`` rather than repaired or escalated.
    """

    repo = fixture.root
    if manifest is None:
        manifest = load_manifest(fixture)
    entries = list(manifest.get("evidence", []))
    resolved: list[dict] = []
    supersedable: list[dict] = []
    gone: list[dict] = []
    for entry in entries:
        superseded_by = entry.get("superseded_by")
        path = superseded_by or entry.get("path", "")
        expected = (
            entry.get("superseded_sha256") if superseded_by else entry.get("sha256")
        )
        if _git_ok(repo, "cat-file", "-e", f"HEAD:{path}"):
            actual = sha256_bytes(_git_bytes(repo, "cat-file", "-p", f"HEAD:{path}"))
            if not expected or actual == expected:
                resolved.append(entry)
            else:
                supersedable.append(
                    {
                        **entry,
                        "reason": (
                            f"recorded sha256 {expected} does not match HEAD "
                            f"content {actual}"
                        ),
                    }
                )
            continue
        if (repo / path).is_file():
            supersedable.append(
                {
                    **entry,
                    "reason": (
                        f"{path} is readable in the worktree but not committed at HEAD"
                    ),
                }
            )
            continue
        gone.append(
            {
                **entry,
                "reason": (
                    f"{path} does not exist at HEAD or anywhere the writer can read"
                ),
            }
        )
    return {
        "entries": entries,
        "resolved": resolved,
        "supersedable": supersedable,
        "gone": gone,
    }


def claim_support_state(fixture: Fixture, manifest: dict | None = None) -> dict:
    """Report whether an accepted claim keeps committed, claim-scoped support.

    ``accepted_claims`` in the manifest names a claim, the evidence entries it
    depends on, and any ``independent_support`` references that support the same
    claim. A reference counts only when all of these hold:

    * the named path resolves at ``HEAD`` — a declaration is not proof;
    * the recorded hash, when one is given, matches the ``HEAD`` content;
    * a declared ``supports_claim``, when present, names this claim; and
    * the committed content substantiates this claim — it carries the claim id.

    Presence at ``HEAD`` alone is never support. An unrelated committed file
    declared as support leaves the missing artifact as the claim's sole basis,
    which is the same standard the auditor applies when it refuses to treat a
    manifest entry as evidence.

    ``sole_claim_basis`` is true for a claim whose every evidence path is gone and
    which has no qualifying committed support left. That is the single evidence
    case the auditor escalates to a human-only stop.
    """

    repo = fixture.root
    if manifest is None:
        manifest = load_manifest(fixture)
    evidence = evidence_state(fixture, manifest)
    gone = {entry.get("path", "") for entry in evidence["gone"]}
    claims: list[dict] = []
    for claim in manifest.get("accepted_claims", []):
        claim_id = str(claim.get("id", "") or "")
        support: list[str] = []
        for reference in claim.get("independent_support", []):
            if isinstance(reference, dict):
                path = reference.get("path", "") or ""
                expected = reference.get("sha256", "") or ""
                declared = str(reference.get("supports_claim", "") or "")
            else:
                path = str(reference)
                expected = ""
                declared = ""
            if declared and declared != claim_id:
                continue
            if not path or not _git_ok(repo, "cat-file", "-e", f"HEAD:{path}"):
                continue
            content = _git_bytes(repo, "cat-file", "-p", f"HEAD:{path}")
            if expected and sha256_bytes(content) != expected:
                continue
            if not claim_id or claim_id.encode() not in content:
                continue
            support.append(path)
        depends = list(claim.get("evidence", []))
        resolved = [path for path in depends if path not in gone]
        claims.append(
            {
                **claim,
                "committed_support": support,
                "resolved_evidence": resolved,
                "sole_claim_basis": bool(depends) and not resolved and not support,
            }
        )
    adjudicated = [claim for claim in claims if claim["sole_claim_basis"]]
    return {
        "claims": claims,
        "sole_claim_basis": bool(adjudicated),
        "adjudicated": adjudicated,
    }


# ---------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------


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
    ``blockers[]``, ``carry_forwards[]``, ``informational[]``, and
    ``next_action``.
    """

    repo = fixture.root
    manifest_read = read_manifest(fixture)
    # A manifest that exists but cannot be used is a check result, not an
    # escaping exception: record it in the evidence check and its blocker. An
    # *absent* manifest is the contract's no-manifest case: nothing references a
    # committed evidence manifest, so `evidence-at-head` passes, and the accepted
    # source set comes from the dispatch input (`fixture.accepted`) instead.
    manifest_state = manifest_read["state"]
    manifest = manifest_read["manifest"] or {}
    unreadable = manifest_state == "unreadable"
    uncommitted = manifest_state == "uncommitted"
    absent = manifest_state == "absent"
    manifest_error = (
        manifest_read["reason"] if unreadable or uncommitted else ""
    )
    if absent:
        accepted_entries = [
            {"path": path, "sha256": ""} for path in fixture.accepted
        ]
        plan_entry = {"path": PLAN_PATH, "sha256": ""}
    else:
        accepted_entries = list(manifest.get("accepted") or [])
        plan_entry = manifest.get("plan") or {}
    head = head_sha(repo)
    checks: list[dict] = []
    blockers: list[dict] = []
    carry_forwards: list[dict] = []
    informational: list[str] = []

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
    for entry in accepted_entries:
        path = entry["path"]
        expected = entry.get("sha256") or ""
        if not _git_ok(repo, "cat-file", "-e", f"HEAD:{path}"):
            missing_source.append(path)
            continue
        if expected and (
            sha256_bytes(_git_bytes(repo, "cat-file", "-p", f"HEAD:{path}"))
            != expected
        ):
            missing_source.append(path)
    if manifest_error:
        # The accepted set lives in the manifest; a manifest that cannot be used
        # means this check cannot be proved. It is `unqualified`, never a pass.
        check(
            "source-manifest-at-head",
            "unqualified",
            "git cat-file -p HEAD:<path> | sha256sum",
            f"cannot prove the accepted set: {manifest_error}",
        )
    elif missing_source:
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
            f"{len(accepted_entries)} accepted source path(s) verified at HEAD",
        )

    # 3. the plan the next change will read exists at HEAD.
    plan_path = plan_entry.get("path", "")
    plan_sha = plan_entry.get("sha256", "") or ""
    if manifest_error:
        check(
            "plan-at-head",
            "unqualified",
            "git cat-file -e HEAD:<plan>",
            f"cannot prove the plan hash: {manifest_error}",
        )
    elif not plan_path:
        check(
            "plan-at-head",
            "failed",
            "git cat-file -e HEAD:<plan>",
            f"{MANIFEST_PATH} declares no plan entry",
        )
    elif not _git_ok(repo, "cat-file", "-e", f"HEAD:{plan_path}") or (
        plan_sha
        and sha256_bytes(_git_bytes(repo, "cat-file", "-p", f"HEAD:{plan_path}"))
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
    waiver = manifest.get("archive_only_waiver")
    if not implementation_paths:
        if waiver:
            # The operator restricted commit authority: the writer SHALL NOT
            # commit the baseline, and the restriction is a stop, not a repair.
            blockers.append(
                {
                    "id": "archive-only-waiver",
                    "severity": "blocking",
                    "location": fixture.archive_commit,
                    "evidence": (
                        "implementation_paths: 0 with an operator waiver recorded "
                        "verbatim"
                    ),
                    "repair": {
                        "action": "operator-decision",
                        "pathspecs": [],
                        "operator_decision": str(waiver),
                    },
                }
            )
            check(
                "archive-commit-implementation-paths",
                "failed",
                "git show --name-only --pretty=format: " + fixture.archive_commit,
                "implementation_paths: 0; operator waiver recorded verbatim",
            )
        else:
            blockers.append(
                {
                    "id": "archive-commit-implementation-paths",
                    "severity": "blocking",
                    "location": fixture.archive_commit,
                    "evidence": "implementation_paths: 0",
                    "repair": {
                        "action": "commit-pathspecs",
                        "pathspecs": list(fixture.accepted),
                        "operator_decision": "none",
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

    # 6. committed evidence manifests resolve at HEAD. A readable artifact whose
    #    recorded checksum is stale is a scoped repair; an artifact that exists
    #    nowhere is an unqualified carry-forward unless it is the sole support for
    #    an accepted claim, which is the one human-only evidence boundary.
    claims = {"claims": [], "sole_claim_basis": False, "adjudicated": []}
    if unreadable or uncommitted:
        # A manifest that exists but cannot be read, parsed, or structurally used
        # is an evidence result inside the envelope: `failed` with a scoped
        # repair, never a crash and never a silent pass. A manifest that exists
        # only in the worktree is the same class: it is not committed content,
        # so it cannot prove evidence at HEAD, and committing it is a scoped
        # repair.
        if uncommitted:
            blocker_id = "evidence-manifest-uncommitted"
            repair = {
                "action": "commit-pathspecs",
                "pathspecs": [MANIFEST_PATH],
                "operator_decision": "none",
            }
        else:
            blocker_id = "evidence-manifest-unreadable"
            repair = {
                "action": "repair-evidence",
                "manifest": MANIFEST_PATH,
                "path": MANIFEST_PATH,
                "retained_artifact": "none",
                "supersede_path": "",
                "checksum": "recompute",
                "pathspecs": [],
                "operator_decision": "none",
            }
        blockers.append(
            {
                "id": blocker_id,
                "severity": "blocking",
                "location": MANIFEST_PATH,
                "evidence": manifest_read["reason"],
                "repair": repair,
            }
        )
        check(
            "evidence-at-head",
            "failed",
            "git cat-file -p HEAD:" + MANIFEST_PATH,
            manifest_read["reason"],
        )
        evidence = {"entries": [], "resolved": [], "supersedable": [], "gone": []}
    elif absent:
        # The contract's other case: a repository with no committed evidence
        # manifest at all. Nothing references one, so there is no retention claim
        # to resolve, and the check passes with that statement as its evidence.
        check(
            "evidence-at-head",
            "passed",
            "git cat-file -e HEAD:" + MANIFEST_PATH,
            (
                "no committed evidence manifest: nothing in the repository or the "
                "plan references one, so there is no evidence retention claim to "
                "resolve at HEAD"
            ),
        )
        evidence = {"entries": [], "resolved": [], "supersedable": [], "gone": []}
    else:
        evidence = evidence_state(fixture, manifest)
        claims = claim_support_state(fixture, manifest)
        for entry in evidence["gone"]:
            carry_forwards.append(
                {
                    "check": "evidence-at-head",
                    "manifest": MANIFEST_PATH,
                    "evidence": entry.get("path", ""),
                    "missing": True,
                    "verified": False,
                    "claim": f"retained evidence at {entry.get('path', '')}",
                    "disposition": "unqualified",
                    "reason": entry["reason"],
                }
            )
        for claim in claims["adjudicated"]:
            # The missing artifact is the only support this accepted claim has:
            # no committed artifact, plan acceptance, or passing hard check backs
            # it. That is the human-only evidence boundary.
            statement = claim.get("statement") or claim.get("id", "")
            blockers.append(
                {
                    "id": "evidence-sole-claim-basis",
                    "severity": "blocking",
                    "location": claim.get("id") or MANIFEST_PATH,
                    "evidence": (
                        f"missing evidence "
                        + ", ".join(claim.get("evidence", []))
                        + f" is the sole committed support for accepted claim "
                        f"'{statement}'; no independent committed artifact, plan "
                        "acceptance, or passing hard check supports it"
                    ),
                    "repair": {
                        "action": "operator-decision",
                        "pathspecs": [],
                        "operator_decision": (
                            f"adjudicate accepted claim '{statement}': its only "
                            "evidence "
                            + ", ".join(claim.get("evidence", []))
                            + " is missing and has no committed independent support"
                        ),
                    },
                }
            )
        if evidence["supersedable"]:
            first = evidence["supersedable"][0]
            original_rel = first.get("path", "")
            supersede_rel = _supersede_path(original_rel)
            blockers.append(
                {
                    "id": "evidence-retention-gap",
                    "severity": "blocking",
                    "location": ", ".join(
                        entry.get("path", "") for entry in evidence["supersedable"]
                    ),
                    "evidence": "; ".join(
                        entry["reason"] for entry in evidence["supersedable"]
                    ),
                    "repair": {
                        "action": "repair-evidence",
                        "manifest": MANIFEST_PATH,
                        "path": original_rel,
                        "supersede_path": supersede_rel,
                        "checksum": "recompute",
                        "pathspecs": [supersede_rel],
                        "operator_decision": "none",
                    },
                }
            )
            check(
                "evidence-at-head",
                "failed",
                "git cat-file -p HEAD:<path> | sha256sum",
                "; ".join(
                    f"{entry.get('path', '')}: {entry['reason']}"
                    for entry in evidence["supersedable"]
                ),
            )
        elif evidence["gone"]:
            check(
                "evidence-at-head",
                "unqualified",
                "git cat-file -e HEAD:<path>",
                ", ".join(entry.get("path", "") for entry in evidence["gone"]),
            )
        else:
            check(
                "evidence-at-head",
                "passed",
                "git cat-file -e HEAD:<path>",
                f"{len(evidence['resolved'])} evidence path(s) resolve at HEAD",
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

    # 8. no active change directory occupied by a *different* change sits at the
    #    next change's planned slug. A directory that declares the planned change
    #    itself is that change already active; an archived directory carrying the
    #    same slug is informational context. The proposal itself is authored
    #    *after* handoff, so its absence is not a check and never a blocker.
    slug = next_change_slug_state(fixture)
    if slug["state"] == "active-collision":
        occupying = slug["occupying_change"] or "undeclared"
        if slug["plan_reachable"]:
            repair = {
                "action": "resolve-slug-collision",
                "owner": "orchestrator",
                "pathspecs": [],
                "operator_decision": "none",
            }
            repair_note = (
                "the occupying change is reachable from the plan, so the "
                "orchestrator resolves this at plan level by not starting or "
                "authoring into the slug and letting the pipeline archive it"
            )
        else:
            repair = {
                "action": "operator-decision",
                "pathspecs": [],
                "operator_decision": (
                    f"resolve or rename the out-of-plan change '{occupying}' at "
                    f"{slug['collision_directory']}; the orchestrator may not "
                    "archive, rename, or rewrite a change outside the plan"
                ),
            }
            repair_note = (
                "the occupying change is not one of the plan's changes or "
                "dependencies, so no worker may archive, rename, or rewrite it"
            )
        blockers.append(
            {
                "id": "next-change-slug-collision",
                "severity": "blocking",
                "location": slug["collision_directory"],
                "evidence": (
                    f"a different change ({occupying}) already occupies the "
                    f"planned slug {slug['slug']}; authoring the next change "
                    f"would write into an occupied change directory; {repair_note}"
                ),
                "planned_slug": slug["slug"],
                "colliding_directory": slug["collision_directory"],
                "occupying_change": occupying,
                "plan_reachable": slug["plan_reachable"],
                "external_collision": slug["external_collision"],
                "repair": repair,
            }
        )
        check(
            "next-change-slug",
            "failed",
            f"test -e {slug['planned_path']}",
            slug["collision_directory"],
        )
    elif slug["state"] == "active-planned":
        check(
            "next-change-slug",
            "passed",
            f"test -e {slug['planned_path']}",
            (
                f"{slug['planned_path']} is the planned next change "
                f"{slug['slug']} already authored and in progress; that is not a "
                "collision, so no mutation and no new start are required"
            ),
        )
    elif slug["state"] == "archived-only":
        informational.append(slug["informational"])
        check(
            "next-change-slug",
            "passed",
            f"test -e {slug['planned_path']}",
            (
                f"only {slug['archived_directory']} carries the {slug['slug']} "
                "slug; an archived directory is informational context, not a "
                "blocker"
            ),
        )
    else:
        check(
            "next-change-slug",
            "passed",
            f"test -e {slug['planned_path']}",
            (
                f"no active change directory occupies {slug['slug']}; its "
                "proposal is not required before handoff"
            ),
        )

    # 9. the next change's dependencies are archived or accepted as deferrals.
    deps = dependencies_state(fixture)
    if deps["unmet"]:
        blockers.append(
            {
                "id": "next-change-dependency-unmet",
                "severity": "blocking",
                "location": fixture.next_change,
                "evidence": (
                    "dependency not archived and not deferred: "
                    + ", ".join(deps["unmet"])
                ),
                "repair": {
                    "action": "operator-decision",
                    "pathspecs": [],
                    "operator_decision": (
                        "complete or explicitly defer "
                        + ", ".join(deps["unmet"])
                    ),
                },
            }
        )
        check(
            "next-change-dependencies",
            "failed",
            "openspec list --changes && openspec list --specs",
            ", ".join(deps["unmet"]),
        )
    else:
        check(
            "next-change-dependencies",
            "passed",
            "openspec list --changes && openspec list --specs",
            (
                f"{len(deps['dependencies'])} dependency path(s) archived or "
                "deferred"
            ),
        )

    # 10. delivery is local: a committed HEAD with no upstream passes, and an
    #     existing upstream is informational only.
    delivery = delivery_state(fixture)
    informational.extend(delivery["informational"])
    check(
        "local-only-delivery",
        "passed",
        "git rev-parse --abbrev-ref --symbolic-full-name @{u}",
        delivery["evidence"],
        delivery["sha256"],
    )

    # 11. a fresh checkout can run the change's focused checks.
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

    carry_checks = {record["check"] for record in carry_forwards}
    sole_claim_basis = bool(claims.get("sole_claim_basis"))

    # The auditor names an owner for every repository repair; a missing owner
    # defaults to the hygiene writer, and `resolve-slug-collision` is the
    # orchestrator's. `operator-decision` is not a repository repair, so it has
    # no acting owner.
    for blocker in blockers:
        repair = blocker.setdefault("repair", {})
        if repair.get("action") == "operator-decision":
            continue
        if repair.get("action") == "resolve-slug-collision":
            repair.setdefault("owner", "orchestrator")
        else:
            repair.setdefault("owner", "opsx-repo-hygiene")

    def acceptable(entry: dict) -> bool:
        if entry["result"] in {"passed", "waived"}:
            return True
        # An unqualified check with a recorded carry-forward does not by itself
        # make handoff_ready false — unless it is the evidence check and the
        # missing artifact is the sole support for an accepted claim.
        if entry["result"] != "unqualified" or entry["id"] not in carry_checks:
            return False
        return not (entry["id"] == "evidence-at-head" and sole_claim_basis)

    ready = not blockers and all(acceptable(entry) for entry in checks)
    human_only = [
        blocker
        for blocker in blockers
        if (blocker.get("repair") or {}).get("action") == "operator-decision"
    ]
    if ready:
        next_action = "continue"
    elif human_only:
        next_action = "stop"
    else:
        next_action = "repair"
    return {
        "status": "ready" if ready else "blocked",
        "handoff_ready": ready,
        "change": fixture.change,
        "head": head,
        "checks": checks,
        "blockers": blockers,
        "carry_forwards": carry_forwards,
        "informational": informational,
        "next_action": next_action,
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


# ---------------------------------------------------------------------------
# hygiene repair
# ---------------------------------------------------------------------------


def _supersede_path(path: str) -> str:
    """Return the versioned sibling a superseding evidence entry points at."""

    candidate = Path(path)
    return str(candidate.with_name(f"{candidate.stem}.v2{candidate.suffix}"))


def _stage_and_commit(fixture: Fixture, pathspecs: list[str], message: str) -> dict:
    """Stage exactly *pathspecs* and commit them.

    The staged set is re-inspected before the commit so an unrelated or
    user-owned path cannot slip into the commit.
    """

    repo = fixture.root
    pathspecs = sorted(dict.fromkeys(path for path in pathspecs if path))
    if not pathspecs:
        return {
            "status": "blocked",
            "reason": "no explicit implementation scope",
            "blocker": "missing-implementation-scope",
            "commit": "",
            "staged_paths": [],
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
    if not staged:
        return {
            "status": "blocked",
            "reason": "nothing staged for the named pathspecs",
            "blocker": "nothing-to-commit",
            "commit": "",
            "staged_paths": [],
        }

    _git(repo, "commit", "-q", "-m", message)
    return {
        "status": "repaired",
        "reason": "",
        "blocker": "",
        "commit": head_sha(repo),
        "staged_paths": staged,
    }


def _supersede_evidence(fixture: Fixture, repair: dict) -> list[str] | None:
    """Add a superseding evidence version and update the recorded checksum.

    The retained original is never rewritten: a new versioned artifact is added,
    the original manifest entry gains an append-only ``superseded_by`` pointer
    with the new checksum, and a superseding entry is appended.

    Returns the pathspecs to commit, or ``None`` when the artifact is gone and
    no scoped repair exists (the caller records it as unqualified).
    """

    repo = fixture.root
    original_rel = repair.get("path") or ""
    original = repo / original_rel
    if not original_rel or not original.is_file():
        return None

    try:
        manifest = load_manifest(fixture)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # The manifest itself is unreadable: the writer cannot safely supersede
        # an entry it cannot parse, so it records the item as unqualified.
        return None

    original_bytes = original.read_bytes()
    target_rel = repair.get("supersede_path") or _supersede_path(original_rel)
    target = repo / target_rel
    _write(
        target,
        json.dumps(
            {
                "supersedes": original_rel,
                "original_sha256": sha256_bytes(original_bytes),
                "rendering": "v2",
                "retained": original_bytes.decode("utf-8", "replace"),
            },
            indent=2,
        )
        + "\n",
    )
    target_sha = sha256_path(target)

    updated: list[dict] = []
    for entry in manifest.get("evidence", []):
        if entry.get("path") == original_rel and not entry.get("superseded_by"):
            entry = dict(entry)
            entry["superseded_by"] = target_rel
            entry["superseded_sha256"] = target_sha
        updated.append(entry)
    updated.append(
        {
            "path": target_rel,
            "sha256": target_sha,
            "supersedes": original_rel,
            "version": 2,
        }
    )
    manifest["evidence"] = updated
    # The manifest is committed content, so the superseding entry and the new
    # checksum must land in the same explicit-pathspec Phase B commit.
    write_manifest(fixture, manifest, commit=False)
    return [target_rel, MANIFEST_PATH]


def apply_blockers(fixture: Fixture, blockers: list[dict]) -> dict:
    """Model `opsx-repo-hygiene`: repair exactly the named blockers, in phases.

    Phase A commits the safe baseline (accepted source, the plan, the run-state
    ignore entry, and the perf/evidence script path repair). Phase B adds or
    supersedes evidence. An evidence blocker therefore cannot hold the baseline
    hostage.

    An operator authority restriction bounds what the writer may commit and
    remains a stop: no phase commits at all when it is present.
    """

    repo = fixture.root
    user_owned_before = {
        path: (repo / path).read_bytes()
        for path in fixture.user_owned
        if (repo / path).is_file()
    }

    human_only = [
        blocker
        for blocker in blockers
        if (blocker.get("repair") or {}).get("action") == "operator-decision"
    ]
    if human_only:
        blocker = human_only[0]
        return {
            "status": "blocked",
            "reason": f"operator decision required: {blocker['id']}",
            "blocker": blocker["id"],
            "operator_decision": (blocker.get("repair") or {}).get(
                "operator_decision"
            ),
            "commit": "",
            "staged_paths": [],
            "phases": [],
            "unqualified": [],
            "remaining": [entry["id"] for entry in blockers],
            "unstaged": [],
        }

    # `resolve-slug-collision` is orchestrator-owned: the writer performs no
    # repository mutation for it, and it never renames, moves, archives, or
    # deletes an active change directory.
    orchestrator_owned = [
        blocker
        for blocker in blockers
        if (blocker.get("repair") or {}).get("action") == "resolve-slug-collision"
        or (blocker.get("repair") or {}).get("owner") == "orchestrator"
    ]
    if orchestrator_owned:
        blocker = orchestrator_owned[0]
        return {
            "status": "blocked",
            "reason": (
                f"{blocker['id']} is resolved at plan level by the orchestrator; "
                "opsx-repo-hygiene performs no repository mutation for it"
            ),
            "blocker": blocker["id"],
            "operator_decision": "",
            "commit": "",
            "staged_paths": [],
            "phases": [],
            "unqualified": [],
            "remaining": [entry["id"] for entry in blockers],
            "unstaged": [],
            "actions": [],
        }

    baseline_pathspecs: list[str] = []
    evidence_repairs: list[dict] = []
    actions: list[str] = []

    for blocker in blockers:
        repair = blocker.get("repair") or {}
        action = repair.get("action")
        if action == "commit-pathspecs":
            baseline_pathspecs.extend(repair.get("pathspecs") or [])
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
            baseline_pathspecs.append(".gitignore")
            actions.append(action)
        elif action == "retarget-script":
            script = repo / repair["path"]
            script.write_text(PERF_SCRIPT_REPAIRED_BODY, encoding="utf-8")
            baseline_pathspecs.extend(repair.get("pathspecs") or [repair["path"]])
            actions.append(action)
        elif action == "repair-evidence":
            evidence_repairs.append(blocker)
            actions.append(action)
        else:
            return {
                "status": "blocked",
                "reason": f"unknown repair action for {blocker['id']}",
                "blocker": blocker["id"],
                "operator_decision": "",
                "commit": "",
                "staged_paths": [],
                "phases": [],
                "unqualified": [],
                "remaining": [entry["id"] for entry in blockers],
                "unstaged": [],
            }

    phases: list[dict] = []
    staged_paths: list[str] = []
    unqualified: list[dict] = []
    remaining: list[str] = []

    if not baseline_pathspecs and not evidence_repairs:
        return {
            "status": "blocked",
            "reason": "no explicit implementation scope",
            "blocker": "missing-implementation-scope",
            "operator_decision": "",
            "commit": "",
            "staged_paths": [],
            "phases": [],
            "unqualified": [],
            "remaining": [entry["id"] for entry in blockers],
            "unstaged": [],
            "actions": actions,
        }

    if baseline_pathspecs:
        baseline = _stage_and_commit(
            fixture,
            baseline_pathspecs,
            f"chore(handoff): commit accepted implementation for {fixture.change}",
        )
        if baseline["status"] != "repaired":
            return {
                **baseline,
                "operator_decision": "",
                "phases": [],
                "unqualified": [],
                "remaining": [entry["id"] for entry in blockers],
                "unstaged": [],
                "actions": actions,
            }
        phases.append(
            {
                "phase": "baseline",
                "commit": baseline["commit"],
                "staged_paths": baseline["staged_paths"],
            }
        )
        staged_paths.extend(baseline["staged_paths"])

    evidence_pathspecs: list[str] = []
    for blocker in evidence_repairs:
        repair = blocker["repair"]
        superseded = _supersede_evidence(fixture, repair)
        if superseded is None:
            unqualified.append(
                {
                    "check": "evidence-at-head",
                    "manifest": repair.get("manifest", MANIFEST_PATH),
                    "evidence": repair.get("path"),
                    "missing": True,
                    "verified": False,
                    "disposition": "unqualified",
                }
            )
            remaining.append(blocker["id"])
            continue
        evidence_pathspecs.extend(superseded)

    if evidence_pathspecs:
        evidence = _stage_and_commit(
            fixture,
            evidence_pathspecs,
            f"chore(handoff): supersede retained evidence for {fixture.change}",
        )
        if evidence["status"] != "repaired":
            return {
                **evidence,
                "operator_decision": "",
                "phases": phases,
                "unqualified": unqualified,
                "remaining": remaining
                + [entry["id"] for entry in blockers if entry["id"] not in remaining],
                "unstaged": [],
                "actions": actions,
            }
        phases.append(
            {
                "phase": "evidence",
                "commit": evidence["commit"],
                "staged_paths": evidence["staged_paths"],
            }
        )
        staged_paths.extend(evidence["staged_paths"])

    status = "repaired" if not remaining else "partial"
    commit = phases[-1]["commit"] if phases else ""
    unstaged = [
        {"path": path, "reason": "user-owned; left untouched"}
        for path in fixture.user_owned
        if (repo / path).is_file()
        and (
            (repo / path).read_bytes() != user_owned_before.get(path)
            or path in _paths_from_status(fixture)
        )
    ]
    return {
        "status": status,
        "reason": "" if not remaining else f"remaining blocker(s): {', '.join(remaining)}",
        "blocker": remaining[0] if remaining else "",
        "operator_decision": "",
        "commit": commit,
        "staged_paths": staged_paths,
        "actions": actions,
        "phases": phases,
        "unqualified": unqualified,
        "remaining": remaining,
        "unstaged": unstaged,
        "next_audit_input": f"git rev-parse HEAD -> {commit}",
    }


# ---------------------------------------------------------------------------
# gate decisions, human gates, and advisory caps
# ---------------------------------------------------------------------------


def gate_decision(audit_report: dict) -> str:
    """Return the orchestrator decision for one audit report."""

    if audit_report.get("handoff_ready") is True:
        return "continue"
    if audit_report.get("next_action") == "stop":
        return "stop"
    return "repair"


def next_change(
    audit_report: dict,
    plan_changes: tuple[str, ...],
    current: str,
    *,
    human_gates: tuple[str, ...] = (),
) -> str | None:
    """Return the change to select next, or ``None`` when the gate blocks.

    A gated change is never returned: the orchestrator stops before it.
    """

    if gate_decision(audit_report) != "continue":
        return None
    index = plan_changes.index(current)
    remaining = plan_changes[index + 1 :]
    if not remaining:
        return None
    candidate = remaining[0]
    if candidate in human_gates:
        return None
    return candidate


def parse_frontmatter_human_gates(text: str) -> tuple[str, ...]:
    """Read ``human_gates`` from a markdown plan's YAML frontmatter.

    Stdlib only: an inline list (``human_gates: [a, b]``) and a block list are
    both supported. An absent or empty field is the empty gate set.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ()
    try:
        end = next(
            index for index in range(1, len(lines)) if lines[index].strip() == "---"
        )
    except StopIteration:
        return ()
    block = lines[1:end]
    for index, line in enumerate(block):
        stripped = line.strip()
        if not stripped.startswith("human_gates:"):
            continue
        inline = stripped.split(":", 1)[1].strip()
        if inline.startswith("[") and inline.endswith("]"):
            items = inline[1:-1].split(",")
            return tuple(
                item.strip().strip("'\"") for item in items if item.strip()
            )
        if inline:
            return (inline.strip("'\""),)
        gates: list[str] = []
        for follow in block[index + 1 :]:
            entry = follow.strip()
            if entry.startswith("- "):
                gates.append(entry[2:].strip().strip("'\""))
            elif entry:
                break
        return tuple(gates)
    return ()


def resolve_human_gates(
    plan_changes: tuple[str, ...],
    *,
    explicit=None,
    plan: str = "",
) -> dict:
    """Resolve the human-gate set: explicit input, then frontmatter, then empty.

    ``explicit`` is ``None`` when the invocation supplied no value. A string is
    parsed as a comma list, and ``"none"`` clears the plan's gates while being
    recorded verbatim. An explicit value replaces the frontmatter list; it is
    never merged with it.

    ``source`` uses the orchestrator's ledger vocabulary — ``invocation``,
    ``plan``, or ``default`` — so the fixture and the shipped agent record the
    same provenance.

    An entry that names a change which is not in the plan is a fail-closed
    preflight error: ``ok`` is false and ``gates`` is empty.
    """

    plan_changes = tuple(plan_changes)
    recorded = ""

    if explicit is None:
        gates = parse_frontmatter_human_gates(plan)
        source = "plan" if gates else "default"
    else:
        if isinstance(explicit, str):
            recorded = explicit
            if explicit.strip().lower() == "none":
                gates = ()
            else:
                gates = tuple(
                    item.strip() for item in explicit.split(",") if item.strip()
                )
        else:
            recorded = ", ".join(str(item) for item in explicit)
            gates = tuple(
                str(item).strip() for item in explicit if str(item).strip()
            )
        source = "invocation"

    # Preserve order, drop duplicates.
    gates = tuple(dict.fromkeys(gates))
    unknown = tuple(slug for slug in gates if slug not in plan_changes)
    return {
        "gates": () if unknown else gates,
        "source": source,
        "recorded": recorded,
        "unknown": unknown,
        "ok": not unknown,
        "error": (
            ""
            if not unknown
            else f"unknown human gate slug: {', '.join(unknown)}"
        ),
    }


def human_gate_stop(
    resolution: dict,
    plan_changes: tuple[str, ...],
    current: str,
) -> dict:
    """Return the orchestrator's stop record before a gated change.

    The record names the gated change, the changes that are still runnable
    before it, and the resume instruction. A preflight error (an unknown gate
    slug) stops before any worker is dispatched.
    """

    if not resolution.get("ok", False):
        return {
            "stop": True,
            "kind": "preflight-error",
            "gated_change": None,
            "runnable": (),
            "reason": resolution.get("error", ""),
            "resume": "correct the human_gates value, then re-run",
        }

    gates = tuple(resolution.get("gates", ()))
    index = plan_changes.index(current) if current in plan_changes else -1
    runnable: list[str] = []
    for slug in plan_changes[index + 1 :]:
        if slug in gates:
            return {
                "stop": True,
                "kind": "human-gate",
                "gated_change": slug,
                "runnable": tuple(runnable),
                "reason": f"{slug} is in the resolved human-gate set",
                "resume": (
                    f"the operator clears the gate for {slug} and the run "
                    f"resumes at {slug}"
                ),
            }
        runnable.append(slug)
    return {
        "stop": False,
        "kind": "",
        "gated_change": None,
        "runnable": tuple(runnable),
        "reason": "",
        "resume": "",
    }


def cap_override(
    cap: str,
    cap_value: str,
    consumed: int,
    remaining_work: str,
    justification: str,
    checkpoint: str,
) -> dict:
    """Build a ``cap_override`` ledger record.

    ``valid`` is true only when the record names the cap, its value, the
    concrete remaining work, why continuing is safer, and the next checkpoint.
    """

    record = {
        "event": "cap_override",
        "cap": cap,
        "cap_value": cap_value,
        "consumed": int(consumed),
        "remaining_work": remaining_work,
        "justification": justification,
        "checkpoint": checkpoint,
    }
    record["valid"] = all(
        str(record[key]).strip()
        for key in ("cap", "cap_value", "remaining_work", "justification", "checkpoint")
    )
    return record


def cap_decision(
    *,
    cap_reached: bool,
    remaining_work: str,
    override: dict | None = None,
    human_only_decision: str = "",
) -> dict:
    """Decide continue-versus-stop when a cap is reached.

    A cap alone never produces ``stop``. ``stop`` is returned only for a true
    human-only decision, and the cap is never reported as the reason.
    """

    if human_only_decision:
        return {
            "decision": "stop",
            "reason": human_only_decision,
            "cap_used_as_reason": False,
            "override_required": False,
            "override": override,
        }
    if not cap_reached:
        return {
            "decision": "continue",
            "reason": "cap not reached",
            "cap_used_as_reason": False,
            "override_required": False,
            "override": override,
        }
    if not str(remaining_work).strip():
        return {
            "decision": "complete",
            "reason": "cap reached with no remaining work",
            "cap_used_as_reason": False,
            "override_required": False,
            "override": override,
        }
    override_recorded = bool(override) and bool(override.get("valid"))
    return {
        "decision": "continue",
        "reason": (
            "cap override recorded; remaining work is safe and scoped"
            if override_recorded
            else "cap is advisory; record a cap_override before exceeding it"
        ),
        "cap_used_as_reason": False,
        "override_required": not override_recorded,
        "override": override,
    }


# ---------------------------------------------------------------------------
# scenario injection
# ---------------------------------------------------------------------------


def add_remote_upstream(fixture: Fixture, name: str = "origin") -> str:
    """Give the local branch a configured upstream without a network remote."""

    repo = fixture.root
    remote = repo.parent / f"{name}.git"
    _git(repo, "remote", "add", name, str(remote))
    _git(repo, "update-ref", f"refs/remotes/{name}/main", "HEAD")
    _git(repo, "branch", f"--set-upstream-to={name}/main", "main")
    return f"{name}/main"


def inject_slug_collision(
    fixture: Fixture, occupying_change: str = OCCUPYING_CHANGE
) -> str:
    """Commit a *different* change's proposal into the planned next-change slug.

    The occupying directory declares ``occupying_change`` rather than
    ``fixture.next_change``, so this is a real collision: the plan's planned slug
    is taken. It is not the planned change being already active.

    The default occupant is ``OCCUPYING_CHANGE``, which is deliberately not in
    the plan. Pass a slug that is in ``fixture.plan_changes`` or a declared
    dependency to model a plan-reachable occupant the orchestrator may resolve
    by letting the pipeline archive it.
    """

    repo = fixture.root
    proposal_rel = f"openspec/changes/{fixture.next_change}/proposal.md"
    _write(repo / proposal_rel, f"# Proposal: {occupying_change}\n")
    _git(repo, "add", "--", proposal_rel)
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        f"docs({occupying_change}): propose the occupying change",
    )
    return f"openspec/changes/{fixture.next_change}"


def inject_active_planned_change(fixture: Fixture) -> str:
    """Commit the planned next change's own proposal into its planned slug.

    The directory declares ``fixture.next_change`` itself, which models the next
    change being already authored and in progress. That is not a collision:
    readiness passes with no blocker, and nothing is started or mutated.
    """

    repo = fixture.root
    proposal_rel = f"openspec/changes/{fixture.next_change}/proposal.md"
    _write(repo / proposal_rel, f"# Proposal: {fixture.next_change}\n")
    _git(repo, "add", "--", proposal_rel)
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        f"docs({fixture.next_change}): author the planned change",
    )
    return f"openspec/changes/{fixture.next_change}"


def remove_manifest(fixture: Fixture) -> str:
    """Delete the evidence manifest from ``HEAD`` and the worktree.

    The auditor contract distinguishes this from an unreadable manifest: no
    committed evidence manifest is a `passed` evidence check, because nothing
    references one. A manifest that exists in either place but cannot be used
    stays the failure path instead.
    """

    repo = fixture.root
    path = repo / MANIFEST_PATH
    if _git_ok(repo, "cat-file", "-e", f"HEAD:{MANIFEST_PATH}"):
        _git(repo, "rm", "-q", "--", MANIFEST_PATH)
        _git(repo, "commit", "-q", "-m", "test: remove the evidence manifest")
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    return MANIFEST_PATH


def uncommit_manifest(fixture: Fixture) -> str:
    """Remove the manifest from ``HEAD`` while leaving the worktree copy.

    This is the absent-at-HEAD / present-in-worktree case: the manifest is not
    committed content, so it must never satisfy the evidence check as if it
    were.
    """

    repo = fixture.root
    if _git_ok(repo, "cat-file", "-e", f"HEAD:{MANIFEST_PATH}"):
        _git(repo, "rm", "--cached", "-q", "--", MANIFEST_PATH)
        _git(
            repo,
            "commit",
            "-q",
            "-m",
            "test: uncommit the evidence manifest",
        )
    return MANIFEST_PATH


def remove_worktree_manifest(fixture: Fixture) -> str:
    """Delete only the worktree manifest, leaving the committed copy at HEAD.

    The audit reads ``HEAD``; a missing worktree copy of a committed manifest is
    therefore not a missing artifact.
    """

    path = fixture.root / MANIFEST_PATH
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    return MANIFEST_PATH


def inject_stale_evidence_checksum(fixture: Fixture) -> dict:
    """Record a stale checksum for a retained artifact (supersedable gap)."""

    manifest = load_manifest(fixture)
    manifest["evidence"] = [
        {
            "path": EVIDENCE_PATH,
            "sha256": sha256_bytes(b"an earlier rendering of the benchmark\n"),
            "format": "v2",
        }
    ]
    write_manifest(fixture, manifest)
    return manifest


def inject_missing_evidence_artifact(
    fixture: Fixture,
    *,
    sole_claim_basis: bool = False,
    claim: dict | None = None,
    independent_support: list | None = None,
) -> dict:
    """Reference an evidence artifact that exists nowhere (unqualified gap).

    ``sole_claim_basis=True`` declares an accepted claim whose only evidence is
    the missing artifact: no other committed artifact, plan acceptance, or
    passing hard check supports it, which is the auditor's one human-only
    evidence boundary. ``sole_claim_basis=False`` gives the same claim an
    independent committed artifact (``SUPPORT_PATH``) whose content carries the
    claim id, so the support genuinely substantiates the claim and the gap is a
    recordable ``unqualified`` carry-forward instead of an adjudication.

    ``independent_support`` declares the claim's support references explicitly,
    so a scenario can point at a path that is absent from ``HEAD`` (for example
    ``UNCOMMITTED_SUPPORT_PATH`` or ``WORKTREE_ONLY_SUPPORT_PATH``), record a
    hash that does not match the ``HEAD`` content, or name an unrelated
    committed file. The claim-support check must refuse every one of those,
    leaving the missing artifact as the sole basis.
    """

    manifest = load_manifest(fixture)
    manifest["evidence"] = [
        {
            "path": GONE_EVIDENCE_PATH,
            "sha256": sha256_bytes(b"retired benchmark artifact\n"),
        }
    ]
    if claim is None:
        if independent_support is not None:
            support = list(independent_support)
        else:
            support = (
                []
                if sole_claim_basis
                else [{"path": SUPPORT_PATH, "supports_claim": GONE_CLAIM_ID}]
            )
        claim = {
            "id": GONE_CLAIM_ID,
            "statement": GONE_CLAIM_STATEMENT,
            "evidence": [GONE_EVIDENCE_PATH],
            "independent_support": support,
        }
    manifest["accepted_claims"] = [claim]
    write_manifest(fixture, manifest)
    return manifest


def inject_malformed_manifest(fixture: Fixture, text: str = '{"accepted": [') -> str:
    """Write a structurally malformed evidence manifest.

    The default is invalid JSON; pass a shape such as
    ``{"accepted": "src/api/cache.py", "plan": []}`` to exercise a manifest that
    parses but does not declare its entries. The audit must report this as an
    evidence check result rather than raising.
    """

    _write(fixture.root / MANIFEST_PATH, text + "\n")
    _commit_manifest(fixture, "test: write a malformed evidence manifest")
    return text


def make_manifest_unreadable(fixture: Fixture) -> str:
    """Commit manifest bytes that cannot decode as UTF-8.

    The audit reads the committed manifest, so replacing the worktree file with
    a directory would no longer exercise the unreadable branch: ``HEAD`` would
    still hold the old readable blob. Invalid bytes are committed instead, which
    is portable even when the suite runs as root.
    """

    path = fixture.root / MANIFEST_PATH
    if path.is_dir():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00not-utf8\n")
    _commit_manifest(fixture, "test: make the evidence manifest unreadable")
    return str(path)


def inject_archived_next_change(fixture: Fixture) -> str:
    """Commit an archived change directory under the planned next-change slug.

    The auditor reports a same-slug *archived* directory as informational
    context, so readiness still passes; only an active directory is a collision.
    """

    repo = fixture.root
    archived_rel = f"openspec/changes/archive/2026-01-01-{fixture.next_change}"
    _write(
        repo / archived_rel / "proposal.md",
        f"# Proposal: {fixture.next_change}\n",
    )
    _git(repo, "add", "--", f"{archived_rel}/proposal.md")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        f"archive({fixture.next_change}): archive an earlier attempt",
    )
    return archived_rel


def inject_regressed_perf_script(fixture: Fixture) -> str:
    """Commit the archived-path default back into the perf script.

    A later commit can legitimately reintroduce the defect, which makes it a
    fresh scoped blocker for the next repair round.
    """

    repo = fixture.root
    _write(repo / PERF_SCRIPT_PATH, PERF_SCRIPT_BODY)
    _git(repo, "add", "--", PERF_SCRIPT_PATH)
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "test: reinstate the archived evidence default",
    )
    return PERF_SCRIPT_PATH


def apply_operator_waiver(
    fixture: Fixture, waiver: str = ARCHIVE_ONLY_WAIVER
) -> str:
    """Record an operator authority restriction verbatim."""

    manifest = load_manifest(fixture)
    manifest["archive_only_waiver"] = waiver
    write_manifest(fixture, manifest)
    return waiver
