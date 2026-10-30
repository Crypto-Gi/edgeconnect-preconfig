"""The GUI's fleet status (docs/gui/BUILD_PLAN.md step 4): every orch_N profile checked at once through preconfig's own .env
reader and ApiClient, green / amber / red with a plain reason, the identity from /gmsserver/info, and never a key. Offline: the
client's opener is a fake that answers per host."""
import dataclasses
import io
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "toolkit" / "scripts"))
sys.path.insert(0, str(ROOT / "toolkit" / "gui"))
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import engine  # noqa: E402  guard-ok: imported after the sys.path insert
import fleet  # noqa: E402  guard-ok: imported after the sys.path insert

KEY = "k3y-Value-0123456789"


class Response:
    def __init__(self, body):
        self.status = 200
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


class Opener:
    """Stands in for urllib's opener. Per host (the nickname): per path, a body, an HTTP status, ("delay", s) or an error to raise."""

    def __init__(self, answers, clock=None, before=None):
        self.answers, self.clock, self.before = answers, clock, before
        self.calls = []
        self.lock = threading.Lock()

    def open(self, req, timeout):
        parts = urlsplit(req.full_url)
        name, path = parts.hostname.split(".")[0], parts.path[len("/gms/rest"):]
        with self.lock:
            self.calls.append((name, req.get_method(), path, timeout))
        if self.before:
            self.before(path)
        answer = self.answers.get(name, {}).get(path)
        if isinstance(answer, tuple):  # ("delay", seconds): answers normally, that much later
            self.clock.now += answer[1]
            answer = None
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            raise HTTPError(req.full_url, answer, "error", {}, io.BytesIO(b""))
        if isinstance(answer, str):  # 404 with this body
            raise HTTPError(req.full_url, 404, "error", {}, io.BytesIO(answer.encode("utf-8")))
        if answer is None:
            answer = {"current": "9.5.1"} if path == "/gms/versions" else {"serialNumber": "SERIAL-" + name}
        return Response(answer)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def dotenv(*names, **extra):
    lines = ["orch_{}={}".format(number, name) for number, name in enumerate(names, 1)]
    for name in names:
        lines += ["{}_base_url=https://{}.invalid".format(name, name), "{}_api_key={}".format(name, KEY)]
    lines += ["{}={}".format(key, value) for key, value in extra.items()]
    return "\n".join(lines) + "\n"


class FleetTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, ".env")
        patcher = mock.patch.dict(os.environ, {}, clear=True)  # preconfig's reader adds the process environment
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()

    def check(self, text, answers=None, before=None):
        Path(self.path).write_text(text, encoding="utf-8")
        self.opener = Opener(answers or {}, self.clock, before)
        with mock.patch.object(common, "build_opener", lambda *handlers: self.opener):
            self.fleet = fleet.check_all(self.path, clock=self.clock)
        return {status.name: status for status in self.fleet.orchestrators}

    def test_each_status_with_its_plain_reason(self):
        names = ("good", "norights", "slow", "busy", "badkey", "broken", "missing", "down", "badcert", "nodns", "refused", "badurl", "nokey")
        text = dotenv(*names).replace("badurl_base_url=https://", "badurl_base_url=http://").replace("nokey_api_key={}\n".format(KEY), "")
        result = self.check(text, {
            "norights": {"/gms/versions": 403},
            "slow": {"/gms/versions": ("delay", 2.5)},
            "busy": {"/gms/versions": 429},
            "badkey": {"/gms/versions": 401},
            "broken": {"/gms/versions": 500},
            "missing": {"/gms/versions": "no such page"},
            "down": {"/gms/versions": URLError(socket.timeout("timed out"))},
            "badcert": {"/gms/versions": URLError(ssl.SSLCertVerificationError("certificate verify failed"))},
            "nodns": {"/gms/versions": URLError(socket.gaierror(8, "nodename nor servname provided"))},
            "refused": {"/gms/versions": URLError(ConnectionRefusedError(61, "Connection refused"))},
        })
        self.assertEqual({name: (status.status, status.reason) for name, status in result.items()}, {
            "good": ("green", "connected (release 9.5.1)"),
            "norights": ("green", "connected (release unknown: GET /gms/versions refused)"),
            "slow": ("amber", "connected (release 9.5.1), but slow: 2.5 s to answer"),
            "busy": ("amber", "Orchestrator is busy (HTTP 429); check again shortly"),
            "badkey": ("red", "the API key was refused (HTTP 401)"),
            "broken": ("red", "Orchestrator answered with a server error (HTTP 500)"),
            "missing": ("red", "HTTP 404: no such page"),
            "down": ("red", "no answer within 5 s"),
            "badcert": ("red", "the certificate was not accepted; check the CA bundle or the certificate pin"),
            "nodns": ("red", "the address could not be found (DNS)"),
            "refused": ("red", "the connection was refused"),
            "badurl": ("red", "fix the settings: base URL for 'badurl' must be an https:// URL"),
            "nokey": ("red", "fix the settings: unknown or incomplete Orchestrator profile 'nokey' (configured: {})".format(", ".join(sorted(names)))),
        })
        self.assertEqual(list(result), list(names))
        good = result["good"]
        self.assertEqual((good.url, good.release, good.id, good.seconds), ("https://good.invalid", "9.5.1", "SERIAL-good", 0.0))
        self.assertEqual((result["norights"].release, result["slow"].seconds, result["badurl"].url), ("", 2.5, "http://badurl.invalid"))
        self.assertEqual((self.fleet.not_listed, self.fleet.note), ([], ""))

    def test_each_read_gets_one_try_of_five_seconds(self):
        self.check(dotenv("lab"))
        self.assertEqual(self.opener.calls, [("lab", "GET", "/gms/versions", 5.0), ("lab", "GET", "/gmsserver/info", 5.0)])

    def test_identity_is_the_serial_and_the_placeholder_or_a_refused_read_is_none(self):
        result = self.check(dotenv("real", "cloud", "noinfo", "down"), {
            "cloud": {"/gmsserver/info": {"serialNumber": "00-00-00-00-00-00"}},
            "noinfo": {"/gmsserver/info": 403},
            "down": {"/gmsserver/info": URLError(socket.timeout("timed out"))},
        })
        self.assertEqual((result["real"].id, result["real"].reason), ("SERIAL-real", "connected (release 9.5.1)"))
        for name in ("cloud", "noinfo", "down"):
            self.assertEqual((result[name].status, result[name].id, result[name].url, result[name].reason),
                             ("green", "", "https://{}.invalid".format(name), "connected (release 9.5.1); no unique serial number, so it is known by its address"))

    def test_all_orchestrators_are_checked_at_the_same_time(self):
        names = ("a", "b", "c", "d", "e", "f")
        all_asked = threading.Barrier(len(names), timeout=5)  # one at a time, the first would wait here alone and fail
        result = self.check(dotenv(*names), before=lambda path: path == "/gms/versions" and all_asked.wait())
        self.assertEqual([status.status for status in result.values()], ["green"] * len(names))

    def test_validator_uploads_label_and_colour(self):
        text = dotenv("lab", "main", "other", "badcolor", "baduploads", EC_VALIDATORS="lab, Other", lab_gui_label="Lab bench", lab_gui_color="#1F6FEB",
                      lab_gui_preconfig_uploads="on", other_gui_preconfig_uploads="off", badcolor_gui_color="red", baduploads_gui_preconfig_uploads="maybe")
        result = self.check(text)
        self.assertEqual((result["lab"].validator, result["lab"].writes, result["lab"].label, result["lab"].color), (True, True, "Lab bench", "#1F6FEB"))
        self.assertEqual((result["main"].validator, result["main"].writes, result["main"].label, result["main"].color), (False, False, "", ""))
        self.assertEqual((result["other"].validator, result["other"].writes), (True, False))
        self.assertEqual((result["badcolor"].status, result["badcolor"].reason), ("red", "fix the settings: badcolor_gui_color must be a colour like #1f6feb"))
        self.assertEqual((result["baduploads"].status, result["baduploads"].reason), ("red", "fix the settings: baduploads_gui_preconfig_uploads must be on or off"))
        self.assertEqual([call[0] for call in self.opener.calls if call[0].startswith("bad")], [])  # bad settings send nothing

    def test_only_on_opens_uploads_in_the_panel_as_at_the_upload_gate(self):
        """One helper reads <nickname>_gui_preconfig_uploads for the panel and the gate: on (any case) opens uploads; off and
        blank are off; anything else, true and yes included, is red in the panel and refused at the gate like off."""
        values = {"a": "on", "b": "ON", "c": "off", "d": "", "e": "true", "f": "yes", "g": "1", "h": "maybe"}
        result = self.check(dotenv(*values, **{name + "_gui_preconfig_uploads": value for name, value in values.items()}))
        env = common.env_map(self.path)
        expected = {"a": ("green", True), "b": ("green", True), "c": ("green", False), "d": ("green", False),
                    "e": ("red", False), "f": ("red", False), "g": ("red", False), "h": ("red", False)}
        for name, (status, opened) in expected.items():
            with self.subTest(value=values[name]):
                self.assertEqual((result[name].status, result[name].writes), (status, opened))
                if status == "red":
                    self.assertEqual(result[name].reason, "fix the settings: {}_gui_preconfig_uploads must be on or off".format(name))
                self.assertEqual(engine.uploads_off(name, env) is None, opened)

    def test_a_base_url_with_a_user_name_or_password_is_red_and_sends_nothing(self):
        Path(self.path).write_text(dotenv("creds").replace("https://", "https://user:pa55word@"), encoding="utf-8")
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("no connection may be opened")):
            result = fleet.check_all(self.path)  # the real ApiClient, opener and URL parsing
        self.assertEqual([(s.name, s.url, s.status, s.reason) for s in result.orchestrators],
                         [("creds", "https://creds.invalid", "red", "fix the settings: the base URL must not hold a user name or password")])
        self.assertNotIn("pa55word", json.dumps(dataclasses.asdict(result)))

    def test_a_base_url_that_cannot_be_parsed_is_red_and_the_others_are_still_checked(self):
        result = self.check(dotenv("lab", "v6").replace("https://v6.invalid", "https://[abc"))
        self.assertEqual([(s.name, s.url, s.status, s.reason) for s in result.values()], [
            ("lab", "https://lab.invalid", "green", "connected (release 9.5.1)"),
            ("v6", "", "red", "fix the settings: the base URL cannot be read as a URL"),
        ])
        self.assertEqual(fleet._origin("https://[abc"), "")

    def test_an_unexpected_error_in_one_check_is_red_for_that_one_only_and_its_text_is_not_shown(self):
        def factory(name):
            if name == "odd":
                raise RuntimeError("odd failure holding {}".format(KEY))
            return common.client_for(name)
        Path(self.path).write_text(dotenv("lab", "odd"), encoding="utf-8")
        self.opener = Opener({}, self.clock)
        with mock.patch.object(common, "build_opener", lambda *handlers: self.opener):
            result = fleet.check_all(self.path, client_factory=factory, clock=self.clock)
        self.assertEqual([(s.name, s.url, s.status, s.reason) for s in result.orchestrators], [
            ("lab", "https://lab.invalid", "green", "connected (release 9.5.1)"),
            ("odd", "https://odd.invalid", "red", "the check stopped on an unexpected error; nothing is known about this Orchestrator"),
        ])

    def test_no_key_or_url_password_in_the_result(self):
        text = dotenv("echo", "creds").replace("creds_base_url=https://", "creds_base_url=https://user:pa55word@")
        result = self.check(text, {"echo": {"/gms/versions": "bad token {}".format(KEY)}})
        self.assertEqual((result["echo"].reason, result["creds"].url), ("HTTP 404: bad token <API_KEY>", "https://creds.invalid"))
        shown = json.dumps(dataclasses.asdict(self.fleet))
        self.assertNotIn(KEY, shown)
        self.assertNotIn("pa55word", shown)

    def test_the_key_is_masked_in_free_text_and_what_the_server_sent_never_in_the_status_word(self):
        result = self.check(dotenv("lab").replace("lab_api_key={}".format(KEY), "lab_api_key=green") + "spare_base_url=https://spare.invalid\n",
                            {"lab": {"/gmsserver/info": {"serialNumber": "green"}}})
        self.assertEqual((result["lab"].status, result["lab"].id, result["lab"].reason), ("green", "<API_KEY>", "connected (release 9.5.1)"))

    def test_release_and_id_from_the_server_are_masked_and_at_most_100_characters(self):
        long = "x" * 300
        result = self.check(dotenv("lab"), {"lab": {"/gms/versions": {"current": KEY + long}, "/gmsserver/info": {"serialNumber": KEY + long}}})
        self.assertEqual((result["lab"].release, result["lab"].id), (("<API_KEY>" + long)[:100], ("<API_KEY>" + long)[:100]))
        self.assertNotIn(KEY, json.dumps(dataclasses.asdict(self.fleet)))
        self.assertEqual(len(result["lab"].reason), len("connected (release )") + 100)

    def test_a_profile_without_an_orch_line_is_not_listed_and_the_result_says_so(self):
        result = self.check(dotenv("lab") + "spare_base_url=https://spare.invalid\nspare_api_key={}\n".format(KEY))
        self.assertEqual((list(result), self.fleet.not_listed), (["lab"], ["spare"]))
        self.assertEqual(self.fleet.note, "spare has settings in .env but no orch_N line, so it is not listed; add an orch_<N>=<nickname> line to list one")
        self.assertEqual((self.check("spare_base_url=https://spare.invalid\n"), self.fleet.not_listed), ({}, ["spare"]))

    def test_keys_the_automation_tools_would_refuse_are_named_without_their_value(self):
        text = dotenv("lab") + "\n".join([
            "orch_validators=lab", "orch_default=lab", "orch_2=Edge-B", "orch_3=Default", "export orch_4=lab", "orch_5=",
            "edge-b_base_url=https://edge-b.invalid", "spare_api_key=\"half-quoted", "spare_label='ok'", "spare_note=\"",
        ]) + "\n"
        self.check(text)
        refuse = "the automation tools (edgeconnect-auto) stop at this key: "
        self.assertEqual(dataclasses.asdict(self.fleet).get("warnings"), [
            "orch_validators: " + refuse + "an orch_ key must be numbered (orch_<N>), or be orch_default",
            "spare_api_key: " + refuse + "its quote is not closed",
            "spare_note: " + refuse + "its quote is not closed",
            "orch_2: " + refuse + "a nickname uses letters, digits or _ only, and is not default, ec, edgeconnect or orchestrator",
            "orch_3: " + refuse + "a nickname uses letters, digits or _ only, and is not default, ec, edgeconnect or orchestrator",
            "orch_4: " + refuse + "this nickname is already used by orch_1",
        ])
        self.assertNotIn("half-quoted", json.dumps(dataclasses.asdict(self.fleet)))
        self.check(dotenv("lab", "edge_b"))
        self.assertEqual(self.fleet.warnings, [])

    def test_a_quote_inside_matched_quotes_is_no_warning(self):
        self.check(dotenv("lab") + "lab_gui_label=\"'Lab\"\nlab_note='\"'\n")
        self.assertEqual(self.fleet.warnings, [])

    def test_an_unclosed_quote_on_an_earlier_copy_of_a_key_is_named(self):
        self.check(dotenv("lab") + "lab_note=\"half\nlab_note=whole\n")
        self.assertEqual(self.fleet.warnings, ["lab_note: the automation tools (edgeconnect-auto) stop at this key: its quote is not closed"])

    def test_orch_keys_in_the_process_environment_are_checked_as_automation_reads_them(self):
        with mock.patch.dict(os.environ, {"ORCH_VALIDATORS": "lab", "ORCH_9": "Edge-B", "ORCH_7": "lab"}):
            self.check(dotenv("lab"))
        refuse = "the automation tools (edgeconnect-auto) stop at this key: "
        self.assertEqual(self.fleet.warnings, [
            "ORCH_VALIDATORS (in the environment): " + refuse + "an orch_ key must be numbered (orch_<N>), or be orch_default",
            "ORCH_7 (in the environment): " + refuse + "this nickname is already used by orch_1",
            "ORCH_9 (in the environment): " + refuse + "a nickname uses letters, digits or _ only, and is not default, ec, edgeconnect or orchestrator",
        ])

    def test_single_orchestrator_keys_get_a_nickname_that_is_not_reserved(self):
        with mock.patch.dict(os.environ, {"EC_BASE_URL": "https://one.invalid", "EC_API_KEY": KEY}):
            self.check(dotenv("lab") + "orchestrator_base_url=https://two.invalid\norchestrator_api_key={}\n".format(KEY))
        self.assertEqual(self.fleet.not_listed, ["ec", "orchestrator"])
        self.assertEqual(self.fleet.note, "ec, orchestrator: single-Orchestrator settings in .env or the environment, not listed, and their "
                                          "names are reserved; copy them to a nickname of your own, for example orch_<N>=main with main_base_url "
                                          "and main_api_key")
        with mock.patch.dict(os.environ, {"SPARE_BASE_URL": "https://spare.invalid", "SPARE_API_KEY": KEY, "OTHER_TOOL_BASE_URL": "https://x.invalid"}):
            self.check(dotenv("lab"))
        self.assertEqual(self.fleet.not_listed, ["spare"])  # a *_BASE_URL of another program, without a key, is not named
        self.assertEqual(self.fleet.note, "spare has settings in .env or the environment but no orch_N line, so it is not listed; add an "
                                          "orch_<N>=<nickname> line to list one")

    def test_the_given_env_is_used_inside_another_run_context(self):
        other = os.path.join(os.path.dirname(self.path), "other.env")
        Path(other).write_text(dotenv("elsewhere"), encoding="utf-8")
        with common.running(common.RunContext(dotenv=other)):
            result = self.check(dotenv("lab", EC_VALIDATORS="lab"))
        self.assertEqual((list(result), result["lab"].status, result["lab"].validator), (["lab"], "green", True))


SHARED_ENV = """# one .env both toolkits read
orch_2=lab
orch_10 = edge_b
export orch_1=Main
orch_3=
main_base_url=https://orchestrator.example.com
main_api_key="<API_KEY>"
lab_base_url=https://192.0.2.10:8443/gms/rest
lab_api_key='<API_KEY>'
edge_b_base_url=https://edge-b.example.com
edge_b_api_key=<API_KEY>
spare_base_url=https://spare.example.com
spare_api_key=<API_KEY>
lab_gui_label=Lab bench
lab_gui_color=#1f6feb
EC_VALIDATORS=lab
"""
# What edgeconnect_automation.config.orchestrator_profiles(path, environ={}) gives for SHARED_ENV (automation 7af5e9d): nicknames in
# menu order with their URL and key.
AUTOMATION_PROFILES = {
    "main": ("https://orchestrator.example.com", "<API_KEY>"),
    "lab": ("https://192.0.2.10:8443/gms/rest", "<API_KEY>"),
    "edge_b": ("https://edge-b.example.com", "<API_KEY>"),
}
AUTOMATION_SRC = Path(os.environ.get("EDGECONNECT_AUTOMATION_SRC") or ROOT.parent / "edgeconnect-orchestrator-automation" / "src")


class SharedEnvTests(unittest.TestCase):
    """One .env gives the same profiles in preconfig's reader and the automation tools' (BUILD_PLAN "The shared .env")."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, ".env")
        Path(self.path).write_text(SHARED_ENV, encoding="utf-8")

    def preconfig_profiles(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = common.env_map(self.path)
        return {name: (env["{}_base_url".format(name)], env["{}_api_key".format(name)]) for name in fleet.profiles(env)}

    def test_same_profiles_as_the_recorded_automation_reading(self):
        self.assertEqual(list(self.preconfig_profiles().items()), list(AUTOMATION_PROFILES.items()))

    def test_same_profiles_as_the_automation_reader(self):
        if not (AUTOMATION_SRC / "edgeconnect_automation" / "config.py").is_file():
            self.skipTest("no automation checkout at {} (set EDGECONNECT_AUTOMATION_SRC to its src/); the recorded comparison above still runs".format(AUTOMATION_SRC))
        sys.path.insert(0, str(AUTOMATION_SRC))
        self.addCleanup(sys.path.remove, str(AUTOMATION_SRC))
        from edgeconnect_automation.config import orchestrator_profiles  # noqa: E402  guard-ok: imported after the sys.path insert, only when present
        automation = {name: (values["base_url"], values["api_key"]) for name, values in orchestrator_profiles(self.path, {}).items()}
        self.assertEqual(list(automation.items()), list(self.preconfig_profiles().items()))
        self.assertEqual(list(automation.items()), list(AUTOMATION_PROFILES.items()))


if __name__ == "__main__":
    unittest.main()
