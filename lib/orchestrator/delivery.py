"""Git delivery: branch resolution, PR prerequisites, and PR creation."""
from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

from lib.orchestrator import base, groundtruth


def _git_current_branch(repo: Path) -> str | None:
    """Return the current symbolic HEAD branch name, or None when detached."""
    res = groundtruth.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if res.returncode != 0:
        return None
    name = res.stdout.strip()
    return name if name and name != "HEAD" else None


def _git_branch_exists(repo: Path, branch_name: str) -> bool:
    """Return True when *branch_name* exists in the local repository."""
    res = groundtruth.git(repo, "rev-parse", "--verify", f"refs/heads/{branch_name}")
    return res.returncode == 0


def _git_create_and_checkout_branch(
    repo: Path, branch_name: str, base_ref: str
) -> str | None:
    """Create and checkout *branch_name* from *base_ref*.  Returns None on
    success or an error message string on failure."""
    res = groundtruth.git(repo, "checkout", "-b", branch_name, base_ref)
    if res.returncode != 0:
        return f"git checkout -b {branch_name} {base_ref} failed: {res.stderr.strip()}"
    return None


def _git_current_head_on_branch(repo: Path, branch_name: str) -> bool:
    """Return True when HEAD points to *branch_name*."""
    current = _git_current_branch(repo)
    return current == branch_name


def resolve_delivery_branch_name(plan_name: str, git_delivery_cfg: dict) -> str:
    """Resolve the delivery branch name from config or derive from plan name."""
    configured = (git_delivery_cfg.get("branch") or "").strip()
    if configured:
        return configured
    return f"opsx/{plan_name}"


def resolve_delivery_base_ref(
    repo: Path, git_delivery_cfg: dict
) -> tuple[str | None, str | None]:
    """Resolve the delivery base ref from config or current HEAD.

    Returns ``(base_ref, error_message)``.  When *error_message* is None,
    *base_ref* is the resolved base ref.
    """
    configured = (git_delivery_cfg.get("base_ref") or "").strip()
    if configured:
        return configured, None
    current = _git_current_branch(repo)
    if current is None:
        return None, "HEAD is detached; cannot resolve base ref for delivery branch"
    return current, None


def ensure_delivery_branch(
    repo: Path,
    cfg: dict,
    state: dict,
    no_branch: bool = False,
) -> tuple[bool, str | None]:
    """Ensure the delivery branch is created/verified before plan dispatch.

    Returns ``(proceed, error)``.  When *proceed* is True, the run may continue.
    When *error* is not None, the run must stop with that error message.
    """
    git_delivery_cfg = cfg.get("git_delivery", {})
    if not git_delivery_cfg.get("enabled", False):
        return True, None

    gd_state = state.get("git_delivery", {})
    recorded_branch = gd_state.get("branch_name")

    # --- Resume guard: branch already recorded ---
    if recorded_branch:
        if no_branch:
            return False, (
                f"cannot use --no-branch: delivery branch "
                f"'{recorded_branch}' has already been recorded for this plan state"
            )
        if not _git_current_head_on_branch(repo, recorded_branch):
            actual = _git_current_branch(repo) or "(detached)"
            return False, (
                f"expected delivery branch '{recorded_branch}' but HEAD is on "
                f"'{actual}'; checkout the recorded branch to resume this plan run"
            )
        return True, None

    # --- First run: create delivery branch ---
    if no_branch:
        base.log("--no-branch: skipping delivery branch creation for this run")
        return True, None

    if not groundtruth.tracked_tree_clean(repo):
        return False, (
            "tracked worktree is dirty; commit or stash changes before "
            "delivery branch creation"
        )

    branch_name = resolve_delivery_branch_name(cfg["name"], git_delivery_cfg)
    base_ref, err = resolve_delivery_base_ref(repo, git_delivery_cfg)
    if err:
        return False, err

    base.log(f"git delivery: creating branch '{branch_name}' from '{base_ref}'")

    if _git_branch_exists(repo, branch_name):
        # Branch exists but state doesn't record it — transition state
        # (branch may have been created manually or from a previous
        # interrupted run).  We treat this as the recorded branch.
        base.log(f"  branch '{branch_name}' already exists; recording in state")
    else:
        create_err = _git_create_and_checkout_branch(repo, branch_name, base_ref)
        if create_err:
            return False, create_err

    # Checkout again in case branch already existed (ensures it's checked out)
    if not _git_current_head_on_branch(repo, branch_name):
        res = groundtruth.git(repo, "checkout", branch_name)
        if res.returncode != 0:
            return False, f"failed to checkout existing branch '{branch_name}': {res.stderr.strip()}"

    # Persist delivery branch identity in plan state
    gd_state["base_ref"] = base_ref
    gd_state["branch_name"] = branch_name
    gd_state["delivery_status"] = "branch_ready"
    base.log(f"git delivery: branch '{branch_name}' ready (base: {base_ref})")

    return True, None


def _resolve_pr_remote(repo: Path) -> tuple[str | None, str | None]:
    """Resolve the git remote name to use for PR delivery.

    Returns ``(remote_name, error_message)``.  Prefers ``origin`` when
    present; otherwise picks the first remote alphabetically.  Returns
    ``(None, error)`` when no remote is configured.
    """
    res = groundtruth.git(repo, "remote")
    if res.returncode != 0:
        return None, "git remote command failed; is this a git repository?"
    remotes = [r.strip() for r in res.stdout.splitlines() if r.strip()]
    if not remotes:
        return None, (
            "no git remote configured; add a remote for "
            "configured pull-request delivery"
        )
    # Prefer origin when available; otherwise use the first remote.
    if "origin" in remotes:
        return "origin", None
    return remotes[0], None


def check_pr_delivery_prerequisites(
    repo: Path, cfg: dict,
) -> tuple[bool, str | None, str | None]:
    """Verify PR delivery prerequisites: gh on PATH and a usable git remote.

    Returns ``(ok, error_message, remote_name)``.  When *ok* is True,
    *remote_name* is the resolved remote that push should target.  When
    *ok* is False, *error_message* explains which prerequisite is missing.
    """
    git_delivery_cfg = cfg.get("git_delivery", {})
    if not git_delivery_cfg.get("create_pull_request", False):
        return True, None, None

    if not shutil.which("gh"):
        return False, (
            "gh is not available on PATH; install GitHub CLI for "
            "configured pull-request delivery"
        ), None

    remote_name, remote_err = _resolve_pr_remote(repo)
    if remote_err:
        return False, remote_err, None

    return True, None, remote_name


def push_delivery_branch(repo: Path, cfg: dict, state: dict) -> tuple[bool, str | None]:
    """Push the recorded delivery branch to the resolved remote.

    Returns ``(ok, error_message)``.
    """
    gd_state = state.get("git_delivery", {})
    branch_name = gd_state.get("branch_name")
    if not branch_name:
        return False, "no recorded delivery branch to push"

    remote_name = gd_state.get("remote_name", "origin")

    base.log(f"git delivery: pushing branch '{branch_name}' to remote '{remote_name}'")

    res = groundtruth.git(repo, "push", "--set-upstream", remote_name, branch_name)
    if res.returncode != 0:
        return False, (
            f"git push failed for branch '{branch_name}' to "
            f"'{remote_name}': "
            f"{res.stderr.strip() or 'unknown error'}"
        )

    base.log(f"git delivery: pushed '{branch_name}' successfully")
    return True, None


def generate_pr_body(repo: Path, cfg: dict, state: dict) -> str:
    """Generate a pull-request body from plan report evidence.

    Includes per-change status, rounds, durations, and estimated cost
    when telemetry is available.  Omits unavailable cost fields instead
    of inventing values.
    """
    plan_name = cfg["name"]
    git_delivery_cfg = cfg.get("git_delivery", {})

    lines: list[str] = []
    lines.append(f"## Plan: {plan_name}")
    lines.append("")

    # Collect per-change evidence from state + telemetry
    try:
        from lib.metrics.aggregator import (
            AggregationError,
            _change_aggregation,
            _read_state,
            _read_telemetry,
            _select_run,
        )
        records, _ = _read_telemetry(repo, plan_name)
        selected_records, _, _ = _select_run(records, None)
        state_for_cm, _ = _read_state(repo, plan_name)
        cm_list, _ = _change_aggregation(state_for_cm, selected_records, plan_name, [])
    except (AggregationError, Exception):
        cm_list = []

    # Filter telemetry-backed change list to enabled changes only
    if cm_list:
        cm_list = [
            c for c in cm_list
            if cfg.get("changes", {}).get(c.change_id, {}).get("enabled", True)
        ]

    if cm_list:
        lines.append("### Change Summary")
        lines.append("")
        lines.append("| Change ID | Status | Rounds | Duration | Tokens | Cost |")
        lines.append("|-----------|--------|--------|----------|--------|------|")
        for c in cm_list:
            dur_str = ""
            if c.duration_ms is not None:
                total_s = int(c.duration_ms) // 1000
                dur_str = f"{total_s // 60}m{total_s % 60}s"
            else:
                dur_str = "—"

            tokens_str = "—"
            if c.tokens is not None:
                n = int(c.tokens)
                if n >= 1_000_000:
                    tokens_str = f"{n / 1_000_000:.1f}M"
                elif n >= 1_000:
                    tokens_str = f"{n / 1_000:.1f}K"
                else:
                    tokens_str = str(n)

            cost_str = "—"
            if c.cost_status == "estimated" and c.estimated_cost is not None:
                cost_str = f"${c.estimated_cost:.2f}"
            elif c.cost_status == "partial":
                if c.estimated_cost is not None:
                    cost_str = f"${c.estimated_cost:.2f} (partial)"
                else:
                    cost_str = "unresolved"
            elif c.cost_status == "unresolved":
                cost_str = "unresolved"

            lines.append(
                f"| `{c.change_id}` | {c.status} | {c.total_rounds} | "
                f"{dur_str} | {tokens_str} | {cost_str} |"
            )

        # Add total cost estimate when available
        total_cost: float | None = None
        has_unresolved = False
        for c in cm_list:
            if c.estimated_cost is not None and c.cost_status in ("estimated", "partial"):
                total_cost = (total_cost or 0.0) + c.estimated_cost
            if c.cost_status in ("unresolved", "partial"):
                has_unresolved = True

        if total_cost is not None:
            lines.append("")
            cost_note = " (partial)" if has_unresolved else ""
            lines.append(f"**Total estimated cost{cost_note}:** ${total_cost:.2f}")

    else:
        # Fallback when metrics unavailable: use state evidence
        lines.append("### Changes")
        lines.append("")
        for cid in cfg["order"]:
            change_cfg = cfg["changes"][cid]
            if not change_cfg["enabled"]:
                continue
            r = state.get("changes", {}).get(cid, {})
            status = r.get("status", "unknown")
            lines.append(f"- `{cid}`: {status}")

    lines.append("")
    lines.append("---")
    lines.append(
        f"*This pull request was generated by opsx-plan from the "
        f"`{plan_name}` plan.*"
    )
    return "\n".join(lines)


def create_github_pull_request(
    repo: Path,
    cfg: dict,
    state: dict,
    body: str,
) -> tuple[bool, str | None, str | None]:
    """Create a GitHub pull request using ``gh pr create``.

    Returns ``(ok, error_message, pr_url)``.  When *ok* is True, *pr_url*
    is the URL of the opened pull request.
    """
    git_delivery_cfg = cfg.get("git_delivery", {})
    gd_state = state.get("git_delivery", {})
    base_ref = gd_state.get("base_ref", git_delivery_cfg.get("base_ref", "main"))
    branch_name = gd_state.get("branch_name", "")

    if not branch_name:
        return False, "no recorded delivery branch for PR creation", None

    title = f"opsx-plan: {cfg['name']}"

    base.log(f"git delivery: creating PR '{title}' from '{branch_name}' to '{base_ref}'")

    cmd = [
        "gh", "pr", "create",
        "--base", base_ref,
        "--head", branch_name,
        "--title", title,
        "--body", body,
    ]

    try:
        res = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "gh pr create timed out after 120s", None
    except FileNotFoundError:
        return False, "gh CLI not found", None

    if res.returncode != 0:
        return False, (
            f"gh pr create failed: {res.stderr.strip() or 'unknown error'}"
        ), None

    pr_url = res.stdout.strip()
    base.log(f"git delivery: PR created: {pr_url}")
    return True, None, pr_url


def attempt_pr_delivery(
    repo: Path,
    cfg: dict,
    state: dict,
    no_pr: bool = False,
) -> tuple[bool, str | None]:
    """Attempt pull-request delivery after plan completion.

    Pushes the recorded delivery branch, creates a GitHub PR, and records
    the result in plan state.  Returns ``(proceed, error_message)``.

    When *no_pr* is True, silently skip PR delivery.
    When the plan state already records a ``pull_request_url``, treat the
    recorded PR as authoritative and skip creation (idempotent rerun).
    """
    git_delivery_cfg = cfg.get("git_delivery", {})
    gd_state = state.get("git_delivery", {})

    if no_pr:
        base.log("git delivery: --no-pr supplied; skipping PR delivery")
        return True, None

    if not git_delivery_cfg.get("create_pull_request", False):
        return True, None

    # Idempotency: if PR already recorded, do not create a duplicate
    existing_url = gd_state.get("pull_request_url")
    if existing_url:
        base.log(f"git delivery: PR already recorded ({existing_url}); skipping creation")
        return True, None

    # Push the delivery branch
    ok, err = push_delivery_branch(repo, cfg, state)
    if not ok:
        # Fail closed: do not claim PR delivery succeeded
        return False, err

    # Generate PR body from report evidence
    body = generate_pr_body(repo, cfg, state)

    # Create the pull request
    ok, err, pr_url = create_github_pull_request(repo, cfg, state, body)
    if not ok:
        # Fail closed: push succeeded but PR creation failed;
        # leave state unambiguous so operator can inspect.
        return False, err

    # Record delivery outcome in plan state
    gd_state["pull_request_url"] = pr_url
    gd_state["delivery_status"] = "pr_opened"
    base.log(f"git delivery: delivery complete, PR opened at {pr_url}")

    return True, None


def verify_post_archive_clean(repo: Path, cfg: dict) -> tuple[bool, str]:
    """Refuse completion when archive/check steps leave tracked edits behind."""
    if not cfg.get("require_clean_tracked", True):
        return True, ""
    if groundtruth.tracked_tree_clean(repo):
        return True, ""
    return (
        False,
        "tracked worktree is dirty; archive must commit or restore tracked changes "
        "before the next change starts",
    )

