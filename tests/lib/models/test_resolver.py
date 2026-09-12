"""Unit tests for lib.models.resolver and supporting types."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest import mock

from lib.models import resolver
from lib.models.resolver import (
    ModelConfigError,
    config_paths,
    resolve,
    resolve_allowlist,
    validate,
)
from lib.models.types import ROLES, ALL_ROLES, OPTIONAL_ROLES, ResolvedModel


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.user_config = self.root / "home" / ".config" / "opsx-controller" / "models.toml"
        self._patch = mock.patch.object(resolver, "USER_CONFIG_PATH", self.user_config)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def repo_config_path(self) -> Path:
        return self.repo / ".opsx-plan" / "models.toml"


class AllowlistResolverTests(TempDirCase):
    def test_allowlist_resolves_from_repo_local_file(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = ["cheap/model-a", "cheap/model-b"]
            """,
        )
        result = resolve_allowlist(self.repo)
        self.assertTrue(result.configured)
        self.assertEqual(result.models, ("cheap/model-a", "cheap/model-b"))
        self.assertIn(str(self.repo_config_path()), result.source)

    def test_repo_local_allowlist_replaces_user_global_wholesale(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = ["repo-only"]
            """,
        )
        _write(
            self.user_config,
            """\
            [allowlist]
            models = ["user-a", "user-b"]
            """,
        )
        result = resolve_allowlist(self.repo)
        self.assertEqual(result.models, ("repo-only",))
        self.assertIn(str(self.repo_config_path()), result.source)

    def test_missing_repo_local_table_falls_through_to_user_global(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [defaults]
            controller = "x/y"
            """,
        )
        _write(
            self.user_config,
            """\
            [allowlist]
            models = ["user-a"]
            """,
        )
        result = resolve_allowlist(self.repo)
        self.assertEqual(result.models, ("user-a",))
        self.assertIn(str(self.user_config), result.source)

    def test_absent_table_yields_empty_unconfigured(self) -> None:
        result = resolve_allowlist(self.repo)
        self.assertFalse(result.configured)
        self.assertEqual(result.models, ())
        self.assertEqual(result.source, "unconfigured")

    def test_explicit_empty_local_table_replaces_and_reports_configured(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = []
            """,
        )
        _write(
            self.user_config,
            """\
            [allowlist]
            models = ["user-a"]
            """,
        )
        result = resolve_allowlist(self.repo)
        self.assertTrue(result.configured)
        self.assertEqual(result.models, ())
        self.assertIn(str(self.repo_config_path()), result.source)

    def test_no_environment_source(self) -> None:
        # An ambient OPSX_* variable must never contribute allowlist entries.
        result = resolve_allowlist(self.repo)
        self.assertEqual(result.models, ())
        self.assertFalse(result.configured)

    def test_malformed_models_not_array_raises_naming_file(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = "not-an-array"
            """,
        )
        with self.assertRaises(ModelConfigError) as ctx:
            resolve_allowlist(self.repo)
        self.assertIn(str(self.repo_config_path()), str(ctx.exception))

    def test_malformed_entry_raises_naming_file(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = ["ok/model", "   "]
            """,
        )
        with self.assertRaises(ModelConfigError) as ctx:
            resolve_allowlist(self.repo)
        self.assertIn(str(self.repo_config_path()), str(ctx.exception))

    def test_non_string_entry_raises(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = ["ok/model", 7]
            """,
        )
        with self.assertRaises(ModelConfigError):
            resolve_allowlist(self.repo)

    def test_allowlist_does_not_change_role_resolution(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [allowlist]
            models = ["cheap/model-a"]

            [adapters.opencode]
            implementer = "deepseek/deepseek-v4-pro"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        self.assertEqual(resolved["implementer"].model, "deepseek/deepseek-v4-pro")


class SupervisedRoleResolutionTests(TempDirCase):
    SUPERVISED = (
        "supervisor",
        "supervised_author",
        "acceptance_reviewer",
        "fixer",
        "verifier",
    )

    def test_supervised_roles_are_optional(self) -> None:
        for role in self.SUPERVISED:
            self.assertIn(role, OPTIONAL_ROLES)
        for role in self.SUPERVISED:
            self.assertNotIn(role, ROLES)

    def test_supervised_role_resolves_like_any_role(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [adapters.opencode]
            acceptance_reviewer = "cheap/acceptance"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["acceptance_reviewer"]
        self.assertEqual(entry.model, "cheap/acceptance")
        self.assertIn(str(self.repo_config_path()), entry.source)

    def test_unset_supervised_roles_are_unresolved_not_errors(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={})
        for role in self.SUPERVISED:
            self.assertIsNone(resolved[role].model)
            self.assertEqual(resolved[role].source, "unresolved")
        for role in ROLES:
            self.assertEqual(resolved[role].source, "unresolved")

    def test_supervised_role_resolves_from_ambient_env(self) -> None:
        resolved = resolve(
            "opencode", repo=self.repo,
            environ={"OPSX_FIXER_MODEL": "cheap/fixer"},
        )
        self.assertEqual(resolved["fixer"].model, "cheap/fixer")
        self.assertEqual(resolved["fixer"].source, "ambient environment")

    def test_custom_required_roles_unchanged_when_supervised_configured(self) -> None:
        _write(
            self.user_config,
            """\
            [defaults]
            controller = "base/controller"
            implementer = "base/implementer"
            reviewer = "base/reviewer"
            archiver = "base/archiver"
            supervised_author = "cheap/author"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        self.assertEqual(resolved["controller"].model, "base/controller")
        self.assertEqual(resolved["supervised_author"].model, "cheap/author")


class PrecedenceLadderTests(TempDirCase):
    def test_repo_local_adapter_table_wins_over_everything(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [adapters.opencode]
            implementer = "repo-local-value"
            """,
        )
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            implementer = "user-global-value"

            [defaults]
            implementer = "user-default-value"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={"OPSX_IMPLEMENTER_MODEL": "ambient-value"})
        entry = resolved["implementer"]
        self.assertEqual(entry.model, "repo-local-value")
        self.assertIn(str(self.repo_config_path()), entry.source)

    def test_user_global_adapter_table_wins_over_defaults_and_ambient(self) -> None:
        _write(
            self.user_config,
            """\
            [adapters.claude-code]
            reviewer = "user-global-adapter-value"

            [defaults]
            reviewer = "user-default-value"
            """,
        )
        resolved = resolve("claude-code", repo=self.repo, environ={"OPSX_REVIEWER_MODEL": "ambient-value"})
        entry = resolved["reviewer"]
        self.assertEqual(entry.model, "user-global-adapter-value")
        self.assertIn(str(self.user_config), entry.source)

    def test_repo_local_defaults_win_over_user_global_defaults(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [defaults]
            archiver = "repo-default-value"
            """,
        )
        _write(
            self.user_config,
            """\
            [defaults]
            archiver = "user-default-value"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={"OPSX_ARCHIVER_MODEL": "ambient-value"})
        entry = resolved["archiver"]
        self.assertEqual(entry.model, "repo-default-value")
        self.assertIn(str(self.repo_config_path()), entry.source)
        self.assertIn("defaults", entry.source)

    def test_user_global_defaults_win_over_ambient(self) -> None:
        _write(
            self.user_config,
            """\
            [defaults]
            controller = "user-default-value"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={"OPSX_CONTROLLER_MODEL": "ambient-value"})
        entry = resolved["controller"]
        self.assertEqual(entry.model, "user-default-value")
        self.assertIn(str(self.user_config), entry.source)

    def test_ambient_environment_used_when_no_file_entry(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={"OPSX_REVIEWER_MODEL": "ambient-value"})
        entry = resolved["reviewer"]
        self.assertEqual(entry.model, "ambient-value")
        self.assertEqual(entry.source, "ambient environment")

    def test_distinct_adapters_resolve_distinct_identifiers(self) -> None:
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            implementer = "deepseek/deepseek-v4-pro"

            [adapters.claude-code]
            implementer = "claude-sonnet-5"
            """,
        )
        opencode_resolved = resolve("opencode", repo=self.repo)
        claude_resolved = resolve("claude-code", repo=self.repo)
        self.assertEqual(opencode_resolved["implementer"].model, "deepseek/deepseek-v4-pro")
        self.assertEqual(claude_resolved["implementer"].model, "claude-sonnet-5")

    def test_adapter_table_overrides_defaults_table(self) -> None:
        _write(
            self.user_config,
            """\
            [defaults]
            reviewer = "default-value"

            [adapters.claude-code]
            reviewer = "adapter-value"
            """,
        )
        resolved = resolve("claude-code", repo=self.repo)
        self.assertEqual(resolved["reviewer"].model, "adapter-value")

    def test_defaults_table_covers_adapter_with_no_override(self) -> None:
        _write(
            self.user_config,
            """\
            [defaults]
            archiver = "default-value"

            [adapters.opencode]
            implementer = "something-else"
            """,
        )
        resolved = resolve("opencode", repo=self.repo)
        self.assertEqual(resolved["archiver"].model, "default-value")

    def test_unresolved_role_reports_unresolved_source(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["archiver"]
        self.assertIsNone(entry.model)
        self.assertEqual(entry.source, "unresolved")

    def test_repo_local_file_overrides_user_global_file_for_adapter_table(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [adapters.opencode]
            controller = "repo-value"
            """,
        )
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            controller = "user-value"
            """,
        )
        resolved = resolve("opencode", repo=self.repo)
        self.assertEqual(resolved["controller"].model, "repo-value")

    def test_all_roles_present_in_result(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={})
        self.assertEqual(set(resolved.keys()), set(ALL_ROLES))
        for role, entry in resolved.items():
            self.assertIsInstance(entry, ResolvedModel)
            self.assertEqual(entry.role, role)

    def test_escalation_resolves_from_repo_local_adapter_table(self) -> None:
        _write(
            self.repo_config_path(),
            """\
            [adapters.opencode]
            implementer_escalation = "repo-local-escalation"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["implementer_escalation"]
        self.assertEqual(entry.model, "repo-local-escalation")
        self.assertIn(str(self.repo_config_path()), entry.source)

    def test_escalation_resolves_from_user_global_adapter_table(self) -> None:
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            implementer_escalation = "user-global-escalation"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["implementer_escalation"]
        self.assertEqual(entry.model, "user-global-escalation")
        self.assertIn(str(self.user_config), entry.source)

    def test_escalation_resolves_from_defaults_table(self) -> None:
        _write(
            self.user_config,
            """\
            [defaults]
            implementer_escalation = "default-escalation"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["implementer_escalation"]
        self.assertEqual(entry.model, "default-escalation")
        self.assertIn(str(self.user_config), entry.source)
        self.assertIn("defaults", entry.source)

    def test_escalation_resolves_from_ambient_environment(self) -> None:
        resolved = resolve("opencode", repo=self.repo,
                           environ={"OPSX_IMPLEMENTER_ESCALATION_MODEL": "ambient-escalation"})
        entry = resolved["implementer_escalation"]
        self.assertEqual(entry.model, "ambient-escalation")
        self.assertEqual(entry.source, "ambient environment")

    def test_escalation_unresolved_everywhere_is_not_an_error(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["implementer_escalation"]
        self.assertIsNone(entry.model)
        self.assertEqual(entry.source, "unresolved")

    def test_variant_resolves_from_adapter_table(self) -> None:
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            reviewer = "moonshotai/kimi-k3"
            reviewer_variant = "max"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["reviewer"]
        self.assertEqual(entry.model, "moonshotai/kimi-k3")
        self.assertEqual(entry.variant, "max")
        self.assertIn(str(self.user_config), entry.variant_source)

    def test_variant_resolves_from_defaults_table(self) -> None:
        _write(
            self.user_config,
            """\
            [defaults]
            implementer_variant = "low"
            """,
        )
        resolved = resolve("opencode", repo=self.repo, environ={})
        self.assertEqual(resolved["implementer"].variant, "low")

    def test_variant_resolves_from_ambient_environment(self) -> None:
        resolved = resolve("opencode", repo=self.repo,
                           environ={"OPSX_REVIEWER_VARIANT": "max"})
        entry = resolved["reviewer"]
        self.assertEqual(entry.variant, "max")
        self.assertEqual(entry.variant_source, "ambient environment")

    def test_variant_unresolved_everywhere_is_none(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={})
        entry = resolved["reviewer"]
        self.assertIsNone(entry.variant)
        self.assertEqual(entry.variant_source, "unresolved")

    def test_variant_adapter_table_wins_over_ambient(self) -> None:
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            reviewer_variant = "max"
            """,
        )
        resolved = resolve("opencode", repo=self.repo,
                           environ={"OPSX_REVIEWER_VARIANT": "low"})
        self.assertEqual(resolved["reviewer"].variant, "max")

    def test_validate_reports_escalation_syntax_violation_for_opencode(self) -> None:
        resolved = {
            "implementer_escalation": ResolvedModel(
                role="implementer_escalation", model="bare-name", source="x"),
        }
        warnings = validate("opencode", resolved)
        self.assertEqual(len(warnings), 1)
        self.assertIn("implementer_escalation", warnings[0])

    def test_validate_reports_escalation_syntax_violation_for_claude_code(self) -> None:
        resolved = {
            "implementer_escalation": ResolvedModel(
                role="implementer_escalation", model="provider/model", source="x"),
        }
        warnings = validate("claude-code", resolved)
        self.assertEqual(len(warnings), 1)
        self.assertIn("implementer_escalation", warnings[0])


class DegradedInputTests(TempDirCase):
    def test_no_configuration_file_present_falls_through_to_ambient(self) -> None:
        resolved = resolve("opencode", repo=self.repo, environ={"OPSX_CONTROLLER_MODEL": "ambient-value"})
        self.assertEqual(resolved["controller"].model, "ambient-value")
        self.assertEqual(resolved["controller"].source, "ambient environment")

    def test_no_repository_context_consults_only_user_global_file(self) -> None:
        _write(
            self.user_config,
            """\
            [adapters.opencode]
            implementer = "user-global-value"
            """,
        )
        resolved = resolve("opencode", repo=None, environ={})
        self.assertEqual(resolved["implementer"].model, "user-global-value")

        paths = config_paths(None)
        self.assertEqual(paths, [self.user_config])

    def test_malformed_toml_raises_naming_the_file(self) -> None:
        bad_path = self.repo_config_path()
        _write(bad_path, "this is not [ valid toml")
        with self.assertRaises(ModelConfigError) as ctx:
            resolve("opencode", repo=self.repo, environ={})
        self.assertIn(str(bad_path), str(ctx.exception))

    def test_malformed_user_global_toml_raises_naming_the_file(self) -> None:
        _write(self.user_config, "not = valid = toml = at = all")
        with self.assertRaises(ModelConfigError) as ctx:
            resolve("opencode", repo=self.repo, environ={})
        self.assertIn(str(self.user_config), str(ctx.exception))


class ValidateTests(TempDirCase):
    def test_provider_prefixed_identifier_rejected_for_claude_code(self) -> None:
        resolved = {
            "implementer": ResolvedModel(role="implementer", model="deepseek/deepseek-v4-pro", source="x"),
        }
        warnings = validate("claude-code", resolved)
        self.assertEqual(len(warnings), 1)
        self.assertIn("implementer", warnings[0])

    def test_bare_identifier_rejected_for_opencode(self) -> None:
        resolved = {
            "reviewer": ResolvedModel(role="reviewer", model="gpt-5.4", source="x"),
        }
        warnings = validate("opencode", resolved)
        self.assertEqual(len(warnings), 1)
        self.assertIn("reviewer", warnings[0])

    def test_multiple_violations_all_reported(self) -> None:
        resolved = {
            "implementer": ResolvedModel(role="implementer", model="deepseek/deepseek-v4-pro", source="x"),
            "reviewer": ResolvedModel(role="reviewer", model="anthropic/claude", source="x"),
            "archiver": ResolvedModel(role="archiver", model="claude-sonnet-5", source="x"),
        }
        warnings = validate("claude-code", resolved)
        self.assertEqual(len(warnings), 2)

    def test_unresolved_role_is_skipped(self) -> None:
        resolved = {
            "controller": ResolvedModel(role="controller", model=None, source="unresolved"),
        }
        warnings = validate("claude-code", resolved)
        self.assertEqual(warnings, [])

    def test_valid_identifiers_produce_no_warnings(self) -> None:
        resolved = {
            "implementer": ResolvedModel(role="implementer", model="claude-sonnet-5", source="x"),
        }
        self.assertEqual(validate("claude-code", resolved), [])
        resolved = {
            "implementer": ResolvedModel(role="implementer", model="deepseek/deepseek-v4-pro", source="x"),
        }
        self.assertEqual(validate("opencode", resolved), [])

    def test_codex_cli_has_no_validation_rule(self) -> None:
        resolved = {
            "implementer": ResolvedModel(role="implementer", model="deepseek/deepseek-v4-pro", source="x"),
        }
        self.assertEqual(validate("codex-cli", resolved), [])


if __name__ == "__main__":
    unittest.main()
