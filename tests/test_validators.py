import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
os.environ["EDGECONNECT_REPORT_DIR"] = ""  # a command logs to reports/ (runlog.py); the run-log tests set their own folder
import cli  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules
import validate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules
from common import configured_validators  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, as in the other test modules

PROFILES = "orch_1=prod_testbed\norch_2=lab_prod\norch_3=vlab\norch_4=sandbox\norch_5=lab\n"


def allowed(dotenv):
    with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True):
        path = Path(directory) / ".env"
        path.write_text(dotenv, encoding="utf-8")
        return configured_validators(str(path))


class ValidatorAllowList(unittest.TestCase):
    """docs/decisions/0011: /validate runs only on nicknames listed exactly in EC_VALIDATORS."""

    def test_only_names_listed_in_ec_validators_are_allowed(self):
        self.assertEqual(allowed(PROFILES + "EC_VALIDATORS=lab\n"), {"lab"})

    def test_nothing_is_allowed_without_ec_validators(self):
        self.assertEqual(allowed(PROFILES), set())

    def test_a_refusal_says_how_to_allow_the_nickname(self):
        with mock.patch("common.VALIDATORS", set()):
            with self.assertRaises(validate.Stop) as stop:
                validate.validator("prod_testbed")
        self.assertIn("EC_VALIDATORS in .env", str(stop.exception))
        with mock.patch("common.VALIDATORS", set()):
            with self.assertRaises(cli.Fail) as fail:
                cli.check_validator("prod_testbed")
        self.assertIn("(now: none)", str(fail.exception))

    def test_the_old_orch_validators_alias_is_not_read(self):
        # an unnumbered orch_ key breaks the automation CLI when the two tools share one .env
        self.assertEqual(allowed(PROFILES + "orch_validators=lab\n"), set())

    def test_the_stage_form_matches_the_nickname_in_any_case(self):
        with mock.patch("common.VALIDATORS", {"lab"}), mock.patch.object(validate, "client_for", return_value=mock.Mock(request=lambda *a, read_only=False, **k: None)) as client_for:
            validate.validator("LAB")
        client_for.assert_called_once_with("LAB")  # client_for normalises the case itself


class StageErrors(unittest.TestCase):
    """The stage form (validate --yaml ..., upload --yaml ...) ends in one error line, like the verbs."""

    def run_cli(self, argv):
        with tempfile.TemporaryDirectory() as directory, mock.patch("common.VALIDATORS", {"lab"}):
            path = Path(directory) / "t.yml"
            path.write_text('applianceInfo:\n  hostname: "t"\n', encoding="utf-8")
            with mock.patch("sys.stderr", new_callable=io.StringIO) as err, mock.patch("sys.stdout", new_callable=io.StringIO):
                code = cli.main(argv[:1] + ["--yaml", str(path)] + argv[1:])
        return code, err.getvalue()

    def test_validate_yaml_on_a_non_validator_is_one_line(self):
        code, err = self.run_cli(["validate", "--orchestrator", "other"])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("other is not a validator Orchestrator", err)

    def test_upload_yaml_network_error_is_one_line(self):
        import upload
        from common import TransportError
        broken = mock.Mock()
        broken.get.side_effect = TransportError("transport error on GET /gms/appliance/preconfiguration: refused")
        with mock.patch.object(upload, "client_for", return_value=broken):
            code, err = self.run_cli(["upload", "--orchestrator", "lab"])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("error: transport error", err)


if __name__ == "__main__":
    unittest.main()
