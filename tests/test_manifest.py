"""Validate plugin.json and the generated schema doc are present and in sync."""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tools import TOOL_NAMES, TOOLS  # noqa: E402


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((REPO / "plugin.json").read_text())

    def test_required_string_fields(self):
        for key in ("name", "version", "description"):
            self.assertIsInstance(self.manifest[key], str)
            self.assertTrue(self.manifest[key])

    def test_matches_the_merged_service_runtime_contract(self):
        # The merged engine's validateServiceSpec (the-assistant
        # packages/server/src/plugins/loader.ts) recognizes only these keys and
        # hard-rejects the manifest unless `command` is a non-empty string.
        self.assertEqual(self.manifest["runtime"], "service")
        service = self.manifest["service"]
        self.assertLessEqual(
            set(service), {"command", "timeoutMs", "toolNamespace", "tools"}
        )
        self.assertIsInstance(service["command"], str)
        self.assertEqual(service["command"], "python3 src/server.py")
        self.assertEqual(service["toolNamespace"], "computer_use")
        # Long holds/waits get an explicit per-tool timeout above the default.
        for tool in ("hold_key", "wait"):
            self.assertGreater(service["tools"][tool]["timeoutMs"], 0)
        # Override keys must name real tools (catches a typo'd override key).
        self.assertLessEqual(set(service["tools"]), set(TOOL_NAMES))

    def test_service_tier_manifest_carries_no_static_tools(self):
        # Service plugins discover their tools live from the child's tools/list
        # (the engine normalizes the manifest `tools` to []). The surface-drift
        # guard is tests/test_stdio_roundtrip.py (tools/list == TOOL_NAMES).
        self.assertEqual(self.manifest["tools"], [])

    def test_schema_doc_exists_and_lists_every_tool(self):
        # AC4: the (previously absent) schema doc now exists and is authoritative.
        doc = (REPO / "docs" / "computer-use-mcp-tools-schema.md").read_text()
        for tool in TOOLS:
            self.assertIn(f"`{tool['name']}`", doc)


if __name__ == "__main__":
    unittest.main()
