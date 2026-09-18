"""Contract tests for the supervised agent contracts, egress gate, and service tool.

Covers the four supervised OpenCode agent definitions and the supervision
skill as installed artifacts, the per-role session/capability contract
(spoof and escalation refusals recorded as ``policy_violation`` incidents),
the named pre-prompt egress enforcement at both supervised choke points, the
journaled worker-initiated delegation paths, the primary's bounded
service-tool surface, and verifier independence for supervised repairs.

No external network and no paid model: the loopback fake OpenCode server and
the action journal are the only moving parts.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lib.supervisor import agent_contracts as contracts_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import budgets as budget_mod
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import ledger, lock as lock_mod, model_policy
from lib.supervisor import service_tool as service_tool_mod
from lib.supervisor import session_bridge as bridge_mod
from lib.supervisor import worker_exec as worker_exec_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENCODE_INSTALLER = REPO_ROOT / "adapters" / "opencode" / "install.sh"

MANIFEST = (
    "[[changes]]\n"
    'id = "enforce-supervised-agent-contracts"\n'
    "pause_before = false\n"
    "depends_on = []\n"
)

# Every role's exact pin in the test policy; a supervised create/prompt must
# carry this normalized identity (the server's {providerID, modelID} shape).
PINNED_MODEL = {"providerID": "openai", "modelID": "gpt-4o"}

SUPERVISED_AGENT_FILES = {
    "supervisor": "opsx-supervisor.md",
    "acceptance_reviewer": "opsx-acceptance-reviewer.md",
    "fixer": "opsx-fixer.md",
    "verifier": "opsx-verifier.md",
}


def _model_env() -> dict[str, str]:
    return {
        "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
        "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
        "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
        "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        "OPSX_SUPERVISOR_MODEL": "test-provider/test-supervisor",
        "OPSX_ACCEPTANCE_REVIEWER_MODEL": "test-provider/test-acceptance",
        "OPSX_FIXER_MODEL": "test-provider/test-fixer",
        "OPSX_VERIFIER_MODEL": "test-provider/test-verifier",
    }


def _selection() -> dict:
    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": {role: "openai/gpt-4o" for role in model_policy.POLICY_ROLES},
        "stages": dict(model_policy.STANDARD_STAGE_MAPPING),
    }


def _policy() -> dict:
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["openai/gpt-4o"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": 1000.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }


class AgentContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.ledger = ledger.open_ledger(
            self.storage / "supervisor.sqlite3", repository_root=self.repo
        )
        self.addCleanup(self.ledger.close)
        self.principal = "opsx-service"
        self.job_id = self.ledger.register_job(
            run_id="run-1",
            worktree=self.repo,
            owner="service",
            owner_principal=self.principal,
            policy=_policy(),
            operator="operator",
            manifest_content=MANIFEST,
        )
        self.policy = self.ledger.current_policy(self.job_id)
        self.credentials = endpoints_mod.PeerCredentials(
            pid=os.getpid(), uid=os.getuid(), gid=os.getgid()
        )

    def worker_request(self, action_id: int, **overrides) -> dict:
        """Build a contract-satisfying worker request, with override support."""
        request = {
            "ledger": self.ledger,
            "job_id": self.job_id,
            "action_id": action_id,
            "role": "fixer",
            "observed_agent": "opsx-fixer",
            "service_identity": self.principal,
            "requested_permissions": ["read", "edit", "bash"],
            "evidence": {"kind": "usage", "payload": {"tokens": 1}},
        }
        request.update(overrides)
        return request

    def incidents(self) -> list[dict]:
        rows = self.ledger.list_incidents(self.job_id)
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Installed artifacts and the bounded primary surface
# ---------------------------------------------------------------------------


class InstalledAgentSurfaceTests(unittest.TestCase):
    """The concrete agents, skill, and shim are real installed artifacts."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {**os.environ, **_model_env(), "HOME": self.home.name}
        subprocess.run(
            ["bash", str(OPENCODE_INSTALLER), "--global"],
            cwd=REPO_ROOT,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def _agents(self) -> Path:
        return Path(self.home.name) / ".config" / "opencode" / "agents"

    def test_each_supervised_role_has_a_concrete_installed_agent(self) -> None:
        for role, filename in SUPERVISED_AGENT_FILES.items():
            path = self._agents() / filename
            self.assertTrue(path.is_file(), f"missing installed agent for {role}")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("{env:", text, f"unsubstituted placeholder in {filename}")
            self.assertIn("task: deny", text, f"{role} must not dispatch Task agents")
            self.assertIn("edit: deny" if role != "fixer" else "edit: allow", text)

    def test_supervision_skill_is_installed(self) -> None:
        skill = (
            Path(self.home.name) / ".config" / "opencode" / "skills"
            / "opsx-supervision" / "SKILL.md"
        )
        self.assertTrue(skill.is_file(), "supervision skill must be installed")
        text = skill.read_text(encoding="utf-8")
        self.assertIn("opsx-supervise", text)

    def test_primary_agent_surface_is_bounded_to_evidence_and_service_tool(self) -> None:
        text = (self._agents() / "opsx-supervisor.md").read_text(encoding="utf-8")
        # Arbitrary Bash and Task dispatch are denied; only the tracked shim
        # pattern is allowed (broad rule first — last match wins).
        self.assertIn('"*": deny', text)
        self.assertIn('"opsx-supervise *": allow', text)
        self.assertIn("task: deny", text)
        self.assertGreater(
            text.index('"*": deny'), -1
        )
        self.assertLess(
            text.index('"*": deny'),
            text.index('"opsx-supervise *": allow'),
            "the broad deny must precede the narrow allow (last match wins)",
        )

    def test_role_capability_allowlist_matches_the_primary_surface(self) -> None:
        caps = contracts_mod.role_capabilities("supervisor")
        self.assertIn("read", caps)
        self.assertIn(contracts_mod.PRIMARY_SERVICE_TOOL_CAPABILITY, caps)
        for forbidden in ("bash", "edit", "task", "skill:opsx-controller"):
            self.assertNotIn(forbidden, caps)

    def test_service_tool_is_a_real_executable(self) -> None:
        shim = Path(self.home.name) / ".local" / "bin" / "opsx-supervise"
        self.assertTrue(shim.is_file())
        self.assertTrue(os.access(str(shim), os.X_OK))
        proc = subprocess.run(
            [str(shim), "list-verbs"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verbs = set(proc.stdout.split())
        self.assertEqual(verbs, set(service_tool_mod.SERVICE_VERBS))


# ---------------------------------------------------------------------------
# The tracked service tool's bounded verb surface
# ---------------------------------------------------------------------------


class ServiceToolSurfaceTests(unittest.TestCase):
    def test_operator_endpoint_is_never_reachable(self) -> None:
        self.assertEqual(
            service_tool_mod.resolve_endpoint(None), endpoints_mod.ENDPOINT_WORKER
        )
        with self.assertRaises(service_tool_mod.ServiceToolError):
            service_tool_mod.resolve_endpoint(endpoints_mod.ENDPOINT_OPERATOR)

    def test_unknown_and_operator_verbs_are_refused(self) -> None:
        for verb in ("approve", "reset_change", "revise_policy", "enable", "cancel", "rm"):
            with self.subTest(verb=verb):
                with self.assertRaises(service_tool_mod.ServiceToolError):
                    service_tool_mod.build_request(
                        verb,
                        job_id=1,
                        role="fixer",
                        observed_agent="opsx-fixer",
                        service_identity="opsx-service",
                    )

    def test_declared_verbs_are_a_subset_of_worker_handlers(self) -> None:
        self.assertTrue(set(service_tool_mod.SERVICE_VERBS).issubset(endpoints_mod.WORKER_HANDLERS))
        self.assertTrue(
            set(service_tool_mod.SERVICE_VERBS).isdisjoint(endpoints_mod.OPERATOR_HANDLERS)
        )

    def test_missing_job_id_fails_closed(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = service_tool_mod.main(["heartbeat"], env={})
        self.assertEqual(code, 2)
        self.assertIn("ServiceToolError", out.getvalue())

    def test_a_request_without_role_agent_or_identity_is_refused(self) -> None:
        """build_request refuses to frame an unidentified supervised request."""
        base = {
            "job_id": 1,
            "role": "fixer",
            "observed_agent": "opsx-fixer",
            "service_identity": "opsx-service",
        }
        for omitted in ("role", "observed_agent", "service_identity"):
            with self.subTest(omitted=omitted):
                kwargs = {key: value for key, value in base.items() if key != omitted}
                with self.assertRaises(service_tool_mod.ServiceToolError):
                    service_tool_mod.build_request("heartbeat", **kwargs)

    def test_an_unregistered_role_is_refused(self) -> None:
        with self.assertRaises(service_tool_mod.ServiceToolError):
            service_tool_mod.build_request(
                "heartbeat",
                job_id=1,
                role="root",
                observed_agent="opsx-supervisor",
                service_identity="opsx-service",
            )

    def test_the_payload_cannot_override_the_identity_fields(self) -> None:
        for key in ("role", "observed_agent", "service_identity", "verb"):
            with self.subTest(key=key):
                with self.assertRaises(service_tool_mod.ServiceToolError):
                    service_tool_mod.build_request(
                        "heartbeat",
                        job_id=1,
                        role="fixer",
                        observed_agent="opsx-fixer",
                        service_identity="opsx-service",
                        payload={key: "spoofed"},
                    )

    def test_a_well_formed_request_carries_the_identity(self) -> None:
        request = service_tool_mod.build_request(
            "heartbeat",
            job_id=7,
            role="verifier",
            observed_agent="opsx-verifier",
            service_identity="opsx-service",
        )
        self.assertEqual(request["role"], "verifier")
        self.assertEqual(request["observed_agent"], "opsx-verifier")
        self.assertEqual(request["service_identity"], "opsx-service")
        self.assertEqual(request["job_id"], 7)


# ---------------------------------------------------------------------------
# Session contract: spoof and escalation are blocked and recorded
# ---------------------------------------------------------------------------


class SessionContractTests(AgentContractTestCase):
    def test_matching_session_is_allowed(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy, "fixer", "opsx-fixer", ["read", "edit"]
        )
        self.assertTrue(check["allowed"], check)
        self.assertEqual(check["expected_agent"], "opsx-fixer")

    def test_another_role_agent_is_a_spoof(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy, "verifier", "opsx-fixer"
        )
        self.assertFalse(check["allowed"])
        self.assertEqual(check["violations"][0]["kind"], "agent_mismatch")

    def test_unbound_agent_is_not_defaulted(self) -> None:
        check = contracts_mod.check_session_contract(self.policy, "verifier", None)
        self.assertFalse(check["allowed"])
        self.assertEqual(check["violations"][0]["kind"], "agent_mismatch")

    def test_unknown_role_is_refused(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy, "root", "opsx-supervisor"
        )
        self.assertFalse(check["allowed"])

    def test_unavailable_policy_is_refused(self) -> None:
        check = contracts_mod.check_session_contract(None, "fixer", "opsx-fixer")
        self.assertFalse(check["allowed"])
        self.assertTrue(
            any(v["kind"] == "policy_unavailable" for v in check["violations"]), check
        )

    def test_unpinned_role_is_refused(self) -> None:
        pins = {role: "openai/gpt-4o" for role in model_policy.POLICY_ROLES}
        pins.pop("fixer")
        check = contracts_mod.check_session_contract(
            {"model_selection": {"version": 1, "roles": pins, "stages": {}}},
            "fixer",
            "opsx-fixer",
        )
        self.assertFalse(check["allowed"])
        self.assertTrue(
            any(v["kind"] == "unpinned_role" for v in check["violations"]), check
        )

    def test_a_model_override_is_refused(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy,
            "fixer",
            "opsx-fixer",
            requested_model="other-provider/other-model",
        )
        self.assertFalse(check["allowed"])
        self.assertIn(
            "model_pin_mismatch", [v["kind"] for v in check["violations"]]
        )

    def test_the_exact_pin_is_accepted(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy, "fixer", "opsx-fixer", requested_model="openai/gpt-4o"
        )
        self.assertTrue(check["allowed"], check)

    def test_an_absent_model_identity_is_refused(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy, "fixer", "opsx-fixer", requested_model=""
        )
        self.assertFalse(check["allowed"])
        self.assertIn("unbound_model", [v["kind"] for v in check["violations"]])

    def test_capability_escalation_is_refused(self) -> None:
        check = contracts_mod.check_session_contract(
            self.policy, "verifier", "opsx-verifier", ["read", "edit", "task"]
        )
        self.assertFalse(check["allowed"])
        kinds = [v["kind"] for v in check["violations"]]
        self.assertIn("capability_escalation", kinds)

    def test_enforce_records_policy_violation_incident(self) -> None:
        with self.assertRaises(contracts_mod.SessionContractViolation) as ctx:
            contracts_mod.enforce_session_contract(
                self.ledger,
                self.job_id,
                policy=self.policy,
                role="verifier",
                observed_agent="opsx-supervisor",
                requested_permissions=["read", "edit"],
            )
        incident_id = ctx.exception.check["incident_id"]
        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["id"], incident_id)
        self.assertEqual(incidents[0]["kind"], "policy_violation")
        self.assertIn("observed_agent=opsx-supervisor", incidents[0]["summary"])
        self.assertIn("expected_agent=opsx-verifier", incidents[0]["summary"])

    def test_worker_endpoint_refuses_escalation_and_records_it(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="fix", run_id="run-1"
        )
        self.ledger.dispatch_action(action_id)
        with self.assertRaises(broker_mod.BrokerMediationError):
            endpoints_mod._worker_record_evidence(
                self.worker_request(
                    action_id,
                    role="verifier",
                    observed_agent="opsx-verifier",
                    requested_permissions=["read", "edit"],
                ),
                self.credentials,
            )
        self.assertEqual(self.ledger.list_evidence(action_id), [])
        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "policy_violation")

    def test_worker_endpoint_accepts_a_contract_matching_request(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="fix", run_id="run-1"
        )
        self.ledger.dispatch_action(action_id)
        result = endpoints_mod._worker_record_evidence(
            self.worker_request(action_id), self.credentials
        )
        self.assertEqual(result["action_id"], action_id)
        self.assertEqual(self.incidents(), [])

    def test_worker_endpoint_refuses_a_request_missing_its_identity(self) -> None:
        """A supervised request that omits role/agent/identity is refused."""
        cases = {
            "omitted_role": {"role": None},
            "omitted_agent": {"observed_agent": None},
            "omitted_identity": {"service_identity": None},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                action_id = self.ledger.begin_action(
                    self.job_id, kind="fix", run_id="run-1"
                )
                self.ledger.dispatch_action(action_id)
                request = self.worker_request(action_id, **override)
                request.pop("role" if "role" in override else "", None)
                if "role" in override:
                    request.pop("role", None)
                if "observed_agent" in override:
                    request.pop("observed_agent", None)
                if "service_identity" in override:
                    request.pop("service_identity", None)
                with self.assertRaises(broker_mod.BrokerMediationError):
                    endpoints_mod._worker_record_evidence(request, self.credentials)
                self.assertEqual(
                    self.ledger.list_evidence(action_id), [],
                    "an unidentified request must never record evidence",
                )
        incidents = self.incidents()
        self.assertEqual(len(incidents), 3)
        self.assertTrue(
            all(row["kind"] == "policy_violation" for row in incidents)
        )

    def test_worker_endpoint_refuses_a_spoofed_service_identity(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="fix", run_id="run-1"
        )
        self.ledger.dispatch_action(action_id)
        with self.assertRaises(broker_mod.BrokerMediationError):
            endpoints_mod._worker_record_evidence(
                self.worker_request(action_id, service_identity="someone-else"),
                self.credentials,
            )
        self.assertEqual(self.ledger.list_evidence(action_id), [])
        self.assertEqual(self.incidents()[0]["kind"], "policy_violation")

    def test_worker_endpoint_refuses_a_model_override(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="fix", run_id="run-1"
        )
        self.ledger.dispatch_action(action_id)
        with self.assertRaises(broker_mod.BrokerMediationError):
            endpoints_mod._worker_record_evidence(
                self.worker_request(
                    action_id, requested_model="other-provider/other-model"
                ),
                self.credentials,
            )
        self.assertEqual(self.ledger.list_evidence(action_id), [])
        self.assertIn(
            "model_pin_mismatch",
            self.incidents()[0]["summary"] + " "
            + json.dumps([row["kind"] for row in self.incidents()]),
        )


# ---------------------------------------------------------------------------
# Pre-prompt egress enforcement
# ---------------------------------------------------------------------------


class EgressEnforcementTests(AgentContractTestCase):
    def test_enforced_with_a_trusted_gateway(self) -> None:
        decision = contracts_mod.evaluate_transport(
            {}, {"gateway_endpoint": "https://gateway.invalid"}
        )
        self.assertTrue(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_GATEWAY)

    def test_a_bare_loopback_target_is_not_isolated_transport(self) -> None:
        """A loopback hostname alone does not prove the service-owned server."""
        decision = contracts_mod.evaluate_transport({}, {"target": "127.0.0.1:4090"})
        self.assertFalse(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_UNENFORCED)

    def test_enforced_for_the_launched_service_server_identity(self) -> None:
        binding = contracts_mod.capture_launched_server_binding(
            "127.0.0.1:4090", self._live_identity()
        )
        decision = contracts_mod.evaluate_transport(
            {}, {"server_binding": binding}
        )
        self.assertTrue(decision.enforced, decision)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_ISOLATED)

    def test_a_caller_supplied_live_pid_and_loopback_target_is_refused(self) -> None:
        """A caller naming a live pid and a loopback port is evidence, not the server."""
        live = self._live_identity()
        decision = contracts_mod.evaluate_transport(
            {},
            {
                "target": "127.0.0.1:4090",
                "server_address": "127.0.0.1:4090",
                "server_identity": live,
            },
        )
        self.assertFalse(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_UNENFORCED)
        self.assertIn("binding", decision.detail)

    def test_an_arbitrary_loopback_target_with_a_foreign_identity_is_refused(self) -> None:
        """A loopback address with someone else's process identity is not the server."""
        foreign = json.dumps(
            {"pid": 999_999_999, "process_start": 1.0, "boot_id": "other-boot"},
            sort_keys=True,
        )
        decision = contracts_mod.evaluate_transport(
            {},
            {"target": "127.0.0.1:4090", "server_identity": foreign},
        )
        self.assertFalse(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_UNENFORCED)

    def test_a_dead_launched_server_binding_is_refused(self) -> None:
        dead = json.dumps(
            {"pid": 999_999_999, "process_start": 1.0, "boot_id": "other-boot"},
            sort_keys=True,
        )
        binding = contracts_mod.capture_launched_server_binding("127.0.0.1:4090", dead)
        self.assertIsNotNone(binding, "the binding captures shape; liveness is decided later")
        decision = contracts_mod.evaluate_transport({}, {"server_binding": binding})
        self.assertFalse(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_UNENFORCED)

    def test_a_target_mismatching_the_launched_address_is_refused(self) -> None:
        binding = contracts_mod.capture_launched_server_binding(
            "127.0.0.1:4090", self._live_identity()
        )
        decision = contracts_mod.evaluate_transport(
            {}, {"server_binding": binding, "target": "127.0.0.1:9999"}
        )
        self.assertFalse(decision.enforced)
        self.assertIn("not the launched", decision.detail)

    def test_unenforced_without_gateway_or_loopback_target(self) -> None:
        decision = contracts_mod.evaluate_transport({}, {"target": "10.0.0.5:4090"})
        self.assertFalse(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_UNENFORCED)

    def test_a_leaked_provider_credential_is_never_enforced(self) -> None:
        environ = {"OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "sk-other"}
        decision = contracts_mod.evaluate_transport(
            environ, {"gateway_endpoint": "https://gateway.invalid"}
        )
        self.assertFalse(decision.enforced)
        self.assertIn("OPENAI_API_KEY", decision.detail)

    def _live_identity(self) -> str:
        return json.dumps(
            {
                "pid": os.getpid(),
                "process_start": lock_mod.process_start_time(os.getpid()),
                "boot_id": lock_mod.boot_identity(),
            },
            sort_keys=True,
        )

    def test_assert_raises_the_named_error_before_any_side_effect(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="fix", run_id="run-1"
        )
        with self.assertRaises(contracts_mod.EgressEnforcementError):
            contracts_mod.assert_pre_prompt_transport(
                self.ledger, action_id, environ={}, config={"target": "10.0.0.5:4090"}
            )
        kinds = [row["kind"] for row in self.ledger.list_evidence(action_id)]
        # The decision is recorded even when it blocks, so the refusal is
        # durable; no dispatch record exists.
        self.assertIn(contracts_mod.EVIDENCE_TRANSPORT_DECISION, kinds)
        self.assertIsNone(self.ledger.latest_dispatch(action_id))

    def test_bridge_refuses_a_model_override_before_any_prompt(self) -> None:
        """The bridge enforces the role's exact pin, not merely that one exists."""
        bridge = self._journaled_bridge()
        action_calls: list[str] = []
        original = bridge.bridge.prompt_async
        bridge.bridge.prompt_async = lambda *a, **k: action_calls.append("prompt")
        try:
            with self.assertRaises(contracts_mod.SessionContractViolation):
                bridge.prompt(
                    "ses_override",
                    text="hi",
                    role="fixer",
                    model={"providerID": "other", "modelID": "other-model"},
                    environ={},
                )
        finally:
            bridge.bridge.prompt_async = original
        self.assertEqual(action_calls, [], "no prompt may be issued")
        rows = self.ledger.list_actions(self.job_id)
        prompt_rows = [r for r in rows if r["kind"] == bridge_mod.PROMPT_ACTION_KIND]
        self.assertEqual(len(prompt_rows), 1)
        self.assertEqual(prompt_rows[0]["state"], "failed")
        self.assertIsNone(self.ledger.latest_dispatch(int(prompt_rows[0]["id"])))
        self.assertEqual(self.incidents()[0]["kind"], "policy_violation")

    def test_bridge_refuses_a_missing_model_before_any_session_or_prompt(self) -> None:
        """A supervised create/prompt with no model is refused, never defaulted."""
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        with FakeOpencodeServer() as server:
            bridge = self._journaled_bridge(server)
            created: list[str] = []
            original = bridge.bridge.create_session
            bridge.bridge.create_session = lambda *a, **k: created.append("create")
            try:
                with self.assertRaises(contracts_mod.SessionContractViolation) as ctx:
                    bridge.create_session(title="unpinned", role="fixer")
            finally:
                bridge.bridge.create_session = original
            self.assertEqual(created, [], "no session request may be issued")
            self.assertIn(
                "unbound_model",
                [v["kind"] for v in ctx.exception.check["violations"]],
            )
            self.assertEqual(self.incidents()[0]["kind"], "policy_violation")

    def test_bridge_refuses_a_malformed_model_mapping(self) -> None:
        bridge = self._journaled_bridge()
        with self.assertRaises(contracts_mod.SessionContractViolation) as ctx:
            bridge.prompt(
                "ses_malformed",
                text="hi",
                role="fixer",
                model={"providerID": "openai"},
                environ={},
            )
        self.assertIn(
            "unbound_model",
            [v["kind"] for v in ctx.exception.check["violations"]],
        )

    def test_bridge_refuses_an_arbitrary_loopback_transport_target(self) -> None:
        """A caller-supplied loopback target is not authority; the bridge refuses it."""
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        with FakeOpencodeServer() as server:
            bridge = self._journaled_bridge(server)
            session = bridge.bridge.create_session(title="arbitrary-loopback")
            action_calls: list[str] = []
            original = bridge.bridge.prompt_async
            bridge.bridge.prompt_async = lambda *a, **k: action_calls.append("prompt")
            # Someone else's loopback port and process identity: not the server,
            # and a caller-supplied override rather than service-owned state.
            foreign = json.dumps(
                {"pid": 999_999_999, "process_start": 1.0, "boot_id": "other-boot"},
                sort_keys=True,
            )
            try:
                with self.assertRaises(contracts_mod.EgressEnforcementError) as ctx:
                    bridge.prompt(
                        session["id"],
                        text="hi",
                        role="fixer",
                        model=PINNED_MODEL,
                        transport_config={
                            "target": "127.0.0.1:4090",
                            "server_address": "127.0.0.1:4090",
                            "server_identity": foreign,
                        },
                        environ={},
                    )
            finally:
                bridge.bridge.prompt_async = original
            self.assertEqual(action_calls, [], "no prompt may be issued")
            self.assertIn("override", ctx.exception.reason)

    def test_bridge_refuses_and_fails_the_action_when_unenforced(self) -> None:
        bridge = self._journaled_bridge_for_address("10.0.0.5:4090")
        action_calls: list[str] = []
        original = bridge.bridge.prompt_async
        bridge.bridge.prompt_async = lambda *a, **k: action_calls.append("prompt")
        try:
            with self.assertRaises(contracts_mod.EgressEnforcementError):
                bridge.prompt(
                    "ses_unenforced",
                    text="hi",
                    role="fixer",
                    model=PINNED_MODEL,
                    environ={},
                )
        finally:
            bridge.bridge.prompt_async = original
        self.assertEqual(action_calls, [], "no prompt may be issued")
        rows = self.ledger.list_actions(self.job_id)
        prompt_rows = [r for r in rows if r["kind"] == bridge_mod.PROMPT_ACTION_KIND]
        self.assertEqual(len(prompt_rows), 1)
        self.assertEqual(prompt_rows[0]["state"], "failed")
        self.assertIsNone(self.ledger.latest_dispatch(int(prompt_rows[0]["id"])))

    def test_bridge_records_the_decision_before_the_dispatch_record(self) -> None:
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        with FakeOpencodeServer() as server:
            bridge = self._journaled_bridge(server)
            session = bridge.bridge.create_session(title="gate-order")
            identity = bridge.prompt(
                session["id"],
                text="ordered",
                role="fixer",
                model=PINNED_MODEL,
                environ={},
            )
        evidence = self.ledger.list_evidence(identity.action_id)
        kinds = [row["kind"] for row in evidence]
        self.assertIn(contracts_mod.EVIDENCE_TRANSPORT_DECISION, kinds)
        self.assertIn(bridge_mod.EVIDENCE_SESSION_BINDING, kinds)
        self.assertLess(
            kinds.index(contracts_mod.EVIDENCE_TRANSPORT_DECISION),
            kinds.index(bridge_mod.EVIDENCE_SESSION_BINDING),
            "the enforcement decision must precede the dispatch-side evidence",
        )

    def test_bridge_create_session_binds_the_roles_concrete_agent(self) -> None:
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        with FakeOpencodeServer() as server:
            bridge = self._journaled_bridge(server)
            captured: list[dict] = []
            original = bridge.bridge.create_session

            def spy(*args, **kwargs):
                captured.append(dict(kwargs))
                return original(*args, **kwargs)

            bridge.bridge.create_session = spy
            try:
                created = bridge.create_session(
                    title="fixer-session", role="fixer", model=PINNED_MODEL
                )
            finally:
                bridge.bridge.create_session = original
        self.assertTrue(created.get("id"))
        self.assertEqual(
            captured,
            [
                {
                    "title": "fixer-session",
                    "agent": "opsx-fixer",
                    "model": PINNED_MODEL,
                }
            ],
        )

    def test_bridge_create_session_refuses_an_unregistered_role(self) -> None:
        from tests.supervisor.test_session_bridge import FakeOpencodeServer

        with FakeOpencodeServer() as server:
            bridge = self._journaled_bridge(server)
            called: list[str] = []
            original = bridge.bridge.create_session
            bridge.bridge.create_session = lambda *a, **k: called.append("create")
            try:
                with self.assertRaises(contracts_mod.SessionContractViolation):
                    bridge.create_session(title="rogue", role="root")
            finally:
                bridge.bridge.create_session = original
        self.assertEqual(called, [], "no session request may be issued")
        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "policy_violation")

    def _journaled_bridge(self, server=None):
        if server is None:
            transport = bridge_mod.LoopbackTransport("127.0.0.1", 1, timeout=0.5)
            self.addCleanup(transport.close)
            raw = bridge_mod.SessionBridge(transport)
        else:
            transport = bridge_mod.LoopbackTransport.from_address(server.address)
            self.addCleanup(transport.close)
            raw = bridge_mod.SessionBridge(transport)
            raw.check_capability()
        # The loopback fake server runs in this process, so this process's
        # fenceable identity is the launched service-server identity the
        # isolated-transport decision requires.
        identity = bridge_mod.serialize_process_identity(os.getpid())
        return bridge_mod.JournaledSessionBridge(
            raw,
            self.ledger,
            job_id=self.job_id,
            run_id="run-1",
            policy=self.policy,
            process_id=identity,
            server_identity=identity,
            change_id="enforce-supervised-agent-contracts",
        )

    def _journaled_bridge_for_address(self, address: str):
        """A bridge whose launched-server address is *address* (immutable)."""

        class _Transport:
            def __init__(self, value: str) -> None:
                self.address = value

        class _RawBridge:
            def __init__(self, value: str) -> None:
                self.transport = _Transport(value)

            def prompt_async(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("no prompt may be issued")

        identity = bridge_mod.serialize_process_identity(os.getpid())
        return bridge_mod.JournaledSessionBridge(
            _RawBridge(address),
            self.ledger,
            job_id=self.job_id,
            run_id="run-1",
            policy=self.policy,
            process_id=identity,
            server_identity=identity,
            change_id="enforce-supervised-agent-contracts",
        )


# ---------------------------------------------------------------------------
# Orchestrator dispatch gate wiring
# ---------------------------------------------------------------------------


class DispatchEgressGateTests(AgentContractTestCase):
    def test_unenforced_environment_blocks_before_the_intent(self) -> None:
        from lib.orchestrator import journal_dispatch

        with self.assertRaises(journal_dispatch.EgressGateError):
            journal_dispatch.assert_egress_gate(
                self.ledger,
                self.job_id,
                environ={"OPENAI_API_KEY": "sk-leaked"},
            )
        self.assertEqual(self.ledger.list_actions(self.job_id), [])

    def test_egress_gate_error_is_also_the_named_contract_error(self) -> None:
        from lib.orchestrator import journal_dispatch

        # Compare by qualified name: ``test_module_layout`` fresh-imports the
        # supervisor modules in the same process, which rebinds class identity
        # while journal_dispatch keeps its original reference.
        base_names = {f"{cls.__module__}.{cls.__qualname__}" for cls in journal_dispatch.EgressGateError.__mro__}
        self.assertIn(
            "lib.supervisor.agent_contracts.EgressEnforcementError", base_names
        )
        self.assertIn("lib.orchestrator.journal_dispatch.DispatchGateError", base_names)

    def test_credential_free_worker_domain_spawn_is_enforced(self) -> None:
        from lib.orchestrator import journal_dispatch

        decision = journal_dispatch.assert_egress_gate(
            self.ledger, self.job_id, environ={}
        )
        self.assertTrue(decision.enforced)
        self.assertEqual(decision.path, contracts_mod.TRANSPORT_ISOLATED)

    def test_gated_dispatch_records_the_decision_before_the_dispatch_record(self) -> None:
        from contextlib import contextmanager

        from lib.orchestrator import journal_dispatch
        from lib.supervisor import lock as lock_mod

        manifest_path = self.repo / "plan.toml"
        manifest_path.write_text(MANIFEST, encoding="utf-8")
        gate = {
            "ledger": self.ledger,
            "job_id": self.job_id,
            "policy": self.policy,
            "policy_revision": int(self.policy["revision"]),
            "manifest_snapshot_hash": self.policy["manifest_snapshot_hash"],
            "manifest_path": str(manifest_path),
        }

        @contextmanager
        def fenced():
            identity = lock_mod.current_identity()
            self.ledger.record_fencing(
                self.job_id,
                event="acquired",
                owner="test-service",
                pid=identity["pid"],
                process_start=identity["process_start"],
                boot_id=identity["boot_id"],
                host=identity["host"],
            )
            try:
                yield
            finally:
                self.ledger.record_fencing(
                    self.job_id,
                    event="released",
                    owner="test-service",
                    pid=identity["pid"],
                    process_start=identity["process_start"],
                    boot_id=identity["boot_id"],
                    host=identity["host"],
                )

        try:
            with fenced():
                entry = journal_dispatch.gated_dispatch(
                    self.repo,
                    {"changes": {"enforce-supervised-agent-contracts": {"timeout_minutes": 1}}},
                    gate,
                    "enforce-supervised-agent-contracts",
                    "implement",
                    1,
                    {"escalation": {"active": False}},
                    "run-1",
                    resolved_model="openai/gpt-4o",
                    environ={},
                )
        finally:
            journal_dispatch.end_active_dispatch()

        action_id = entry["action_id"]
        kinds = [row["kind"] for row in self.ledger.list_evidence(action_id)]
        self.assertIn(contracts_mod.EVIDENCE_TRANSPORT_DECISION, kinds)
        dispatch = self.ledger.latest_dispatch(action_id)
        self.assertIsNotNone(dispatch, "the dispatch record must exist")


# ---------------------------------------------------------------------------
# Journaled worker-initiated delegation
# ---------------------------------------------------------------------------


class DelegationJournalTests(AgentContractTestCase):
    def _dispatched_action(self, kind: str = "fix") -> int:
        action_id = self.ledger.begin_action(self.job_id, kind=kind, run_id="run-1")
        self.ledger.dispatch_action(action_id)
        return action_id

    def test_native_task_session_binding_is_journaled(self) -> None:
        from lib.orchestrator import journal_dispatch

        action_id = self._dispatched_action()
        endpoints_mod._worker_record_evidence(
            self.worker_request(
                action_id,
                session_id="task-session-1",
                evidence={"kind": "usage", "payload": {"tokens": 3}},
            ),
            self.credentials,
        )
        row = self.ledger.latest_dispatch(action_id)
        self.assertEqual(row["session_id"], "task-session-1")
        kinds = [e["kind"] for e in self.ledger.list_evidence(action_id)]
        self.assertIn("session_binding", kinds)
        # The same binding surface the orchestrator uses is reachable.
        self.assertTrue(callable(journal_dispatch.record_session_binding))

    def test_worker_reported_subprocess_identity_is_journaled(self) -> None:
        action_id = self._dispatched_action()
        identity = json.dumps(
            {"pid": os.getpid(), "process_start": 1.0, "boot_id": "boot-x"},
            sort_keys=True,
        )
        endpoints_mod._worker_record_evidence(
            self.worker_request(
                action_id,
                process_identity=identity,
                evidence={"kind": "usage", "payload": {"tokens": 4}},
            ),
            self.credentials,
        )
        row = self.ledger.latest_dispatch(action_id)
        self.assertEqual(row["process_id"], identity)
        payloads = [
            e["payload"] for e in self.ledger.list_evidence(action_id)
            if json.loads(e["payload"] or "{}").get("process_identity")
        ]
        self.assertTrue(payloads, "the subprocess identity must be journaled")

    def test_binding_failure_for_delegation_fails_closed(self) -> None:
        # An action with no dispatch row cannot bind a delegation identity, so
        # the worker learns its delegation was not journaled rather than
        # running unjournaled.
        action_id = self.ledger.begin_action(self.job_id, kind="fix", run_id="run-1")
        with self.assertRaises(ledger.UnknownRecordError):
            endpoints_mod._worker_record_evidence(
                self.worker_request(
                    action_id,
                    session_id="task-session-2",
                    evidence={"kind": "usage", "payload": {"tokens": 5}},
                ),
                self.credentials,
            )
        self.assertEqual(self.ledger.list_evidence(action_id), [])


# ---------------------------------------------------------------------------
# Repair verdicts never self-certify completion
# ---------------------------------------------------------------------------


class RepairConsumptionTransitionTests(AgentContractTestCase):
    """Every repair-consuming transition is gated on an independent verifier."""

    def _fixer_action(self, *, session_id: str = "ses_fixer") -> int:
        action_id = self.ledger.begin_action(
            self.job_id,
            kind="fix",
            run_id="run-1",
            detail=json.dumps(
                {"change_id": "enforce-supervised-agent-contracts"}, sort_keys=True
            ),
        )
        self.ledger.dispatch_action(action_id, session_id=session_id)
        return action_id

    def _record_fixer_report(
        self, action_id: int, *, session_id: str = "ses_fixer"
    ) -> None:
        contracts_mod.record_repair_evidence(
            self.ledger,
            action_id,
            fixer_report={
                "role": "fixer",
                "repair": "repaired",
                "checks": [{"command": "unit", "result": "pass"}],
                "self_certified": False,
            },
            verifier_verdict=None,
            decision={"consumable": False, "fixer_claim": "repaired"},
        )

    def test_a_fixer_only_report_blocks_a_dispatch_transition(self) -> None:
        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        with self.assertRaises(contracts_mod.RepairConsumptionError):
            contracts_mod.assert_repair_consumable(
                self.ledger,
                self.job_id,
                transition="dispatch",
                change_id="enforce-supervised-agent-contracts",
            )
        incidents = self.incidents()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "policy_violation")

    def test_a_same_session_verifier_verdict_blocks_consumption(self) -> None:
        action_id = self._fixer_action(session_id="ses_same")
        self._record_fixer_report(action_id, session_id="ses_same")
        with self.assertRaises(contracts_mod.RepairConsumptionError) as ctx:
            contracts_mod.assert_repair_consumable(
                self.ledger,
                self.job_id,
                transition="resume",
                change_id="enforce-supervised-agent-contracts",
                fixer_report={"repair": "repaired", "session_id": "ses_same"},
                verifier_verdict={
                    "verdict": "pass",
                    "repair_verified": True,
                    "diff_reviewed": True,
                    "session_id": "ses_same",
                },
            )
        self.assertIn("not independently verified", ctx.exception.reason)

    def test_an_independent_pass_unblocks_consumption(self) -> None:
        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        decision = contracts_mod.assert_repair_consumable(
            self.ledger,
            self.job_id,
            transition="dispatch",
            change_id="enforce-supervised-agent-contracts",
            verifier_verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": True,
                "session_id": "ses_verify",
            },
        )
        self.assertTrue(decision["consumable"])
        self.assertEqual(self.incidents(), [])

    def test_a_change_with_no_recorded_repair_passes_unchanged(self) -> None:
        decision = contracts_mod.assert_repair_consumable(
            self.ledger,
            self.job_id,
            transition="dispatch",
            change_id="no-repair-here",
        )
        self.assertTrue(decision["consumable"])
        self.assertEqual(self.incidents(), [])

    def test_the_gate_reads_the_journal_when_context_is_omitted(self) -> None:
        """A caller cannot escape the gate by not passing the repair context."""
        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        recorded = contracts_mod.repair_state_from_ledger(
            self.ledger, self.job_id, "enforce-supervised-agent-contracts"
        )
        self.assertIsNotNone(recorded["fixer_report"])
        with self.assertRaises(contracts_mod.RepairConsumptionError):
            contracts_mod.assert_repair_consumable(
                self.ledger,
                self.job_id,
                transition="reset",
                change_id="enforce-supervised-agent-contracts",
            )

    def test_the_dispatch_boundary_consumes_the_gate(self) -> None:
        """``assert_repair_gate`` is a production consumer of the repair gate."""
        from lib.orchestrator import journal_dispatch

        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        with self.assertRaises(journal_dispatch.RepairGateError):
            journal_dispatch.assert_repair_gate(
                self.ledger,
                self.job_id,
                "enforce-supervised-agent-contracts",
                transition="dispatch",
            )

    def test_a_fixer_only_report_blocks_the_commit_transition(self) -> None:
        """The archive-stage commit is reached only through the gated boundary."""
        from lib.orchestrator import journal_dispatch

        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        with self.assertRaises(journal_dispatch.RepairGateError):
            journal_dispatch.assert_repair_gate(
                self.ledger,
                self.job_id,
                "enforce-supervised-agent-contracts",
                transition="commit",
            )

    def test_a_same_session_verdict_blocks_the_commit_transition(self) -> None:
        from lib.orchestrator import journal_dispatch

        action_id = self._fixer_action(session_id="ses_same")
        self._record_fixer_report(action_id, session_id="ses_same")
        with self.assertRaises(journal_dispatch.RepairGateError):
            journal_dispatch.assert_repair_gate(
                self.ledger,
                self.job_id,
                "enforce-supervised-agent-contracts",
                transition="commit",
                fixer_report={"repair": "repaired", "session_id": "ses_same"},
                verifier_verdict={
                    "verdict": "pass",
                    "repair_verified": True,
                    "diff_reviewed": True,
                    "session_id": "ses_same",
                },
            )

    def test_the_reset_endpoint_consumes_the_gate(self) -> None:
        """The operator reset endpoint is a repair-consuming transition."""
        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        with self.assertRaises(contracts_mod.RepairConsumptionError):
            endpoints_mod._operator_reset_change(
                {
                    "ledger": self.ledger,
                    "job_id": self.job_id,
                    "change_ids": ["enforce-supervised-agent-contracts"],
                },
                self.credentials,
            )

    def test_the_worker_release_endpoint_consumes_the_gate(self) -> None:
        """The worker delegated release is a repair-consuming transition."""
        action_id = self._fixer_action()
        self._record_fixer_report(action_id)
        with self.assertRaises(contracts_mod.RepairConsumptionError):
            endpoints_mod._worker_release_delegated_gate(
                self.worker_request(
                    action_id,
                    change_id="enforce-supervised-agent-contracts",
                    requested_permissions=["read", "edit", "bash"],
                ),
                self.credentials,
            )

    def test_an_independent_verifier_unblocks_reset_and_release(self) -> None:
        """A same-session verdict still blocks; a distinct verifier unblocks."""
        action_id = self._fixer_action(session_id="ses_same")
        self._record_fixer_report(action_id, session_id="ses_same")
        with self.assertRaises(contracts_mod.RepairConsumptionError):
            endpoints_mod._operator_reset_change(
                {
                    "ledger": self.ledger,
                    "job_id": self.job_id,
                    "change_ids": ["enforce-supervised-agent-contracts"],
                    "fixer_report": {"repair": "repaired", "session_id": "ses_same"},
                    "verifier_verdict": {
                        "verdict": "pass",
                        "repair_verified": True,
                        "diff_reviewed": True,
                        "session_id": "ses_same",
                    },
                },
                self.credentials,
            )
        # The same recorded repair consumed by a distinct verifier passes.
        contracts_mod.assert_repair_consumable(
            self.ledger,
            self.job_id,
            transition="reset",
            change_id="enforce-supervised-agent-contracts",
            verifier_verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": True,
                "session_id": "ses_verify",
            },
        )


_SUPERVISED_ENV = {
    "OPSX_SUPERVISOR_JOB_ID": "1",
    "OPSX_SUPERVISOR_ROLE": "verifier",
    "OPSX_SUPERVISOR_AGENT": "opsx-verifier",
    "OPSX_SUPERVISOR_SERVICE_PRINCIPAL": "opsx-service",
}


class _WorkerExecMainMixin:
    """Run the wrapper's main with a fake runner and a fake report dispatch."""

    def _run_main(
        self, argv: list[str], env: dict | None = None, preflight_fn=None
    ) -> tuple[int, list[dict], list[dict]]:
        runner_calls: list[dict] = []
        reported: list[dict] = []

        def runner(command, **kwargs):
            runner_calls.append({"argv": list(command), **kwargs})
            return 0

        def dispatch(verb, **kwargs):
            reported.append({"verb": verb, **kwargs})
            return {"incident_id": 1}

        code = worker_exec_mod.main(
            argv,
            env=dict(_SUPERVISED_ENV) if env is None else env,
            dispatch_fn=dispatch,
            runner=runner,
            preflight_fn=preflight_fn,
        )
        return code, runner_calls, reported

    def _assert_refused_and_reported(
        self, argv: list[str], expected_kind: str, expected_token: str
    ) -> None:
        """Every refusal is durable: no runner, non-zero exit, violation report."""
        decision = worker_exec_mod.classify_command(argv)
        self.assertFalse(decision["allowed"], decision)
        self.assertEqual(decision["kind"], expected_kind, decision)
        code, runner_calls, reported = self._run_main(argv)
        self.assertEqual(code, 3, "a refusal must fail closed")
        self.assertEqual(runner_calls, [], "the command must not execute")
        self.assertEqual(reported[0]["verb"], "report_violation")
        self.assertEqual(reported[0]["payload"]["bypass_kind"], expected_kind)
        self.assertIn(expected_token, reported[0]["payload"]["command"])


class WorkerExecSafeSurfaceTests(_WorkerExecMainMixin, unittest.TestCase):
    """The allowlisted surface is explicit, and allowed commands really run.

    Property 3 of the executable contract: the checks the supervised roles
    need still run through the wrapper and return their real exit status.
    """

    def test_the_safe_surface_is_explicit(self) -> None:
        for command in (
            # The named project check tool.
            "openspec validate enforce-supervised-agent-contracts --strict",
            "openspec list --all",
            # Read-only git (config-mediated execution neutralized by the
            # wrapper; see GitFormConstraintTests).
            "git status",
            "git status --porcelain",
            "git diff --stat",
            "git log --oneline -5",
            "git show HEAD",
            "git rev-parse HEAD",
            "git ls-files",
            "git grep -n pattern",
            # Single-purpose inspection tools.
            "ls -la lib",
            "cat core/plan-supervision.md",
            "grep -n classify lib/supervisor/worker_exec.py",
            "find . -name '*.py'",
            "rg pattern lib",
            "sort -u names.txt",
            "head -20 f",
            "tail -5 f",
            "wc -l f",
            "diff a b",
            "stat f",
        ):
            with self.subTest(command=command):
                decision = worker_exec_mod.classify_command(command)
                self.assertTrue(decision["allowed"], decision)
                self.assertIsNone(decision["kind"])

    def test_an_ordinary_dotfile_argument_is_not_indirection(self) -> None:
        """`find .` is a path argument, not a shell source builtin."""
        decision = worker_exec_mod.classify_command("find . -name x")
        self.assertTrue(decision["allowed"], decision)

    def test_an_allowed_command_returns_its_real_exit_status(self) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(args=command, returncode=7)

        code = worker_exec_mod.main(
            ["openspec", "validate", "x", "--strict"],
            env=dict(_SUPERVISED_ENV),
            dispatch_fn=lambda verb, **kwargs: {"incident_id": 1},
            runner=runner,
        )
        self.assertEqual(code, 7)

    def test_an_allowed_command_runs_with_a_scrubbed_and_pinned_env(self) -> None:
        env = {
            **_SUPERVISED_ENV,
            "PATH": os.environ.get("PATH", ""),
            "GIT_PAGER": "opencode run",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.st",
            "GIT_CONFIG_VALUE_0": "!opencode run",
            "NODE_OPTIONS": "--require /tmp/worker-controlled.js",
            "LD_PRELOAD": "/tmp/worker-controlled.so",
            "RIPGREP_CONFIG_PATH": "/tmp/worker-controlled-rg.conf",
        }
        _, runner_calls, _ = self._run_main(["grep", "-n", "x", "f"], env=env)
        child_env = runner_calls[0]["env"]
        # Wrapper-pinned inert values replace the inherited GIT_* variables.
        self.assertEqual(child_env["GIT_PAGER"], "cat")
        self.assertEqual(child_env["PAGER"], "cat")
        self.assertEqual(child_env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(child_env["GIT_CONFIG_GLOBAL"], "/dev/null")
        # Interpreter/loader hooks and tool config indirection are stripped.
        for stripped in (
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "NODE_OPTIONS",
            "LD_PRELOAD",
            "RIPGREP_CONFIG_PATH",
        ):
            self.assertNotIn(stripped, child_env)
        # Ordinary environment is preserved.
        self.assertEqual(child_env["PATH"], env["PATH"])

    def test_an_allowed_git_command_passes_the_real_filter_preflight(self) -> None:
        """Integration: this repo defines no external filter, so git runs."""
        code, runner_calls, _ = self._run_main(["git", "status", "--porcelain"])
        self.assertEqual(code, 0)
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(runner_calls[0]["argv"][0], "git")


class ShellBypassFailClosedTests(_WorkerExecMainMixin, unittest.TestCase):
    """Nothing outside the allowlist executes, and every refusal is durable.

    The classifier is fail-closed by construction: prevention is defined by
    the allowlist, so the whole bypass *class* — interpreters, launchers,
    execution-prefix wrappers, nested shells, model clients, and programs
    with configuration-, hook-, alias-, or embedded-language-mediated
    execution — shares one refusal path. Each vector below is an adversarial
    regression: it names one way a worker could try to reach a model client
    or agent runner, and proves the runner is never invoked, the exit is
    non-zero, and the attempt lands as a ``policy_violation`` report.
    """

    def test_inline_interpreter_code_is_refused(self) -> None:
        """`python3 -c` / `node -e` run code the wrapper cannot inspect."""
        for argv, token in (
            (["python3", "-c", "import subprocess; subprocess.run(['opencode'])"], "python3"),
            (["node", "-e", "require('child_process').execSync('opencode')"], "node"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "not_allowlisted", token)

    def test_a_runner_module_or_script_path_is_refused(self) -> None:
        """No module name or repository path is trusted executable content."""
        for argv, token in (
            (["python3", "-m", "doctest", "tests/agent_runner.py"], "doctest"),
            (["python3", "-m", "unittest", "tests"], "unittest"),
            (["python3", "-m", "pytest", "tests/supervisor"], "pytest"),
            (["node", "scripts/agent_runner.js"], "node"),
            (["node", "tests/opencode/test-opsx-usage-emitter.js"], "node"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "not_allowlisted", token)

    def test_an_execution_prefix_wrapper_is_refused(self) -> None:
        """`nice python3 -c ...`: a prefix may never relaunch execution."""
        for argv, token in (
            (["nice", "python3", "-c", "import os"], "nice"),
            (["timeout", "1", "node", "scripts/agent_runner.js"], "timeout"),
            (["nohup", "opencode", "run"], "nohup"),
            (["setsid", "bash", "-c", "opencode run"], "setsid"),
            (["env", "-i", "python3", "-c", "import os"], "env"),
            (["sudo", "python3", "-c", "import os"], "sudo"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "not_allowlisted", token)

    def test_a_config_mediated_git_exec_is_refused(self) -> None:
        """`git -c alias.run='!opencode run' run`: config is not a safe argv."""
        self._assert_refused_and_reported(
            ["git", "-c", "alias.run=!opencode run", "run"],
            "unsafe_form",
            "alias.run",
        )

    def test_a_git_alias_or_hook_form_is_refused(self) -> None:
        """Only built-in read-only subcommands run; aliases and hooks never do."""
        for argv, token in (
            # `run` is not a builtin: as an alias it would shell out.
            (["git", "run"], "run"),
            # `commit` runs repository hooks, which are arbitrary commands.
            (["git", "commit", "-m", "x"], "commit"),
            (["git", "push"], "push"),
            (["git", "difftool"], "difftool"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "unsafe_form", token)

    def test_a_programmable_tool_is_refused(self) -> None:
        """make/awk/sed/tar/ssh/xargs each carry an embedded execution feature."""
        for argv, token in (
            (["make", "test"], "make"),
            (["awk", "BEGIN{system(\"opencode run\")}"], "awk"),
            (["sed", "-n", "1e opencode run", "f"], "sed"),
            (["tar", "--to-command=opencode", "-xf", "a.tar"], "tar"),
            (["ssh", "host", "opencode"], "ssh"),
            (["xargs", "opencode"], "xargs"),
            (["bash", "-c", "opencode run"], "bash"),
            (["sh", "-c", "true"], "sh"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "not_allowlisted", token)

    def test_an_exec_flag_on_a_constrained_tool_is_refused(self) -> None:
        """`find -exec` / `sort --compress-program` / `rg --pre` re-enter exec."""
        for argv, token in (
            (["find", ".", "-exec", "opencode", "{}", "+"], "-exec"),
            (["find", ".", "-execdir", "opencode", "{}", "+"], "-execdir"),
            (["sort", "--compress-program=opencode", "big.txt"], "--compress-program"),
            (["rg", "--pre", "opencode", "pattern"], "--pre"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "unsafe_form", token)

    def test_a_model_client_or_agent_runner_is_refused(self) -> None:
        for argv, token in (
            (["opencode", "run"], "opencode"),
            (["codex", "exec"], "codex"),
            (["claude", "-p", "hi"], "claude"),
            (["aider", "--yes"], "aider"),
        ):
            with self.subTest(argv=argv):
                self._assert_refused_and_reported(argv, "not_allowlisted", token)

    def test_shell_indirection_is_refused(self) -> None:
        """`$(...)`, backticks, here-docs, and `${...}` are refused raw forms."""
        for command in (
            "$(opencode run)",
            "echo `opencode run`",
            "cat <<EOF",
            "cat ${HOME}/x",
        ):
            with self.subTest(command=command):
                decision = worker_exec_mod.classify_command(command)
                self.assertFalse(decision["allowed"], decision)
                self.assertEqual(decision["kind"], "shell_indirection")

    def test_an_empty_command_is_refused(self) -> None:
        decision = worker_exec_mod.classify_command([])
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["kind"], "empty_command")

    def test_a_refusal_reports_the_worker_identity(self) -> None:
        _, _, reported = self._run_main(["opencode", "run"])
        self.assertEqual(reported[0]["role"], "verifier")
        self.assertEqual(reported[0]["observed_agent"], "opsx-verifier")
        self.assertIn("opencode", reported[0]["payload"]["command"])


class GitFormConstraintTests(_WorkerExecMainMixin, unittest.TestCase):
    """git runs only as built-in read-only subcommands, config neutralized."""

    def test_read_only_subcommands_are_allowlisted(self) -> None:
        for command in (
            "git status",
            "git diff",
            "git diff --stat",
            "git log --oneline",
            "git show HEAD",
            "git rev-parse HEAD",
            "git rev-list HEAD",
            "git ls-files",
            "git grep pattern",
            "git shortlog -s",
            "git describe --always",
            "git show-ref",
            "git cat-file -p HEAD",
            "git --no-pager log",
            "git -P status",
        ):
            with self.subTest(command=command):
                decision = worker_exec_mod.classify_command(command)
                self.assertTrue(decision["allowed"], decision)

    def test_worker_supplied_global_config_options_are_refused(self) -> None:
        for command in (
            "git -c alias.run=!opencode run",
            "git -c core.pager=opencode log",
            "git --config-env=core.pager=EVIL log",
            "git --exec-path=/tmp status",
            "git --git-dir=/tmp/fake status",
            "git --work-tree=/tmp status",
            "git -C /tmp status",
            "git --bare status",
            "git --version",
            "git",
        ):
            with self.subTest(command=command):
                decision = worker_exec_mod.classify_command(command)
                self.assertFalse(decision["allowed"], decision)
                self.assertEqual(decision["kind"], "unsafe_form", decision)

    def test_non_allowlisted_subcommands_are_refused(self) -> None:
        for command in (
            "git run",  # an alias name, never a builtin
            "git st",  # likewise
            "git commit -m x",
            "git push",
            "git fetch",
            "git clone url",
            "git checkout main",
            "git submodule update",
            "git config --list",
            "git difftool",
            "git mergetool",
            "git help",
            "git remote -v",
        ):
            with self.subTest(command=command):
                decision = worker_exec_mod.classify_command(command)
                self.assertFalse(decision["allowed"], decision)
                self.assertEqual(decision["kind"], "unsafe_form", decision)

    def test_flags_reenabling_exec_surfaces_are_refused(self) -> None:
        for command in (
            "git cat-file --filters blob abc1234",
            "git cat-file --textconv blob abc1234",
            "git cat-file --path=f blob abc1234",
            "git diff --ext-diff",
            "git diff --textconv",
            "git log --ext-diff",
            "git show --textconv",
        ):
            with self.subTest(command=command):
                decision = worker_exec_mod.classify_command(command)
                self.assertFalse(decision["allowed"], decision)
                self.assertEqual(decision["kind"], "unsafe_form", decision)

    def test_a_flag_shaped_pathspec_after_the_separator_is_allowed(self) -> None:
        decision = worker_exec_mod.classify_command("git log --oneline -- --textconv")
        self.assertTrue(decision["allowed"], decision)

    def test_allowed_git_exec_argv_pins_config(self) -> None:
        decision = worker_exec_mod.classify_command("git diff --stat")
        self.assertTrue(decision["allowed"], decision)
        exec_argv = decision["exec_argv"]
        self.assertEqual(exec_argv[0], "git")
        rendered = " ".join(exec_argv)
        for pin in (
            "core.pager=cat",
            "core.fsmonitor=",
            "diff.external=",
            "gpg.program=/bin/true",
            "gpg.ssh.program=/bin/true",
            "credential.helper=",
            "filter.lfs.clean=",
        ):
            self.assertIn(pin, rendered)
        # The diff-driver neutralizers are injected right after the subcommand.
        diff_index = exec_argv.index("diff")
        self.assertEqual(exec_argv[diff_index + 1], "--no-ext-diff")
        self.assertEqual(exec_argv[diff_index + 2], "--no-textconv")
        self.assertEqual(exec_argv[-1], "--stat")

    def test_allowed_git_commands_carry_the_filter_preflight(self) -> None:
        decision = worker_exec_mod.classify_command("git status")
        self.assertEqual(decision.get("preflight"), "git_external_filters")


class GitConfigPreflightTests(_WorkerExecMainMixin, unittest.TestCase):
    """A worktree-controlled external filter fails closed before execution."""

    @staticmethod
    def _proc(returncode: int, stdout: str = ""):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )

    def test_an_external_filter_command_in_repo_config_is_refused(self) -> None:
        """`filter.pwn.clean` is worktree-controlled command execution."""
        preflight_fn = lambda *a, **kw: self._proc(  # noqa: E731
            0, "filter.pwn.clean opencode run\n"
        )
        code, runner_calls, reported = self._run_main(
            ["git", "status"], preflight_fn=preflight_fn
        )
        self.assertEqual(code, 3)
        self.assertEqual(runner_calls, [], "git status must not execute")
        self.assertEqual(reported[0]["verb"], "report_violation")
        self.assertEqual(
            reported[0]["payload"]["bypass_kind"], "config_controlled_exec"
        )

    def test_the_pinned_lfs_filter_is_tolerated(self) -> None:
        """LFS is pinned inert at exec time, so its config is not a refusal."""
        preflight_fn = lambda *a, **kw: self._proc(  # noqa: E731
            0,
            "filter.lfs.clean git-lfs clean -- %f\n"
            "filter.lfs.smudge git-lfs smudge -- %f\n"
            "filter.lfs.process git-lfs filter-process\n",
        )
        code, runner_calls, _ = self._run_main(
            ["git", "status"], preflight_fn=preflight_fn
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(runner_calls), 1)

    def test_an_empty_filter_value_is_inert(self) -> None:
        preflight_fn = lambda *a, **kw: self._proc(0, "filter.pwn.clean \n")  # noqa: E731
        code, runner_calls, _ = self._run_main(
            ["git", "diff"], preflight_fn=preflight_fn
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(runner_calls), 1)

    def test_no_filter_configuration_passes(self) -> None:
        preflight_fn = lambda *a, **kw: self._proc(1, "")  # noqa: E731
        code, runner_calls, _ = self._run_main(
            ["git", "log", "--oneline"], preflight_fn=preflight_fn
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(runner_calls), 1)

    def test_a_failed_preflight_fails_closed(self) -> None:
        for preflight_fn in (
            lambda *a, **kw: self._proc(2, ""),
            lambda *a, **kw: self._proc(128, ""),
        ):
            with self.subTest(preflight_fn=preflight_fn):
                code, runner_calls, reported = self._run_main(
                    ["git", "status"], preflight_fn=preflight_fn
                )
                self.assertEqual(code, 3)
                self.assertEqual(runner_calls, [])
                self.assertEqual(
                    reported[0]["payload"]["bypass_kind"], "config_controlled_exec"
                )

    def test_a_preflight_exception_fails_closed(self) -> None:
        def preflight_fn(*a, **kw):
            raise OSError("no git binary")

        code, runner_calls, reported = self._run_main(
            ["git", "status"], preflight_fn=preflight_fn
        )
        self.assertEqual(code, 3)
        self.assertEqual(runner_calls, [])
        self.assertEqual(
            reported[0]["payload"]["bypass_kind"], "config_controlled_exec"
        )

    def test_the_preflight_never_runs_for_a_refused_command(self) -> None:
        calls: list[int] = []

        def preflight_fn(*a, **kw):
            calls.append(1)
            return self._proc(1, "")

        code, _, _ = self._run_main(["git", "commit"], preflight_fn=preflight_fn)
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])


class InstalledWorkerExecSurfaceTests(unittest.TestCase):
    """The worker shell wrapper is a real installed artifact."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.env = {**os.environ, **_model_env(), "HOME": self.home.name}
        subprocess.run(
            ["bash", str(OPENCODE_INSTALLER), "--global"],
            cwd=REPO_ROOT,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worker_exec_shim_is_installed_executable_and_refuses_a_bypass(self) -> None:
        shim = Path(self.home.name) / ".local" / "bin" / "opsx-worker-exec"
        self.assertTrue(shim.is_file(), "worker exec shim must be installed")
        self.assertTrue(os.access(str(shim), os.X_OK))
        proc = subprocess.run(
            [str(shim), "opencode", "run"],
            env={**self.env, "OPSX_SUPERVISOR_JOB_ID": "1"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("ShellBypassError", proc.stdout + proc.stderr)

    def test_each_supervised_worker_shell_allows_only_the_tracked_wrapper(self) -> None:
        agents = Path(self.home.name) / ".config" / "opencode" / "agents"
        for name in ("opsx-fixer", "opsx-verifier", "opsx-acceptance-reviewer"):
            with self.subTest(agent=name):
                text = (agents / f"{name}.md").read_text(encoding="utf-8")
                self.assertIn('"*": deny', text)
                self.assertIn('"opsx-worker-exec *": allow', text)
                self.assertNotIn("\n  bash: allow\n", text)


class VerifierIndependenceTests(unittest.TestCase):
    def test_fixer_report_is_not_consumable_without_a_verdict(self) -> None:
        result = contracts_mod.repair_consumable(
            fixer_report={"repair": "repaired", "session_id": "ses_fix"},
            verifier_verdict=None,
        )
        self.assertFalse(result["consumable"])
        self.assertEqual(result["fixer_claim"], "repaired")
        self.assertIn("self-certifies", result["reason"])

    def test_verdict_from_the_fixer_session_is_not_independent(self) -> None:
        result = contracts_mod.repair_consumable(
            fixer_report={"repair": "repaired", "session_id": "ses_same"},
            verifier_verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": True,
                "session_id": "ses_same",
            },
        )
        self.assertFalse(result["consumable"])
        self.assertIn("not independently verified", result["reason"])

    def test_a_contradicting_verdict_blocks_the_repair(self) -> None:
        result = contracts_mod.repair_consumable(
            fixer_report={"repair": "repaired", "session_id": "ses_fix"},
            verifier_verdict={
                "verdict": "fail",
                "repair_verified": False,
                "diff_reviewed": True,
                "session_id": "ses_verify",
            },
        )
        self.assertFalse(result["consumable"])
        self.assertIn("does not pass", result["reason"])

    def test_an_independent_pass_reviewing_the_diff_is_consumable(self) -> None:
        result = contracts_mod.repair_consumable(
            fixer_report={"repair": "repaired", "session_id": "ses_fix"},
            verifier_verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": True,
                "session_id": "ses_verify",
            },
        )
        self.assertTrue(result["consumable"], result)

    def test_a_pass_without_diff_review_is_not_consumable(self) -> None:
        result = contracts_mod.repair_consumable(
            fixer_report={"repair": "repaired", "session_id": "ses_fix"},
            verifier_verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": False,
                "session_id": "ses_verify",
            },
        )
        self.assertFalse(result["consumable"])
        self.assertIn("actual diff", result["reason"])


class TaskCompletenessUnaffectedTests(unittest.TestCase):
    """Repair verdicts feed but never replace the task-completeness gates."""

    def test_repair_verdict_never_checks_a_task_or_waives_a_gate(self) -> None:
        from lib.orchestrator import state as state_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        change_dir = repo / "openspec" / "changes" / "a-change"
        change_dir.mkdir(parents=True)
        (change_dir / "tasks.md").write_text(
            "# Tasks\n\n- [ ] 1.1 Do a thing\n- [ ] 1.2 Do another\n",
            encoding="utf-8",
        )
        before = state_mod.remaining_automatable_tasks(repo, "a-change")
        self.assertEqual(before, ["1.1 Do a thing", "1.2 Do another"])

        # A consumable independent repair verdict is not consulted by the
        # completeness reader at all: the unchecked tasks stay blocking.
        result = contracts_mod.repair_consumable(
            fixer_report={"repair": "repaired", "session_id": "ses_fix"},
            verifier_verdict={
                "verdict": "pass",
                "repair_verified": True,
                "diff_reviewed": True,
                "session_id": "ses_verify",
            },
        )
        self.assertTrue(result["consumable"])
        after = state_mod.remaining_automatable_tasks(repo, "a-change")
        self.assertEqual(after, before)
        self.assertTrue((change_dir / "tasks.md").read_text(encoding="utf-8").count("- [ ]") == 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
