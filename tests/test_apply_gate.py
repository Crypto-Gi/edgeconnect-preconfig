import builtins
import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
import upload  # noqa: E402
from common import GateRefused, human_gate  # noqa: E402
from generate import generate  # noqa: E402


class FakeTTY:
    """A terminal: answers are computed from what the gate printed, so the code is the one this run generated."""

    def __init__(self, *answers):
        self.out, self.answers = "", list(answers)

    def write(self, text):
        self.out += text

    def flush(self):
        pass

    def readline(self):
        answer = self.answers.pop(0)
        code = re.findall(r"type (APPLY-[0-9A-F]{6})", self.out)[-1]
        return (code if answer == "CODE" else answer) + "\n"


class Gates(unittest.TestCase):
    def test_code_then_yes_passes(self):
        tty = FakeTTY("CODE", "yes")
        human_gate("Applying a preconfig", ["Appliance: 1.NE"], tty)
        self.assertIn("Appliance: 1.NE", tty.out)
        self.assertIn("Gate 2 of 2", tty.out)

    def test_every_wrong_answer_stops(self):
        for answers in (("APPLY-000000", "yes"), ("", "yes"), ("CODE", "y"), ("CODE", "YES please"), ("yes", "yes")):
            with self.assertRaises(GateRefused):
                human_gate("Applying", [], FakeTTY(*answers))

    def test_a_fresh_code_each_time(self):
        first, second = FakeTTY("CODE", "yes"), FakeTTY("CODE", "yes")
        human_gate("a", [], first)
        human_gate("a", [], second)
        self.assertNotEqual(re.findall(r"APPLY-\w+", first.out), re.findall(r"APPLY-\w+", second.out))

    def test_no_terminal_stops_and_stdin_is_never_read(self):
        real_open = builtins.open

        def no_tty(path, *args, **kwargs):
            if path == "/dev/tty":
                raise OSError(6, "Device not configured")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", no_tty), mock.patch("sys.stdin") as stdin:
            with self.assertRaises(GateRefused) as stop:
                human_gate("Applying", [])
        self.assertIn("no terminal", str(stop.exception))
        stdin.readline.assert_not_called()


class AutoApplyUpload(unittest.TestCase):
    def test_auto_apply_uploads_nothing_unless_the_gates_pass(self):
        class Client:
            write_requests = 0
            posted = []

            def get(self, path, query=None):
                return []

            def request(self, method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
                if path == "/gms/appliance/preconfiguration" and method == "POST":
                    self.posted.append(json_body)
                    return {"id": 1}
                return b""

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            meta = generate({"applianceInfo": {"hostname": "br-1"}}, out, "br", decide=lambda p, v, s: p == "applianceInfo.hostname")
            with (out / "rows.csv").open("w", newline="") as handle:
                w = csv.DictWriter(handle, fieldnames=["hostname", "preconfig_name", "serial_number"])
                w.writeheader()
                w.writerow({"hostname": "br-2", "preconfig_name": "br-2", "serial_number": ""})
            client = Client()
            with mock.patch.object(upload, "human_gate", side_effect=GateRefused("gate 1: the code did not match")):
                with self.assertRaises(GateRefused):
                    upload.run_batch(out / "br.jinja2", out / "rows.csv", out, client, meta["columns"], approve=True, auto_apply=True)
            self.assertEqual(client.posted, [])
            with mock.patch.object(upload, "human_gate") as gate:  # without autoApply the upload needs no gate
                upload.run_batch(out / "br.jinja2", out / "rows.csv", out, client, meta["columns"], approve=True)
            gate.assert_not_called()
            self.assertEqual([p["autoApply"] for p in client.posted], [False])


if __name__ == "__main__":
    unittest.main()
