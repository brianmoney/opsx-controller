#!/usr/bin/env python3
"""Extract test classes from test_opsx_plan.py into per-module test files."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("/home/brian/opsx-controller")
TEST_FILE = ROOT / "tests/orchestrator/test_opsx_plan.py"

# Mapping: class name -> target module (or "ENTRYPOINT" to keep)
CLASS_MODULE_MAP = {
    "VerifyChangeCreatedTests": "groundtruth",
    "ArchiveCommitEvidenceGateTests": "groundtruth",
    "DirectStageTelemetryTests": "telemetry",
    "DirectStageUsageExtractionTests": "telemetry",
    "ClaudeResultEnvelopeUsagePrecedenceTests": "telemetry",
    "DirectStageUsageIntegrationTests": "telemetry",
    "CompileTests": "compiler",
    "CompileOptionalOutputTests": "compiler",
    "SamplePlanTests": "compiler",
    "LogsCommandTests": "logs",
    "GitDeliveryBranchNameResolutionTests": "delivery",
    "GitDeliveryBaseRefResolutionTests": "delivery",
    "GitDeliveryEnsureBranchTests": "delivery",
    "GitDeliveryCmdRunIntegrationTests": "delivery",
    "GitDeliveryDefaultOffTests": "delivery",
    "PRDeliveryTests": "delivery",
    "DoctorPreflightTests": "doctor",
    "DirectWorkerAgentDoctorCheckTests": "doctor",
    "GitDeliveryStatePersistenceTests": "state",
    "EscalationStateMigrationTests": "state",
}

# Module name -> import alias (e.g., "compiler" -> "compiler_mod")
MODULE_NAMES = {
    "groundtruth": "groundtruth_mod",
    "state": "state_mod",
    "telemetry": "telemetry_mod",
    "delivery": "delivery_mod",
    "doctor": "doctor_mod",
    "compiler": "compiler_mod",
    "logs": "logs_mod",
}

# Module name -> import statement
MODULE_IMPORTS = {
    "groundtruth": "from lib.orchestrator import groundtruth as groundtruth_mod",
    "state": "from lib.orchestrator import state as state_mod",
    "telemetry": "from lib.orchestrator import telemetry as telemetry_mod",
    "delivery": "from lib.orchestrator import delivery as delivery_mod",
    "doctor": "from lib.orchestrator import doctor as doctor_mod",
    "compiler": "from lib.orchestrator import compiler as compiler_mod",
    "logs": "from lib.orchestrator import logs as logs_mod",
    "base": "from lib.orchestrator import base as base_mod",
}


def parse_classes(content: str) -> list[tuple[str, int, int, str]]:
    """Parse class names and their line ranges from content."""
    classes = []
    lines = content.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^class (\w+)\(', line)
        if m:
            name = m.group(1)
            if name in CLASS_MODULE_MAP:
                start = i  # 0-indexed
                # Find the end: next class at same indent level
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    if re.match(r'^class \w+\(', lines[j]):
                        end = j
                        break
                classes.append((name, start, end, CLASS_MODULE_MAP[name]))
        i += 1
    return classes


def get_needed_imports(class_code: str, target_module: str) -> list[str]:
    """Determine which module imports are needed for a class."""
    imports = set()
    # Always need base if base.XXX is referenced
    if "base_mod." in class_code or "self.opsx_plan.base." in class_code:
        imports.add("base")
    # Check for each module name usage
    for mod_name in MODULE_NAMES:
        if mod_name == target_module:
            imports.add(mod_name)
        mod_alias = MODULE_NAMES[mod_name]
        if f"{mod_alias}." in class_code:
            imports.add(mod_name)
        # Check for opsx_plan patterns that will be replaced
        if f"self.opsx_plan.{mod_name}." in class_code:
            imports.add(mod_name)
    if "state_mod." in class_code:
        imports.add("state")
    if "base_mod." in class_code:
        imports.add("base")
    return sorted(imports)


def transform_class_code(code: str, target_module: str) -> str:
    """Transform class code to use direct module imports instead of opsx_plan."""
    # Replace self.opsx_plan.<module>.<func> with <module_alias>.<func>
    for mod_name, mod_alias in MODULE_NAMES.items():
        code = code.replace(
            f"self.opsx_plan.{mod_name}.",
            f"{mod_alias}."
        )
    # Replace self.opsx_plan.base.<func> with base_mod.<func>
    code = code.replace("self.opsx_plan.base.", "base_mod.")

    # Update @patch targets
    code = re.sub(
        r"@mock\.patch\.object\(\s*self\.opsx_plan,\s*['\"]run_compile_client['\"]",
        "@mock.patch('lib.orchestrator.compiler.run_compile_client'",
        code,
    )
    code = re.sub(
        r"@mock\.patch\.object\(\s*self\.opsx_plan,\s*['\"]git['\"]",
        "@mock.patch('lib.orchestrator.groundtruth.git')",
        code,
    )
    code = re.sub(
        r"@mock\.patch\.object\(\s*self\.opsx_plan,\s*['\"]run_fast_checks['\"]",
        "@mock.patch('lib.orchestrator.groundtruth.run_fast_checks')",
        code,
    )

    return code


def extract_module(source: str) -> str:
    """Extract the `load_opsx_plan`-like function and `git` function from the source."""
    # Get the git function, _TOM_BLOCK, and the _load_entrypoint function
    # Read them from source
    lines = source.split('\n')
    result = []
    in_git = False
    in_load = False
    for line in lines:
        if line.startswith("def git("):
            in_git = True
        if in_git:
            result.append(line)
            if line.strip() == "" and in_git:
                in_git = False
                continue
            if line.strip() and not line.startswith(" ") and not line.startswith("def ") and not line.startswith("    "):
                if in_git and result:
                    in_git = False
    return '\n'.join(result)


def main():
    content = TEST_FILE.read_text()
    classes = parse_classes(content)
    
    # Group by module
    module_classes: dict[str, list[tuple[str, int, int, str]]] = {}
    for name, start, end, mod in classes:
        module_classes.setdefault(mod, []).append((name, start, end, mod))
    
    for target_module, cls_list in module_classes.items():
        print(f"Processing module: {target_module} ({len(cls_list)} classes)")
        
        # Build the test file content
        lines = content.split('\n')
        
        test_file_content = []
        test_file_content.append("from __future__ import annotations")
        test_file_content.append("")
        test_file_content.append("import argparse")
        test_file_content.append("import importlib.util")
        test_file_content.append("import io")
        test_file_content.append("import json")
        test_file_content.append("import os")
        test_file_content.append("import re")
        test_file_content.append("import shlex")
        test_file_content.append("import subprocess")
        test_file_content.append("import tempfile")
        test_file_content.append("import textwrap")
        test_file_content.append("import unittest")
        test_file_content.append("import uuid")
        test_file_content.append("from pathlib import Path")
        test_file_content.append("from unittest import mock")
        test_file_content.append("")
        
        # Determine needed additional imports from class bodies
        all_code = ""
        for name, start, end, mod in cls_list:
            class_lines = lines[start:end]
            all_code += '\n'.join(class_lines)
        
        needed = set()
        for mod_name in MODULE_NAMES:
            mod_alias = MODULE_NAMES[mod_name]
            if f"self.opsx_plan.{mod_name}." in all_code or f"{mod_alias}." in all_code:
                needed.add(mod_name)
        if "self.opsx_plan.base." in all_code or "base_mod." in all_code:
            needed.add("base")
        
        for mod_name in sorted(needed):
            if mod_name in MODULE_IMPORTS:
                test_file_content.append(MODULE_IMPORTS[mod_name])
        
        test_file_content.append("")
        
        # Check if model resolver is needed
        if "resolver" in all_code or "ResolvedModel" in all_code:
            test_file_content.append("from lib.models import resolver")
            test_file_content.append("from lib.models.types import ResolvedModel")
            test_file_content.append("")
        
        # Add __init__.py for imports at module level
        test_file_content.append("_SCRIPT = Path(__file__).resolve().parents[2] / \"orchestrator\" / \"opsx-plan.py\"")
        test_file_content.append("")
        test_file_content.append("_TOM_BLOCK = re.compile(r\"```toml\\s*\\n(.*?)```\", re.DOTALL)")
        test_file_content.append("")
        test_file_content.append("_MODEL_HOME: tempfile.TemporaryDirectory | None = None")
        test_file_content.append("_MODEL_CONFIG_PATCH = None")
        test_file_content.append("_MODEL_ENV_PATCH = None")
        test_file_content.append("")
        test_file_content.append("")
        test_file_content.append("def setUpModule() -> None:")
        test_file_content.append("    global _MODEL_HOME, _MODEL_CONFIG_PATCH, _MODEL_ENV_PATCH")
        test_file_content.append("    _MODEL_HOME = tempfile.TemporaryDirectory()")
        test_file_content.append("    _MODEL_CONFIG_PATCH = mock.patch.object(")
        test_file_content.append("        resolver, \"USER_CONFIG_PATH\", Path(_MODEL_HOME.name) / \"models.toml\"")
        test_file_content.append("    )")
        test_file_content.append("    _MODEL_CONFIG_PATCH.start()")
        test_file_content.append("    _MODEL_ENV_PATCH = mock.patch.dict(")
        test_file_content.append("        os.environ,")
        test_file_content.append("        {")
        test_file_content.append("            \"OPSX_CONTROLLER_MODEL\": \"test-provider/test-controller\",")
        test_file_content.append("            \"OPSX_IMPLEMENTER_MODEL\": \"test-provider/test-implementer\",")
        test_file_content.append("            \"OPSX_REVIEWER_MODEL\": \"test-provider/test-reviewer\",")
        test_file_content.append("            \"OPSX_ARCHIVER_MODEL\": \"test-provider/test-archiver\",")
        test_file_content.append("        },")
        test_file_content.append("    )")
        test_file_content.append("    _MODEL_ENV_PATCH.start()")
        test_file_content.append("")
        test_file_content.append("")
        test_file_content.append("def tearDownModule() -> None:")
        test_file_content.append("    assert _MODEL_ENV_PATCH is not None")
        test_file_content.append("    assert _MODEL_CONFIG_PATCH is not None")
        test_file_content.append("    assert _MODEL_HOME is not None")
        test_file_content.append("    _MODEL_ENV_PATCH.stop()")
        test_file_content.append("    _MODEL_CONFIG_PATCH.stop()")
        test_file_content.append("    _MODEL_HOME.cleanup()")
        test_file_content.append("")
        test_file_content.append("")
        test_file_content.append("def git(repo: Path, *args: str) -> None:")
        test_file_content.append("    subprocess.run([\"git\", *args], cwd=repo, check=True, capture_output=True, text=True)")
        test_file_content.append("")
        test_file_content.append("")
        test_file_content.append("def load_opsx_plan():")
        test_file_content.append("    spec = importlib.util.spec_from_file_location(\"opsx_plan\", _SCRIPT)")
        test_file_content.append("    assert spec is not None")
        test_file_content.append("    assert spec.loader is not None")
        test_file_content.append("    module = importlib.util.module_from_spec(spec)")
        test_file_content.append("    spec.loader.exec_module(module)")
        test_file_content.append("    return module")
        test_file_content.append("")
        test_file_content.append("")
        
        # Add each class
        for name, start, end, mod in cls_list:
            class_lines = lines[start:end]
            class_code = '\n'.join(class_lines)
            transformed = transform_class_code(class_code, target_module)
            test_file_content.append(transformed)
            test_file_content.append("")
        
        # Write the file
        out_path = ROOT / f"tests/orchestrator/test_{target_module}.py"
        out_content = '\n'.join(test_file_content)
        out_path.write_text(out_content)
        print(f"  Wrote {out_path} ({len(out_content)} chars)")


if __name__ == "__main__":
    main()
