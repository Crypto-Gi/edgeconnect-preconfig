import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert


class TemplateChoice(unittest.TestCase):
    """docs/decisions/0014: exact build, else newest of the same major.minor, else nearest older, else nearest newer."""

    def choose(self, release, have=("9.6.4.40046", "9.7.1.42046", "10.0.0.40516")):
        with tempfile.TemporaryDirectory() as tmp:
            for r in have:
                (Path(tmp) / "orchestrator-{}.yml".format(r)).write_text("applianceInfo: {}\n")
            with mock.patch.object(common, "DEFAULT_TEMPLATES_DIR", Path(tmp)), mock.patch.object(common, "WORK_DIR", Path(tmp) / "none"):
                path, why = common.find_default_template(release)
        return (path.name[len("orchestrator-"):-len(".yml")] if path else None), why

    def test_exact_build(self):
        self.assertEqual(self.choose("9.7.1.42046")[0], "9.7.1.42046")

    def test_same_family_uses_its_newest_template(self):
        got, why = self.choose("9.7.1.42060", have=("9.7.0.40000", "9.7.1.42046", "9.6.4.40046"))
        self.assertEqual(got, "9.7.1.42046")
        self.assertIn("9.7", why)
        self.assertEqual(self.choose("9.7.3.45000")[0], "9.7.1.42046")

    def test_unknown_release_uses_the_nearest_older_and_says_untested(self):
        got, why = self.choose("9.8.0.43000")
        self.assertEqual(got, "9.7.1.42046")
        self.assertIn("untested", why)
        self.assertEqual(self.choose("10.1.0.1")[0], "10.0.0.40516")

    def test_older_than_every_template_uses_the_nearest_newer(self):
        got, why = self.choose("9.5.2.1000")
        self.assertEqual(got, "9.6.4.40046")
        self.assertIn("untested", why)

    def test_no_templates_at_all(self):
        self.assertIsNone(self.choose("9.7.1.42046", have=())[0])


if __name__ == "__main__":
    unittest.main()
