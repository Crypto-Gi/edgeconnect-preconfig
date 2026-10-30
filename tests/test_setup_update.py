"""setup.sh's re-run update step (update_clone): a clean clone keeps its branch, so a gui clone stays on gui."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.sh"
GIT = shutil.which("git")


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main"] + list(args),
                          cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(GIT and shutil.which("bash"), "needs git and bash")  # guard-ok: both are on every CI runner; only a bare machine skips
class UpdateClone(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin, work = self.tmp / "origin.git", self.tmp / "work"
        git("init", "-q", "--bare", str(origin), cwd=self.tmp)
        git("clone", "-q", str(origin), str(work), cwd=self.tmp)
        for branch in ("main", "gui"):
            git("checkout", "-q", "-B", branch, cwd=work)
            (work / branch).write_text(branch)
            git("add", branch, cwd=work)
            git("commit", "-q", "-m", branch, cwd=work)
            git("push", "-q", "origin", branch, cwd=work)
        self.root = self.tmp / "root"
        self.root.mkdir()
        git("clone", "-q", "-b", "main", str(origin), str(self.root / "repo"), cwd=self.tmp)
        self.work = work

    def update(self):
        """Run setup.sh's update_clone on root/repo; (exit code, output)."""
        body = subprocess.run(["sed", "-n", "/^update_clone()/,/^}/p", str(SETUP)], capture_output=True, text=True, check=True).stdout
        self.assertIn("update_clone()", body)
        script = 'set -euo pipefail\n' + 'die() { printf "error: %s\\n" "$*" >&2; exit 1; }\nROOT="$1"\n' + body + '\nupdate_clone repo\n'
        done = subprocess.run(["bash", "-c", script, "bash", str(self.root)], capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
        return done.returncode, done.stdout + done.stderr

    def advance(self, branch):
        git("checkout", "-q", branch, cwd=self.work)
        (self.work / branch).write_text(branch + " 2")
        git("commit", "-q", "-am", branch + " 2", cwd=self.work)
        git("push", "-q", "origin", branch, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def test_a_clone_on_gui_stays_on_gui_and_is_updated(self):
        repo = self.root / "repo"
        git("checkout", "-q", "gui", cwd=repo)
        new = self.advance("gui")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo), "gui")
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), new)
        self.assertIn("updated gui to", out)

    def test_a_clone_on_main_is_updated_on_main(self):
        new = self.advance("main")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.root / "repo"), "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root / "repo"), new)

    def test_a_detached_clone_goes_back_to_main(self):
        repo = self.root / "repo"
        git("checkout", "-q", "--detach", "HEAD", cwd=repo)
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo), "main")

    def test_local_changes_are_left_alone(self):
        repo = self.root / "repo"
        git("checkout", "-q", "gui", cwd=repo)
        (repo / "gui").write_text("mine")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("has local changes, not updated", out)
        self.assertEqual((repo / "gui").read_text(), "mine")

    def test_a_branch_not_on_github_stops_without_switching(self):
        repo = self.root / "repo"
        git("checkout", "-q", "-b", "mine", cwd=repo)
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn("could not update mine", out)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo), "mine")


if __name__ == "__main__":
    unittest.main()
