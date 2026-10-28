import base64
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolkit" / "scripts"))
import capture  # noqa: E402
import translate  # noqa: E402  guard-ok: scripts are imported after the sys.path insert, like capture above
import common  # noqa: E402  guard-ok: scripts are imported after the sys.path insert
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

    def test_a_read_the_key_may_not_make_is_recorded_as_denied_and_capture_continues(self):
        final = self.run_capture(FakeClient(fail={"/deployment": PermissionDeniedError("403")}), ["deployment", "bgp-system"])
        index = json.loads((final / "capture-index.json").read_text())
        self.assertEqual([(i["id"], i["status"]) for i in index],
                         [("deployment", "denied"), ("deployment", "denied"), ("bgp-system", "ok"), ("bgp-system", "ok")])

    def test_a_refused_reachability_read_is_reported_and_live_reads_still_run(self):
        final = self.run_capture(FakeClient(fail={"/reachability/gms": PermissionDeniedError("403")}), ["deployment"])
        index = json.loads((final / "capture-index.json").read_text())
        self.assertIn(("reachability", "denied"), [(i["id"], i["status"]) for i in index])
        self.assertEqual([(i["label"], i["status"]) for i in index if i["id"] == "deployment"], [("cached", "ok"), ("live", "ok")])

    def test_sync_in_progress_counts_as_reachable(self):
        final = self.run_capture(FakeClient(state=5), ["deployment"])  # state 5: Orchestrator is re-syncing a reachable appliance
        self.assertTrue(json.loads((final / "run.json").read_text())["reachable"])
        index = json.loads((final / "capture-index.json").read_text())
        self.assertEqual([(i["label"], i["status"]) for i in index if i["id"] == "deployment"], [("cached", "ok"), ("live", "ok")])

    def test_a_refused_routes_read_stops_the_capture(self):
        for path, only in (("/subnets", ["subnets"]), ("/subnets/all", ["subnets-all-vrfs"])):
            with self.assertRaises(capture.Stop) as stop:
                self.run_capture(FakeClient(fail={path: PermissionDeniedError("403")}), only)
            self.assertIn("routes", str(stop.exception))

    def test_a_stopped_capture_leaves_no_folder(self):
        capture.client_for = lambda nickname: FakeClient()
        out = Path(tempfile.mkdtemp())
        with self.assertRaises(capture.Stop):
            capture.capture("lab", "no-such-appliance", "reference", 0, out, ["deployment"])
        self.assertEqual(list(out.iterdir()), [])

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

    def test_an_empty_cli_secret_does_not_eat_the_next_line(self):
        cli = "   ip ospf authentication-key \n   interface lan0 mtu 1500\n   md5\n   interface pass-through max-bandwidth 50000\n   password 0 s3cret"
        out, _ = redact_secrets({"cli": cli})
        self.assertEqual(out["cli"].splitlines()[:4], cli.splitlines()[:4])  # nothing to hide on those lines, so they stay byte for byte
        self.assertEqual(out["cli"].splitlines()[4], "   password 0 <SECRET:text>")

    def test_free_text_credentials(self):
        motd = "Monitor login (192.0.2.10:88)\nuser: demo  pass: Hunter2x\nPassword=Swordfish9 pwd: abc123"
        out, paths = redact_secrets({"banners": {"motd": motd}, "cli": "   interface pass-through max-bandwidth 50000\n   bypass: on"})
        self.assertEqual(out["banners"]["motd"], "Monitor login (192.0.2.10:88)\nuser: demo  pass: <SECRET:text>\nPassword=<SECRET:text> pwd: <SECRET:text>")
        self.assertEqual(out["cli"], "   interface pass-through max-bandwidth 50000\n   bypass: on")
        self.assertEqual(paths, ["$.banners.motd"])
        out, _ = redact_secrets({"cli": 'web meta "{\\"note\\":\\"pass: abc\\"}"'})  # JSON inside a CLI line: the value ends at the escape
        self.assertEqual(out["cli"], 'web meta "{\\"note\\":\\"pass: <SECRET:text>\\"}"')

    def test_free_text_credentials_review_forms(self):
        text = "password = Alpha1x\nPassword : Bravo2x\nPW: Charlie3x\npasscode=Delta4x\npassphrase: Echo5x"
        out, paths = redact_secrets({"banners": {"issue": text}})
        for value in ("Alpha1x", "Bravo2x", "Charlie3x", "Delta4x", "Echo5x"):
            self.assertNotIn(value, out["banners"]["issue"])
        self.assertEqual(paths, ["$.banners.issue"])
        cli = 'web proxy-server "{\\"host\\":\\"192.0.2.8\\",\\"username\\":\\"u\\",\\"password\\":\\"Foxtrot6x\\"}"'
        out, _ = redact_secrets({"cli": cli})
        self.assertEqual(out["cli"], 'web proxy-server "{\\"host\\":\\"192.0.2.8\\",\\"username\\":\\"u\\",\\"password\\":\\"<SECRET:text>\\"}"')
        out, _ = redact_secrets({"note": '{"password":"Golf7x"} and password="Hotel8x"'})
        self.assertEqual(out["note"], '{"password":"<SECRET:text>"} and password="<SECRET:text>"')
        out, _ = redact_secrets({"note": '{"pwd":12345,"next":1}'})  # guard-ok: a JSON number is left alone, like a keyed field (review of a3adc51)
        self.assertEqual(out["note"], '{"pwd":12345,"next":1}')
        out, _ = redact_secrets({"note": '{"pwd":abc12,"next":1}'})  # an unquoted word value ends at the next JSON delimiter
        self.assertEqual(out["note"], '{"pwd":<SECRET:text>,"next":1}')

    def test_free_text_credentials_whole_value(self):
        cases = {
            "password: ab,cd": "password: <SECRET:text>",
            "pass: x,y}z": "pass: <SECRET:text>",
            '{"password":"a,b c","x":1}': '{"password":"<SECRET:text>","x":1}',
            '{"password":null,"pw":true}': '{"password":null,"pw":true}',
            "secret = Alpha1x": "secret = <SECRET:text>",
            "set psk = Bravo2x": "set psk = <SECRET:text>",
            "token=Charlie3x api-key: Delta4x": "token=<SECRET:text> api-key: <SECRET:text>",
            "pre-shared-key = Echo5x": "pre-shared-key = <SECRET:text>",
            'x "{\\"password\\":\\"ab"cd\\"}"': 'x "{\\"password\\":\\"<SECRET:text>\\"}"',  # an escaped-quoted value ends at the escaped quote
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(redact_secrets({"t": text})[0]["t"], want)

    def test_free_text_credentials_quote_edges(self):
        cases = {
            'password: "Ab12 rest': 'password: <SECRET:text> rest',
            'password: "ab\ncd"': 'password: <SECRET:text>\ncd"',
            '{"password":"","user":"admin"}': '{"password":"","user":"admin"}',
            "password: 'two words' x": "password: '<SECRET:text>' x",
            "the secret password: Tango9x": "the secret <SECRET:text> <SECRET:text>",  # over-redacting the keyword is the safe side
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(redact_secrets({"t": text})[0]["t"], want)

    def assertRedacts(self, cases):
        for text, want in cases.items():
            with self.subTest(text):
                out, paths = redact_secrets({"t": text})
                self.assertEqual(out["t"], want)
                self.assertEqual(paths, ["$.t"] if want != text else [])
                try:
                    json.loads(text)
                except ValueError:
                    continue
                json.loads(out["t"])  # redaction never breaks JSON structure

    def test_free_text_compound_key_names(self):
        self.assertRedacts({
            '{"adminPassword":"Alpha1x"}': '{"adminPassword":"<SECRET:text>"}',
            '{"apiKey":"Bravo2x","x":1}': '{"apiKey":"<SECRET:text>","x":1}',
            '{"clientSecret": "Charlie3x"}': '{"clientSecret": "<SECRET:text>"}',
            '{"client_secret":"Delta4x"}': '{"client_secret":"<SECRET:text>"}',
            '{"access_token":"Echo5x"}': '{"access_token":"<SECRET:text>"}',
            '{"preSharedKey":"Foxtrot6x"}': '{"preSharedKey":"<SECRET:text>"}',
            '{"authToken":"Golf7x"}': '{"authToken":"<SECRET:text>"}',
            '{"ikePsk":"12345"}': '{"ikePsk":"<SECRET:text>"}',
            'x "{\\"sharedSecret\\":\\"Hotel8x\\"}"': 'x "{\\"sharedSecret\\":\\"<SECRET:text>\\"}"',
            "admin_password: India9x": "admin_password: <SECRET:text>",
            "secret_key: Juliet1x": "secret_key: <SECRET:text>",
            "api_key=Kilo2x": "api_key=<SECRET:text>",
            "ike-pre-shared-key = Lima3x": "ike-pre-shared-key = <SECRET:text>",
            "pre_shared_key: Lima4x authentication_key: Lima5x auth_key: Lima6x": "pre_shared_key: <SECRET:text> authentication_key: <SECRET:text> auth_key: <SECRET:text>",
            '"adminPassword" = "Lima7x"': '"adminPassword" = "<SECRET:text>"',
            '{"secretKeyId":"Mike4x"}': '{"secretKeyId":"<SECRET:text>"}',  # same as a keyed field of that name
            # not secrets: a word that only contains "pass", a secret word with a suffix, booleans
            '{"passThrough":"on","bypass":"Nov5x","md5Enabled":true}': '{"passThrough":"on","bypass":"Nov5x","md5Enabled":true}',
            "token-lifetime: 3600 pass-through: on": "token-lifetime: 3600 pass-through: on",
            "xpassword: Oscar6x": "xpassword: <SECRET:text>",  # guard-ok: any name holding a keyed-field word, as for quoted keys (review of a3adc51)
        })

    def test_free_text_value_with_quotes_and_backslashes(self):
        self.assertRedacts({
            "pass: P@ss'w0rd! next": "pass: <SECRET:text> next",
            "password: Ab\\cd9 next": "password: <SECRET:text> next",
            '{"password":"ab\\"cd9","x":1}': '{"password":"<SECRET:text>","x":1}',
            "password: 'it\\'s9x' next": "password: '<SECRET:text>' next",
            'password: \\"ab\\ncd9x\\" next': 'password: \\"<SECRET:text>\\" next',  # an escape inside a \\"-quoted value
            'note "pass: Alpha1x" end': 'note "pass: <SECRET:text>" end',  # a quote before whitespace ends the value
            "pass: (Bravo2x), next": "pass: <SECRET:text> next",
            '{"x":"pass: Charlie3x","y":1}': '{"x":"pass: <SECRET:text>","y":1}',  # a quote before a delimiter ends the value
            "note 'pass: Delta4x'": "note 'pass: <SECRET:text>'",
            'x "{\\"password\\":\\"a\\\\\\"b9x\\"}"': 'x "{\\"password\\":\\"<SECRET:text>\\"}"',  # escaped JSON of "a\\"b9x"
        })

    def test_free_text_secret_with_a_nested_value(self):
        self.assertRedacts({
            '{"token":{"value":"Alpha1x","password":"Bravo2x"}}': '{"token":{"value":"Alpha1x","password":"<SECRET:text>"}}',
            '{"secret":["Charlie3x", "Delta4x"],"x":[1]}': '{"secret":["<SECRET:text>"],"x":[1]}',
            '{"psk":["a]b9x"]}': '{"psk":["<SECRET:text>"]}',
            '{"secret":[],"pw":[{"user":"u"}]}': '{"secret":[],"pw":[{"user":"u"}]}',
            'x "{\\"secret\\":[\\"Echo5x\\"]}"': 'x "{\\"secret\\":[\\"<SECRET:text>\\"]}"',
        })

    def test_free_text_filler_words(self):
        self.assertRedacts({
            "the password is Alpha1x": "the password is <SECRET:text>",
            "Password - Bravo2x": "Password - <SECRET:text>",
            "password => Charlie3x": "password => <SECRET:text>",
            "password -> Delta4x": "password -> <SECRET:text>",
            "admin_password is Echo5x": "admin_password is <SECRET:text>",
            "admin_password -> Foxtrot6x": "admin_password -> <SECRET:text>",
            "pass island": "pass island",
            "md5 => Golf7x": "md5 => <SECRET:text>",
        })

    def test_free_text_json_numbers_are_left_alone(self):
        self.assertRedacts({
            '{"tokenLifetime":3600}': '{"tokenLifetime":3600}',
            '{"passwordMinLength":8,"md5KeyId":1}': '{"passwordMinLength":8,"md5KeyId":1}',
            '{"psk":12345,"secret":-1.5e3,"pw":null}': '{"psk":12345,"secret":-1.5e3,"pw":null}',
            '{"secret":[1,2]}': '{"secret":[1,2]}',
            "token-lifetime: 3600 tokenLifetime=3600": "token-lifetime: 3600 tokenLifetime=3600",
            "password: 12345": "password: <SECRET:text>",  # a plain secret word in prose still takes a number
        })

    def test_free_text_unquoted_compound_keys(self):
        self.assertRedacts({
            "adminPassword: Echo5x": "adminPassword: <SECRET:text>",
            "adminPassword=Echo5x": "adminPassword=<SECRET:text>",
            "privateKey: Golf7x": "privateKey: <SECRET:text>",
            "private_key=Golf7x": "private_key=<SECRET:text>",
            "ike_key: Hotel8x": "ike_key: <SECRET:text>",
            "md5: India9x": "md5: <SECRET:text>",
            "md5=India9x": "md5=<SECRET:text>",
            "md5: 12345": "md5: <SECRET:text>",
            "admin_pwd: Papa1x pass_key: Quebec1x admin_password: 12345": "admin_pwd: <SECRET:text> pass_key: <SECRET:text> admin_password: <SECRET:text>",
            "{'password': 'Juliet1x'}": "{'password': '<SECRET:text>'}",
            "{'adminPassword': 'Juliet1x'}": "{'adminPassword': '<SECRET:text>'}",
            '{"secret":[["Kilo2x"]]}': '{"secret":["<SECRET:text>"]}',
            "passThrough: on bypass: on keyId: k1 authType: pap auth-vlan: 10": "passThrough: on bypass: on keyId: k1 authType: pap auth-vlan: 10",
        })

    def test_free_text_prose_fillers_need_a_value_that_is_not_a_word(self):
        self.assertRedacts({
            "password is required": "password is required",
            "the token is valid for 3600s": "the token is valid for 3600s",
            "password - see vault": "password - see vault",
            "admin_password is set": "admin_password is set",
            "password is Abc123!": "password is <SECRET:text>",
            "password => alpha": "password => <SECRET:text>",
        })

    def test_free_text_redaction_keeps_json_valid(self):
        objects = [
            {"note": "pass=x\\", "y": "z"}, {"psk": "a\\"}, {"psk": "\\\""}, {"psk": "ab\"cd\\\\"}, {"a": "pass: 'x", "b": "don't"},
            {"note": "password: \"abc\" ok", "x": 1}, {"desc": "password: x\"", "x": 1}, {"desc": "password='a\"b'", "x": 1},
            {"secret": ["a]b", "c\"]d"]}, {"psk": ["a", "b"], "nest": {"token": ["c"]}}, {"psk": "a,b}c", "x": 1},
            {"passwordMinLength": 8, "tokenLifetime": 3600, "md5KeyId": 1}, {"secret": [["x"]], "pw": [1]},
        ]
        for obj in objects:
            for text in (json.dumps(obj), json.dumps(obj, separators=(",", ":")), json.dumps(json.dumps(obj))):
                with self.subTest(text):
                    out = redact_secrets({"t": text})[0]["t"]
                    if text.startswith("{"):
                        self.assertEqual(json.loads(out).keys(), obj.keys())  # no key is swallowed into a value
                    self.assertEqual(redact_secrets({"t": out})[0]["t"], out)  # a second pass changes nothing
        self.assertEqual(redact_secrets({"t": json.dumps({"a": "pass: 'x", "b": "don't"})})[0]["t"], "{\"a\": \"pass: <SECRET:text>\", \"b\": \"don't\"}")
        out = redact_secrets({"t": '{"note":"pass=x\\\\","y":"z"}'})[0]["t"]
        self.assertEqual(out, '{"note":"pass=<SECRET:text>","y":"z"}')

    def test_long_runs_stay_linear(self):
        n = 2000
        for text in ('banner "password: \\"' + "\\\\" * n + "x", 'password: "' + "\\" * n, "password: " + "\\" * n, 'password: "' + '\\"' * n,
                     '"secret":[' + '"a",' * n, '"secret":[' + "[" * n, "pw " + "-" * n * 50, "a-" * n * 50, '"' + "a-" * n * 50,
                     "password" + " " * n * 50, '"' * n * 50, "password is " * n, "x_" * n * 50, '"pw":' * n * 10):
            start = time.perf_counter()
            redact_secrets({"t": text})
            self.assertLess(time.perf_counter() - start, 1.0, text[:40])

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
        bundled, work = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        capture.client_for = lambda nickname: client
        err = io.StringIO()
        with mock.patch.object(capture, "DEFAULT_TEMPLATES_DIR", bundled), mock.patch.object(capture, "WORK_DIR", work), contextlib.redirect_stderr(err):
            capture.capture("lab", "lab-a", "reference", 0, Path(tempfile.mkdtemp()))
        self.assertEqual(list(bundled.iterdir()), [])  # the toolkit folder stays clean: a downloaded template goes to the work folder
        return work / "reference" / "default-templates" / "orchestrator-10.0.0.99999.yml", err.getvalue()

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

    def test_a_downloaded_template_is_found_for_its_release(self):
        saved, _ = self.run_with("applianceInfo:\n  hostname: example\n")
        with mock.patch.object(common, "WORK_DIR", saved.parents[2]):
            path, why = common.find_default_template("10.0.0.99999")
        self.assertEqual((path, why.startswith("the exact template")), (saved, True))

    def test_plain_text_template_is_still_cached(self):
        saved, _ = self.run_with("applianceInfo:\n  hostname: example\n")
        self.assertEqual(saved.read_text(encoding="utf-8"), "applianceInfo:\n  hostname: example\n")

    def test_unusable_answer_is_reported_not_silently_skipped(self):
        for body in ({"configData": None}, {"configData": 123}):
            saved, err = self.run_with(body)
            self.assertFalse(saved.exists())
            self.assertIn("10.0.0.99999", err)


class TranslateMissingTemplateTest(unittest.TestCase):
    def test_no_template_at_all_stops_with_a_clear_message(self):
        # an unknown build falls back to a bundled template (docs/decisions/0014); only an empty template folder stops
        err = io.StringIO()
        empty = Path(tempfile.mkdtemp())
        with mock.patch.object(common, "DEFAULT_TEMPLATES_DIR", empty), mock.patch.object(common, "WORK_DIR", empty), contextlib.redirect_stderr(err):
            code = translate.main(["--capture", tempfile.mkdtemp(), "--site-type", "t", "--target-release", "10.0.0.99999"])
        self.assertEqual(code, 2)
        self.assertIn("no default templates", err.getvalue())


if __name__ == "__main__":
    unittest.main()
