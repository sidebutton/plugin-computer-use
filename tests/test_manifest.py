"""Validate plugin.json and the generated schema doc are present and in sync."""

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tools import TOOLS  # noqa: E402


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((REPO / "plugin.json").read_text())

    def test_required_string_fields(self):
        for key in ("name", "version", "description"):
            self.assertIsInstance(self.manifest[key], str)
            self.assertTrue(self.manifest[key])

    def test_proposes_the_service_runtime_contract(self):
        self.assertEqual(self.manifest["runtime"], "service")
        service = self.manifest["service"]
        self.assertEqual(service["protocol"], "mcp-stdio")
        self.assertIsInstance(service["command"], list)
        self.assertTrue(service["command"])  # non-empty launch command
        self.assertEqual(service["toolDiscovery"], "tools/list")
        self.assertTrue(service["singleOwner"])

    def test_tools_match_the_source_of_truth(self):
        # plugin.json is generated from tools.py; this guards against drift.
        self.assertEqual(self.manifest["tools"], TOOLS)

    def test_schema_doc_exists_and_lists_every_tool(self):
        # AC4: the (previously absent) schema doc now exists and is authoritative.
        doc = (REPO / "docs" / "computer-use-mcp-tools-schema.md").read_text()
        for tool in TOOLS:
            self.assertIn(f"`{tool['name']}`", doc)


if __name__ == "__main__":
    unittest.main()
