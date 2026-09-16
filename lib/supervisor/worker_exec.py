"""Executable-level shell-bypass prevention for the supervised worker domain.

A permission block is defense in depth, not the trust root: a model client or
an agent runner invoked through a shell can circumvent a prompt-level
prohibition, and a prompt's prose cannot block it. This module is the
executable layer that makes the bypass *prevented or durably surfaced*.

**The design is a fail-closed allowlist, not a denylist.** A denylist of
dangerous executables is unbounded: any program with a programmable or
configuration-mediated execution feature (``git -c alias.x='!cmd'``, a repo
hook or filter, ``make``, ``awk system()``, ``sed``'s ``e`` flag,
``find -exec``, ``tar --to-command``, ``ssh``, an execution-prefix wrapper
such as ``nice``/``nohup``/``setsid``/``env -i``) can reach a shell or a
model client without naming it in a way a denylist can enumerate. Closing one
vector exposes the next. So the single rule here is the reverse:

    A command executes only when its leading executable is one of the
    explicitly enumerated safe executables AND its argv satisfies that
    executable's form constraints. Everything else is refused before
    execution.

Every allowlisted executable is either

- a **single-purpose inspection tool** whose argv cannot make it execute
  another program (no embedded command language, no exec-capable flag), or a
  constrained variant of one whose exec-capable flags are refused
  (``find -exec``, ``sort --compress-program``, ``rg --pre``), or
- a **named project check tool** (``openspec``), or
- **git**, restricted to built-in read-only subcommands with every
  configuration-, alias-, and hook-mediated execution surface neutralized:
  worker-supplied global config options (``-c``, ``--config-env``,
  ``--exec-path``, ``--git-dir``, ...) are refused; only built-in read-only
  subcommands are allowlisted, so aliases and external ``git-*`` commands can
  never be the subcommand (an alias cannot shadow a builtin, and hooks never
  run for the allowlisted read-only builtins); the wrapper injects config
  pins that make the pager, the external diff driver, textconv, the fsmonitor
  hook, credential helpers, SSH, and every signing helper inert; and a
  preflight refuses the command when the repository's effective config
  defines any external clean/smudge/process filter the wrapper has not
  already pinned inert.

The child environment is scrubbed the same way for every allowed command:
inherited ``GIT_*`` variables and interpreter/loader hooks (``NODE_OPTIONS``,
``PYTHONPATH``, ``LD_PRELOAD``, ``RIPGREP_CONFIG_PATH``, ...) are stripped so
environment-mediated execution cannot smuggle code into an allowed tool, and
the wrapper then pins its own inert values.

**Auditing.** A refused attempt is a policy violation: the worker's tracked
wrapper (:func:`main`) reports it to the worker-actions endpoint through the
``report_violation`` verb, so it lands as a durable ``policy_violation``
incident in the same incident surface a spoof or escalation uses, rather
than a transcript blip. The refusal is never a silent no-op: the wrapper
invokes no runner and exits non-zero.

Residual trust assumptions, stated explicitly:

- The allowlist trusts the *binaries* it names. ``openspec`` is the project's
  own check tool; the inspection tools are standard single-purpose utilities.
  The allowlist is deliberately small so this set is auditable.
- The wrapper's own environment is service-provisioned: the per-role bash
  permission allows only the literal ``opsx-worker-exec *`` pattern, so a
  worker cannot prefix environment assignments onto the wrapper invocation.
  The child-environment scrub is defense in depth on top of that.
- Shell metacharacters in the *caller's* shell (``$(...)``, backticks,
  here-documents) are expanded before this wrapper sees its argv; refusing
  them here is a durable signal, but the platform permission layer is what
  must refuse a command line containing them.

Importing this module has no side effects, parses no arguments, spawns no
process, and touches no ``.opsx-plan/`` state.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# The safe surface.
#
# The allowlist is the whole design: an executable not named here is refused
# no matter how benign it looks, and an executable named here runs only in
# its constrained form. Adding a tool to this module is a security decision
# and must come with an argument that its argv (and its configuration) cannot
# make it execute another program.
# ---------------------------------------------------------------------------

# Single-purpose inspection tools: no embedded command language, no
# exec-capable flag, no configuration-mediated execution. A value in the argv
# (a filename, a pattern) is data to these tools, never a command.
SAFE_INSPECTION_TOOLS: frozenset[str] = frozenset(
    {
        "basename",
        "cat",
        "comm",
        "cut",
        "date",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "expand",
        "false",
        "file",
        "fold",
        "grep",
        "head",
        "hostname",
        "id",
        "join",
        "ls",
        "md5sum",
        "nl",
        "paste",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "sha1sum",
        "sha256sum",
        "stat",
        "strings",
        "tail",
        "test",
        "tr",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)

# Tools that are safe only when their exec-capable flags are absent. A token
# matches a forbidden flag when it equals the flag or starts with ``flag=``.
CONSTRAINED_TOOLS: dict[str, tuple[str, ...]] = {
    # find re-enters execution with an argv the wrapper never classified.
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete"),
    # rg --pre/--hostname-bin run an external preprocessor/hostname command;
    # its config file could name the same, so RIPGREP_CONFIG_PATH is scrubbed.
    "rg": ("--pre", "--pre-glob", "--hostname-bin"),
    # sort runs --compress-program when a large sort spills to temp files.
    "sort": ("--compress-program",),
}

# Named project check tools the supervised roles need. Each is trusted as a
# fixed non-interpreter binary (see the module docstring's trust assumptions).
CHECK_TOOLS: frozenset[str] = frozenset({"openspec"})

# git is special: it is programmable through configuration (aliases, hooks,
# filters, pagers, external diff drivers, signing helpers), so only built-in
# read-only subcommands are allowlisted and the wrapper neutralizes every
# config-mediated execution surface before running one.
GIT_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "cat-file",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)

# Global git options a worker may pass. Everything else before the subcommand
# is refused: ``-c``/``--config-env`` (config-mediated programmable exec, the
# ``git -c alias.run='!opencode run' run`` vector), ``--exec-path`` (external
# command lookup), ``--git-dir``/``--work-tree``/``-C`` (redirecting which
# repository's config and hooks apply), and every other global form.
GIT_ALLOWED_GLOBAL_OPTIONS: frozenset[str] = frozenset(
    {
        "--no-pager",
        "-P",
        "--paginate",
        "-p",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    }
)

# Per-subcommand flags that would re-enable an execution surface the wrapper
# pins off; they are refused rather than overridden so flag order cannot
# matter. ``--filters``/``--textconv``/``--path`` make cat-file run content
# filters; ``--ext-diff``/``--textconv`` re-enable external diff drivers.
GIT_FORBIDDEN_SUBCOMMAND_FLAGS: dict[str, tuple[str, ...]] = {
    "cat-file": ("--filters", "--textconv", "--path"),
    "diff": ("--ext-diff", "--textconv"),
    "log": ("--ext-diff", "--textconv"),
    "show": ("--ext-diff", "--textconv"),
}

# Subcommands that read working-tree content through the diff driver stack;
# the wrapper injects --no-ext-diff --no-textconv right after the subcommand.
GIT_DIFF_FAMILY: tuple[str, ...] = ("diff", "log", "show")

# Config the wrapper pins on every git invocation so repository, global, or
# system configuration cannot make a read-only command execute anything:
# pager, fsmonitor hook, external diff driver, signing helpers, credential
# helpers, SSH, and the LFS filter driver (the one common legitimate filter;
# pinned inert here so the filter preflight can ignore it).
GIT_CONFIG_PINS: tuple[tuple[str, str], ...] = (
    ("core.pager", "cat"),
    ("core.fsmonitor", ""),
    ("diff.external", ""),
    ("gpg.program", "/bin/true"),
    ("gpg.openpgp.program", "/bin/true"),
    ("gpg.x509.program", "/bin/true"),
    ("gpg.ssh.program", "/bin/true"),
    ("credential.helper", ""),
    ("core.sshCommand", "/bin/true"),
    ("core.askPass", "/bin/true"),
    ("filter.lfs.clean", ""),
    ("filter.lfs.smudge", ""),
    ("filter.lfs.process", ""),
)

# Filter config keys the exec-time pins above make inert; the preflight
# ignores exactly these and refuses any other external filter command.
GIT_PINNED_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "filter.lfs.clean",
        "filter.lfs.smudge",
        "filter.lfs.process",
    }
)

# Environment variables removed from every allowed command's environment:
# interpreter/loader hooks that would smuggle worker-controlled code into an
# allowed tool's process (``openspec`` is a Node program, so ``NODE_OPTIONS``
# is a live vector), plus tool config indirection (``RIPGREP_CONFIG_PATH``).
ENV_STRIP_NAMES: frozenset[str] = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "GREP_OPTIONS",
        "JAVA_TOOL_OPTIONS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PERL5LIB",
        "PERL5OPT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RIPGREP_CONFIG_PATH",
        "RUBYOPT",
        "_JAVA_OPTIONS",
    }
)

# Every inherited GIT_* variable is stripped, then these inert values are
# pinned so the environment cannot re-open a config-mediated execution
# surface (GIT_PAGER beats pager.<cmd>, GIT_CONFIG_* neutralizes global and
# system config, and no inherited GIT_CONFIG_PARAMETERS/KEY/VALUE survives).
ENV_PINS: dict[str, str] = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "EDITOR": "/bin/true",
    "VISUAL": "/bin/true",
    "GIT_EDITOR": "/bin/true",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_ASKPASS": "/bin/true",
    "SSH_ASKPASS": "/bin/true",
    "GIT_SSH_COMMAND": "/bin/true",
}

# Shell constructs a bypass uses to reach a model client without naming it in
# the visible command (a command substitution, a here-document, or a
# parameter expansion). Matched as literal metacharacter sequences against
# the raw command, so an ordinary `find . -name` is not mistaken for one.
# (Metacharacters expanded by the caller's shell never reach this wrapper;
# see the module docstring.)
INDIRECTION_TOKENS: tuple[str, ...] = (
    "$(",
    "`",
    "<<",
    "${",
)

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


def _refusal(kind: str, reason: str, raw: str) -> dict[str, Any]:
    return {"allowed": False, "kind": kind, "reason": reason, "command": raw}


def _not_allowlisted(executable_name: str, raw: str) -> dict[str, Any]:
    """Refuse an executable that is not on the safe surface.

    This is the fail-closed half of the contract: interpreters, launchers,
    execution-prefix wrappers, nested shells, model clients, and every other
    programmable or unknown executable share one fate, because prevention is
    defined by what is allowed, never by what is known to be dangerous.
    """
    return _refusal(
        "not_allowlisted",
        (
            f"{executable_name!r} is not on the supervised worker's "
            "allowlisted command surface; the tracked wrapper executes only "
            "explicitly safe commands, and a supervised worker reaches a "
            "model only through the tracked, journaled service tool"
        ),
        raw,
    )


def _unsafe_form(executable_name: str, detail: str, raw: str) -> dict[str, Any]:
    """Refuse an allowlisted executable used in a forbidden form."""
    return _refusal(
        "unsafe_form",
        (
            f"{executable_name!r} is allowlisted only in constrained "
            f"read-only forms: {detail}; the attempt is surfaced as a "
            "policy violation"
        ),
        raw,
    )


def _allowed(exec_argv: list[str], raw: str, *, preflight: str | None = None) -> dict[str, Any]:
    decision: dict[str, Any] = {
        "allowed": True,
        "kind": None,
        "reason": None,
        "command": raw,
        "exec_argv": list(exec_argv),
    }
    if preflight is not None:
        decision["preflight"] = preflight
    return decision


def _matches_forbidden_flag(token: str, flag: str) -> bool:
    return token == flag or token.startswith(flag + "=")


def _classify_constrained(
    executable_name: str, tokens: list[str], raw: str
) -> dict[str, Any]:
    """Classify a tool that is safe only without its exec-capable flags."""
    forbidden = CONSTRAINED_TOOLS[executable_name]
    for token in tokens[1:]:
        for flag in forbidden:
            if _matches_forbidden_flag(token, flag):
                return _unsafe_form(
                    executable_name,
                    (
                        f"the {flag!r} flag makes it execute another "
                        "program, and the tracked wrapper never runs a "
                        "command it did not classify"
                    ),
                    raw,
                )
    return _allowed(tokens, raw)


def _classify_git(tokens: list[str], raw: str) -> dict[str, Any]:
    """Classify git: built-in read-only subcommands with config neutralized.

    git is programmable through configuration, so the subcommand must be a
    built-in read-only one (an alias cannot shadow a builtin, and hooks never
    run for the allowlisted builtins), worker-supplied global config options
    are refused, and the wrapper injects config pins plus a filter preflight
    for the execution surfaces that remain.
    """
    rest = tokens[1:]
    subcommand_index: int | None = None
    for index, token in enumerate(rest):
        if token == "--":
            break
        if token.startswith("-"):
            if token not in GIT_ALLOWED_GLOBAL_OPTIONS:
                return _unsafe_form(
                    "git",
                    (
                        f"the global option {token!r} can redirect "
                        "configuration, aliases, or command lookup "
                        "(for example -c alias.run='!cmd'), so only "
                        "pathspec/pager display options are accepted before "
                        "the subcommand"
                    ),
                    raw,
                )
        else:
            subcommand_index = index
            break
    if subcommand_index is None:
        return _unsafe_form(
            "git",
            "no allowlisted read-only subcommand was given",
            raw,
        )
    subcommand = rest[subcommand_index]
    if subcommand not in GIT_READ_ONLY_SUBCOMMANDS:
        return _unsafe_form(
            "git",
            (
                f"{subcommand!r} is not an allowlisted built-in read-only "
                "subcommand; aliases, external git-* commands, and "
                "hook-running write operations can execute arbitrary "
                "commands and never run here"
            ),
            raw,
        )
    forbidden = GIT_FORBIDDEN_SUBCOMMAND_FLAGS.get(subcommand, ())
    for token in rest[subcommand_index + 1 :]:
        if token == "--":
            break
        for flag in forbidden:
            if _matches_forbidden_flag(token, flag):
                return _unsafe_form(
                    "git",
                    (
                        f"the {flag!r} flag re-enables a content-filter or "
                        "external-diff execution surface the wrapper pins "
                        "off"
                    ),
                    raw,
                )
    exec_rest = list(rest)
    if subcommand in GIT_DIFF_FAMILY:
        exec_rest = (
            rest[: subcommand_index + 1]
            + ["--no-ext-diff", "--no-textconv"]
            + rest[subcommand_index + 1 :]
        )
    pin_argv: list[str] = []
    for key, value in GIT_CONFIG_PINS:
        pin_argv.extend(["-c", f"{key}={value}"])
    return _allowed(
        ["git", *pin_argv, *exec_rest], raw, preflight="git_external_filters"
    )


def classify_command(command: Any, *, args: Sequence[str] | None = None) -> dict[str, Any]:
    """Classify a worker command against the fail-closed safe surface.

    Returns a plain decision: ``allowed`` plus a named ``reason`` and the
    ``kind`` of refusal (``not_allowlisted``, ``unsafe_form``,
    ``config_controlled_exec``, ``shell_indirection``, or ``empty_command``).
    A command is allowed only when its leading executable is an explicitly
    enumerated safe executable **and** its argv satisfies that executable's
    form constraints; everything else is refused before execution, because
    prevention is defined by the allowlist, not by a list of known dangers.
    An allowed decision carries the exact ``exec_argv`` the wrapper will run
    (with wrapper-injected neutralization for git) and, when needed, the
    ``preflight`` the wrapper must pass before executing. The refusal is
    never a silent no-op: the caller surfaces it as a policy violation.
    """
    if isinstance(command, (list, tuple)):
        argv = [str(item) for item in command]
    elif isinstance(command, str):
        argv = [command]
    else:
        argv = []

    if not argv or not argv[0].strip():
        return _refusal("empty_command", "an empty command is not executable", "")
    raw = " ".join(argv)
    if args:
        raw = " ".join([raw, *(str(item) for item in args)])

    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
    else:
        tokens = list(argv)
    if not tokens or not tokens[0].strip():
        return _refusal("empty_command", "an empty command is not executable", raw)

    haystack = f" {raw} "
    for token in INDIRECTION_TOKENS:
        if token in haystack:
            return _refusal(
                "shell_indirection",
                (
                    f"the command uses shell indirection ({token!r}), which "
                    "is how an unjournaled model client is reached without "
                    "naming it; the attempt is surfaced as a policy violation"
                ),
                raw,
            )

    executable_name = _basename(tokens[0])
    if executable_name in SAFE_INSPECTION_TOOLS:
        return _allowed(tokens, raw)
    if executable_name in CONSTRAINED_TOOLS:
        return _classify_constrained(executable_name, tokens, raw)
    if executable_name in CHECK_TOOLS:
        return _allowed(tokens, raw)
    if executable_name == "git":
        return _classify_git(tokens, raw)
    return _not_allowlisted(executable_name, raw)


def _execution_env(base: Mapping[str, str]) -> dict[str, str]:
    """Scrub and pin the environment an allowed command runs with.

    Inherited ``GIT_*`` variables and interpreter/loader hooks are stripped
    so the environment cannot smuggle worker-controlled code or config into
    an allowed tool; the wrapper then pins its own inert values for the
    pager, editor, git config sources, and SSH/askpass helpers.
    """
    scrubbed = {
        key: value
        for key, value in base.items()
        if not key.startswith("GIT_") and key not in ENV_STRIP_NAMES
    }
    scrubbed.update(ENV_PINS)
    return scrubbed


def git_filter_preflight(
    *,
    env: Mapping[str, str] | None = None,
    run_fn: Any = None,
) -> dict[str, Any] | None:
    """Refuse a git command when repo config defines an external filter.

    Clean/smudge/process filters are configuration- and worktree-controlled
    command execution: a repository can define ``filter.<driver>.clean`` and
    match paths to it through ``.gitattributes``, and a read-only command
    that re-hashes working-tree content (``git status``, ``git diff``) then
    executes that command. The exec-time config pins neutralize the one
    common legitimate driver (LFS); any other external filter command in the
    effective configuration fails closed. A preflight that cannot complete
    also fails closed: the refusal is returned, never raised.
    """
    runner = subprocess.run if run_fn is None else run_fn
    base = os.environ if env is None else env
    try:
        proc = runner(
            [
                "git",
                "config",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process)$",
            ],
            capture_output=True,
            text=True,
            env=_execution_env(base),
        )
    except Exception as exc:  # noqa: BLE001 - a failed preflight fails closed
        return _refusal(
            "config_controlled_exec",
            (
                "the git external-filter preflight could not run "
                f"({type(exc).__name__}: {exc}), so the command fails "
                "closed rather than trusting configuration it could not "
                "inspect"
            ),
            "git",
        )
    returncode = getattr(proc, "returncode", 1)
    stdout = getattr(proc, "stdout", "") or ""
    if returncode != 0:
        if returncode == 1 and not stdout.strip():
            return None  # no filter configuration: nothing to neutralize
        return _refusal(
            "config_controlled_exec",
            (
                "the git external-filter preflight failed, so the command "
                "fails closed rather than trusting configuration it could "
                "not inspect"
            ),
            "git",
        )
    for line in stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.strip().lower() in GIT_PINNED_FILTER_KEYS:
            continue
        if value.strip():
            return _refusal(
                "config_controlled_exec",
                (
                    f"the repository configuration defines the external "
                    f"filter command {key.strip()}={value.strip()!r}, which "
                    "is worktree-controlled command execution a read-only "
                    "git command would run; the command is refused rather "
                    "than executed"
                ),
                "git",
            )
    return None


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
    preflight_fn: Any = None,
) -> int:
    """Tracked worker shell wrapper: classify against the allowlist, then run.

    An allowed command — an explicitly safe inspection tool, a named project
    check, or a constrained read-only git invocation — is executed with the
    wrapper's exact ``exec_argv`` and a scrubbed, pinned environment, and
    returns its real exit status, so supervised workers keep their
    verification reach. Anything else is **never executed**: the attempt is
    reported as a durable policy violation and the wrapper returns non-zero,
    so a caller can never mistake a refusal for execution.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if env is None else dict(env)
    decision = classify_command(tokens)
    if decision["allowed"] and decision.get("preflight") == "git_external_filters":
        preflight = git_filter_preflight(env=environment, run_fn=preflight_fn)
        if preflight is not None:
            decision = preflight
    if decision["allowed"]:
        executor = runner
        if executor is None:
            executor = subprocess.run
        completed = executor(
            list(decision.get("exec_argv") or tokens),
            env=_execution_env(environment),
        )
        returncode = getattr(completed, "returncode", None)
        if returncode is None and isinstance(completed, int):
            returncode = completed
        return int(returncode or 0)
    report = report_violation(decision, env=environment, dispatch_fn=dispatch_fn)
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
    "CHECK_TOOLS",
    "CONSTRAINED_TOOLS",
    "ENV_PINS",
    "ENV_STRIP_NAMES",
    "GIT_ALLOWED_GLOBAL_OPTIONS",
    "GIT_CONFIG_PINS",
    "GIT_DIFF_FAMILY",
    "GIT_FORBIDDEN_SUBCOMMAND_FLAGS",
    "GIT_PINNED_FILTER_KEYS",
    "GIT_READ_ONLY_SUBCOMMANDS",
    "INDIRECTION_TOKENS",
    "JOB_ID_ENV",
    "ROLE_ENV",
    "SAFE_INSPECTION_TOOLS",
    "SERVICE_IDENTITY_ENV",
    "ShellBypassError",
    "classify_command",
    "git_filter_preflight",
    "main",
    "report_violation",
]
