"""``opsx-plan autopilot`` — unattended-run wrapper around the orchestrator engine.

Autopilot does not own a scheduler. It repeatedly invokes the existing
``opsx-plan run`` engine as a child process, classifies the resulting plan
state, and acts on the small set of outcomes that an unattended host must
handle on the operator's behalf:

* gated changes get a bounded veto window and are auto-approved when the
  operator does not veto;
* a transient worker failure (invalid output / stage timeout) gets a bounded
  number of auto-resets, persisted across process restarts;
* every other actionable failure escalates once (ntfy + JSONL) and stops.

The engine itself stays authoritative: this module never edits plan state.
``run``/``status``/``approve``/``reset`` are driven through the
``opsx-plan`` executable; the only files this module reads directly are the
plan state file (through :mod:`lib.orchestrator.state`) and stage logs.

All subprocess interaction goes through the injectable *runner* callable so
tests can fake the child engine; wall-clock access goes through the
injectable *now_func* (default :func:`_utcnow`) so veto windows and reset
spacing are deterministic under test.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from lib.orchestrator import base, groundtruth, planref
from lib.orchestrator import state as state_mod

# ---------------------------------------------------------------------------
# Failure markers.
#
# These copy the entrypoint's marker lists verbatim so the wrapper classifies
# what the engine's own stage parser already named.  Sources in
# orchestrator/opsx-plan.py:
#   * PERMISSION_REJECTION_MARKERS  (lines 927-932)
#   * PROVIDER_FAILURE_MARKERS      (lines 934-939)
# ---------------------------------------------------------------------------
PERMISSION_REJECTION_MARKERS = [
    "permission requested",
    "auto-rejecting",
    "The user rejected permission",
    "external_directory permission denied",
]

PROVIDER_FAILURE_MARKERS = [
    "Insufficient Balance",
    "insufficient credits",
    "quota exceeded",
    "billing hard limit",
]

# last_result values that name a bounded-budget exhaustion or an archive
# failure the engine will not retry by itself (orchestrator/opsx-plan.py
# sets these alongside a ``failed`` status).
NEEDS_HUMAN_LAST_RESULTS = {
    "finding_recurrence_exceeded",
    "max_rounds_reached",
    "no_progress",
    "archive_failed",
    "archive_invalid",
}

# reasons/last_results that mean the engine paused intentionally on a budget
# limit, per docs/opsx-plan-operator-workflow.md ("Budget stop semantics":
# "budget exhausted while waiting to run archive" / "spend budget exhausted:
# ...").
BUDGET_REASON_FRAGMENT = "budget exhausted"
BUDGET_LAST_RESULTS = {
    "spend_budget_exhausted",
    "budget_deadline_exhausted",
    "budget_exhausted",
}

SUGGESTED_ACTIONS = {
    "transient_exhausted": (
        "check provider status/model id, then `opsx-plan reset <cid>` and "
        "restart autopilot"
    ),
    "permanent_provider": "top up provider balance / fix model id",
    "permission": "adjust opencode permissions for the worker",
    "finding_recurrence_exceeded": (
        "inspect findings loci; manual fix or trusted-model dispatch per "
        "opsx-plan-ops skill"
    ),
    "max_rounds_reached": (
        "inspect findings loci; manual fix or trusted-model dispatch per "
        "opsx-plan-ops skill"
    ),
    "no_progress": (
        "inspect findings loci; manual fix or trusted-model dispatch per "
        "opsx-plan-ops skill"
    ),
    "archive_failed": (
        "fix the DELTA, never the canonical spec; see opsx-plan-ops"
    ),
    "archive_invalid": (
        "fix the DELTA, never the canonical spec; see opsx-plan-ops"
    ),
    "human_veto": "operator vetoed gate approval",
    "environment": "clean tracked tree / stale execution lock",
    "no_forward_progress": (
        "engine made no progress across 3 passes; inspect status and logs"
    ),
    "unknown": "inspect stage log",
}

DEFAULT_VETO_WINDOW_MINUTES = 30.0
DEFAULT_MAX_AUTO_RESETS = 2
DEFAULT_RESET_SPACING_SECONDS = 300.0
DEFAULT_POLL_SECONDS = 30.0

# A run shorter than this is a "quick exit" for the no-progress guard; a run
# that spent real time is presumed to have done work even if the coarse
# per-change status tuple is unchanged.
NO_PROGRESS_MAX_RUN_SECONDS = 5.0
NO_PROGRESS_PASSES = 3

_STATUS_TIMEOUT_SECONDS = 60.0
_MUTATE_TIMEOUT_SECONDS = 60.0
_LOG_TAIL_BYTES = 8192
_LOG_TAIL_LINES = 40

_EVENTS_FILENAME = "autopilot-events.jsonl"
_ESCALATIONS_FILENAME = "escalations.jsonl"
_AP_STATE_FILENAME = "autopilot-state.json"
_VETO_DIRNAME = "veto"


def _utcnow() -> datetime:
    """Default injectable clock: timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def _config_path() -> Path:
    return Path.home() / ".config" / "opsx-controller" / "autopilot.toml"


def _load_config() -> dict:
    """Load ``~/.config/opsx-controller/autopilot.toml`` (stdlib ``tomllib``).

    The file is optional; an unreadable or malformed file is treated as
    absent so a bad config never prevents an unattended run.
    """
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce(default: object, value: object) -> object:
    if value is None:
        return default
    try:
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        return default
    return value


def _resolve_options(args: argparse.Namespace) -> dict:
    """Resolve options from defaults < config file < CLI flags.

    ``OPSX_AUTOPILOT_NTFY_TOPIC`` overrides the file's ``ntfy_topic``.  CLI
    flags are declared with ``default=None`` so "not passed" is
    distinguishable from an explicit value.
    """
    file_cfg = _load_config()
    options = {
        "veto_window_minutes": DEFAULT_VETO_WINDOW_MINUTES,
        "max_auto_resets": DEFAULT_MAX_AUTO_RESETS,
        "reset_spacing_seconds": DEFAULT_RESET_SPACING_SECONDS,
        "poll_seconds": DEFAULT_POLL_SECONDS,
    }
    for key in options:
        if key in file_cfg:
            options[key] = _coerce(options[key], file_cfg[key])
    for key in options:
        value = getattr(args, key, None)
        if value is not None:
            options[key] = _coerce(options[key], value)
    topic = os.environ.get("OPSX_AUTOPILOT_NTFY_TOPIC", "").strip()
    if not topic:
        raw = file_cfg.get("ntfy_topic", "")
        topic = raw.strip() if isinstance(raw, str) else ""
    options["ntfy_topic"] = topic
    return options


def _resolve_opsx_plan() -> str | None:
    """Locate the opsx-plan executable for child invocations.

    Preference order: the running script itself when it is the installed
    ``opsx-plan``/``opsx-run`` executable, then ``shutil.which``.  Returns
    ``None`` when the child engine cannot be resolved (a preflight failure).
    """
    argv0 = Path(sys.argv[0])
    if argv0.name in ("opsx-plan", "opsx-run"):
        resolved = argv0.resolve()
        if resolved.is_file():
            return str(resolved)
    return shutil.which("opsx-plan")


def _subprocess_runner(cmd, *, cwd, capture, timeout):
    """Default runner: wraps :func:`subprocess.run` with inherited output.

    ``capture=False`` (the ``run`` invocation) inherits the parent's
    stdout/stderr so journald captures the engine's stream; ``capture=True``
    (status/approve/reset) buffers output for parsing.
    """
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_budget_pause(record: dict) -> bool:
    if record.get("last_result") in BUDGET_LAST_RESULTS:
        return True
    return BUDGET_REASON_FRAGMENT in (record.get("reason", "") or "").lower()


class Autopilot:
    """One autopilot run: loop the engine, classify, act, escalate."""

    def __init__(
        self,
        *,
        repo: Path,
        plan_arg: str | None,
        plan_name: str,
        cfg: dict,
        options: dict,
        executable: str,
        runner,
        now_func=_utcnow,
        sleep_func=time.sleep,
        once: bool = False,
    ) -> None:
        self.repo = repo
        self.plan_arg = plan_arg
        self.plan_name = plan_name
        self.cfg = cfg
        self.options = options
        self.executable = executable
        self.runner = runner
        self.now_func = now_func
        self.sleep_func = sleep_func
        self.once = once

        root = repo / ".opsx-plan"
        self.events_path = root / _EVENTS_FILENAME
        self.escalations_path = root / _ESCALATIONS_FILENAME
        self.ap_state_path = root / _AP_STATE_FILENAME
        self.veto_dir = root / _VETO_DIRNAME

        self.ap_state = self._load_ap_state()
        self._last_signature: tuple | None = None
        self._no_progress_streak = 0

    # -- injected-clock helpers ------------------------------------------
    def _now(self) -> datetime:
        return self.now_func()

    def _now_iso(self) -> str:
        return self.now_func().isoformat(timespec="seconds")

    def _today(self) -> str:
        return self.now_func().strftime("%Y-%m-%d")

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleep_func(seconds)

    # -- persistent autopilot state --------------------------------------
    def _load_ap_state(self) -> dict:
        if self.ap_state_path.is_file():
            try:
                data = json.loads(self.ap_state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("signatures", {})
        data.setdefault("notified", {})
        return data

    def _save_ap_state(self) -> None:
        path = self.ap_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        gi = path.parent / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.ap_state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # -- event log / subprocess ------------------------------------------
    def _append_jsonl(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        gi = path.parent / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def _event(self, name: str, **fields) -> None:
        record = {"ts": self._now_iso(), "event": name, "plan": self.plan_name}
        record.update(fields)
        self._append_jsonl(self.events_path, record)

    def _plan_args(self) -> list[str]:
        return [str(self.plan_arg)] if self.plan_arg else []

    def _invoke(self, argv: list[str], *, capture: bool, timeout):
        cmd = [self.executable, *argv]
        try:
            return self.runner(cmd, cwd=self.repo, capture=capture, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return SimpleNamespace(
                returncode=127, stdout="", stderr=f"{type(exc).__name__}: {exc}"
            )

    # -- ntfy push --------------------------------------------------------
    def _push_ntfy(
        self, *, title: str, body: str, priority: str = "default", tags: str = "opsx"
    ) -> None:
        """Best-effort ntfy.sh POST.  Never raises; skips silently without a topic."""
        topic = self.options.get("ntfy_topic") or ""
        if not topic:
            return
        try:
            url = "https://ntfy.sh/" + urllib.parse.quote(str(topic), safe="")
            request = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                method="POST",
                headers={
                    "Title": title,
                    "Priority": priority,
                    "Tags": tags,
                },
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read(1)
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            self._event("notify_failed", error=type(exc).__name__, detail=str(exc))

    # -- evidence helpers -------------------------------------------------
    def _log_tail(self, record: dict) -> str:
        path_text = (
            record.get("last_log")
            or (record.get("last_stage") or {}).get("log_path")
            or ""
        )
        if not path_text:
            return ""
        path = Path(path_text)
        if not path.is_absolute():
            path = self.repo / path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = text[-_LOG_TAIL_BYTES:]
        return "\n".join(tail.splitlines()[-_LOG_TAIL_LINES:])

    def _escalation_loci(self, record: dict) -> list[str]:
        skip_warning = bool(self.cfg.get("skip_warning", False))
        skip_suggestion = bool(self.cfg.get("skip_suggestion", False)) or skip_warning
        blocking = {"critical"}
        if not skip_warning:
            blocking.add("warning")
        if not skip_suggestion:
            blocking.add("note")
        for entry in reversed(record.get("history", []) or []):
            if entry.get("phase") != "review":
                continue
            seen: set[str] = set()
            loci: list[str] = []
            for finding in entry.get("findings", []) or []:
                if finding.get("severity") not in blocking:
                    continue
                for locus in finding.get("locus", []) or []:
                    if locus not in seen:
                        seen.add(locus)
                        loci.append(locus)
            return loci
        return []

    # -- classification ---------------------------------------------------
    def _status_document(self) -> dict | None:
        proc = self._invoke(
            ["status", *self._plan_args(), "--json"],
            capture=True,
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
        return self._parse_status_stdout(proc.stdout)

    @staticmethod
    def _parse_status_stdout(stdout: object) -> dict | None:
        """Parse the JSON document out of ``status --json`` stdout.

        The child CLI logs plan-resolution banners ("using plan from
        OPSX_PLAN: ...", "using active plan: ...") to stdout ahead of the
        document, so skip to the first JSON object instead of requiring
        stdout to begin with ``{``.  ``raw_decode`` also tolerates trailing
        text; anything without a JSON object is a parse failure.
        """
        if not isinstance(stdout, str):
            return None
        start = stdout.find("{")
        if start < 0:
            return None
        try:
            document, _ = json.JSONDecoder().raw_decode(stdout[start:])
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None

    def _classify(self, run_rc: int) -> dict:
        document = self._status_document()
        if document is None:
            return {
                "kind": "environment",
                "reason": "opsx-plan status --json did not return valid JSON",
            }
        changes = [c for c in document.get("changes", []) if isinstance(c, dict)]
        statuses = [(c.get("id"), c.get("status")) for c in changes]

        if statuses and all(status in (base.DONE, base.SKIPPED) for _, status in statuses):
            return {"kind": "complete"}

        awaiting = [
            (c.get("id"), c.get("reason", ""))
            for c in changes
            if c.get("status") == "awaiting_approval"
        ]
        if awaiting:
            return {"kind": "awaiting", "awaiting": awaiting}

        failed = [
            c.get("id") for c in changes if c.get("status") == base.FAILED
        ]
        if failed:
            return {"kind": "failed", "failed": failed}

        if run_rc == 2:
            return {"kind": "environment", "reason": "opsx-plan run exited 2"}

        pendingish = [
            c for c in changes if c.get("status") in ("pending", "ready", "running")
        ]
        state = state_mod.load_state(self.repo, self.plan_name)
        for change in pendingish:
            record = state_mod.rec(state, change.get("id"))
            if _is_budget_pause(record):
                return {"kind": "budget", "change_id": change.get("id")}

        return {"kind": "continue", "statuses": statuses}

    # -- actions ----------------------------------------------------------
    def _handle_approval(self, cid: str, reason: str) -> int | None:
        """Wait out the veto window for one gated change.

        Returns ``None`` to continue the loop, or an exit code to stop.
        """
        window = float(self.options["veto_window_minutes"])
        self._event(
            "approval_wait",
            change_id=cid,
            window_minutes=window,
            reason=reason,
        )
        self._push_ntfy(
            title=f"opsx-plan gate: {cid}",
            body=(
                f"plan: {self.plan_name}\n"
                f"change: {cid}\n"
                f"reason: {reason or 'awaiting approval'}\n"
                f"auto-approves in {window:g} minute(s)\n"
                f"veto: touch .opsx-plan/{_VETO_DIRNAME}/{cid}"
            ),
            tags="lock,opsx",
        )

        deadline = self._now() + timedelta(minutes=window)
        while True:
            if (self.veto_dir / cid).exists():
                self._event("veto", change_id=cid)
                self._escalate(
                    cid,
                    "human_veto",
                    self._load_record(cid),
                    reason="operator veto file present",
                )
                return 0
            state = state_mod.load_state(self.repo, self.plan_name)
            if cid in (state.get("approvals") or []):
                return None
            if self._now() >= deadline:
                break
            self._sleep(float(self.options["poll_seconds"]))

        proc = self._invoke(
            ["approve", *self._plan_args(), cid],
            capture=True,
            timeout=_MUTATE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            self._escalate(
                cid,
                "environment",
                self._load_record(cid),
                reason=(
                    "opsx-plan approve failed: "
                    + (proc.stderr or "").strip()
                ),
            )
            return 2
        self._event("auto_approve", change_id=cid)
        return None

    def _load_record(self, cid: str) -> dict:
        state = state_mod.load_state(self.repo, self.plan_name)
        return state_mod.rec(state, cid)

    def _handle_failure(self, cid: str) -> int | None:
        """Classify one failed change and act.

        Returns ``None`` when a bounded auto-reset was scheduled (continue the
        loop) or an exit code when the change escalated.
        """
        record = self._load_record(cid)
        reason = record.get("reason", "") or ""
        last_result = record.get("last_result", "") or ""
        tail = self._log_tail(record)
        haystack = f"{reason}\n{tail}".lower()

        if any(marker.lower() in haystack for marker in PROVIDER_FAILURE_MARKERS):
            self._escalate(cid, "permanent_provider", record, reason)
            return 0
        if any(marker.lower() in haystack for marker in PERMISSION_REJECTION_MARKERS):
            self._escalate(cid, "permission", record, reason)
            return 0

        is_invalid_output = last_result == "subagent_output_invalid"
        is_timeout = "timed out" in reason.lower() or last_result.endswith("_timeout")
        if is_invalid_output or is_timeout:
            return self._bounded_reset(cid, record)

        archive_status = (record.get("archive") or {}).get("status")
        if last_result in NEEDS_HUMAN_LAST_RESULTS:
            self._escalate(cid, last_result, record, reason)
            return 0
        if archive_status == "failed" or "post_archive" in reason:
            self._escalate(cid, "archive_failed", record, reason)
            return 0

        self._escalate(cid, "unknown", record, reason)
        return 0

    def _bounded_reset(self, cid: str, record: dict) -> int | None:
        reason = record.get("reason", "") or ""
        first_line = reason.splitlines()[0] if reason.splitlines() else ""
        last_result = record.get("last_result", "") or ""
        signature = f"{cid}|{last_result}|{first_line[:80]}"

        signatures = self.ap_state.setdefault("signatures", {})
        entry = signatures.get(signature)
        if not isinstance(entry, dict):
            entry = {"count": 0, "last_attempt": ""}
        count = int(entry.get("count", 0))
        if count >= int(self.options["max_auto_resets"]):
            self._escalate(cid, "transient_exhausted", record, reason)
            return 0

        last_attempt = self._parse_iso(entry.get("last_attempt"))
        spacing = float(self.options["reset_spacing_seconds"])
        now = self._now()
        if last_attempt is not None and (now - last_attempt).total_seconds() < spacing:
            self._escalate(cid, "transient_exhausted", record, reason)
            return 0

        proc = self._invoke(
            ["reset", *self._plan_args(), cid],
            capture=True,
            timeout=_MUTATE_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            self._escalate(
                cid,
                "environment",
                record,
                reason=(
                    "opsx-plan reset failed: " + (proc.stderr or "").strip()
                ),
            )
            return 2

        entry["count"] = count + 1
        entry["last_attempt"] = now.isoformat(timespec="seconds")
        signatures[signature] = entry
        self._save_ap_state()
        self._event(
            "auto_reset",
            change_id=cid,
            attempt=entry["count"],
            signature=signature,
        )
        return None

    @staticmethod
    def _parse_iso(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    # -- escalation -------------------------------------------------------
    def _escalate(
        self, change_id: str | None, klass: str, record: dict, reason: str
    ) -> None:
        record = record or {}
        last_result = record.get("last_result", "") or ""
        loci = self._escalation_loci(record)
        log_path = (
            record.get("last_log")
            or (record.get("last_stage") or {}).get("log_path")
            or ""
        )
        entry = {
            "ts": self._now_iso(),
            "plan": self.plan_name,
            "change_id": change_id,
            "class": klass,
            "last_result": last_result,
            "reason": reason or record.get("reason", "") or "",
            "loci": loci,
            "attempts": record.get("attempts", 0),
            "log_path": log_path,
            "suggested_action": SUGGESTED_ACTIONS.get(
                klass, SUGGESTED_ACTIONS["unknown"]
            ),
        }
        self._append_jsonl(self.escalations_path, entry)
        self._event("escalate", change_id=change_id, klass=klass, reason=entry["reason"])

        notified = self.ap_state.setdefault("notified", {})
        key = f"{change_id}|{klass}|{self._today()}"
        if key not in notified:
            self._push_ntfy(
                title=f"opsx-plan escalate: {klass}",
                body=self._digest(entry),
                priority="high",
                tags="warning,opsx",
            )
            notified[key] = self._now_iso()
            self._save_ap_state()

    @staticmethod
    def _digest(entry: dict) -> str:
        loci = ", ".join(entry.get("loci") or []) or "none"
        return "\n".join(
            [
                "opsx-plan autopilot escalation",
                f"plan: {entry['plan']}",
                f"change: {entry.get('change_id') or 'n/a'}",
                f"class: {entry['class']}",
                f"last_result: {entry.get('last_result') or 'n/a'}",
                f"reason: {entry.get('reason') or 'n/a'}",
                f"loci: {loci}",
                f"attempts: {entry.get('attempts')}",
                f"log: {entry.get('log_path') or 'n/a'}",
                f"action: {entry.get('suggested_action')}",
            ]
        )

    # -- main loop --------------------------------------------------------
    def _invoke_run(self) -> int:
        proc = self._invoke(["run", *self._plan_args()], capture=False, timeout=None)
        return int(proc.returncode)

    def run(self) -> int:
        while True:
            self._event("run_start")
            started = self._now()
            run_rc = self._invoke_run()
            elapsed = (self._now() - started).total_seconds()
            self._event("run_exit", returncode=run_rc)

            outcome = self._classify(run_rc)
            kind = outcome["kind"]

            if kind == "complete":
                self._event("plan_complete")
                self._push_ntfy(
                    title=f"opsx-plan complete: {self.plan_name}",
                    body=(
                        f"plan: {self.plan_name}\n"
                        "all changes done/skipped; autopilot exiting"
                    ),
                    tags="white_check_mark,opsx",
                )
                return 0

            if kind == "awaiting":
                for cid, reason in outcome["awaiting"]:
                    code = self._handle_approval(cid, reason)
                    if code is not None:
                        return code
                if self.once:
                    return 0
                continue

            if kind == "failed":
                for cid in outcome["failed"]:
                    code = self._handle_failure(cid)
                    if code is not None:
                        return code
                if self.once:
                    return 0
                continue

            if kind == "environment":
                self._escalate(None, "environment", {}, outcome.get("reason", ""))
                return 2

            if kind == "budget":
                cid = outcome.get("change_id")
                self._push_ntfy(
                    title=f"opsx-plan budget pause: {self.plan_name}",
                    body=(
                        f"plan: {self.plan_name}\n"
                        f"change: {cid}\n"
                        "engine paused on a budget limit; autopilot stopping "
                        "(intentional)"
                    ),
                    tags="moneybag,opsx",
                )
                return 0

            # kind == "continue": no gates, no failures, no budget pause.
            if self.once:
                return 0

            statuses = tuple(sorted(outcome["statuses"]))
            quick = elapsed < NO_PROGRESS_MAX_RUN_SECONDS
            if quick and statuses == self._last_signature:
                self._no_progress_streak += 1
            else:
                self._no_progress_streak = 1
            self._last_signature = statuses
            if self._no_progress_streak >= NO_PROGRESS_PASSES:
                self._event("no_progress_guard")
                self._escalate(
                    None,
                    "no_forward_progress",
                    {},
                    reason=(
                        "identical per-change status across "
                        f"{NO_PROGRESS_PASSES} consecutive quick passes"
                    ),
                )
                return 0


def cmd_autopilot(
    args: argparse.Namespace,
    *,
    runner=None,
    now_func=None,
    sleep_func=None,
    executable: str | None = None,
) -> int:
    """``opsx-plan autopilot`` handler.

    Optional keyword arguments exist for tests: *runner* replaces the
    subprocess wrapper, *now_func* replaces the clock, *sleep_func* replaces
    :func:`time.sleep`, and *executable* pins the child engine path.
    """
    repo = Path(args.repo).resolve()

    try:
        plan_src = planref.resolve_plan(repo, getattr(args, "plan", None))
        cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
    except base.PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if cfg.get("require_clean_tracked") and not groundtruth.tracked_tree_clean(repo):
        print(
            "error: tracked worktree is dirty; commit/stash then re-run autopilot",
            file=sys.stderr,
        )
        return 2

    resolved_executable = executable or _resolve_opsx_plan()
    if not resolved_executable:
        print(
            "error: cannot locate the opsx-plan executable; install the "
            "orchestrator or ensure opsx-plan is on PATH",
            file=sys.stderr,
        )
        return 2

    options = _resolve_options(args)
    autopilot = Autopilot(
        repo=repo,
        plan_arg=getattr(args, "plan", None),
        plan_name=cfg["name"],
        cfg=cfg,
        options=options,
        executable=resolved_executable,
        runner=runner or _subprocess_runner,
        now_func=now_func or _utcnow,
        sleep_func=sleep_func or time.sleep,
        once=bool(getattr(args, "once", False)),
    )
    try:
        return autopilot.run()
    except base.PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
