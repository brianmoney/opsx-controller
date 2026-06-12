#!/usr/bin/env python3
"""opsx-plan: deterministic plan-level orchestrator for /opsx-drive.

Iterates a TOML plan manifest of OpenSpec changes (a DAG), invoking the
client adapter's opsx-drive controller headlessly for each ready change,
verifying completion from ground truth (controller state file, archive
directory, archive commit), and gating progress on configurable fast checks.

Design rules:
  - The orchestrator is deterministic. All LLM judgment lives inside
    /opsx-drive. This layer only does ordering, dispatch, and verification.
  - Never trust the drive process exit code or stdout as success. A change
    is done only when independent evidence agrees.
  - A failed change blocks its dependents; independent branches continue.
  - Changes with pause_before=true wait for explicit `approve`.
  - State is reconciled against the repository on startup, so the run can
    be killed and resumed at any time.

Requires Python 3.11+ (tomllib). Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("opsx-plan requires Python 3.11+ (tomllib)")

# ---------------------------------------------------------------------------
# Adapter defaults. Both fields accept a {change} placeholder and may be
# overridden in the [plan] table. Verify the invoke command for your client
# version before an unattended run.
# ---------------------------------------------------------------------------
ADAPTER_DEFAULTS = {
    "opencode": {
        "invoke": 'opencode run "/opsx-drive {change}"',
        "state_file": ".opencode/opsx-controller/{change}.json",
    },
    "claude-code": {
        "invoke": 'claude -p "/opsx-drive {change}"',
        "state_file": ".claude/opsx-controller/{change}.json",
    },
    "codex-cli": {
        "invoke": 'codex exec "$opsx-drive {change}"',
        "state_file": ".opsx-controller/{change}.json",
    },
}

DONE = "done"
PENDING = "pending"
RUNNING = "running"
FAILED = "failed"
SKIPPED = "skipped"

ARCHIVE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

_current_proc: subprocess.Popen | None = None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[opsx-plan {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Plan manifest
# ---------------------------------------------------------------------------

class PlanError(Exception):
    pass


def load_plan(path: Path) -> dict:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    plan = raw.get("plan", {})
    changes = raw.get("changes", [])
    if not changes:
        raise PlanError("plan has no [[changes]] entries")

    adapter = plan.get("adapter", "opencode")
    if adapter not in ADAPTER_DEFAULTS and not (
        plan.get("invoke") and plan.get("state_file")
    ):
        raise PlanError(
            f"unknown adapter '{adapter}' and no invoke/state_file overrides given"
        )
    defaults = ADAPTER_DEFAULTS.get(adapter, {})

    cfg = {
        "name": plan.get("name") or path.stem,
        "adapter": adapter,
        "invoke": plan.get("invoke", defaults.get("invoke")),
        "state_file": plan.get("state_file", defaults.get("state_file")),
        "timeout_minutes": float(plan.get("timeout_minutes", 90)),
        "max_attempts": int(plan.get("max_attempts", 2)),
        "fast_checks": list(plan.get("fast_checks", [])),
        "check_timeout_minutes": float(plan.get("check_timeout_minutes", 15)),
        "require_clean_tracked": bool(plan.get("require_clean_tracked", True)),
        # --- create stage (the /opsx-ff automation) ---
        "plan_doc": plan.get("plan_doc", ""),
        "create_invoke": plan.get("create_invoke", ""),
        "create_timeout_minutes": float(plan.get("create_timeout_minutes", 30)),
        "create_max_attempts": int(plan.get("create_max_attempts", 2)),
        "review_created": bool(plan.get("review_created", True)),
        "created_check": plan.get(
            "created_check", "openspec validate {change} --strict"
        ),
    }

    by_id: dict[str, dict] = {}
    for c in changes:
        cid = c.get("id")
        if not cid:
            raise PlanError("a [[changes]] entry is missing 'id'")
        if cid in by_id:
            raise PlanError(f"duplicate change id: {cid}")
        by_id[cid] = {
            "id": cid,
            "phase": c.get("phase"),
            "depends_on": list(c.get("depends_on", [])),
            "pause_before": bool(c.get("pause_before", False)),
            "enabled": bool(c.get("enabled", True)),
            "timeout_minutes": float(
                c.get("timeout_minutes", cfg["timeout_minutes"])
            ),
            "max_attempts": int(c.get("max_attempts", cfg["max_attempts"])),
            "create_invoke": c.get("create_invoke", cfg["create_invoke"]),
            "create_max_attempts": int(
                c.get("create_max_attempts", cfg["create_max_attempts"])
            ),
        }

    for c in by_id.values():
        for dep in c["depends_on"]:
            if dep not in by_id:
                raise PlanError(f"{c['id']}: unknown dependency '{dep}'")

    cfg["order"] = topo_sort(by_id)
    cfg["changes"] = by_id
    return cfg


def topo_sort(by_id: dict[str, dict]) -> list[str]:
    """Kahn's algorithm; deterministic (manifest order breaks ties)."""
    ids = list(by_id)
    indeg = {cid: len(by_id[cid]["depends_on"]) for cid in ids}
    dependents: dict[str, list[str]] = {cid: [] for cid in ids}
    for cid in ids:
        for dep in by_id[cid]["depends_on"]:
            dependents[dep].append(cid)

    queue = [cid for cid in ids if indeg[cid] == 0]
    order: list[str] = []
    while queue:
        cid = queue.pop(0)
        order.append(cid)
        for nxt in dependents[cid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(ids):
        cyclic = sorted(set(ids) - set(order))
        raise PlanError(f"dependency cycle involving: {', '.join(cyclic)}")
    return order


# ---------------------------------------------------------------------------
# Orchestrator state (.opsx-plan/<name>.state.json)
# ---------------------------------------------------------------------------

def state_path(repo: Path, plan_name: str) -> Path:
    return repo / ".opsx-plan" / f"{plan_name}.state.json"


def load_state(repo: Path, plan_name: str) -> dict:
    p = state_path(repo, plan_name)
    if p.exists():
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return {"plan": plan_name, "approvals": [], "changes": {}}


def save_state(repo: Path, plan_name: str, state: dict) -> None:
    p = state_path(repo, plan_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    gi = p.parent / ".gitignore"
    if not gi.exists():  # orchestrator state is operational, never committed
        gi.write_text("*\n", encoding="utf-8")
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def rec(state: dict, cid: str) -> dict:
    return state["changes"].setdefault(
        cid,
        {
            "status": PENDING, "attempts": 0, "reason": "", "updated_at": "",
            "create_attempts": 0, "created_by_orchestrator": False,
            "accepted": False,
        },
    )


def set_status(state: dict, cid: str, status: str, reason: str = "") -> None:
    r = rec(state, cid)
    r["status"] = status
    r["reason"] = reason
    r["updated_at"] = utcnow()


# ---------------------------------------------------------------------------
# Ground-truth verification
# ---------------------------------------------------------------------------

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
        if entry.is_dir() and entry.name.endswith(f"-{cid}") and ARCHIVE_DIR_RE.match(
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
    if (repo / "openspec" / "changes" / cid).exists():
        return False, f"openspec/changes/{cid} still exists"

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
        log(f"  note: {cid} archived but {'; '.join(warnings)}")

    return True, ""


def run_fast_checks(repo: Path, cfg: dict) -> tuple[bool, str]:
    timeout = cfg["check_timeout_minutes"] * 60
    for cmd in cfg["fast_checks"]:
        log(f"  check: {cmd}")
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
    return repo / "openspec" / "changes" / cid


def verify_change_created(repo: Path, cfg: dict, cid: str) -> tuple[bool, str]:
    """A change counts as created only when independent evidence agrees:
    1. openspec/changes/<id> exists with proposal.md and tasks.md
    2. the configured created_check command (default
       `openspec validate <id> --strict`) exits 0
    3. creation touched no tracked files (change authoring is additive)
    """
    reasons: list[str] = []
    cdir = change_dir(repo, cid)
    if not cdir.is_dir():
        return False, f"openspec/changes/{cid} does not exist"
    for artifact in ("proposal.md", "tasks.md"):
        if not (cdir / artifact).is_file():
            reasons.append(f"missing {artifact}")

    check = cfg["created_check"].format(change=cid)
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

    if not tracked_tree_clean(repo):
        reasons.append(
            "creation modified tracked files; change authoring must be "
            "additive (review with `git status` before continuing)"
        )
    return (not reasons, "; ".join(reasons))


def tracked_tree_clean(repo: Path) -> bool:
    res = git(repo, "status", "--porcelain", "--untracked-files=no")
    if res.returncode != 0:
        return False
    lines = [
        ln for ln in res.stdout.splitlines()
        if ln.strip() and not ln[3:].startswith(".opsx-plan/")
    ]
    return not lines


# ---------------------------------------------------------------------------
# Retry policy: read the controller's own schema-v3 state to decide whether
# re-invoking /opsx-drive can plausibly succeed.
# ---------------------------------------------------------------------------

NO_RETRY_RESULTS = {"max_rounds_reached", "no_progress"}


def retry_makes_sense(controller_state: dict | None) -> tuple[bool, str]:
    if controller_state is None:
        return True, "no controller state written; treating as transient"
    if controller_state.get("_malformed"):
        return False, "controller state file is malformed; needs operator"
    last = controller_state.get("last_result", "")
    if last in NO_RETRY_RESULTS:
        return False, f"controller stopped with {last}; needs operator"
    if last == "archive_failed":
        outlook = (
            controller_state.get("archive", {})
            .get("triage", {})
            .get("retry_outlook", "unknown")
        )
        if outlook == "same_failure":
            return False, "archive triage says retry would fail the same way"
        return True, f"archive failed with retry_outlook={outlook}"
    return True, f"last_result={last or 'unset'}"


# ---------------------------------------------------------------------------
# Drive invocation
# ---------------------------------------------------------------------------

def run_stage(
    repo: Path, cfg: dict, cid: str, stage: str, invoke_tpl: str,
    timeout_minutes: float, attempt: int,
) -> tuple[str, Path]:
    """Run a templated stage command ('create' or 'drive'). Returns
    (outcome, log_path) where outcome is 'exited', 'timeout', or
    'spawn_error'. Output goes to a log file so it can be tailed live;
    the exit code is informational only."""
    global _current_proc
    log_dir = repo / ".opsx-plan" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cid}.{stage}{attempt}.log"

    cmd = shlex.split(
        invoke_tpl.format(change=cid, plan_doc=cfg["plan_doc"])
    )
    timeout_s = timeout_minutes * 60
    log(f"  exec[{stage}]: {' '.join(cmd)}  "
        f"(timeout {timeout_s/60:g}m, log {log_path})")

    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"# {utcnow()} {stage} attempt {attempt}: {' '.join(cmd)}\n")
            lf.flush()
            proc = subprocess.Popen(
                cmd, cwd=repo, stdout=lf, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _current_proc = proc
            try:
                proc.wait(timeout=timeout_s)
                return "exited", log_path
            except subprocess.TimeoutExpired:
                terminate_group(proc)
                return "timeout", log_path
            finally:
                _current_proc = None
    except FileNotFoundError:
        return "spawn_error", log_path


def drive_change(repo: Path, cfg: dict, cid: str, attempt: int) -> tuple[str, Path]:
    return run_stage(
        repo, cfg, cid, "drive", cfg["invoke"],
        cfg["changes"][cid]["timeout_minutes"], attempt,
    )


def terminate_group(proc: subprocess.Popen, grace: float = 15.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.5)
        os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=10)
    except (ProcessLookupError, PermissionError):
        pass


def handle_sigint(signum, frame):  # noqa: ARG001
    log("interrupted; terminating active drive process group")
    if _current_proc is not None:
        terminate_group(_current_proc)
    sys.exit(130)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def classify(cfg: dict, state: dict, cid: str) -> str:
    """Computed status for reporting: includes blocked/awaiting_approval."""
    c = cfg["changes"][cid]
    r = rec(state, cid)
    if not c["enabled"]:
        return SKIPPED
    if r["status"] in (DONE, FAILED, RUNNING):
        return r["status"]
    for dep in c["depends_on"]:
        dep_status = classify(cfg, state, dep)
        if dep_status in (FAILED, "blocked"):
            return "blocked"
        if dep_status != DONE:
            return PENDING
    if c["pause_before"] and cid not in state["approvals"]:
        return "awaiting_approval"
    if (
        cfg["review_created"]
        and r.get("created_by_orchestrator")
        and not r.get("accepted")
    ):
        return "awaiting_acceptance"
    return "ready"


def reconcile(repo: Path, cfg: dict, state: dict) -> None:
    """Make recorded state agree with repository reality."""
    for cid in cfg["order"]:
        r = rec(state, cid)
        if r["status"] == RUNNING:  # stale from a killed run
            set_status(state, cid, PENDING, "recovered from interrupted run")
        if r["status"] != DONE:
            ok, _ = verify_change_done(repo, cfg, cid)
            if ok:
                set_status(state, cid, DONE, "verified from repository evidence")
                log(f"reconcile: {cid} already archived; marked done")
        else:
            ok, why = verify_change_done(repo, cfg, cid)
            if not ok:
                set_status(
                    state, cid, FAILED,
                    f"recorded done but evidence missing: {why}",
                )
                log(f"reconcile: {cid} done-state no longer verifiable: {why}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, handle_sigint)

    reconcile(repo, cfg, state)
    save_state(repo, cfg["name"], state)

    if args.dry_run:
        return cmd_status_inner(cfg, state, header="dry run: planned order")

    budget_deadline = (
        time.monotonic() + args.budget_minutes * 60 if args.budget_minutes else None
    )
    ran = 0
    visited: set[str] = set()  # avoid re-picking the same change this run

    while True:
        if budget_deadline and time.monotonic() > budget_deadline:
            log("wall-clock budget exhausted; stopping")
            break
        if args.max_changes and ran >= args.max_changes:
            log("max-changes reached; stopping")
            break

        ready = [
            c for c in cfg["order"]
            if c not in visited and classify(cfg, state, c) == "ready"
        ]
        if args.only:
            ready = [c for c in ready if c in args.only]
        if not ready:
            break

        cid = ready[0]
        change_cfg = cfg["changes"][cid]
        r = rec(state, cid)
        needs_create = not change_dir(repo, cid).exists()

        if cfg["require_clean_tracked"] and not tracked_tree_clean(repo):
            log("tracked worktree is dirty; refusing to start a new stage")
            log("commit/stash tracked modifications, then re-run")
            return 2

        # ----- create stage: automate the repetitive /opsx-ff invocation -----
        if needs_create:
            if not change_cfg["create_invoke"]:
                set_status(
                    state, cid, FAILED,
                    "change not created and no create_invoke configured",
                )
                save_state(repo, cfg["name"], state)
                continue
            c_attempt = r["create_attempts"] + 1
            if c_attempt > change_cfg["create_max_attempts"]:
                set_status(state, cid, FAILED, "create retry budget exhausted")
                save_state(repo, cfg["name"], state)
                continue

            log(f"=== {cid} create "
                f"(attempt {c_attempt}/{change_cfg['create_max_attempts']}) ===")
            r["create_attempts"] = c_attempt
            set_status(state, cid, RUNNING, "creating change")
            save_state(repo, cfg["name"], state)

            outcome, log_path = run_stage(
                repo, cfg, cid, "create", change_cfg["create_invoke"],
                cfg["create_timeout_minutes"], c_attempt,
            )
            r["last_log"] = str(log_path)

            if outcome == "spawn_error":
                set_status(state, cid, FAILED,
                           f"could not spawn create: {change_cfg['create_invoke']}")
                save_state(repo, cfg["name"], state)
                return 2

            ok, why = verify_change_created(repo, cfg, cid)
            if ok:
                r["created_by_orchestrator"] = True
                set_status(state, cid, PENDING, "created and verified")
                log(f"  created: {cid}")
                if cfg["review_created"]:
                    log(f"  awaiting acceptance — review openspec/changes/{cid}/ "
                        f"then run: opsx-plan accept <plan> {cid}")
            else:
                if outcome == "timeout":
                    why = f"create timed out; {why}"
                if c_attempt < change_cfg["create_max_attempts"]:
                    set_status(state, cid, PENDING, f"create will retry: {why}")
                    log(f"  create not verified ({why}); retrying")
                else:
                    set_status(state, cid, FAILED, f"create failed: {why}")
                    log(f"  CREATE FAILED: {why}")
            save_state(repo, cfg["name"], state)
            # re-classify: acceptance gate may now hold this change
            continue

        if args.create_only:
            visited.add(cid)  # exists already; nothing to create, don't drive
            continue

        # ----- drive stage -----
        attempt = r["attempts"] + 1
        if attempt > change_cfg["max_attempts"]:
            set_status(state, cid, FAILED, "retry budget exhausted")
            save_state(repo, cfg["name"], state)
            continue

        log(f"=== {cid} (attempt {attempt}/{change_cfg['max_attempts']}) ===")
        r["attempts"] = attempt
        set_status(state, cid, RUNNING)
        save_state(repo, cfg["name"], state)

        outcome, log_path = drive_change(repo, cfg, cid, attempt)
        rec(state, cid)["last_log"] = str(log_path)

        if outcome == "spawn_error":
            set_status(state, cid, FAILED, f"could not spawn: {cfg['invoke']}")
            save_state(repo, cfg["name"], state)
            return 2

        ok, why = verify_change_done(repo, cfg, cid)
        if ok:
            checks_ok, check_why = run_fast_checks(repo, cfg)
            if checks_ok:
                set_status(state, cid, DONE, "verified + checks passed")
                log(f"  done: {cid}")
                ran += 1
            else:
                # The change is archived but the repo fails checks. Re-driving
                # cannot fix this; an operator must intervene.
                set_status(state, cid, FAILED, f"post-archive {check_why}")
                log(f"  FAILED post-archive checks: {check_why}")
        else:
            cs = read_controller_state(repo, cfg, cid)
            can_retry, retry_why = retry_makes_sense(cs)
            if outcome == "timeout":
                why = f"drive timed out; {why}"
            detail = f"{why} :: {retry_why}"
            if can_retry and attempt < change_cfg["max_attempts"]:
                set_status(state, cid, PENDING, f"will retry: {detail}")
                log(f"  not done yet ({detail}); retrying")
            else:
                set_status(state, cid, FAILED, detail)
                log(f"  FAILED: {detail}")

        save_state(repo, cfg["name"], state)

    print()
    return cmd_status_inner(cfg, state, header="run finished")


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    reconcile(repo, cfg, state)
    save_state(repo, cfg["name"], state)
    return cmd_status_inner(cfg, state, header=f"plan: {cfg['name']}")


def cmd_status_inner(cfg: dict, state: dict, header: str) -> int:
    print(header)
    width = max(len(c) for c in cfg["order"])
    failed = 0
    for cid in cfg["order"]:
        status = classify(cfg, state, cid)
        r = rec(state, cid)
        extra = f"  ({r['reason']})" if r.get("reason") and status != DONE else ""
        phase = cfg["changes"][cid].get("phase")
        phase_s = f"P{phase} " if phase is not None else ""
        print(f"  {phase_s}{cid.ljust(width)}  {status}{extra}")
        if status in (FAILED, "blocked"):
            failed += 1
    return 1 if failed else 0


def cmd_approve(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    for cid in args.change:
        if cid not in cfg["changes"]:
            print(f"unknown change: {cid}", file=sys.stderr)
            return 2
        if cid not in state["approvals"]:
            state["approvals"].append(cid)
            log(f"approved: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    """Mark orchestrator-created changes as reviewed so drive may proceed."""
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    for cid in args.change:
        if cid not in cfg["changes"]:
            print(f"unknown change: {cid}", file=sys.stderr)
            return 2
        ok, why = verify_change_created(repo, cfg, cid)
        if not ok:
            print(f"refusing to accept {cid}: {why}", file=sys.stderr)
            return 2
        rec(state, cid)["accepted"] = True
        log(f"accepted: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    cfg = load_plan(Path(args.plan).resolve())
    state = load_state(repo, cfg["name"])
    for cid in args.change:
        if cid not in cfg["changes"]:
            print(f"unknown change: {cid}", file=sys.stderr)
            return 2
        state["changes"][cid] = {
            "status": PENDING, "attempts": 0, "create_attempts": 0,
            "created_by_orchestrator": False, "accepted": False,
            "reason": "reset by operator", "updated_at": utcnow(),
        }
        log(f"reset: {cid}")
    save_state(repo, cfg["name"], state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="opsx-plan", description=__doc__)
    ap.add_argument("--repo", default=".", help="host project root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the plan")
    p_run.add_argument("plan", help="path to plan TOML")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--only", nargs="*", default=None,
                       help="restrict to these change ids (deps must be done)")
    p_run.add_argument("--max-changes", type=int, default=0)
    p_run.add_argument("--budget-minutes", type=float, default=0)
    p_run.add_argument("--create-only", action="store_true",
                       help="create+verify ready changes without driving them")
    p_run.set_defaults(fn=cmd_run)

    p_status = sub.add_parser("status", help="reconcile and show plan status")
    p_status.add_argument("plan")
    p_status.set_defaults(fn=cmd_status)

    p_approve = sub.add_parser("approve", help="approve pause_before changes")
    p_approve.add_argument("plan")
    p_approve.add_argument("change", nargs="+")
    p_approve.set_defaults(fn=cmd_approve)

    p_accept = sub.add_parser(
        "accept", help="accept orchestrator-created changes for driving"
    )
    p_accept.add_argument("plan")
    p_accept.add_argument("change", nargs="+")
    p_accept.set_defaults(fn=cmd_accept)

    p_reset = sub.add_parser("reset", help="reset a failed change to pending")
    p_reset.add_argument("plan")
    p_reset.add_argument("change", nargs="+")
    p_reset.set_defaults(fn=cmd_reset)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
