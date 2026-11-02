"""setup.sh's re-run update step (update_clone): a clean clone ends on main, pulled from origin/main; no edit or commit is lost."""
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

    def branch(self):
        return git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.root / "repo")

    def test_a_clone_on_gui_is_switched_to_main_and_updated(self):
        repo = self.root / "repo"
        git("checkout", "-q", "gui", cwd=repo)
        new = self.advance("main")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), new)
        self.assertIn("switched gui to main", out)
        self.assertIn("gui", git("branch", "--list", "gui", cwd=repo))  # the old branch is kept

    def test_a_clone_on_main_is_updated_on_main(self):
        new = self.advance("main")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.root / "repo"), new)

    def test_a_clone_without_a_local_main_gets_one_from_origin(self):
        repo = self.root / "repo"
        git("checkout", "-q", "gui", cwd=repo)
        git("branch", "-q", "-D", "main", cwd=repo)
        new = self.advance("main")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), new)

    def test_a_detached_clone_already_on_main_is_switched_to_main(self):
        repo = self.root / "repo"
        git("checkout", "-q", "--detach", "HEAD", cwd=repo)
        new = self.advance("main")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), new)

    def test_a_detached_clone_with_its_own_commits_is_left_alone(self):
        repo = self.root / "repo"
        git("checkout", "-q", "--detach", "HEAD", cwd=repo)
        (repo / "mine").write_text("mine")
        git("add", "mine", cwd=repo)
        git("commit", "-q", "-m", "mine", cwd=repo)
        mine = git("rev-parse", "HEAD", cwd=repo)
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("not on a branch (detached at", out)
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), mine)
        self.assertEqual(self.branch(), "HEAD")

    def test_local_changes_are_left_alone(self):
        repo = self.root / "repo"
        git("checkout", "-q", "gui", cwd=repo)
        (repo / "gui").write_text("mine")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("has local changes, not updated", out)
        self.assertEqual((repo / "gui").read_text(), "mine")
        self.assertEqual(self.branch(), "gui")

    def test_a_local_branch_is_kept_when_the_clone_is_switched_to_main(self):
        repo = self.root / "repo"
        git("checkout", "-q", "-b", "mine", cwd=repo)
        (repo / "mine").write_text("mine")
        git("add", "mine", cwd=repo)
        git("commit", "-q", "-m", "mine", cwd=repo)
        mine = git("rev-parse", "HEAD", cwd=repo)
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(git("rev-parse", "mine", cwd=repo), mine)

    def test_a_diverged_main_stops_without_resetting(self):
        repo = self.root / "repo"
        (repo / "main").write_text("local")
        git("commit", "-q", "-am", "local", cwd=repo)
        local = git("rev-parse", "HEAD", cwd=repo)
        self.advance("main")
        code, out = self.update()
        self.assertEqual(code, 1, out)
        self.assertIn("could not update main", out)
        self.assertEqual(git("rev-parse", "HEAD", cwd=repo), local)


if __name__ == "__main__":
    unittest.main()
