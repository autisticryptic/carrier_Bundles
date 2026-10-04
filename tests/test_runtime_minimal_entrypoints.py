"""Keep documented and Actions release entrypoints on explicit runtime slimming.

These checks only read text; the database/consumer invariants live in
 test_runtime_minimal.py. Run the full suite on Actions, not locally.
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeMinimalEntrypointTests(unittest.TestCase):
    def assert_runtime_pruning(self, command):
        self.assertIn("--runtime-minimal", command)
        self.assertIn("--simulation-report simulation_pruning/verified-evidence.json", command)

    def test_both_actions_variant_steps_enable_runtime_minimal(self):
        for filename in ("build-catalog-set.yml", "build-pixel-catalog.yml"):
            with self.subTest(workflow=filename):
                workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
                steps = re.split(r"(?m)^      - ", workflow)
                variant_steps = [step for step in steps if "python3 tools/build_variants.py" in step]
                self.assertEqual(len(variant_steps), 1)
                self.assert_runtime_pruning(variant_steps[0])

    def test_documented_four_source_command_enables_runtime_minimal(self):
        document = (ROOT / "docs/CATALOG_VARIANTS.md").read_text(encoding="utf-8")
        section = document.split("构建四来源、三版本：", 1)[1]
        command = section.split("```bash\n", 1)[1].split("```", 1)[0]
        self.assert_runtime_pruning(command)
        self.assertEqual(command.count(".sqlite3"), 4)


if __name__ == "__main__":
    unittest.main()
