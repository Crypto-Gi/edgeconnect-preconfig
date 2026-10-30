"""The GUI's upload approval (docs/gui/BUILD_PLAN.md step 2): typed nickname, single use, bound to one batch fingerprint and one
Orchestrator, expires after 10 minutes; redeemed by upload.run_batch's approver just before the first upload."""
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "gui"))
import capture  # noqa: E402  guard-ok: modules are imported after the sys.path insert, as in the other test modules
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import upload  # noqa: E402  guard-ok: imported after the sys.path insert
import validate  # noqa: E402  guard-ok: imported after the sys.path insert
from approval import ApprovalError, GuiApprovals, upload_gates  # noqa: E402  guard-ok: imported after the sys.path insert
from test_golden import Orchestrator, write_capture  # noqa: E402  guard-ok: the shared synthetic capture and fake Orchestrator


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class GuiApprovalTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.approvals = GuiApprovals(clock=self.clock)
        self.gates = upload_gates("lab")

    def issue(self, fingerprint="f" * 64, orchestrator="lab"):
        return self.approvals.issue(self.gates, ["lab"], fingerprint, orchestrator)

    def test_approval_is_used_once(self):
        token = self.issue()
        self.approvals.redeem(token, "f" * 64, "lab")
        with self.assertRaisesRegex(ApprovalError, "already used"):
            self.approvals.redeem(token, "f" * 64, "lab")

    def test_approval_expires_after_ten_minutes(self):
        token = self.issue()
        self.clock.now += 599
        self.approvals.redeem(token, "f" * 64, "lab")
        token = self.issue()
        self.clock.now += 600
        with self.assertRaisesRegex(ApprovalError, "expired"):
            self.approvals.redeem(token, "f" * 64, "lab")

    def test_approval_is_refused_for_another_batch_or_orchestrator_and_is_then_spent(self):
        token = self.issue()
        with self.assertRaisesRegex(ApprovalError, "batch changed"):
            self.approvals.redeem(token, "0" * 64, "lab")
        with self.assertRaisesRegex(ApprovalError, "already used"):
            self.approvals.redeem(token, "f" * 64, "lab")
        token = self.issue()
        with self.assertRaisesRegex(ApprovalError, "different Orchestrator"):
            self.approvals.redeem(token, "f" * 64, "other")

    def test_unknown_token_is_refused(self):
        with self.assertRaisesRegex(ApprovalError, "unknown"):
            self.approvals.redeem("made-up", "f" * 64, "lab")

    def test_only_the_exact_nickname_issues_an_approval(self):
        for typed in (["Lab"], ["lab "], [""], []):
            with self.assertRaisesRegex(ApprovalError, "nickname typed is not lab|every confirmation"):
                self.approvals.issue(self.gates, typed, "f" * 64, "lab")


class Recorded:
    """The GUI's side of a run: a context with its own .env, folders and recorder, its printed text captured."""

    def __init__(self, tmp, env):
        self.tmp, self.env = tmp, env

    def run(self, argv, approver=None):
        context = common.RunContext(dotenv=str(self.env), out_dir=self.tmp, work_dir=self.tmp / "work", recorder=common.Recorder(),
                                    approver=approver, source="gui")
        out, err = io.StringIO(), io.StringIO()
        with common.running(context), common.captured_output(out, err):
            code = cli.main(argv)
        fingerprints = [r["value"] for r in context.recorder.results if r["kind"] == "fingerprint"]
        return code, err.getvalue(), fingerprints


class ApprovedUpload(unittest.TestCase):
    """End to end through cli.main: check the batch, approve its fingerprint, upload; a changed batch is refused."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = self.tmp / ".env"
        env.write_text("orch_1=lab\nlab_base_url=https://orch.example.com\nlab_api_key=k\nEC_VALIDATORS=lab\n", encoding="utf-8")
        self.gui = Recorded(self.tmp, env)
        self.orch = Orchestrator()
        posts = []
        original = self.orch.request

        def request(method, path, query=None, json_body=None, expected_status=(200,), expect_json=True, read_only=False):
            if method == "POST" and path == "/gms/appliance/preconfiguration":
                posts.append(json_body["name"])
            return original(method, path, query, json_body, expected_status, expect_json, read_only)
        self.orch.request, self.posts = request, posts
        environ = {k: v for k, v in os.environ.items() if k.upper() != "EC_VALIDATORS"}  # the process environment wins over .env
        for patch in (mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(sys, "stdin", io.StringIO("")),
                      mock.patch.object(capture, "capture", side_effect=lambda *a, **k: write_capture(common.work_dir() / "cap")),
                      mock.patch.object(cli, "client_for", return_value=self.orch), mock.patch.object(validate, "client_for", return_value=self.orch),
                      mock.patch.object(upload, "client_for", return_value=self.orch)):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(self.gui.run(["build", "--orchestrator", "lab", "--appliance", "h1"])[0], 0)
        self.build = self.tmp / "preconfig-output" / "h1"
        self.assertEqual(self.gui.run(["variables", str(self.build), "--pick", "top10", "--yes", "--name", "t"])[0], 0)
        with (self.build / "site-t" / "sites.csv").open(newline="", encoding="utf-8") as handle:
            example = next(csv.DictReader(handle))
        self.fields = list(example)
        self.sheet = self.tmp / "sheet.csv"
        self.rows = [dict(example, preconfig_name=name, hostname=name) for name in ("site-a", "site-b")]
        self.write(self.rows)
        self.upload = ["upload", str(self.build), "--csv", str(self.sheet), "--orchestrator", "lab"]
        self.approvals = GuiApprovals()

    def write(self, rows):
        with self.sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fields)
            writer.writeheader()
            writer.writerows(rows)

    def approve(self):
        """The GUI's check run, then the person types the nickname: a token for exactly that batch."""
        code, _, fingerprints = self.gui.run(self.upload)
        self.assertEqual((code, len(fingerprints), self.posts), (0, 1, []))
        return self.approvals.issue(upload_gates("lab"), ["lab"], fingerprints[0], "lab")

    def test_the_approved_batch_is_uploaded_once(self):
        token = self.approve()
        code, err, _ = self.gui.run(self.upload + ["--approve"], self.approvals.approver(token, "lab"))
        self.assertEqual((code, self.posts), (0, ["site-a", "site-b"]), err)
        del self.orch.existing[-2:]  # the same batch again, as if the uploads were removed: its token is spent
        code, err, _ = self.gui.run(self.upload + ["--approve"], self.approvals.approver(token, "lab"))
        self.assertEqual((code, self.posts), (2, ["site-a", "site-b"]))
        self.assertIn("NOT APPROVED, nothing uploaded: approval is unknown or was already used", err)

    def test_a_batch_changed_after_its_approval_sends_nothing(self):
        token = self.approve()
        self.write([dict(self.rows[0], hostname="site-a2"), self.rows[1]])
        code, err, _ = self.gui.run(self.upload + ["--approve"], self.approvals.approver(token, "lab"))
        self.assertEqual((code, self.posts), (2, []))
        self.assertIn("NOT APPROVED, nothing uploaded: the batch changed since it was approved", err)

    def test_an_approval_for_another_orchestrator_sends_nothing(self):
        token = self.approve()
        code, err, _ = self.gui.run(self.upload + ["--approve"], self.approvals.approver(token, "other"))
        self.assertEqual((code, self.posts), (2, []))
        self.assertIn("approval is for a different Orchestrator", err)


if __name__ == "__main__":
    unittest.main()
