"""End-to-end fault-injection suite for supervised execution.

This suite proves the supervision fault matrix with **real local
subprocesses** and a loopback fake OpenCode API, requiring no external
network, no paid model call, and no operator global install. It kills the
real controller, the supervised service host, and a fake worker at the
durable intent / dispatch / result / verification boundaries, starts a fresh
service against the same durable store, and observes real continuation or a
correct human wait re-derived from durable state rather than a fixture stub.

Everything runs under :class:`hermetic_supervision`, a fail-closed guard that
permits only ``AF_UNIX`` and loopback (``127.0.0.1`` / ``::1``) connections,
scrubs paid-model credentials, rejects non-fake model identifiers, and
refuses operator global-install / daemon-provisioning commands. The same
guard is installed inside every spawned helper through
:data:`CHILD_GUARD_PREAMBLE` before the helper runs its target.

See ``core/plan-supervision.md`` (fault-injection and test-policy section)
and ``openspec/changes/add-supervision-fault-injection-tests/design.md``.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from lib.orchestrator import journal_dispatch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "orchestrator" / "opsx-plan.py"

# ---------------------------------------------------------------------------
# Hermetic fixture guard
# ---------------------------------------------------------------------------

#: Hosts the guard treats as loopback. A literal ``localhost`` resolves
#: locally on every supported platform; both literal IP forms are included so
#: an ``AF_INET`` and an ``AF_INET6`` fake server both pass.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Provider credentials that must never be present while a supervised check
#: runs. They are scrubbed on guard entry and refused if reintroduced.
PAID_CREDENTIAL_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "MISTRAL_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "TOGETHER_API_KEY",
    "GROQ_API_KEY",
    "PERPLEXITY_API_KEY",
)

#: Model-identifier prefixes the suite is allowed to use. Everything else is a
#: real provider model and is refused, so a scenario can never silently reach a
#: paid model.
FAKE_MODEL_PREFIXES = ("fake/", "test-provider/", "fake-")

#: Command fragments that identify an operator global install or daemon
#: provisioning step. A helper that tries to run one fails closed.
FORBIDDEN_COMMAND_MARKERS = (
    "install.sh",
    "install-orchestrator.sh",
    "install-common.sh",
    "systemctl",
    "launchctl",
    "daemon-reload",
    "loginctl",
)


class HermeticGuardError(RuntimeError):
    """Raised when a supervised check attempts a prohibited resource."""


def _coerce_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def assert_loopback_address(address: Any) -> None:
    """Refuse a connect address that is not explicitly loopback.

    Accepts an ``AF_UNIX`` path or an ``(host, port)`` pair whose host is one
    of :data:`LOOPBACK_HOSTS`. Every other host — including a resolvable name —
    fails closed rather than resolving and connecting off-host.
    """
    if isinstance(address, (str, bytes)) or address is None:
        # An AF_UNIX path (or the abstract namespace) carries no host.
        return
    try:
        host = _coerce_text(address[0])
    except (TypeError, IndexError):
        raise HermeticGuardError(
            f"unrecognized connect address {address!r}; refusing a "
            "non-loopback connection"
        ) from None
    if host not in LOOPBACK_HOSTS:
        raise HermeticGuardError(
            f"non-loopback connect to {host!r} is prohibited under the "
            "hermetic supervision guard; only AF_UNIX and 127.0.0.1/::1 are "
            "permitted"
        )


def assert_connect_allowed(family: Any, address: Any) -> None:
    """Guard one ``socket.connect`` call for the given address family."""
    if family == getattr(socket, "AF_UNIX", None):
        return
    if isinstance(address, (str, bytes)) or address is None:
        return
    if family in (socket.AF_INET, getattr(socket, "AF_INET6", None)):
        assert_loopback_address(address)
        return
    # An unknown family with a host/port tuple is not provably local.
    assert_loopback_address(address)


def scrub_paid_credentials(env: Mapping[str, Any] | None = None) -> list[str]:
    """Remove provider credentials from *env*, returning the names removed."""
    target = os.environ if env is None else env
    removed: list[str] = []
    for key in PAID_CREDENTIAL_ENV:
        if _coerce_text(target.get(key)).strip():
            removed.append(key)
        target.pop(key, None)  # type: ignore[attr-defined]
    return removed


def assert_no_paid_credentials(env: Mapping[str, Any] | None = None) -> None:
    """Fail closed when a paid-model credential is present in *env*."""
    target = os.environ if env is None else env
    present = sorted(
        key for key in PAID_CREDENTIAL_ENV if _coerce_text(target.get(key)).strip()
    )
    if present:
        raise HermeticGuardError(
            "paid-model credentials are prohibited under the hermetic "
            f"supervision guard: {', '.join(present)}"
        )


def assert_fake_model_identifier(model: Any) -> str:
    """Refuse a model identifier that is not a permitted fake identifier."""
    text = _coerce_text(model).strip()
    if not text:
        raise HermeticGuardError("an empty model identifier is prohibited")
    if any(text.startswith(prefix) for prefix in FAKE_MODEL_PREFIXES):
        return text
    raise HermeticGuardError(
        f"non-fake model identifier {text!r} is prohibited under the hermetic "
        "supervision guard; use a fake/test-provider pin"
    )


def assert_command_allowed(argv: Any) -> None:
    """Refuse an operator global-install or daemon-provisioning command."""
    if isinstance(argv, (str, bytes)):
        tokens: Sequence[Any] = [argv]
    else:
        try:
            tokens = list(argv or ())
        except TypeError:
            tokens = [argv]
    for token in tokens:
        text = _coerce_text(token)
        for marker in FORBIDDEN_COMMAND_MARKERS:
            if marker in text:
                raise HermeticGuardError(
                    f"operator global install / daemon provisioning is "
                    f"prohibited under the hermetic supervision guard "
                    f"(matched {marker!r} in {text!r})"
                )


def _guarded_connect(original: Any) -> Any:
    def connect(self: Any, address: Any) -> Any:
        assert_connect_allowed(getattr(self, "family", None), address)
        return original(self, address)

    return connect


def _guarded_connect_ex(original: Any) -> Any:
    def connect_ex(self: Any, address: Any) -> Any:
        assert_connect_allowed(getattr(self, "family", None), address)
        return original(self, address)

    return connect_ex


def _guarded_popen_init(original: Any) -> Any:
    def init(self: Any, args: Any, *rest: Any, **kwargs: Any) -> Any:
        assert_command_allowed(args)
        return original(self, args, *rest, **kwargs)

    return init


class hermetic_supervision(contextlib.AbstractContextManager):
    """Fail-closed guard enforcing the supervised test policy.

    Entering the context scrubs paid credentials, patches the socket connect
    paths to permit only loopback / AF_UNIX, and patches ``subprocess.Popen``
    to refuse installer and daemon-provisioning commands. Leaving restores the
    original callables. A violation raises :class:`HermeticGuardError`; nothing
    is silently permitted.
    """

    def __init__(self, *, environ: Mapping[str, Any] | None = None) -> None:
        self._env = os.environ if environ is None else environ
        self._saved_connect: Any = None
        self._saved_connect_ex: Any = None
        self._saved_popen_init: Any = None

    def __enter__(self) -> "hermetic_supervision":
        scrub_paid_credentials(self._env)
        self._saved_connect = socket.socket.connect
        self._saved_connect_ex = socket.socket.connect_ex
        self._saved_popen_init = subprocess.Popen.__init__
        socket.socket.connect = _guarded_connect(self._saved_connect)
        socket.socket.connect_ex = _guarded_connect_ex(self._saved_connect_ex)
        subprocess.Popen.__init__ = _guarded_popen_init(self._saved_popen_init)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._saved_connect is not None:
            socket.socket.connect = self._saved_connect
        if self._saved_connect_ex is not None:
            socket.socket.connect_ex = self._saved_connect_ex
        if self._saved_popen_init is not None:
            subprocess.Popen.__init__ = self._saved_popen_init
        return None


# ---------------------------------------------------------------------------
# Child-process guard preamble
# ---------------------------------------------------------------------------

#: Source run as ``python -c`` before any spawned helper's target. It installs
#: the same socket/credential/command guard inside the child, so a subprocess
#: the suite kills and restarts is covered exactly like the parent.
CHILD_GUARD_PREAMBLE = textwrap.dedent(
    f"""
    import sys
    sys.path.insert(0, {str(REPO_ROOT)!r})
    from tests.supervisor.test_supervision_faults import run_guarded_child
    run_guarded_child(sys.argv[1] if len(sys.argv) > 1 else "{{}}")
    """
).strip()

_CHILD_GUARD: hermetic_supervision | None = None


def install_child_guard() -> hermetic_supervision:
    """Install (once) and retain the process-wide child guard."""
    global _CHILD_GUARD
    if _CHILD_GUARD is None:
        _CHILD_GUARD = hermetic_supervision()
        _CHILD_GUARD.__enter__()
    return _CHILD_GUARD


def announce_child_guard() -> None:
    """Install the guard and assert, on stdout, that it is live here.

    The parent reads the ``GUARD <pid>`` handshake and checks the pid against
    the process it spawned, so every helper proves the guard installed in that
    exact process before its target ran, rather than trusting the launcher by
    construction.
    """
    install_child_guard()
    sys.stdout.write(f"GUARD {os.getpid()}\n")
    sys.stdout.flush()


def run_guarded_child(spec_json: str) -> None:
    """Entry point for :data:`CHILD_GUARD_PREAMBLE`.

    Installs and announces the guard *first*, then dispatches to the named
    target with the JSON-decoded spec, so no target code runs before the guard
    is in place and the parent can prove it.
    """
    announce_child_guard()
    spec = json.loads(spec_json or "{}")
    # Importing the test package clears ambient ``OPSX_*`` variables, so the
    # harness re-applies the dispatch environment it explicitly wants the
    # child to inherit (for example the service-owned store) after the import.
    for key, value in (spec.get("child_env") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            os.environ[key] = value
    target = str(spec.get("target") or "")
    if target == "controller":
        _child_controller(spec)
    elif target == "supervised_prompt":
        _child_supervised_prompt(spec)
    elif target == "human_wait":
        _child_human_wait(spec)
    elif target == "service_host":
        _child_service_host(spec)
    elif target == "fresh_service":
        _child_fresh_service(spec)
    elif target == "broker_call":
        _child_broker_call(spec)
    elif target == "write_authority":
        _child_write_authority(spec)
    elif target == "lock_holder":
        _child_lock_holder(spec)
    elif target == "non_loopback_connect":
        _child_non_loopback_connect(spec)
    elif target == "model_check":
        _child_model_check(spec)
    else:
        raise SystemExit(f"unknown guarded child target: {target!r}")


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write("FACT " + json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _child_service_host(spec: Mapping[str, Any]) -> None:
    from lib.orchestrator import supervision as supervision_mod

    repo = Path(spec["repo"])
    store_path = Path(spec["store_path"])
    host = supervision_mod.open_service_host(
        repo,
        spec["cfg"],
        store_path=store_path,
        job_id=spec.get("job_id"),
        operator_principal=SimpleNamespace(uid=os.getuid()),
        service_principal=SimpleNamespace(uid=os.getuid()),
    )
    checkpoint = spec.get("checkpoint")
    if checkpoint:
        # Fault-injection mode: the service-owned session ledger rendezvous
        # blocks inside the production evidence transaction
        # (``record_evidence``) — after the worker's mediated request for the
        # in-flight action has been accepted, authorized, and bound to that
        # action, but before any evidence row is written — so the parent can
        # kill the real supervised service inside a transaction on the very
        # action the named checkpoint's scenario is holding at its boundary.
        host.session.ledger = _RendezvousLedger(
            host.session.ledger, {"record_evidence": str(checkpoint)}
        )
    host.bind()
    sys.stdout.write("service-ready\n")
    sys.stdout.flush()
    if spec.get("once"):
        host.poll(timeout=float(spec.get("timeout", 5.0)))
    else:
        host.serve_forever(timeout=1.0)
    host.close()


def _child_fresh_service(spec: Mapping[str, Any]) -> None:
    """Restart a fresh service against the same store and report reconciliation.

    Exercises the real service boot (``open_service_host`` + ``run_boot_scan``)
    and the real durable reconciliation (``journal_dispatch.reconcile_pending``)
    the run engine performs on resume, then projects the job through the
    read-only supervision projection.
    """
    from lib.orchestrator import journal_dispatch, supervision as supervision_mod
    from lib.supervisor import ledger as ledger_mod

    repo = Path(spec["repo"])
    store_path = Path(spec["store_path"])
    job_id = int(spec["job_id"])
    host = supervision_mod.open_service_host(
        repo,
        spec["cfg"],
        store_path=store_path,
        job_id=job_id,
        operator_principal=SimpleNamespace(uid=os.getuid()),
        service_principal=SimpleNamespace(uid=os.getuid()),
    )
    try:
        scan = host.run_boot_scan()
        handle = ledger_mod.open_ledger(store_path, repository_root=repo)
        try:
            pending = journal_dispatch.reconcile_pending(handle, job_id)
            job = handle.get_job(job_id)
            projection = supervision_mod.project_job(handle, job, repo=repo)
        finally:
            handle.close()
        _emit(
            {
                "pending": pending,
                "scan": _jsonable(scan),
                "projection": projection,
            }
        )
    finally:
        host.close()


class _RendezvousLedger:
    """A ledger handle that blocks at named production call boundaries.

    Every attribute and method delegates to the real, service-owned
    :class:`~lib.supervisor.ledger.Ledger`; nothing is stubbed and no journal
    row is written here. The only added behaviour is that each method named in
    *rendezvous* (a mapping of ledger method name to checkpoint name) emits a
    handshake *before* delegating and then holds: by default it waits on
    stdin, and with a *hold* callable it runs that instead (the hold receives
    the emitted payload, including the action id when the rendezvoused call
    carries one). A hold that returns immediately turns the rendezvous into a
    non-blocking tap. Either way the parent can kill the *real* participating
    process at a deterministic production boundary — ``dispatch_action`` for
    the intent checkpoint, ``complete_action`` for the verification
    checkpoint, and ``record_evidence`` for the supervised service's mediated
    evidence transaction on the same in-flight action.
    """

    def __init__(
        self,
        handle: Any,
        rendezvous: Mapping[str, str],
        hold: Any = None,
    ) -> None:
        self._handle = handle
        self._rendezvous = dict(rendezvous)
        self._hold = hold

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._handle, name)
        checkpoint = self._rendezvous.get(name)
        if checkpoint is None or not callable(target):
            return target

        def guarded(*args: Any, **kwargs: Any) -> Any:
            payload: dict[str, Any] = {"checkpoint": checkpoint}
            if args and isinstance(args[0], int) and not isinstance(args[0], bool):
                payload["action_id"] = int(args[0])
            _emit(payload)
            if self._hold is not None:
                self._hold(payload)
            else:
                sys.stdin.readline()
            return target(*args, **kwargs)

        return guarded


def _report_checkpoint_evidence(spec: Mapping[str, Any], action_id: Any) -> None:
    """Report the in-flight action to the real service over the worker endpoint.

    This is the production worker-to-service mediation path
    (``service_tool.dispatch("record_evidence")``): the call is served by the
    real supervised service, which records — or, at the rendezvous, is killed
    before recording — evidence against *the same action* the reporting worker
    is holding at its named checkpoint. The call blocks until the service
    answers; the fault scenario kills every participant before that happens,
    so any failure here is expected and swallowed — the durable observation is
    the evidence row that was never written.
    """
    from lib.supervisor import service_tool as service_tool_mod

    try:
        service_tool_mod.dispatch(
            "record_evidence",
            job_id=int(spec["job_id"]),
            payload={
                "action_id": int(action_id),
                "kind": "stage_observation",
                "payload": {
                    "checkpoint": str(spec["checkpoint"]),
                    "source": "fault-worker",
                },
            },
            role=str(spec.get("worker_role") or "implementer"),
            observed_agent=str(spec.get("worker_agent") or "opsx-implementer"),
            service_identity=str(spec["service_identity"]),
            socket_path=str(spec["worker_socket"]),
            timeout=120.0,
        )
    except Exception:  # noqa: BLE001 - the kill, not the reply, is the point
        pass


def _spawn_evidence_thread(spec: Mapping[str, Any], action_id: Any) -> None:
    """Run the mediated evidence report off the checkpoint-holding thread."""
    threading.Thread(
        target=_report_checkpoint_evidence,
        args=(spec, action_id),
        daemon=True,
    ).start()


def _child_supervised_prompt(spec: Mapping[str, Any]) -> None:
    """Drive the real supervised prompt lifecycle against the fake API.

    This is the participating process the checkpoint tests fault and kill: it
    boots a real :class:`~lib.supervisor.session_bridge.SessionBridge` against
    the loopback fake OpenCode API and runs the production
    :class:`~lib.supervisor.session_bridge.JournaledSessionBridge` lifecycle,
    which journals the intent, commits the transport decision and reservation,
    writes the dispatch record, launches the prompt side effect, observes the
    terminal result, and reconciles the action. The intent and verification
    checkpoints rendezvous through :class:`_RendezvousLedger`; the result
    checkpoint blocks between the production poll and the production resolve;
    the dispatch checkpoint is held by the fake server's response gate so the
    process is blocked inside the production ``prompt_async`` side effect.
    At every checkpoint the process also reports the in-flight action to the
    real supervised service through the production worker-endpoint
    ``record_evidence`` mediation, coupling the service to the same action.
    """
    from lib.supervisor import session_bridge as bridge_mod
    from lib.supervisor import ledger as ledger_mod

    repo = Path(spec["repo"])
    store_path = Path(spec["store_path"])
    job_id = int(spec["job_id"])
    checkpoint = str(spec["checkpoint"])
    role = "verifier" if checkpoint == "verification" else "implementer"
    stage = "verify" if role == "verifier" else "implement"
    model = bridge_mod.parse_model_identity(spec["model"])
    handle = ledger_mod.open_ledger(store_path, repository_root=repo)
    rendezvous: dict[str, str] = {}
    if checkpoint == "intent":
        rendezvous = {"dispatch_action": "intent"}
    elif checkpoint == "verification":
        rendezvous = {"complete_action": "verification"}
    elif checkpoint == "dispatch":
        rendezvous = {"dispatch_action": "dispatch"}

    def _hold(payload: Mapping[str, Any]) -> None:
        # Couple the supervised service to this action: the worker reports
        # the in-flight action through the production worker-endpoint
        # ``record_evidence`` mediation, which the real service serves (and
        # the scenario faults inside of). At the dispatch checkpoint the hold
        # is a tap — the dispatch record is written and the side effect
        # proceeds into the fake server's held response; at the other
        # checkpoints the worker stays blocked at the boundary.
        action_id = payload.get("action_id")
        if action_id is not None:
            _spawn_evidence_thread(spec, action_id)
        if checkpoint != "dispatch":
            sys.stdin.readline()

    ledger = _RendezvousLedger(
        handle, rendezvous, hold=_hold if rendezvous else None
    )
    try:
        transport = bridge_mod.LoopbackTransport.from_address(
            str(spec["api_address"])
        )
        raw = bridge_mod.SessionBridge(transport)
        raw.check_capability()
        journaled = bridge_mod.JournaledSessionBridge(
            raw,
            ledger,
            job_id=job_id,
            run_id=str(spec["run_id"]),
            policy=spec["policy"],
            change_id=spec["change_id"],
            server_identity=spec["server_identity"],
        )
        created = journaled.create_session(
            title=f"fault-{checkpoint}", role=role, model=model
        )
        session_id = str(created["id"])
        identity = journaled.prompt(
            session_id,
            text=f"fault injection {checkpoint}",
            stage=stage,
            role=role,
            model=model,
            reserved_cost_usd=float(spec.get("reserved_cost_usd", 0.5)),
            reserved_elapsed_minutes=1.0,
        )
        if checkpoint == "result":
            result = bridge_mod.poll_until_terminal(
                raw, session_id, marker=identity.request_id
            )
            # The fake API produced the result; the production resolve (and its
            # evidence reconciliation) has not run yet.
            _emit(
                {
                    "checkpoint": "result",
                    "action_id": int(identity.action_id),
                    "terminal": bool(result.get("terminal")),
                    "status": str(result.get("status") or ""),
                }
            )
            _spawn_evidence_thread(spec, identity.action_id)
            sys.stdin.readline()
            return
        if checkpoint == "verification":
            result = bridge_mod.poll_until_terminal(
                raw, session_id, marker=identity.request_id
            )
            # Blocks inside the production ``complete_action`` rendezvous,
            # after the verifier's stage-result evidence but before completion.
            journaled.resolve(identity, result=result)
            return
        # intent and dispatch never reach here: the process is killed while it
        # is blocked inside the production prompt lifecycle.
        _emit(
            {
                "checkpoint": checkpoint,
                "action_id": int(identity.action_id),
                "terminal": False,
            }
        )
        sys.stdin.readline()
    finally:
        handle.close()


def _child_human_wait(spec: Mapping[str, Any]) -> None:
    """Record a durable human wait through the real resume-authority gate.

    The helper runs the production ``journal_dispatch.assert_authority_gate``
    against a registered human-only change: an unsatisfied human gate records
    the durable wait and fails closed. It reports the wait id through the real
    ``lifecycle.open_human_wait`` lookup and then blocks so the parent controls
    the kill.
    """
    from lib.orchestrator import journal_dispatch
    from lib.supervisor import ledger as ledger_mod
    from lib.supervisor import lifecycle as lifecycle_mod

    repo = Path(spec["repo"])
    handle = ledger_mod.open_ledger(
        Path(spec["store_path"]), repository_root=repo
    )
    try:
        try:
            journal_dispatch.assert_authority_gate(
                handle, int(spec["job_id"]), str(spec["change_id"])
            )
        except journal_dispatch.AuthorityGateError:
            pass  # the durable human wait is the observation
        wait = lifecycle_mod.open_human_wait(
            handle, int(spec["job_id"]), change_id=str(spec["change_id"])
        )
        _emit(
            {
                "checkpoint": "human_wait",
                "wait_id": int(wait["id"]) if wait is not None else None,
            }
        )
        sys.stdin.readline()
    finally:
        handle.close()


def _child_controller(spec: Mapping[str, Any]) -> None:
    """Run the real controller entrypoint behind the announced child guard.

    With a ``mediated_verb`` in the spec the child announces — before
    delegating to the real :func:`lib.supervisor.broker_client.call` — the
    moment the controller enters its production mediated request path for
    that verb, so the parent can prove the controller is genuinely blocked
    inside the mediated call (awaiting the held service) before killing it.
    The request, the transport, and the block are the production path; only
    the handshake is added.
    """
    import runpy

    mediated_verb = spec.get("mediated_verb")
    if mediated_verb:
        from lib.supervisor import broker_client as broker_client_mod

        real_call = broker_client_mod.call

        def announcing_call(request: Any, *args: Any, **kwargs: Any) -> Any:
            verb = request.get("verb") if isinstance(request, Mapping) else None
            if verb == mediated_verb:
                payload: dict[str, Any] = {"mediated": str(verb)}
                job_id = request.get("job_id")
                if isinstance(job_id, int):
                    payload["job_id"] = int(job_id)
                _emit(payload)
            return real_call(request, *args, **kwargs)

        broker_client_mod.call = announcing_call

    sys.argv = [
        str(SCRIPT),
        "--repo",
        str(spec["repo"]),
        *[str(token) for token in spec.get("args", [])],
    ]
    runpy.run_path(str(SCRIPT), run_name="__main__")


def _child_broker_call(spec: Mapping[str, Any]) -> None:
    from lib.supervisor import broker_client

    try:
        result = broker_client.call(
            spec["request"],
            kind=spec["kind"],
            socket_path=spec["socket_path"],
            timeout=10.0,
        )
        _emit({"ok": True, "result": result})
    except Exception as exc:  # noqa: BLE001 - the refusal is the observation
        _emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})


def _child_write_authority(spec: Mapping[str, Any]) -> None:
    """Attempt a worker-domain write against the authority store."""
    path = Path(spec["authority_path"])
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("worker-write\n")
    except OSError as exc:
        _emit({"ok": False, "error": type(exc).__name__, "message": str(exc)})
        return
    _emit({"ok": True, "wrote": str(path)})


def _child_lock_holder(spec: Mapping[str, Any]) -> None:
    """Hold the real worktree execution lock, then block until killed."""
    from lib.supervisor import ledger as ledger_mod
    from lib.supervisor import lock as lock_mod

    repo = Path(spec["repo"])
    handle = None
    if spec.get("store_path"):
        handle = ledger_mod.open_ledger(Path(spec["store_path"]), repository_root=repo)
    try:
        with lock_mod.acquire(
            repo,
            owner=str(spec.get("owner") or "fault-holder"),
            owner_kind=str(spec.get("owner_kind") or "supervised"),
            job_id=spec.get("job_id"),
            ledger=handle,
        ):
            _emit({"acquired": True, "owner": spec.get("owner") or "fault-holder"})
            sys.stdin.readline()
    finally:
        if handle is not None:
            handle.close()


def _child_non_loopback_connect(spec: Mapping[str, Any]) -> None:
    address = (spec["host"], int(spec["port"]))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(address)
    except HermeticGuardError as exc:
        _emit({"refused": True, "reason": str(exc)})
        return
    finally:
        try:
            sock.close()
        except OSError:
            pass
    _emit({"refused": False})


def _child_model_check(spec: Mapping[str, Any]) -> None:
    try:
        assert_fake_model_identifier(spec.get("model"))
    except HermeticGuardError as exc:
        _emit({"refused": True, "reason": str(exc)})
        return
    _emit({"refused": False})


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return None if value is None else str(value)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


FAKE_MODEL = "fake/fake-model"

_STAGE_ENV = {
    "OPSX_CONTROLLER_MODEL": FAKE_MODEL,
    "OPSX_IMPLEMENTER_MODEL": FAKE_MODEL,
    "OPSX_REVIEWER_MODEL": FAKE_MODEL,
    "OPSX_ARCHIVER_MODEL": FAKE_MODEL,
}

MANIFEST = "[[changes]]\nid = \"fault-change\"\npause_before = false\ndepends_on = []\n"

#: A gated change whose approval authority is human-only. The real resume
#: gate records a durable human wait for it instead of dispatching.
HUMAN_ONLY_MANIFEST = (
    "[[changes]]\n"
    'id = "fault-change"\n'
    "pause_before = true\n"
    "depends_on = []\n"
)


def _selection() -> dict[str, Any]:
    from lib.supervisor import model_policy

    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": {role: FAKE_MODEL for role in model_policy.POLICY_ROLES},
        "stages": dict(model_policy.STANDARD_STAGE_MAPPING),
    }


def supervision_policy(**overrides: Any) -> dict[str, Any]:
    from lib.supervisor import budgets as budget_mod
    from lib.supervisor import model_policy

    policy: dict[str, Any] = {
        "authority_config": {"mode": "policy-bound", "approval": "supervisor"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": [FAKE_MODEL],
            "source": "fault fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": 3,
        },
        "deadlines": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }
    policy.update(overrides)
    return policy


def plan_cfg(
    *, name: str = "fault-plan", order: Sequence[str] = ("fault-change",),
    max_rounds: int = 3,
) -> dict[str, Any]:
    return {
        "name": name,
        "adapter": "opencode",
        "order": list(order),
        "max_rounds": max_rounds,
        "state_file": ".opsx-plan/state.json",
        "changes": {cid: {"timeout_minutes": 5} for cid in order},
    }


class SupervisionFaultHarness(unittest.TestCase):
    """Shared fixtures: temp repo/store, a real ledger, and process reaping."""

    #: The protected manifest the harness registers the job with. A subclass
    #: may override it to register a differently gated change.
    manifest_content = MANIFEST

    def setUp(self) -> None:
        guard = hermetic_supervision()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c",
            "user.email=fault@example.invalid",
            "-c",
            "user.name=Fault Harness",
            "commit",
            "-m",
            "init",
        )
        self.worktree = self.repo
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.cid = "fault-change"
        self.cfg = plan_cfg(order=(self.cid,))

        self._processes: list[subprocess.Popen] = []
        self.addCleanup(self._reap_processes)
        self._stdout_buffers: dict[subprocess.Popen, bytearray] = {}

        self.ledger = self.open_ledger()
        self.job_id = self.register_job(self.ledger)
        self.activate_job()

        self._env = {key: value for key, value in os.environ.items()}
        self._env.update(_STAGE_ENV)
        self._env["PYTHONPATH"] = str(REPO_ROOT)
        self._env["OPSX_SUPERVISOR_STATE_FILE"] = str(self.db_path)

    # -- durable fixtures --------------------------------------------------

    def open_ledger(self, **kwargs: Any) -> Any:
        from lib.supervisor import ledger

        kwargs.setdefault("repository_root", self.repo)
        handle = ledger.open_ledger(self.db_path, **kwargs)
        self.addCleanup(handle.close)
        return handle

    def register_job(self, handle: Any, **overrides: Any) -> int:
        params: dict[str, Any] = {
            "run_id": "run-1",
            "worktree": self.worktree,
            "owner": "service",
            # The registered service identity a supervised worker request must
            # carry; without it the worker endpoint refuses every request as
            # unbound (the checkpoint tests exercise the real mediated path).
            "owner_principal": "service",
            "policy": supervision_policy(),
            "operator": "operator",
            "manifest_content": self.manifest_content,
            "linkage_config": {"adapter": "opencode", "primary_session": False},
        }
        params.update(overrides)
        return handle.register_job(**params)

    def activate_job(self) -> None:
        from lib.supervisor import lifecycle

        lifecycle.start(self.ledger, self.job_id)

    def start_dispatched_action(
        self, *, reserved_cost_usd: float = 0.5
    ) -> tuple[int, int]:
        from lib.supervisor import budgets as budget_mod

        action_id = self.ledger.begin_action(
            self.job_id,
            kind="implement",
            run_id="run-1",
            detail=json.dumps({"change_id": self.cid, "stage": "implement"}),
        )
        reservation_id = budget_mod.reserve(
            self.ledger,
            job_id=self.job_id,
            action_id=action_id,
            role="implementer",
            requested_model=FAKE_MODEL,
            reserved_cost_usd=reserved_cost_usd,
            reserved_elapsed_minutes=1.0,
            policy=supervision_policy(),
        )
        self.ledger.dispatch_action(action_id, session_id="session-1")
        return action_id, int(reservation_id)

    def gate(self, **overrides: Any) -> dict[str, Any]:
        policy = self.ledger.current_policy(self.job_id)
        gate = {
            "ledger": self.ledger,
            "job_id": self.job_id,
            "policy": policy,
            "policy_revision": int(policy["revision"]),
            "manifest_snapshot_hash": str(policy["manifest_snapshot_hash"]),
        }
        gate.update(overrides)
        return gate

    def observed_record(self, *, cost: float = 0.25) -> dict[str, Any]:
        return {
            "usage": {
                "usage_available": True,
                "input_tokens": 20,
                "output_tokens": 10,
                "reasoning_tokens": 0,
                "cached_input_tokens": 0,
            },
            "cost": {"status": "estimated", "estimated_cost": cost},
            "duration_ms": 0,
        }

    def state_file_env(self) -> Any:
        from unittest import mock

        return mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(self.db_path)}
        )

    # -- process management ------------------------------------------------

    def _reap_processes(self) -> None:
        for proc in getattr(self, "_processes", []):
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def kill_process(self, proc: subprocess.Popen) -> None:
        """Kill *proc* and reap it with a bounded wait (never a bare sleep)."""
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.fail("a killed helper did not exit within the bounded wait")

    def _assert_guard_line(self, proc: subprocess.Popen, line: str) -> None:
        """Prove the child guard installed in the process just spawned."""
        token = line.split("GUARD ", 1)[-1].strip()
        self.assertEqual(
            token,
            str(proc.pid),
            "the child guard did not announce itself in the spawned process",
        )

    def spawn_guarded_child(
        self,
        target: str,
        *,
        wait: bool = True,
        env: Mapping[str, str] | None = None,
        **spec: Any,
    ) -> subprocess.Popen:
        effective_env = self._env if env is None else env
        payload = {
            "target": target,
            # Re-applied inside the child after the test package import clears
            # ambient OPSX_* variables, so the helper sees the store and role
            # environment the harness intended.
            "child_env": {
                key: value
                for key, value in effective_env.items()
                if key.startswith("OPSX_") or key == "PYTHONPATH"
            },
            **spec,
        }
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD_GUARD_PREAMBLE, json.dumps(payload)],
            cwd=str(self.repo),
            env=effective_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._processes.append(proc)
        self._assert_guard_line(proc, self.await_token(proc, "GUARD "))
        if wait:
            self.await_token(proc, "FACT ")
        return proc

    def spawn_controller(
        self, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.Popen:
        """Run the real controller behind the guard-first child launcher.

        The controller is never launched directly: it goes through
        :data:`CHILD_GUARD_PREAMBLE` like every other helper, and the guard
        announcement proves the guard was live in the controller process
        before its target ran.
        """
        return self.spawn_guarded_child(
            "controller",
            wait=False,
            env=env,
            repo=str(self.repo),
            args=list(args),
        )

    def await_token(
        self, proc: subprocess.Popen, token: str, *, timeout: float = 20.0
    ) -> str:
        """Block for a stdout handshake line under a real bounded deadline.

        The wait is readiness-driven (``select`` on the pipe) with bytes read
        one at a time via ``os.read``, so no ``readline()`` call can block
        past the deadline and no bytes past the handshake newline are
        consumed — a later ``communicate()`` on the same process still sees
        everything the child writes afterwards. Non-matching lines are
        skipped exactly as before; a timeout or an early exit fails the test.
        """
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout
        buffer = self._stdout_buffers.setdefault(proc, bytearray())
        fd = proc.stdout.fileno()
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                line = bytes(buffer[: newline + 1]).decode("utf-8", "replace")
                del buffer[: newline + 1]
                if line.startswith(token):
                    return line.rstrip("\n")
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"helper never signalled {token!r}")
            ready, _w, _x = select.select([fd], [], [], remaining)
            if not ready:
                self.fail(f"helper never signalled {token!r}")
            try:
                chunk = os.read(fd, 1)
            except BlockingIOError:  # pragma: no cover - defensive
                continue
            if not chunk:
                stderr = ""
                if proc.poll() is not None and proc.stderr is not None:
                    stderr = proc.stderr.read()
                self.fail(f"helper exited before signalling {token!r}: {stderr}")
            buffer.extend(chunk)

    def await_fact(self, proc: subprocess.Popen, *, timeout: float = 20.0) -> dict:
        line = self.await_token(proc, "FACT ", timeout=timeout)
        return json.loads(line[len("FACT "):])

    def await_exit(self, proc: subprocess.Popen, *, timeout: float = 20.0) -> int:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail("child did not exit within the bounded wait")

    # -- fake API ----------------------------------------------------------

    def start_fake_api(self) -> Any:
        """Start the shared loopback fake OpenCode API and assert loopback."""
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        server = FakeOpencodeServer()
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        host, _sep, port = server.address.rpartition(":")
        assert_loopback_address((host, int(port)))
        self.fake_api = server
        return server

    # -- durable assertions ------------------------------------------------

    def action_states(self, job_id: int | None = None) -> dict[int, str]:
        job = self.job_id if job_id is None else job_id
        return {
            int(row["id"]): str(row["state"])
            for row in self.ledger.list_actions(job)
        }

    def reservations(self, job_id: int | None = None) -> list[Any]:
        job = self.job_id if job_id is None else job_id
        return list(self.ledger.reservations_for_job(job))

    def prompt_actions(self, job_id: int | None = None) -> list[Any]:
        """Return the job's journaled primary-prompt actions, in order."""
        from lib.supervisor import session_bridge as bridge_mod

        job = self.job_id if job_id is None else job_id
        return [
            row
            for row in self.ledger.list_actions(job)
            if row["kind"] == bridge_mod.PROMPT_ACTION_KIND
        ]

    def projected_job(self, handle: Any, job_id: int) -> dict:
        from lib.orchestrator import supervision as supervision_mod

        return supervision_mod.project_job(handle, handle.get_job(job_id), repo=self.repo)


# ---------------------------------------------------------------------------
# Guard self-tests (task 1.3)
# ---------------------------------------------------------------------------


class HermeticGuardSelfTests(SupervisionFaultHarness):
    """The guard itself refuses prohibited resources and permits loopback."""

    def test_non_loopback_connect_is_refused(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(HermeticGuardError):
                sock.connect(("93.184.216.34", 80))
        finally:
            sock.close()
        with self.assertRaises(HermeticGuardError):
            assert_connect_allowed(socket.AF_INET, ("example.com", 443))

    def test_paid_model_credential_and_identifier_are_refused(self) -> None:
        env = dict(os.environ)
        env["OPENAI_API_KEY"] = "sk-real-looking"
        with self.assertRaises(HermeticGuardError):
            assert_no_paid_credentials(env)
        self.assertEqual(scrub_paid_credentials(env), ["OPENAI_API_KEY"])
        self.assertNotIn("OPENAI_API_KEY", env)

        with self.assertRaises(HermeticGuardError):
            assert_fake_model_identifier("openai/gpt-4o")
        self.assertEqual(assert_fake_model_identifier(FAKE_MODEL), FAKE_MODEL)

    def test_installer_command_is_refused(self) -> None:
        with self.assertRaises(HermeticGuardError):
            assert_command_allowed(["bash", "install.sh", "--global", "--verify"])
        with self.assertRaises(HermeticGuardError):
            assert_command_allowed(["systemctl", "daemon-reload"])
        assert_command_allowed(["git", "status"])

    def test_loopback_connection_to_the_fake_server_is_permitted(self) -> None:
        server = self.start_fake_api()
        host, _sep, port = server.address.rpartition(":")
        assert_loopback_address((host, int(port)))
        conn = socket.create_connection((host, int(port)), timeout=2.0)
        try:
            conn.sendall(b"GET /global/health HTTP/1.1\r\nHost: fake\r\n\r\n")
            payload = conn.recv(4096)
        finally:
            conn.close()
        self.assertIn(b"200", payload)

    def test_child_preamble_installs_the_same_guard(self) -> None:
        refused = self.spawn_guarded_child(
            "non_loopback_connect", wait=False, host="93.184.216.34", port=80
        )
        fact = self.await_fact(refused)
        self.assertTrue(fact["refused"], fact)

        real_model = self.spawn_guarded_child(
            "model_check", wait=False, model="openai/gpt-4o"
        )
        fact = self.await_fact(real_model)
        self.assertTrue(fact["refused"], fact)

        fake_model = self.spawn_guarded_child(
            "model_check", wait=False, model=FAKE_MODEL
        )
        fact = self.await_fact(fake_model)
        self.assertFalse(fact["refused"], fact)


# ---------------------------------------------------------------------------
# Real kill/restart harness (tasks 2.2, 2.3, 2.4)
# ---------------------------------------------------------------------------


class RealProcessHarnessTests(SupervisionFaultHarness):
    """The real controller and service host run as local subprocesses."""

    def test_real_controller_projects_the_registered_job(self) -> None:
        import json as _json

        proc = self.spawn_controller(
            "supervise", "inspect", "--job-id", str(self.job_id), "--json"
        )
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        payload = _json.loads(out[out.index("{") :])
        self.assertEqual(payload["job_id"], self.job_id)

    def test_service_host_binds_and_serves_as_a_real_process(self) -> None:
        proc = self.spawn_guarded_child(
            "service_host",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        self.await_token(proc, "service-ready")
        self.assertIsNone(proc.poll())
        self.kill_process(proc)

    def test_fake_worker_is_refused_a_spoofed_approve(self) -> None:
        from lib.supervisor import endpoints as endpoints_mod

        store = self.db_path
        worker_socket = store.parent / "worker.sock"
        proc = self.spawn_guarded_child(
            "service_host",
            wait=False,
            repo=str(self.repo),
            store_path=str(store),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        self.await_token(proc, "service-ready")
        try:
            worker = self.spawn_guarded_child(
                "broker_call",
                wait=False,
                kind=endpoints_mod.ENDPOINT_WORKER,
                socket_path=str(worker_socket),
                request={"verb": "approve", "change_id": self.cid},
            )
            fact = self.await_fact(worker)
            self.assertFalse(fact["ok"], fact)
            self.assertIn(fact["error"], {"BrokerError", "BrokerMediationError"})
        finally:
            self.kill_process(proc)


class CheckpointKillRestartTests(SupervisionFaultHarness):
    """Kill every real participant at each production checkpoint.

    One registered action flow — a journaled primary-prompt action driven
    against the loopback fake API — couples three real participants, and all
    three are faulted while that action sits at the named durable boundary:

    - the **fake worker**, a real process that boots the production session
      bridge against the loopback fake API and runs the production
      :class:`~lib.supervisor.session_bridge.JournaledSessionBridge`
      lifecycle, rendezvoused at the intent / dispatch / result /
      verification boundary of the action it drives;
    - the **supervised service**, a real ``open_service_host`` process bound
      and serving, which the worker calls through the production
      worker-endpoint ``record_evidence`` mediation *for the same action*;
      the service's session-ledger rendezvous holds it inside that evidence
      transaction — after the request has been accepted, authorized, and
      bound to the action, before any evidence row is written;
    - the **real controller** (``orchestrator/opsx-plan.py``), blocked in its
      production mediated stop-boundary request (``supervise pause``)
      against the action's job: the single-threaded service is holding the
      action's evidence transaction, so the controller's request — the stop
      boundary the dispatch path itself observes before every dispatch — is
      genuinely in flight against the same in-flight action when it is
      killed.

    The rendezvous handshakes carry the action id, so each test proves the
    service was faulted inside a transaction on the very action the worker
    holds at the checkpoint, and the controller inside the mediated request
    that transaction holds. Each is killed with ``Popen.kill()`` plus a
    bounded wait while the scenario sits at the named checkpoint, and a
    fresh service then reconciles the same durable store: no evidence row
    from the interrupted transaction, no pause receipt or stop request, and
    the action's journal exactly as the boundary left it.
    """

    def _spawn_supervised(self, checkpoint: str) -> tuple[subprocess.Popen, Any]:
        from lib.supervisor import session_bridge as bridge_mod

        server = self.start_fake_api()
        if checkpoint == "dispatch":
            server.state.hold_prompt_response = True
            self.addCleanup(server.state.release_prompt.set)
        identity = bridge_mod.serialize_process_identity(os.getpid())
        proc = self.spawn_guarded_child(
            "supervised_prompt",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            job_id=self.job_id,
            checkpoint=checkpoint,
            change_id=self.cid,
            policy=supervision_policy(),
            model=FAKE_MODEL,
            run_id="run-1",
            api_address=server.address,
            server_identity=identity,
            worker_socket=str(self.db_path.parent / "worker.sock"),
            service_identity="service",
        )
        return proc, server

    def _spawn_faulted_service(self, checkpoint: str) -> subprocess.Popen:
        """Start the real service the checkpoint's action is reported to.

        The service binds and serves before returning, so the worker's
        production ``record_evidence`` mediation for the in-flight action is
        served — and held inside the evidence transaction by the rendezvous —
        rather than refused.
        """
        service = self.spawn_guarded_child(
            "service_host",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
            checkpoint=checkpoint,
        )
        self.await_token(service, "service-ready")
        return service

    def _spawn_held_controller(self) -> subprocess.Popen:
        """Run the real controller's mediated stop boundary against the job.

        ``opsx-plan supervise pause`` is the production stop boundary the
        dispatch path observes before every dispatch; mediated over the
        operator endpoint it cannot be served while the service holds the
        action's evidence transaction, so the controller blocks inside the
        real broker call. The returned process has already announced its
        entry into that mediated request.
        """
        env = dict(self._env)
        env["OPSX_SUPERVISOR_OPERATOR_SOCKET"] = str(
            self.db_path.parent / "operator.sock"
        )
        controller = self.spawn_guarded_child(
            "controller",
            wait=False,
            env=env,
            repo=str(self.repo),
            args=["supervise", "pause", "--json"],
            mediated_verb="pause",
        )
        fact = self.await_fact(controller)
        self.assertEqual(fact["mediated"], "pause")
        self.assertEqual(int(fact["job_id"]), int(self.job_id))
        return controller

    def _kill_all_participants(
        self,
        worker: subprocess.Popen,
        service: subprocess.Popen,
        controller: subprocess.Popen,
    ) -> None:
        """Kill the worker, service, and controller at the held boundary.

        The service and controller are asserted alive first: the kill lands
        while each is genuinely blocked at its boundary rather than after an
        early exit.
        """
        self.kill_process(worker)
        self.assertIsNone(
            controller.poll(),
            "the real controller was not still blocked in its mediated "
            "request at the checkpoint",
        )
        self.kill_process(controller)
        self.assertIsNone(
            service.poll(),
            "the supervised service was not still blocked inside the "
            "action's evidence transaction",
        )
        self.kill_process(service)

    def _assert_no_mediated_side_effects(self, action_id: Any) -> None:
        """Neither held transaction recorded anything partial.

        The service was killed before its evidence write and the controller
        before its stop boundary was served, so the interrupted transactions
        leave no evidence row, no pause receipt, no stop request, and no job
        transition — the action's journal is exactly what the worker's
        checkpoint boundary produced.
        """
        from lib.supervisor import lifecycle as lifecycle_mod

        kinds = [
            row["kind"] for row in self.ledger.list_evidence(int(action_id))
        ]
        self.assertNotIn("stage_observation", kinds)
        receipts = self.ledger.receipts_for_change(
            self.job_id, self.cid, kind="pause"
        )
        self.assertEqual(list(receipts), [])
        self.assertFalse(
            lifecycle_mod.stop_request_pending(self.ledger, self.job_id)
        )
        self.assertEqual(str(self.ledger.get_job(self.job_id)["state"]), "active")

    def _restart_fresh_service(self) -> dict:
        proc = self.spawn_guarded_child(
            "fresh_service",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        fact = self.await_fact(proc)
        self.assertEqual(self.await_exit(proc), 0)
        return fact

    def _single_prompt_action(self) -> Any:
        actions = self.prompt_actions()
        self.assertEqual(len(actions), 1, actions)
        return actions[0]

    def test_intent_checkpoint_kill_leaves_no_claimed_effect(self) -> None:
        service = self._spawn_faulted_service("intent")
        proc, _server = self._spawn_supervised("intent")
        fact = self.await_fact(proc)
        self.assertEqual(fact["checkpoint"], "intent")
        service_fact = self.await_fact(service)
        self.assertEqual(service_fact["checkpoint"], "intent")
        self.assertEqual(int(service_fact["action_id"]), int(fact["action_id"]))
        controller = self._spawn_held_controller()
        self._kill_all_participants(proc, service, controller)

        action = self._single_prompt_action()
        self.assertEqual(int(action["id"]), int(fact["action_id"]))
        self.assertEqual(action["state"], "intent")
        self.assertIsNone(self.ledger.latest_dispatch(int(action["id"])))
        self._assert_no_mediated_side_effects(fact["action_id"])

        restarted = self._restart_fresh_service()
        self.assertNotEqual(restarted["projection"]["state"], "completed")
        self.assertEqual(
            self.ledger.get_action(int(action["id"]))["state"], "intent"
        )

    def test_dispatch_checkpoint_kill_reconciles_uncertain_and_retains(self) -> None:
        service = self._spawn_faulted_service("dispatch")
        proc, server = self._spawn_supervised("dispatch")
        self.assertTrue(
            server.state.prompt_blocked.wait(timeout=20.0),
            "the fake API never observed the dispatch side effect",
        )
        self.assertEqual(len(server.state.prompt_calls), 1)
        service_fact = self.await_fact(service)
        self.assertEqual(service_fact["checkpoint"], "dispatch")
        controller = self._spawn_held_controller()
        self._kill_all_participants(proc, service, controller)
        server.state.release_prompt.set()

        action = self._single_prompt_action()
        self.assertEqual(int(service_fact["action_id"]), int(action["id"]))
        self.assertEqual(action["state"], "dispatched")
        self.assertIsNotNone(self.ledger.latest_dispatch(int(action["id"])))
        reservation = self.ledger.reservation_for_action(int(action["id"]))
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation["state"], "reserved")
        self._assert_no_mediated_side_effects(action["id"])

        restarted = self._restart_fresh_service()
        self.assertTrue(restarted["pending"], restarted)
        self.assertEqual(restarted["pending"][0]["action_id"], int(action["id"]))
        reservation = self.ledger.reservation_for_action(int(action["id"]))
        self.assertEqual(reservation["state"], "retained")
        self.assertNotEqual(restarted["projection"]["state"], "completed")

    def test_result_checkpoint_kill_is_not_treated_as_free_or_complete(self) -> None:
        service = self._spawn_faulted_service("result")
        proc, server = self._spawn_supervised("result")
        fact = self.await_fact(proc)
        self.assertEqual(fact["checkpoint"], "result")
        self.assertTrue(fact["terminal"], fact)
        service_fact = self.await_fact(service)
        self.assertEqual(service_fact["checkpoint"], "result")
        self.assertEqual(int(service_fact["action_id"]), int(fact["action_id"]))
        controller = self._spawn_held_controller()
        self._kill_all_participants(proc, service, controller)

        action = self._single_prompt_action()
        self.assertEqual(int(action["id"]), int(fact["action_id"]))
        self.assertEqual(action["state"], "dispatched")
        kinds = [row["kind"] for row in self.ledger.list_evidence(int(action["id"]))]
        self.assertNotIn("stage_result", kinds)
        self.assertEqual(len(server.state.prompt_calls), 1)
        self._assert_no_mediated_side_effects(fact["action_id"])

        restarted = self._restart_fresh_service()
        self.assertTrue(restarted["pending"], restarted)
        self.assertEqual(
            self.ledger.get_action(int(action["id"]))["state"], "uncertain"
        )
        self.assertNotEqual(restarted["projection"]["state"], "completed")

    def test_verification_checkpoint_kill_never_records_completion(self) -> None:
        service = self._spawn_faulted_service("verification")
        proc, _server = self._spawn_supervised("verification")
        fact = self.await_fact(proc)
        self.assertEqual(fact["checkpoint"], "verification")
        service_fact = self.await_fact(service)
        self.assertEqual(service_fact["checkpoint"], "verification")
        self.assertEqual(int(service_fact["action_id"]), int(fact["action_id"]))
        controller = self._spawn_held_controller()
        self._kill_all_participants(proc, service, controller)

        action = self._single_prompt_action()
        self.assertEqual(int(action["id"]), int(fact["action_id"]))
        self.assertEqual(action["state"], "dispatched")
        kinds = [row["kind"] for row in self.ledger.list_evidence(int(action["id"]))]
        self.assertIn("stage_result", kinds)
        reservation = self.ledger.reservation_for_action(int(action["id"]))
        self.assertEqual(reservation["state"], "reconciled")
        self._assert_no_mediated_side_effects(fact["action_id"])

        restarted = self._restart_fresh_service()
        self.assertNotEqual(restarted["projection"]["state"], "completed")
        self.assertNotEqual(
            self.ledger.get_action(int(action["id"]))["state"], "completed"
        )


class HumanWaitRestartTests(SupervisionFaultHarness):
    """A real gate records a durable human wait; a real restart preserves it.

    The change is gated human-only, so the production resume-authority gate
    records the durable wait and fails closed rather than dispatching. The
    helper that records it runs the production gate against the same store.
    """

    manifest_content = HUMAN_ONLY_MANIFEST

    def _restart_fresh_service(self) -> dict:
        proc = self.spawn_guarded_child(
            "fresh_service",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        fact = self.await_fact(proc)
        self.assertEqual(self.await_exit(proc), 0)
        return fact

    def test_restart_during_a_recorded_human_wait_preserves_the_wait(self) -> None:
        from lib.supervisor import lifecycle

        waiter = self.spawn_guarded_child(
            "human_wait",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            job_id=self.job_id,
            change_id=self.cid,
        )
        fact = self.await_fact(waiter)
        self.assertEqual(fact["checkpoint"], "human_wait")
        self.assertIsNotNone(fact["wait_id"], fact)
        self.kill_process(waiter)

        restarted = self._restart_fresh_service()
        open_wait = lifecycle.open_human_wait(
            self.ledger, self.job_id, change_id=self.cid
        )
        self.assertIsNotNone(open_wait)
        self.assertEqual(int(open_wait["id"]), int(fact["wait_id"]))
        waits = restarted["projection"]["waits"]
        self.assertTrue(any(w["state"] == "open" for w in waits), waits)
        self.assertNotEqual(restarted["projection"]["state"], "completed")


# ---------------------------------------------------------------------------
# Fault matrix scenarios (section 3)
# ---------------------------------------------------------------------------


class FaultMatrixTests(SupervisionFaultHarness):
    """Each required fault is injected at a real seam and asserted durable."""

    def write_manifest(self) -> Path:
        path = self.repo / "plan.toml"
        path.write_text(MANIFEST, encoding="utf-8")
        return path

    def test_lost_event_and_lost_ack_reconcile_from_durable_state(self) -> None:
        from lib.supervisor import session_bridge as bridge_mod

        server = self.start_fake_api()
        server.state.drop_ack_once = True
        old = os.environ.get("FAKE_EVENT_DISCONNECT")
        os.environ["FAKE_EVENT_DISCONNECT"] = "1"
        if old is None:
            self.addCleanup(os.environ.pop, "FAKE_EVENT_DISCONNECT", None)
        else:
            self.addCleanup(os.environ.__setitem__, "FAKE_EVENT_DISCONNECT", old)

        transport = bridge_mod.LoopbackTransport.from_address(server.address)
        self.addCleanup(transport.close)
        bridge = bridge_mod.SessionBridge(transport)
        bridge.check_capability()
        session = bridge.create_session(title="lost-ack")
        try:
            bridge.prompt_async(session["id"], text="lost-ack", marker="req_lost")
        except Exception:  # noqa: BLE001 - the dropped acknowledgement is the fault
            pass
        # The event stream is disconnected; the authoritative poll still finds
        # the accepted, completed turn.
        result = bridge.result(session["id"], marker="req_lost")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source"], "poll")
        self.assertEqual(len(server.state.prompt_calls), 1)

        # Durable reconciliation: the uncertain action is resolved from the
        # observed evidence with no replay and exactly one budget effect.
        action_id, reservation_id = self.start_dispatched_action()
        self.ledger.mark_uncertain(action_id, detail="lost acknowledgement")
        self.ledger.record_evidence(
            action_id,
            kind="stage_result",
            payload=json.dumps(
                {"confirmed": True, "completed": True, "outcome": "ok"}
            ),
        )
        self.ledger.reconcile_action(action_id)
        self.ledger.complete_action(action_id)
        from lib.supervisor import budgets as budget_mod

        budget_mod.reconcile(
            self.ledger,
            reservation_id=reservation_id,
            observation_state="observed",
            observed_cost_usd=0.25,
            observed_elapsed_minutes=0.0,
        )
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")
        reservation = self.ledger.reservation_for_action(action_id)
        self.assertEqual(reservation["state"], "reconciled")
        self.assertEqual(float(reservation["observed_cost_usd"]), 0.25)
        self.assertEqual(len(server.state.prompt_calls), 1)

    def test_duplicate_response_has_one_journal_and_one_budget_effect(self) -> None:
        from lib.supervisor import session_bridge as bridge_mod

        # The loopback fake API emits a genuine duplicate response: one
        # request, two identical completions carrying the same message
        # identity.
        server = self.start_fake_api()
        server.state.duplicate_response_once = True

        # Drive the production journaled lifecycle against the fake API:
        # intent, reservation, dispatch record, prompt side effect, terminal
        # poll, and resolve all run through JournaledSessionBridge.
        transport = bridge_mod.LoopbackTransport.from_address(server.address)
        self.addCleanup(transport.close)
        raw = bridge_mod.SessionBridge(transport)
        raw.check_capability()
        model = bridge_mod.parse_model_identity(FAKE_MODEL)
        journaled = bridge_mod.JournaledSessionBridge(
            raw,
            self.ledger,
            job_id=self.job_id,
            run_id="run-1",
            policy=supervision_policy(),
            change_id=self.cid,
            server_identity=bridge_mod.serialize_process_identity(os.getpid()),
        )
        created = journaled.create_session(
            title="duplicate-response", role="implementer", model=model
        )
        session_id = str(created["id"])
        identity = journaled.prompt(
            session_id,
            text="duplicate response fault",
            stage="implement",
            role="implementer",
            model=model,
            reserved_cost_usd=0.5,
            reserved_elapsed_minutes=1.0,
        )
        self.assertTrue(identity.acknowledged)
        self.assertEqual(len(server.state.prompt_calls), 1)

        result = bridge_mod.poll_until_terminal(
            raw, session_id, marker=identity.request_id
        )
        self.assertTrue(result["terminal"], result)
        self.assertEqual(result["status"], "completed")
        # The fake API really emitted the duplicate response.
        self.assertEqual(len(server.state.duplicated_answers), 1)
        assistants = [
            entry
            for entry in server.state.messages[session_id]
            if (entry.get("info") or {}).get("role") == "assistant"
        ]
        self.assertEqual(len(assistants), 2)
        self.assertEqual(
            assistants[0]["info"]["id"], assistants[1]["info"]["id"]
        )

        first = journaled.resolve(identity, result=result)
        self.assertEqual(first, "completed")
        # Re-observe the duplicated response through the production lifecycle:
        # the replayed observation deduplicates at the journal and budget
        # boundaries.
        reobserved = bridge_mod.poll_until_terminal(
            raw, session_id, marker=identity.request_id
        )
        second = journaled.resolve(identity, result=reobserved)
        self.assertEqual(second, "completed")

        action_id = int(identity.action_id)
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")
        for kind in (bridge_mod.EVIDENCE_STAGE_RESULT, bridge_mod.EVIDENCE_USAGE):
            rows = [
                row
                for row in self.ledger.list_evidence(action_id)
                if row["kind"] == kind
            ]
            self.assertEqual(len(rows), 1, (kind, rows))
        reservation = self.ledger.reservation_for_action(action_id)
        self.assertEqual(reservation["state"], "reconciled")
        self.assertEqual(float(reservation["observed_cost_usd"]), 0.25)
        consumption = self.ledger.consumption_for_job(self.job_id)
        self.assertEqual(float(consumption["cost_usd"]), 0.25)
        self.assertEqual(len(server.state.prompt_calls), 1)

    def test_stale_approval_fails_closed_and_unrelated_update_does_not(self) -> None:
        from lib.orchestrator import journal_dispatch

        manifest_path = self.write_manifest()
        anchor = self.gate(manifest_path=str(manifest_path))
        journal_dispatch.assert_material_freshness(
            self.ledger, self.job_id, anchor
        )
        # An unrelated repository update does not invalidate the valid anchor.
        (self.repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        journal_dispatch.assert_material_freshness(
            self.ledger, self.job_id, anchor
        )
        # A stale material revision does not satisfy the gate.
        current = self.ledger.current_policy(self.job_id)
        self.ledger.revise_policy(
            self.job_id,
            revision=int(current["revision"]) + 1,
            policy=supervision_policy(
                manifest_snapshot_hash=current["manifest_snapshot_hash"]
            ),
            operator="operator",
        )
        with self.assertRaises(journal_dispatch.StaleMaterialGateError):
            journal_dispatch.assert_material_freshness(
                self.ledger, self.job_id, anchor
            )
        # The valid anchor still holds against the *current* material.
        refreshed = self.gate(manifest_path=str(manifest_path))
        journal_dispatch.assert_material_freshness(
            self.ledger, self.job_id, refreshed
        )

    def test_spoofed_worker_approve_reset_and_run_are_refused(self) -> None:
        from lib.supervisor import endpoints as endpoints_mod

        before_policy = self.ledger.current_policy(self.job_id)
        before_state = str(self.ledger.get_job(self.job_id)["state"])
        proc = self.spawn_guarded_child(
            "service_host",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        self.await_token(proc, "service-ready")
        try:
            for verb in ("approve", "reset_change", "run"):
                worker = self.spawn_guarded_child(
                    "broker_call",
                    wait=False,
                    kind=endpoints_mod.ENDPOINT_WORKER,
                    socket_path=str(self.db_path.parent / "worker.sock"),
                    request={"verb": verb, "change_id": self.cid},
                )
                fact = self.await_fact(worker)
                self.assertFalse(fact["ok"], fact)
        finally:
            self.kill_process(proc)
        self.assertEqual(
            int(self.ledger.current_policy(self.job_id)["revision"]),
            int(before_policy["revision"]),
        )
        self.assertEqual(str(self.ledger.get_job(self.job_id)["state"]), before_state)
        self.assertEqual(self.ledger.open_waits(self.job_id, kind="human"), [])

    @unittest.skipIf(os.getuid() == 0, "root bypasses file permissions")
    def test_sandbox_bypass_refuses_authority_write_and_non_loopback_egress(
        self,
    ) -> None:
        authority_store = self.storage / "authority.json"
        authority_store.write_text("{}\n", encoding="utf-8")
        os.chmod(authority_store, 0o400)
        write = self.spawn_guarded_child(
            "write_authority",
            wait=False,
            authority_path=str(authority_store),
        )
        fact = self.await_fact(write)
        self.assertFalse(fact["ok"], fact)
        self.assertIn(fact["error"], {"PermissionError", "OSError"})
        self.assertEqual(authority_store.read_text(encoding="utf-8"), "{}\n")

        egress = self.spawn_guarded_child(
            "non_loopback_connect", wait=False, host="93.184.216.34", port=80
        )
        fact = self.await_fact(egress)
        self.assertTrue(fact["refused"], fact)

    def test_competing_processes_hold_mutual_exclusion(self) -> None:
        from lib.supervisor import lock as lock_mod

        holder = self.spawn_guarded_child(
            "lock_holder",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            job_id=self.job_id,
            owner="first",
            owner_kind="supervised",
        )
        self.await_fact(holder)
        try:
            with self.assertRaises(lock_mod.LockError):
                with lock_mod.acquire(self.worktree, owner="second"):
                    pass
            contender = self.spawn_guarded_child(
                "lock_holder",
                wait=False,
                repo=str(self.repo),
                store_path=str(self.db_path),
                job_id=self.job_id,
                owner="second",
                owner_kind="supervised",
            )
            self.assertNotEqual(self.await_exit(contender), 0)
        finally:
            self.kill_process(holder)
        with lock_mod.acquire(self.worktree, owner="after"):
            pass

    def test_budget_reset_and_unknown_cost_do_not_loosen_policy(self) -> None:
        import argparse

        from lib.orchestrator import cost as cost_mod, journal_dispatch as jd
        from lib.orchestrator import supervision as supervision_mod
        from lib.supervisor import budgets as budget_mod

        action_id, reservation_id = self.start_dispatched_action()
        signature = budget_mod.incident_signature(
            kind="implement", change_id=self.cid, stage="implement",
            discriminator="dispatch",
        )
        self.ledger.record_incident_attempt(self.job_id, signature=signature)

        # An unpriceable model blocks with the named unknown-cost error. The
        # eager pricing catalog may be re-imported by other suites, so the
        # refusal is matched by its named type rather than class identity.
        try:
            jd.reservation_estimate_for_dispatch(
                self.repo, supervision_policy(), "implementer"
            )
        except Exception as exc:  # noqa: BLE001 - the named refusal is the fact
            self.assertEqual(type(exc).__name__, "UnknownPricingError", str(exc))
        else:
            self.fail("an unpriceable model must block dispatch")

        # The operator reset transaction does not erase durable reservations,
        # incident attempts, or restart signatures.
        plans = self.repo / "openspec" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / "fault-plan.toml").write_text(
            "[plan]\nname = \"fault-plan\"\nadapter = \"opencode\"\n\n"
            "[[changes]]\nid = \"fault-change\"\n",
            encoding="utf-8",
        )
        service = self.spawn_guarded_child(
            "service_host",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        self.await_token(service, "service-ready")
        try:
            env = dict(self._env)
            env["OPSX_SUPERVISOR_OPERATOR_SOCKET"] = str(
                self.db_path.parent / "operator.sock"
            )
            reset = self.spawn_controller(
                "reset",
                "openspec/plans/fault-plan.toml",
                self.cid,
                env=env,
            )
            out, err = reset.communicate(timeout=60)
            self.assertEqual(reset.returncode, 0, err or out)
        finally:
            self.kill_process(service)

        reservation = self.ledger.reservation_for_action(action_id)
        self.assertIsNotNone(reservation)
        self.assertEqual(int(reservation["id"]), reservation_id)
        self.assertGreaterEqual(
            self.ledger.incident_attempt_count(self.job_id, signature), 1
        )
        consumption = self.ledger.consumption_for_job(self.job_id)
        self.assertGreaterEqual(float(consumption["cost_usd"]), 0.5)

    def test_wrong_model_identity_fails_closed(self) -> None:
        policy = self.ledger.current_policy(self.job_id)
        journal_dispatch.assert_model_policy_gate(
            policy, "implementer", resolved_model=FAKE_MODEL
        )
        with self.assertRaises(journal_dispatch.ModelPolicyGateError):
            journal_dispatch.assert_model_policy_gate(
                policy, "implementer", resolved_model="fake/other-model"
            )

    def test_false_completion_is_rejected(self) -> None:
        from lib.orchestrator import journal_dispatch

        action_id, reservation_id = self.start_dispatched_action()
        journal_dispatch.resolve_dispatch(
            self.gate(),
            action_id=action_id,
            reservation_id=reservation_id,
            outcome="completed",
            record=self.observed_record(),
        )
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")

        module = _load_opsx_plan()
        cfg = {"name": "fault-plan", "order": [self.cid],
               "changes": {self.cid: {"enabled": True}}}
        from unittest import mock

        with mock.patch.object(
            module.state_mod, "load_state", return_value={"changes": {}}
        ), mock.patch.object(
            module, "verify_direct_archive_done",
            return_value=(False, "archive missing"),
        ), mock.patch.object(
            module.state_mod, "pending_manual_tasks", return_value=[]
        ):
            complete, failures, _manual = module.evaluate_supervised_completion(
                self.repo, cfg, None, self.job_id
            )
        # A completed action is not a completed job: canonical archive/check
        # evidence is missing, so the claim fails closed.
        self.assertFalse(complete)
        self.assertEqual(failures[0]["change_id"], self.cid)

    def _restart_fresh_service(self) -> dict:
        proc = self.spawn_guarded_child(
            "fresh_service",
            wait=False,
            repo=str(self.repo),
            store_path=str(self.db_path),
            cfg=self.cfg,
            job_id=self.job_id,
        )
        fact = self.await_fact(proc)
        self.assertEqual(self.await_exit(proc), 0)
        return fact


# ---------------------------------------------------------------------------
# Durable-correctness and projection assertions (section 4)
# ---------------------------------------------------------------------------


class DurableCorrectnessTests(SupervisionFaultHarness):
    def test_unknown_and_interrupted_are_never_free_or_complete(self) -> None:
        from lib.supervisor import budgets as budget_mod

        action_id, reservation_id = self.start_dispatched_action(
            reserved_cost_usd=0.5
        )
        self.ledger.mark_uncertain(action_id, detail="interrupted")
        self.ledger.retain_reservation(reservation_id)
        reservation = self.ledger.reservation_for_action(action_id)
        self.assertEqual(reservation["state"], "retained")
        self.assertEqual(
            budget_mod.reservation_charge(dict(reservation))["cost_usd"], 0.5
        )
        self.assertNotEqual(self.ledger.get_action(action_id)["state"], "completed")
        self.assertNotEqual(self.ledger.get_action(action_id)["state"], "failed")

    def test_reconcile_pending_marks_dispatched_uncertain_and_retains(self) -> None:
        action_id, reservation_id = self.start_dispatched_action(
            reserved_cost_usd=0.75
        )
        pending = journal_dispatch.reconcile_pending(self.ledger, self.job_id)
        ids = [item["action_id"] for item in pending]
        self.assertIn(action_id, ids)
        self.assertEqual(self.ledger.get_action(action_id)["state"], "uncertain")
        reservation = self.ledger.reservation_for_action(action_id)
        self.assertEqual(reservation["state"], "retained")
        self.assertEqual(float(reservation["reserved_cost_usd"]), 0.75)

    def test_projection_reflects_reconciled_state_never_completed(self) -> None:
        action_id, _reservation_id = self.start_dispatched_action()
        self.ledger.mark_uncertain(action_id, detail="interrupted")
        projection = self.projected_job(self.ledger, self.job_id)
        self.assertNotEqual(projection["state"], "completed")
        states = {item["id"]: item["state"] for item in projection["recent_actions"]}
        self.assertEqual(states.get(action_id), "uncertain")

    def test_projection_reports_an_open_human_wait_after_restart(self) -> None:
        from lib.supervisor import lifecycle

        lifecycle.record_human_wait(
            self.ledger,
            self.job_id,
            change_id=self.cid,
            checkpoint="approval",
            material_hash="deadbeef",
        )
        reopened = self.open_ledger()
        projection = self.projected_job(reopened, self.job_id)
        self.assertNotEqual(projection["state"], "completed")
        self.assertTrue(
            any(w["kind"] == "human" and w["state"] == "open" for w in projection["waits"]),
            projection["waits"],
        )

    def test_reset_receipt_does_not_erase_retained_usage_projection(self) -> None:
        from lib.supervisor import budgets as budget_mod

        action_id, reservation_id = self.start_dispatched_action()
        self.ledger.mark_uncertain(action_id, detail="interrupted")
        self.ledger.retain_reservation(reservation_id)
        projection = self.projected_job(self.ledger, self.job_id)
        consumption = projection["budget_posture"]["consumption"]
        self.assertGreaterEqual(float(consumption["cost_usd"]), 0.5)
        self.assertNotEqual(projection["state"], "completed")
        reservation = self.ledger.reservation_for_action(action_id)
        self.assertTrue(budget_mod.is_retained(dict(reservation)))

    def test_unregistered_legacy_runs_are_unchanged_by_the_suite(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        legacy = self.root / "legacy-repo"
        legacy.mkdir()
        git(legacy, "init")
        self.assertFalse(supervision_mod.is_registered(legacy))
        self.assertIsNone(
            self.ledger.find_job_by_worktree(legacy, repository_root=legacy)
        )
        # The suite created no execution lock or state marker in the legacy
        # worktree.
        self.assertFalse((legacy / ".opsx-plan").exists())


def _load_opsx_plan() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("opsx_plan", module)
    spec.loader.exec_module(module)
    return module
