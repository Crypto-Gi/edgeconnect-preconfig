"""The run context (cli-requests R1): a run reads its .env, validators, work folder and output folder from its own context,
so two runs in one process (the GUI) do not see each other's; with no context every value is what it was before."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import translate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
import validate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from common import RunContext, running  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
from test_golden import write_capture  # noqa: E402  guard-ok: the shared synthetic capture folder


def dotenv(folder, nick):
    path = Path(folder) / (nick + ".env")
    path.write_text("orch_1={0}\n{0}_base_url=https://{0}.example.com\n{0}_api_key=k\nEC_VALIDATORS={0}\n".format(nick), encoding="utf-8")
    return str(path)


class TwoRunsAtOnce(unittest.TestCase):
    def test_each_thread_sees_its_own_env_validators_and_folders(self):
        seen, errors = {}, []
        together = threading.Barrier(2, timeout=10)

        def run(nick, tmp):
            try:
                with running(RunContext(dotenv=dotenv(tmp, nick), out_dir=Path(tmp) / nick, work_dir=Path(tmp) / ("work-" + nick))):
                    together.wait()  # both contexts are live before either reads
                    other = "lab_b" if nick == "lab_a" else "lab_a"
                    with self.assertRaises(cli.Fail):
                        cli.check_validator(other)
                    with self.assertRaises(validate.Stop):
                        validate.validator(other)
                    cli.check_validator(nick)
                    together.wait()
                    seen[nick] = (common.validators(), common.client_for(nick).config.base_url, common.output_root(), common.work_dir())
            except Exception as error:  # reported by the main thread
                errors.append((nick, error))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"EC_VALIDATORS": ""}):
            del os.environ["EC_VALIDATORS"]
            threads = [threading.Thread(target=run, args=(nick, tmp)) for nick in ("lab_a", "lab_b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            for nick in ("lab_a", "lab_b"):
                validators, url, out, work = seen[nick]
                self.assertIn(nick, validators)
                self.assertNotIn("lab_b" if nick == "lab_a" else "lab_a", validators)
                self.assertTrue(url.startswith("https://{}.example.com".format(nick)), url)
                self.assertEqual(out, Path(tmp) / nick / "preconfig-output")
                self.assertEqual(work, Path(tmp) / ("work-" + nick))
        self.assertIsNone(common.run_context())  # the main thread never had one


class NoContext(unittest.TestCase):
    def test_without_a_context_the_values_are_the_modules(self):
        self.assertIsNone(common.run_context())
        self.assertIs(common.validators(), common.VALIDATORS)
        self.assertEqual(common.work_dir(), common.WORK_DIR)
        self.assertEqual(common.output_root(), Path.cwd() / "preconfig-output")
        self.assertIsNone(common.run_dotenv())

    def test_a_context_without_a_dotenv_keeps_the_module_validators(self):
        with mock.patch.object(common, "VALIDATORS", {"lab"}), running(RunContext(out_dir=Path("/x"))):
            self.assertEqual(common.validators(), {"lab"})
            self.assertEqual(common.work_dir(), common.WORK_DIR)

    def test_explicit_validators_win_over_the_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp, running(RunContext(dotenv=dotenv(tmp, "lab_a"), validators={"other"})):
            self.assertEqual(common.validators(), {"other"})


class BuildReadsTheContext(unittest.TestCase):
    def test_build_keeps_the_read_in_the_context_work_folder_and_writes_to_its_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = write_capture(Path(tmp) / "cap")
            client = mock.Mock(write_requests=0, request=lambda *a, read_only=False, **k: b"")
            with mock.patch("capture.capture", return_value=cap) as read, mock.patch.object(validate, "client_for", return_value=client), \
                    running(RunContext(validators={"lab"}, work_dir=Path(tmp) / "work", out_dir=Path(tmp) / "run")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["build", "--orchestrator", "lab", "--appliance", "h1"]), 0)
            self.assertEqual(read.call_args[0][4], Path(tmp) / "work" / "reference" / "lab")
            self.assertTrue((Path(tmp) / "run" / "preconfig-output" / "h1" / "build.json").is_file())


class IgnoreOwnershipIsPerRun(unittest.TestCase):
    def test_the_flag_holds_for_that_translate_run_only(self):
        seen = []
        real = translate.build

        def build(cap, ledger, target_ecos=None):
            seen.append(bool(common.run_context() and common.run_context().ignore_ownership))
            return real(cap, ledger, target_ecos)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(translate, "build", side_effect=build), contextlib.redirect_stdout(io.StringIO()):
            cap = write_capture(Path(tmp) / "cap")
            base = ["--capture", str(cap), "--site-type", "t", "--target-release", "9.7.1.42046"]
            self.assertEqual(translate.main(base + ["--out", str(Path(tmp) / "a"), "--ignore-template-ownership"]), 0)
            self.assertEqual(translate.main(base + ["--out", str(Path(tmp) / "b")]), 0)
            owned = [[e for e in json.loads((Path(tmp) / out / "ledger.json").read_text())["entries"] if e["disposition"] == "template-group"] for out in "ab"]
        self.assertEqual((len(owned[0]), len(owned[1])), (0, 1))  # the template group's nac section: mapped with the flag, left to the group without
        self.assertEqual(seen, [True, False])
        self.assertIsNone(common.run_context())


if __name__ == "__main__":
    unittest.main()
