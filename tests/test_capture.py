import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "building-edgeconnect-preconfig-templates" / "scripts"))
import capture  # noqa: E402
import translate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, like capture above
from common import redact_secrets  # noqa: E402


class AuthenticationError(Exception):
    status = 401


class ServerError(Exception):
    status = 500


class PermissionDeniedError(Exception):
    status = 403


class FakeClient:
    def __init__(self, state=1, fail=None):
        self.calls, self.write_requests, self.state, self.fail = [], 0, state, fail or {}

    def request(self, method, path, query=None, expected_status=(200,), expect_json=True):
        self.calls.append((method, path, dict(query or {})))
        if path in self.fail:
            raise self.fail[path]
        body = {
            "/gms/versions": {"current": "9.7.1.42046"},
            "/appliance": [{"nePk": "0.NE", "hostName": "lab-a", "model": "EC-XS", "softwareVersion": "9.6.4.0", "state": 1},
                           {"nePk": "1.NE", "hostName": "lab-b"}],
            "/reachability/gms": {"state": self.state},
            "/deployment": {"mode": "router", "src": (query or {}).get("cached"), "pppoePassword": "hunter2"},
            "/bgp/config/system": {"asn": 65000, "src": (query or {}).get("fromGms")},
            "/template/applianceAssociation": {"0.NE": ["G1"]},
            "/ids/state": {"ids": ["0.NE"]},
        }.get(path)
        return json.dumps(body).encode()


class CaptureTest(unittest.TestCase):
    def run_capture(self, client, only):
        capture.client_for = lambda nickname: client
        out = Path(tempfile.mkdtemp())
        return capture.capture("lab", "lab-a", "reference", 0, out, only)

    def test_live_and_cached_are_labelled_and_get_only(self):
        client = FakeClient()
        final = self.run_capture(client, ["deployment", "bgp-system", "template-association"])
        files = sorted(p.name for p in (final / "capture").iterdir())
        self.assertEqual(files, ["bgp-system.cached.json", "bgp-system.live.json", "deployment.cached.json",
                                 "deployment.live.json", "template-association.orchestrator.json"])
        live = json.loads((final / "capture" / "deployment.live.json").read_text())
        self.assertEqual((live["label"], live["data"]["src"]), ("live", "false"))
        cached = json.loads((final / "capture" / "bgp-system.cached.json").read_text())
        self.assertEqual(cached["query"], {"nePk": "0.NE", "fromGms": "true"})
        self.assertEqual({c[0] for c in client.calls}, {"GET"})
        self.assertEqual(json.loads((final / "run.json").read_text())["write_requests"], 0)

    def test_recapture_rereads_orchestrator_scope_unless_reuse_is_asked(self):
        capture.client_for = lambda nickname: client
        out = Path(tempfile.mkdtemp())
        client = FakeClient()
        stamps = iter("20261017T10{:04d}Z".format(i) for i in range(100))
        real_stamp, capture.utc_stamp = capture.utc_stamp, lambda: next(stamps)  # three captures inside one second
        self.addCleanup(setattr, capture, "utc_stamp", real_stamp)
        capture.capture("lab", "lab-a", "sample", 0, out, ["ids-state"])
        client.calls.clear()
        fresh = capture.capture("lab", "lab-a", "sample", 0, out, ["ids-state"])  # e.g. the re-capture after an apply
        self.assertIn(("GET", "/ids/state", {}), client.calls)
        self.assertFalse(json.loads((fresh / "run.json").read_text())["reused_orchestrator_reads"])
        client.calls.clear()
        reused = capture.capture("lab", "lab-a", "sample", 0, out, ["ids-state"], reuse=True)
        self.assertNotIn(("GET", "/ids/state", {}), client.calls)
        self.assertTrue(json.loads((reused / "capture-index.json").read_text())[0].get("reused"))

    def test_secrets_never_written(self):
        final = self.run_capture(FakeClient(), ["deployment"])
        text = "".join(p.read_text() for p in final.rglob("*.json"))
        self.assertNotIn("hunter2", text)
        self.assertIn("<SECRET:$.pppoePassword>", text)

    def test_unreachable_appliance_reads_cached_only(self):
        client = FakeClient(state=2)
        final = self.run_capture(client, ["deployment"])
        index = json.loads((final / "capture-index.json").read_text())
        self.assertEqual([(i["label"], i["status"]) for i in index], [("cached", "ok"), ("live", "unavailable")])
        self.assertNotIn(("GET", "/deployment", {"nePk": "0.NE", "cached": "false"}), client.calls)

    def test_endpoint_error_is_recorded_and_capture_continues(self):
        final = self.run_capture(FakeClient(fail={"/deployment": ServerError("boom")}), ["deployment", "bgp-system"])
        index = json.loads((final / "capture-index.json").read_text())
        self.assertEqual([i["status"] for i in index], ["error", "error", "ok", "ok"])

    def test_authentication_failure_stops(self):
        with self.assertRaises(capture.Stop):
            self.run_capture(FakeClient(fail={"/deployment": AuthenticationError("401")}), ["deployment"])

    def test_a_key_that_may_not_read_the_release_still_captures(self):
        final = self.run_capture(FakeClient(fail={"/gms/versions": PermissionDeniedError("403")}), ["deployment"])
        self.assertIsNone(json.loads((final / "run.json").read_text())["release"])
        with self.assertRaises(capture.Stop):  # any other refused read stops: it would be a silently missing section
            self.run_capture(FakeClient(fail={"/deployment": PermissionDeniedError("403")}), ["deployment"])

    def test_write_counter_above_zero_stops(self):
        client = FakeClient()
        client.write_requests = 1
        with self.assertRaises(capture.Stop):
            self.run_capture(client, ["deployment"])


class RedactTest(unittest.TestCase):
    def test_keys_text_and_types(self):
        value = {"bgp": {"neighbors": [{"md5Password": "x1", "md5Enabled": True, "password": ""}]},
                 "tags": [{"key": "site", "value": "a"}], "community": "65000:100",
                 "cli": "username admin password 0 s3cret\nsnmp-server", "cert": "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
                 "hash": "$6$abcdefgh$ijklmnopqrstuv"}
        out, paths = redact_secrets(value)
        self.assertEqual(out["bgp"]["neighbors"][0], {"md5Password": "<SECRET:$.bgp.neighbors[0].md5Password>", "md5Enabled": True, "password": ""})
        self.assertEqual(out["tags"], value["tags"])
        self.assertEqual(out["community"], "65000:100")
        self.assertNotIn("s3cret", out["cli"])
        self.assertEqual(out["cert"], "<SECRET:pem>")
        self.assertEqual(out["hash"], "<SECRET:hash>")
        self.assertEqual(len(paths), 4)

    def test_nac_server_key_is_secret_but_tag_key_is_not(self):
        out, paths = redact_secrets({"nac": {"server": [{"host": "192.0.2.5", "authport": 1812, "key": "radius-shared"}]},
                                     "tags": [{"key": "site", "value": "a"}]})
        self.assertEqual(out["nac"]["server"][0]["key"], "<SECRET:$.nac.server[0].key>")
        self.assertEqual(out["tags"][0]["key"], "site")
        self.assertEqual(paths, ["$.nac.server[0].key"])


class DefaultTemplateTest(unittest.TestCase):
    """capture.py caches the default template of an Orchestrator release the toolkit does not bundle."""

    def run_with(self, default_body):
        client = FakeClient()
        real = client.request

        def request(method, path, query=None, expected_status=(200,), expect_json=True):
            if path == "/gms/versions":
                client.calls.append((method, path, {}))
                return json.dumps({"current": "10.0.0.99999"}).encode()
            if path == "/gms/appliance/preconfiguration/default":
                client.calls.append((method, path, {}))
                return default_body.encode() if isinstance(default_body, str) else json.dumps(default_body).encode()
            return real(method, path, query, expected_status, expect_json)

        client.request = request
        folder = Path(tempfile.mkdtemp())
        real_dir, capture.DEFAULT_TEMPLATES_DIR = capture.DEFAULT_TEMPLATES_DIR, folder
        self.addCleanup(setattr, capture, "DEFAULT_TEMPLATES_DIR", real_dir)
        capture.client_for = lambda nickname: client
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            capture.capture("lab", "lab-a", "reference", 0, Path(tempfile.mkdtemp()))
        return folder / "orchestrator-10.0.0.99999.yml", err.getvalue()

    def test_template_in_a_base64_json_configdata_field_is_cached(self):
        # The shape an Orchestrator on 10.0.0.40625 returns: base64 YAML inside a JSON object.
        yaml_text = "# Preconfiguration Template\napplianceInfo:\n  hostname: example\n"
        saved, _ = self.run_with({"configData": base64.b64encode(yaml_text.encode()).decode(), "autoApply": False, "nepk": None})
        self.assertEqual(saved.read_text(encoding="utf-8"), yaml_text)

    def test_line_wrapped_base64_configdata_is_cached(self):
        yaml_text = "# Preconfiguration Template\napplianceInfo:\n  hostname: example\n" * 5
        saved, _ = self.run_with({"configData": base64.encodebytes(yaml_text.encode()).decode()})  # MIME-style lines of 76
        self.assertEqual(saved.read_text(encoding="utf-8"), yaml_text)

    def test_configdata_that_is_not_a_yaml_mapping_is_not_cached(self):
        saved, err = self.run_with({"configData": base64.b64encode(b"just a sentence").decode()})
        self.assertFalse(saved.exists())
        self.assertIn("10.0.0.99999", err)

    def test_plain_text_template_is_still_cached(self):
        saved, _ = self.run_with("applianceInfo:\n  hostname: example\n")
        self.assertEqual(saved.read_text(encoding="utf-8"), "applianceInfo:\n  hostname: example\n")

    def test_unusable_answer_is_reported_not_silently_skipped(self):
        for body in ({"configData": None}, {"configData": 123}):
            saved, err = self.run_with(body)
            self.assertFalse(saved.exists())
            self.assertIn("10.0.0.99999", err)


class TranslateMissingTemplateTest(unittest.TestCase):
    def test_unknown_release_stops_with_a_clear_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = translate.main(["--capture", tempfile.mkdtemp(), "--site-type", "t", "--target-release", "10.0.0.99999"])
        self.assertEqual(code, 2)
        self.assertIn("10.0.0.99999", err.getvalue())
        self.assertIn("capture", err.getvalue())


if __name__ == "__main__":
    unittest.main()
