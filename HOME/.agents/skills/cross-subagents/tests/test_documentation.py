"""Keep progressive-disclosure entry points and local references usable."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent


class TestDocumentation(unittest.TestCase):
    def test_local_markdown_links_resolve(self):
        for document in [ROOT / "SKILL.md", *(ROOT / "references").rglob("*.md")]:
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                filename = target.split("#", 1)[0]
                if filename:
                    self.assertTrue((document.parent / filename).exists(), (document, target))

    def test_skill_keeps_safety_decisions_in_entry_point(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for decision in (
            "Never hardcode", "--require-vision", "--quota-state",
            "--no-fallback", "not enforced", "not a sandbox",
            "does not restore", "Never poll", "significance",
            "--include-defects",
        ):
            with self.subTest(decision=decision):
                self.assertIn(decision, text.replace("**", ""))
