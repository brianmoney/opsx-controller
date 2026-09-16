"""Executable-level shell-bypass prevention for the supervised worker domain.

A permission block is defense in depth, not the trust root: a model client or
an agent runner invoked through a shell can circumvent a prompt-level
prohibition, and a prompt's prose cannot block it. This module is the
executable layer that makes the bypass *prevented or durably surfaced*:

- **Prevention.** :func:`classify_command` refuses the known unjournaled model
  client / agent runner binaries and the shell/indirection constructs a bypass
  uses to reach one. The worker domain's only sanctioned model path is the
  service-owned endpoint; anything the classifier refuses never executes.
- **Auditing.** A refused attempt is a policy violation: the worker's tracked
  wrapper (:func:`main`) reports it to the worker-actions endpoint through the
  ``report_violation`` verb, so it lands as a durable ``policy_violation``
  incident in the same incident surface a spoof or escalation uses, rather
  than a transcript blip.

The classifier is deliberately conservative and name/argv based (stdlib only,
no process spawn, no import of another runtime package). It is not a general
sandbox: it is the executable half of the contract, layered under the
per-role permission block and above the journaled service tool.

Importing this module has no side effects, parses no arguments, spawns no
process, and touches no ``.opsx-plan/`` state.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Any, Mapping, Sequence

# Model-client / agent-runner binaries a supervised worker must never launch.
# These are the names a bypass actually uses: a provider CLI, a local model
# client, or another agent's runner. Matching is on the executable basename so
# an absolute path is caught as well as a bare name.
UNJOURNALED_MODEL_CLIENTS: tuple[str, ...] = (
    "aider",
    "claude",
    "codex",
    "continue",
    "crush",
    "cursor-agent",
    "droid",
    "gemini",
    "goose",
    "llm",
    "ollama",
    "opencode",
    "open-interpreter",
    "plandex",
)

# Shell constructs a bypass uses to reach one of the binaries above without
# naming it in the visible command (a command substitution, a here-document, or
# a parameter expansion). These are matched as literal metacharacter sequences,
# not as loose substrings, so an ordinary `find . -name` is not mistaken for a
# bypass.
INDIRECTION_TOKENS: tuple[str, ...] = (
    "$(",
    "`",
    "<<",
    "${",
)

# Interpreters that would run a *script or string* the wrapper cannot see.
# Refused when they are the executable token (an argument value like
# `--source x` is not an interpreter invocation).
SCRIPT_INTERPRETERS: tuple[str, ...] = ("eval", "source", ".")

# Shells that would let a command be re-entered out of sight of the wrapper's
# own argv check. The supervised worker's tracked wrapper is the only
# sanctioned entry point, so a nested shell is refused.
NESTED_SHELLS: tuple[str, ...] = ("bash", "sh", "zsh", "dash", "ksh", "fish")

# Environment the wrapper reads; identical vocabulary to the service tool so
# the wrapper and the tracked service tool are one identity contract.
JOB_ID_ENV = "OPSX_SUPERVISOR_JOB_ID"
SERVICE_IDENTITY_ENV = "OPSX_SUPERVISOR_SERVICE_PRINCIPAL"
ROLE_ENV = "OPSX_SUPERVISOR_ROLE"
AGENT_ENV = "OPSX_SUPERVISOR_AGENT"


class ShellBypassError(Exception):
    """A command was refused as an unjournaled model/agent shell bypass."""

    def __init__(self, reason: str, *, command: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.command = command


def _basename(token: str) -> str:
    token = token.strip().strip("'\"")
    if not token:
        return ""
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def classify_command(command: Any, *, args: Sequence[str] | None = None) -> dict[str, Any]:
    """Classify a worker command as allowed or a shell bypass.

    Returns a plain decision: ``allowed`` plus a named ``reason`` and the
    ``kind`` of refusal (``unjournaled_model_client``, ``shell_indirection``,
    ``nested_shell``, or ``empty_command``). A command is refused when it
    launches a known model client / agent runner, when it uses shell
    indirection a bypass relies on, or when it re-enters a nested shell. The
    refusal is never a silent no-op: the caller surfaces it as a policy
    violation.
    """
    if isinstance(command, (list, tuple)):
        argv = [str(item) for item in command]
    elif isinstance(command, str):
        argv = [command]
    else:
        argv = []

    if not argv or not argv[0].strip():
        return {
            "allowed": False,
            "kind": "empty_command",
            "reason": "an empty command is not executable",
            "command": "",
        }
    raw = " ".join(argv)
    if args:
        raw = " ".join([raw, *(str(item) for item in args)])

    # The leading token is the executable; a leading `env VAR=... cmd` is
    # normalized so `env OPENAI_API_KEY=x opencode run` is still classified as
    # the model client, not as `env`.
    tokens: list[str] = []
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
    else:
        tokens = list(argv)
    while tokens and _basename(tokens[0]) == "env":
        tokens = [token for token in tokens[1:] if "=" not in token]
    executable = tokens[0] if tokens else ""
    executable_name = _basename(executable)

    if executable_name in UNJOURNALED_MODEL_CLIENTS:
        return {
            "allowed": False,
            "kind": "unjournaled_model_client",
            "reason": (
                f"{executable_name!r} is an unjournaled model client or agent "
                "runner; a supervised worker reaches a model only through the "
                "tracked, journaled service tool"
            ),
            "command": raw,
        }
    if executable_name in NESTED_SHELLS:
        # A nested shell re-enters execution outside the wrapper's argv view;
        # the only sanctioned shell surface is the tracked wrapper itself.
        return {
            "allowed": False,
            "kind": "nested_shell",
            "reason": (
                f"{executable_name!r} would re-enter execution outside the "
                "tracked wrapper; a supervised worker runs only the tracked "
                "service tool"
            ),
            "command": raw,
        }
    if executable_name in SCRIPT_INTERPRETERS:
        return {
            "allowed": False,
            "kind": "script_interpreter",
            "reason": (
                f"{executable_name!r} runs a script or string the wrapper cannot "
                "inspect; a supervised worker may not hide a command behind an "
                "interpreter"
            ),
            "command": raw,
        }
    haystack = f" {raw} "
    for token in INDIRECTION_TOKENS:
        if token in haystack:
            return {
                "allowed": False,
                "kind": "shell_indirection",
                "reason": (
                    f"the command uses shell indirection ({token!r}), which is "
                    "how an unjournaled model client is reached without naming "
                    "it; the attempt is surfaced as a policy violation"
                ),
                "command": raw,
            }
    # A model client named anywhere in the argv (e.g. `xargs opencode`) is
    # still a bypass even when it is not the leading token.
    for token in tokens[1:]:
        name = _basename(token)
        if name in UNJOURNALED_MODEL_CLIENTS:
            return {
                "allowed": False,
                "kind": "unjournaled_model_client",
                "reason": (
                    f"the command invokes the unjournaled model client "
                    f"{name!r}; a supervised worker reaches a model only "
                    "through the tracked, journaled service tool"
                ),
                "command": raw,
            }
    return {
        "allowed": True,
        "kind": None,
        "reason": None,
        "command": raw,
    }


def report_violation(
    decision: Mapping[str, Any],
    *,
    job_id: int | None = None,
    role: str | None = None,
    observed_agent: str | None = None,
    service_identity: str | None = None,
    env: Mapping[str, str] | None = None,
    dispatch_fn: Any = None,
) -> dict[str, Any]:
    """Report a refused bypass to the worker endpoint as a policy violation.

    The report rides the same ``report_violation`` verb the endpoint exposes,
    so the refusal is durable even though the command never executed. A
    reporting failure is returned rather than raised: the command has already
    been refused, and the caller must still fail closed.
    """
    environment = os.environ if env is None else env

    def _value(explicit: Any, name: str) -> Any:
        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        configured = environment.get(name)
        return configured.strip() if isinstance(configured, str) and configured.strip() else None

    if job_id is None:
        job_id = int(environment.get(JOB_ID_ENV) or 0) or None
    role = _value(role, ROLE_ENV)
    observed_agent = _value(observed_agent, AGENT_ENV)
    service_identity = _value(service_identity, SERVICE_IDENTITY_ENV)

    if dispatch_fn is None:
        from lib.supervisor import service_tool as service_tool_mod

        dispatch_fn = service_tool_mod.dispatch
    try:
        result = dispatch_fn(
            "report_violation",
            job_id=int(job_id),
            payload={
                "reason": str(decision.get("reason") or "shell bypass refused"),
                "command": str(decision.get("command") or ""),
                "bypass_kind": str(decision.get("kind") or ""),
            },
            role=role,
            observed_agent=observed_agent,
            service_identity=service_identity,
            env=environment,
        )
        return {"reported": True, "result": result}
    except Exception as exc:  # noqa: BLE001 - reporting is best-effort durability
        return {"reported": False, "error": f"{type(exc).__name__}: {exc}"}


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    dispatch_fn: Any = None,
    runner: Any = None,
) -> int:
    """Tracked worker shell wrapper: classify, then execute or refuse.

    An allowed command (a check, a build, a read-only inspection) is executed
    with its real exit status, so supervised workers keep their verification
    reach. A refused command — an unjournaled model client, an agent runner, a
    nested shell, or shell indirection — is **never executed**: the attempt is
    reported as a durable policy violation and the wrapper returns non-zero, so
    a caller can never mistake a refusal for execution.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    decision = classify_command(tokens)
    if decision["allowed"]:
        executor = runner
        if executor is None:
            import subprocess as subprocess_mod

            executor = subprocess_mod.run
        environment = os.environ if env is None else env
        completed = executor(list(tokens), env=dict(environment))
        returncode = getattr(completed, "returncode", None)
        if returncode is None and isinstance(completed, int):
            returncode = completed
        return int(returncode or 0)
    report = report_violation(decision, env=env, dispatch_fn=dispatch_fn)
    print(
        json.dumps(
            {
                "ok": False,
                "error": "ShellBypassError",
                "kind": decision["kind"],
                "message": decision["reason"],
                "command": decision["command"],
                "reported": report.get("reported", False),
            },
            sort_keys=True,
        )
    )
    return 3


__all__ = [
    "AGENT_ENV",
    "INDIRECTION_TOKENS",
    "JOB_ID_ENV",
    "NESTED_SHELLS",
    "ROLE_ENV",
    "SCRIPT_INTERPRETERS",
    "SERVICE_IDENTITY_ENV",
    "ShellBypassError",
    "UNJOURNALED_MODEL_CLIENTS",
    "classify_command",
    "main",
    "report_violation",
]
