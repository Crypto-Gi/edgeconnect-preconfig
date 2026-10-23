import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "toolkit" / "scripts" / "common.py"


class SkillCopy(unittest.TestCase):
    def test_the_skill_ships_an_exact_copy_of_the_toolkit_and_dist_matches(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import build_skill  # noqa: E402  guard-ok: tools/ is put on the path just above
        self.assertEqual(build_skill.check(), [], "run: python3 tools/build_skill.py")


class WorkFolder(unittest.TestCase):
    def test_the_install_folders_preconfig_work_is_used_from_the_toolkit_and_from_the_skill_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp).resolve() / "install"
            (install / "preconfig-work").mkdir(parents=True)
            elsewhere = Path(tmp).resolve() / "elsewhere"
            elsewhere.mkdir()
            env = {k: v for k, v in os.environ.items() if k != "PRECONFIG_WORK"}
            for scripts in (install / "edgeconnect-preconfig" / "toolkit" / "scripts",
                            install / "edgeconnect-preconfig" / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"):
                scripts.mkdir(parents=True)
                shutil.copy(COMMON, scripts)
                out = subprocess.run([sys.executable, "-c", "import common; print(common.WORK_DIR)"], cwd=str(elsewhere), env=dict(env, PYTHONPATH=str(scripts)),
                                     capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(out, str(install / "preconfig-work"), scripts)


if __name__ == "__main__":
    unittest.main()
