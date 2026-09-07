"""Every executable spine file stays byte-identical between `company/` and
`company/init_templates/agents/company/` (the AGENTS.md shipping rule)."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "company"
TEMPLATE = SOURCE / "init_templates" / "agents" / "company"

# Template-only or source-only trees that never ship as the spine.
SKIP_PARTS = {"init_templates", "tests", "__pycache__"}


def _spine_files(root: Path) -> set[str]:
    files = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_PARTS for part in rel.parts):
            continue
        if path.suffix == ".pyc":
            continue
        files.add(rel.as_posix())
    return files


class SpineIdentityTests(unittest.TestCase):
    def test_every_template_spine_file_exists_in_source_identically(self):
        template_files = _spine_files(TEMPLATE)
        self.assertTrue(template_files, "template spine appears empty")
        for rel in sorted(template_files):
            source_file = SOURCE / rel
            self.assertTrue(source_file.is_file(),
                            f"missing from company/: {rel}")
            self.assertEqual(
                (SOURCE / rel).read_bytes(),
                (TEMPLATE / rel).read_bytes(),
                f"spine file diverged: {rel}")

    def test_critical_spine_modules_ship_in_both_trees(self):
        for rel in (
            "layout.py", "__main__.py", "cli.py",
            "runtime/bootstrap.py", "runtime/config.py",
            "runtime/paths.py", "runtime/engine.py",
            "commands/goal_runtime.py", "state/database.py",
        ):
            self.assertTrue((SOURCE / rel).is_file(), rel)
            self.assertTrue((TEMPLATE / rel).is_file(), rel)

    def test_source_only_files_are_deliberate(self):
        """company/ may carry tests and templates; nothing else is extra."""
        source_files = _spine_files(SOURCE)
        template_files = _spine_files(TEMPLATE)
        extra = {rel for rel in source_files - template_files
                 if rel not in {
                     "tests/__init__.py",
                     "tests/test_clean_core_acceptance.py",
                     "tests/test_layout_contract.py",
                     "tests/test_update_preservation.py",
                     "tests/test_context_projection.py",
                     "tests/test_host_adapter_contract.py",
                     "tests/test_harness_behavior.py",
                     "tests/test_observability_dashboard.py",
                     "tests/test_decide_boundary.py",
                     "tests/test_spine_identity.py",
                 }}
        # Department fixtures power the behavioral suite; test data only,
        # never shipped spine (a fresh home still ships zero departments).
        extra = {rel for rel in extra
                 if not rel.startswith("tests/fixtures/")}
        self.assertEqual(set(), extra,
                         "files in company/ outside the spine need a listed"
                         " exception here")


if __name__ == "__main__":
    unittest.main()
