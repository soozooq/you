import importlib
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

BASELINE = Path(__file__).resolve().parents[1] / "baseline"


class BaselineConfigurationTests(unittest.TestCase):
    def config(self):
        self.assertTrue((BASELINE / "you_config.py").is_file(),
                        "shared runtime configuration is required")
        return importlib.import_module("baseline.you_config")

    def test_missing_credential_is_rejected(self):
        config = self.config()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Missing or invalid credential"):
                config.credential("TEST_CREDENTIAL")

    def test_credential_can_be_supplied_by_environment(self):
        config = self.config()
        with patch.dict(os.environ, {"TEST_CREDENTIAL": "fixture-only"}, clear=True):
            self.assertEqual(config.credential("TEST_CREDENTIAL"), "fixture-only")

    def test_sessions_are_explicit_and_empty_by_default(self):
        config = self.config()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.sessions(), {})
        with patch.dict(os.environ, {"YOU_SESSIONS": '["dm-1", "grp-2"]'}, clear=True):
            self.assertEqual(config.sessions(), {"dm-1": ("private", 1), "grp-2": ("group", 2)})

    def test_dsh_runtime_and_dependency_are_locked(self):
        directory = BASELINE / "dsh-runtime"
        self.assertTrue((directory / "package-lock.json").is_file(), "missing runtime lock")
        manifest = json.loads((directory / "package.json").read_text())
        lock = json.loads((directory / "package-lock.json").read_text())
        package = "@deepseek-ai/dsh"
        version = manifest["dependencies"][package]
        self.assertRegex(version, r"^\d+\.\d+\.\d+(?:-[\w.]+)?$")
        self.assertRegex(manifest["engines"]["node"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(lock["packages"][""]["dependencies"][package], version)
        entry = lock["packages"][f"node_modules/{package}"]
        self.assertEqual(entry["version"], version)
        self.assertTrue(entry["integrity"].startswith("sha512-"))

    def test_python_requirements_pin_versions_and_hashes(self):
        lock = BASELINE / "requirements-cp313-linux-x86_64.lock"
        self.assertTrue(lock.is_file(), "missing hash-locked dependencies")
        rows = [line for line in lock.read_text().splitlines() if line and not line.startswith("#")]
        packages = set()
        for row in rows:
            self.assertTrue(
                re.fullmatch(r"[\w-]+==\d+(?:\.\d+)+ --hash=sha256:[0-9a-f]{64}", row),
                "each requirement must include an exact version and wheel hash",
            )
            packages.add(row.split("==", 1)[0].lower())
        self.assertTrue({"pyyaml", "pillow"} <= packages)
