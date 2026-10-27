import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
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
        with mock.patch.object(validate, "VALIDATORS", set()):
            with self.assertRaises(validate.Stop) as stop:
                validate.validator("prod_testbed")
        self.assertIn("EC_VALIDATORS in .env", str(stop.exception))
        with mock.patch.object(cli, "VALIDATORS", set()):
            with self.assertRaises(cli.Fail) as fail:
                cli.check_validator("prod_testbed")
        self.assertIn("(now: none)", str(fail.exception))


if __name__ == "__main__":
    unittest.main()
