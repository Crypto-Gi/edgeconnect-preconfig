"""install.sh (this repository only) and install-all.sh: offline checks with a stub install-all.sh.
The same file is in all three repositories."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = {"edgeconnect-orchestrator-automation": "1", "edgeconnect-preconfig": "2", "edgeconnect-mcp": "3"}
STUB = '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$OUT"\n'
FAKE_CURL = '#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) echo "$1" >> "$URLS"; shift ;; esac; done\ncp "$STUB_FILE" "$out"\n'


def var(name):
    return re.search(r"^{}=(\S+)$".format(name), (ROOT / "install.sh").read_text(), re.M).group(1)


@unittest.skipUnless(shutil.which("bash"), "needs bash")  # guard-ok: every CI runner has bash
class Installers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, OUT=str(self.tmp / "args"), URLS=str(self.tmp / "urls"), STUB_FILE=str(self.tmp / "stub.sh"))
        (self.tmp / "stub.sh").write_text(STUB)

    def clone(self, *names):
        folder = self.tmp / "clone"
        folder.mkdir()
        for name in names:
            shutil.copy(ROOT / name, folder / name)
        (folder / "install-all.sh").write_text(STUB)
        return folder

    def args(self):
        return (self.tmp / "args").read_text().splitlines()

    def downloaded(self, script, *argv):
        """Run script as `bash <(cat script)`, with a fake curl that serves the stub; (result, urls asked for)."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "curl").write_text(FAKE_CURL)
        (bin_dir / "curl").chmod(0o755)
        env = dict(self.env, PATH="{}:{}".format(bin_dir, os.environ["PATH"]))
        done = subprocess.run(["bash", "-c", 'bash <(cat "$0") "$@"', str(ROOT / script)] + list(argv), cwd=str(self.tmp), env=env, capture_output=True, text=True)
        return done, (self.tmp / "urls").read_text().split() if (self.tmp / "urls").exists() else []

    def test_install_sh_names_its_own_repository_number(self):
        self.assertEqual(NUMBERS[var("REPO")], var("NUMBER"))

    def test_install_sh_from_a_clone_adds_repos_and_passes_the_rest_through(self):
        folder = self.clone("install.sh")
        done = subprocess.run(["bash", str(folder / "install.sh"), "--yes", "--agents", "claude-code", "--dir", "a b"], env=self.env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.args(), ["--repos", var("NUMBER"), "--yes", "--agents", "claude-code", "--dir", "a b"])

    def test_install_sh_refuses_repos(self):
        folder = self.clone("install.sh")
        for argv in (["--repos", "1,3"], ["--yes", "--repos=all"]):
            done = subprocess.run(["bash", str(folder / "install.sh")] + argv, env=self.env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, argv)
            self.assertIn("install-all.sh", done.stderr)
            self.assertFalse((self.tmp / "args").exists(), argv)

    def test_install_sh_downloaded_fetches_install_all_from_its_repository(self):
        done, urls = self.downloaded("install.sh", "--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(urls, ["https://raw.githubusercontent.com/mirwasay/{}/main/install-all.sh".format(var("REPO"))])
        self.assertEqual(self.args(), ["--repos", var("NUMBER"), "--yes"])

    def test_setup_sh_is_gone(self):
        self.assertFalse((ROOT / "setup.sh").exists())  # removed; install.sh and install-all.sh replace it

    def test_install_all_sh_still_marks_the_commands_it_wrote_under_its_old_name(self):
        self.assertIn('MARK="# edgeconnect setup.sh"', (ROOT / "install-all.sh").read_text())

    def test_install_all_sh_still_updates_an_agents_md_written_under_its_old_name(self):
        text = (ROOT / "install-all.sh").read_text()  # its Installed here list ends with the old line naming setup.sh
        self.assertIn("If the user needs it, tell them to run setup.sh again and pick it.'", text)
        self.assertIn('"$LIST_END" "$LIST_END_OLD" > "$LIST_LINES"', text)


if __name__ == "__main__":
    unittest.main()
