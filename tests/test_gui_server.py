"""The GUI server (docs/gui/BUILD_PLAN.md step 5), as the automation GUI's security tests: the launch link works once, every
request needs the session cookie and an allowed Host, every POST the same Origin and JSON, no CORS, and neither an API key nor a
secret CSV cell is in any response or event. Offline: the fake Orchestrator of the engine tests, and the fleet tests' opener."""
import http.client
import json
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gui_tasks  # noqa: E402  guard-ok: sets the paths and the run log off, as the other GUI tests
from test_gui_tasks import KEY, Fixture  # noqa: E402  guard-ok: the shared build, site type and fake Orchestrator
from test_gui_fleet import Opener  # noqa: E402  guard-ok: the fleet tests' fake urllib opener
import common  # noqa: E402  guard-ok: imported after the sys.path insert
import gui_server  # noqa: E402  guard-ok: imported after the sys.path insert


class ServerCase(Fixture):
    def setUp(self):
        super().setUp()
        self.server = gui_server.GuiServer(self.manager(), 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.host = "127.0.0.1:{}".format(self.port)
        self.cookie = "edgeconnect_preconfig_gui_{}={}".format(self.port, self.server.session)
        self.bodies = []

    def request(self, method, path, body=None, host=None, cookie=True, origin=True, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"Host": host or self.host}
        if cookie:
            headers["Cookie"] = self.cookie if cookie is True else cookie
        if origin and method == "POST":
            headers["Origin"] = origin if isinstance(origin, str) else "http://" + (host or self.host)
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = content_type
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        text = response.read().decode()
        connection.close()
        self.bodies.append(text)
        self.assertFalse([name for name in response.headers if name.lower().startswith("access-control-")], "no CORS headers, ever")
        return response, text

    def post_task(self, body):
        response, text = self.request("POST", "/api/tasks", body)
        self.assertEqual(response.status, 201, text)
        return self.server.tasks.get(json.loads(text)["id"])

    def events(self, task):
        response, stream = self.request("GET", "/api/tasks/{}/events".format(task.id))
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        return [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]


class ServerTests(ServerCase):
    def test_launch_link_works_once_and_sets_a_strict_http_only_cookie_that_is_not_the_token(self):
        self.assertEqual(self.request("GET", "/?token=wrong", cookie=False)[0].status, 401)
        response, _ = self.request("GET", "/?token=" + self.server.token, cookie=False)
        self.assertEqual((response.status, response.headers["Location"]), (303, "/"))
        name, _, rest = response.headers["Set-Cookie"].partition("=")
        session, _, flags = rest.partition(";")
        self.assertEqual((name, flags.strip()), ("edgeconnect_preconfig_gui_{}".format(self.port), "HttpOnly; SameSite=Strict; Path=/"))
        self.assertNotEqual(session, self.server.token)
        response, text = self.request("GET", "/", cookie="{}={}".format(name, session))
        self.assertEqual((response.status, response.headers["Content-Security-Policy"].split(";")[0]), (200, "default-src 'self'"))
        self.assertIn("<title>EdgeConnect Preconfig</title>", text)
        response, text = self.request("GET", "/?token=" + self.server.token, cookie=False)  # another account that read the link
        self.assertEqual(response.status, 401)
        self.assertIn("already used", text)
        response, _ = self.request("GET", "/?token=" + self.server.token, cookie="{}={}".format(name, session))  # the same browser again
        self.assertEqual((response.status, response.headers["Location"]), (303, "/"))
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.request("GET", "/api/tasks", cookie="{}={}".format(name, self.server.token))[0].status, 401)

    def test_missing_or_wrong_cookie_is_refused_everywhere(self):
        for path in ("/", "/index.html", "/api/tasks", "/api/orchestrators", "/api/paths", "/api/builds", "/api/builds/h1/t/csv",
                     "/api/tasks/task-1", "/api/tasks/task-1/events", "/api/tasks/task-1/result"):
            for cookie in (False, "edgeconnect_preconfig_gui_{}=guess".format(self.port), "edgeconnect_preconfig_gui_1=" + self.server.session):
                self.assertEqual(self.request("GET", path, cookie=cookie)[0].status, 401, path)
        self.assertEqual(self.request("POST", "/api/tasks", {"action": "list", "orchestrator": "lab"}, cookie=False)[0].status, 401)
        self.assertEqual(self.server.tasks.list(), [])

    def test_dns_rebinding_host_is_refused_even_with_the_cookie(self):
        for host in ("evil.example:{}".format(self.port), "127.0.0.1", "localhost:1", "127.0.0.1.evil.example:{}".format(self.port)):
            self.assertEqual(self.request("GET", "/api/tasks", host=host)[0].status, 421, host)
            self.assertEqual(self.request("POST", "/api/tasks", {"action": "list", "orchestrator": "lab"}, host=host)[0].status, 421, host)
        for host in ("localhost:{}".format(self.port), "[::1]:{}".format(self.port)):
            self.assertEqual(self.request("GET", "/api/tasks", host=host)[0].status, 200, host)
        self.assertEqual(self.server.tasks.list(), [])

    def test_cross_origin_or_non_json_posts_are_refused(self):
        body = {"action": "list", "orchestrator": "lab"}
        self.assertEqual(self.request("POST", "/api/tasks", body, origin="http://evil.example")[0].status, 403)
        self.assertEqual(self.request("POST", "/api/tasks", body, origin=False)[0].status, 403)
        self.assertEqual(self.request("POST", "/api/tasks", body, content_type="text/plain")[0].status, 415)
        self.assertEqual(self.request("POST", "/api/tasks", body, content_type="application/x-www-form-urlencoded")[0].status, 415)
        self.assertEqual(self.server.tasks.list(), [])
        self.assertEqual(self.request("POST", "/api/tasks", body)[0].status, 201)

    def test_an_upload_over_http_with_its_event_stream_and_no_key_in_any_response(self):
        with mock.patch.object(common, "build_opener", lambda *handlers: Opener({})):
            response, text = self.request("GET", "/api/orchestrators")
        fleet = json.loads(text)
        self.assertEqual(sorted(fleet), ["not_listed", "note", "orchestrators", "warnings"])
        self.assertEqual([(o["name"], o["status"], o["writes"]) for o in fleet["orchestrators"]], [("lab", "green", True), ("other", "green", False)])
        task = self.post_task({"action": "upload", "orchestrator": "lab", "build": "h1", "site": "t", "csv_text": self.csv_text([("site-h", {})])})
        self.wait(task, "awaiting_approval")
        self.assertEqual(self.request("POST", "/api/tasks/{}/approve".format(task.id), {"answers": ["lab"]})[0].status, 202)
        events = self.events(task)
        self.assertEqual([e["state"] for e in events if "state" in e], ["queued", "running", "awaiting_approval", "uploading", "done"])
        self.assertIn("uploading sites.csv on lab ...", [e["line"] for e in events if "line" in e])
        self.assertEqual(events[-1]["task"]["state"], "done")
        self.assertEqual(json.loads(self.request("GET", "/api/tasks/{}/result".format(task.id))[1])["result"]["counts"], {"CREATED": 1})
        self.assertEqual(self.request("POST", "/api/tasks/{}/cancel".format(task.id), {})[0].status, 409)
        for path in ("/api/tasks", "/api/tasks/" + task.id, "/api/paths", "/api/builds", "/api/builds/h1/t/csv", "/api/builds/h1/t/template"):
            self.assertEqual(self.request("GET", path)[0].status, 200, path)
        self.assertEqual([body for body in self.bodies if KEY in body], [])

    def test_a_secret_csv_cell_never_appears_in_any_event_or_task_json(self):
        site = self.build / "site-secret"
        shutil.rmtree(str(site), ignore_errors=True)
        shutil.copytree(str(self.site), str(site))
        meta = json.loads((site / "columns.json").read_text(encoding="utf-8"))
        meta["columns"]["contact_name"]["secret"] = True
        (site / "columns.json").write_text(json.dumps(meta), encoding="utf-8")
        secret = 'Hunter"2 <&> Secret'  # holds a " that breaks YAML parsing; /validate quotes the document back
        sheet = self.csv_text([("site-s", {"contact_name": secret, "site": "REJECT"}), ("site-ok", {"contact_name": secret})])
        for body in ({"action": "validate", "build": "h1", "site": "secret", "csv_text": sheet},
                     {"action": "upload", "orchestrator": "lab", "build": "h1", "site": "secret", "csv_text": sheet}):
            task = self.post_task(body)
            self.wait(task, "done", "failed", "awaiting_approval")
            if task.state == "awaiting_approval":
                self.request("POST", "/api/tasks/{}/approve".format(task.id), {"answers": ["lab"]})
            events = self.events(task)
            self.assertTrue([e for e in events if "line" in e], "the command's lines are streamed")
            for path in ("/api/tasks", "/api/tasks/" + task.id, "/api/tasks/{}/result".format(task.id)):
                self.request("GET", path)
        self.assertIn("<SECRET:contact_name>", "".join(self.bodies))
        for part in ("Hunter", "2 <&> Secret", "2 \\u003c\\u0026\\u003e Secret"):
            self.assertEqual([body for body in self.bodies if part in body], [], part)

    def test_unknown_paths_and_bad_bodies(self):
        for path in ("/api/nope", "/api/tasks/task-9", "/api/tasks/x/y/z", "/missing.js", "/api/builds/h1/t", "/api/builds/h1/t/columns"):
            self.assertEqual(self.request("GET", path)[0].status, 404, path)
        self.assertEqual(self.request("GET", "/api/builds/h1/..%2F..%2Fh1/csv")[0].status, 400)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/tasks", body=b"{not json", headers={"Host": self.host, "Cookie": self.cookie, "Origin": "http://" + self.host,
                                                                             "Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 400)
        connection.close()


class LaunchTests(unittest.TestCase):
    def test_folders_are_absolute_and_the_run_log_folder_is_fixed_once(self):
        with mock.patch.dict(gui_server.os.environ, {"EC_DOTENV": "some/.env", "EDGECONNECT_REPORT_DIR": "reports"}):
            paths = gui_server.folders()
            self.assertEqual(gui_server.os.environ["EDGECONNECT_REPORT_DIR"], paths["reports"])
        self.assertTrue(all(Path(p).is_absolute() for p in paths.values()), paths)
        self.assertTrue(paths["dotenv"].endswith("some/.env") and paths["output"].endswith("preconfig-output"))
        with mock.patch.dict(gui_server.os.environ, {"EDGECONNECT_REPORT_DIR": ""}):
            self.assertIsNone(gui_server.folders()["reports"])  # an empty value keeps the log off

    def test_main_binds_to_loopback_prints_the_folders_and_opens_the_browser(self):
        opened = []
        with mock.patch.object(gui_server, "open_browser", opened.append), mock.patch.object(gui_server.GuiServer, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.dict(gui_server.os.environ, {"EC_DOTENV": "/nonexistent/.env", "EDGECONNECT_REPORT_DIR": ""}), \
                mock.patch("sys.stdout") as out, mock.patch("sys.stderr"):
            self.assertEqual(gui_server.main(["--port", "0"]), 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("http://127.0.0.1:") and "?token=" in opened[0])
        self.assertIn(".env:              /nonexistent/.env", printed)
        self.assertIn(opened[0], printed)


if __name__ == "__main__":
    unittest.main()
