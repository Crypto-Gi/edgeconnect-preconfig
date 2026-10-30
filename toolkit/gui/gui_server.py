"""The local GUI server (docs/gui/BUILD_PLAN.md step 5): standard-library HTTP on 127.0.0.1 only, for one person at this computer.

Copied from the automation GUI's gui_server.py (7af5e9d) with its security unchanged. Who may call it: whoever opened the
launch URL printed in the terminal. The link works once (other accounts on the computer can read it from the browser's
command line) and gives an HttpOnly, SameSite=Strict session cookie, a different secret, that every page, API call and
progress stream needs. Requests naming any other Host (DNS rebinding) and state-changing requests from any other Origin are
refused, every POST must be JSON, and no CORS headers are ever sent. API keys stay in this process: no response carries them.
Not copied: the run log tables (gui_tables, step 7) and Undo (gui_revert, D7).
"""
import argparse
import dataclasses
import hmac
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import common
import runlog
from fleet import check_all
from gui_tasks import FINAL, TaskError, TaskManager, dumps

STATIC = Path(__file__).with_name("static")
MAX_BODY = 5 * 1024 * 1024
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tasks: TaskManager, port: int = 0, report_dir: Optional[str] = None) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.token = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.token_used = False
        self.login_lock = threading.Lock()
        self.tasks = tasks
        self.report_dir = report_dir
        port = self.server_address[1]
        self.hosts = {"127.0.0.1:{}".format(port), "localhost:{}".format(port), "[::1]:{}".format(port)}
        self.url = "http://127.0.0.1:{}/?token={}".format(port, self.token)
        self.cookie = "edgeconnect_preconfig_gui_{}".format(port)  # cookies ignore the port, so two GUIs on one computer need different names


class Handler(BaseHTTPRequestHandler):
    server: GuiServer
    server_version = "edgeconnect-preconfig-gui"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # the launch token is in a URL; keep request lines out of the terminal
        pass

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        try:
            self._guard(method)
            parts = urlsplit(self.path)
            path = parts.path
            if method == "GET" and path == "/" and "token" in parse_qs(parts.query):
                return self._login(parse_qs(parts.query)["token"][0])
            self._require_cookie()
            if method == "GET" and path.startswith("/api/"):
                return self._get_api(path)
            if method == "POST" and path.startswith("/api/"):
                return self._post_api(path)
            if method == "GET":
                return self._static(path)
            raise TaskError("not found", 404)
        except TaskError as error:
            self._json(error.status, {"error": str(error)})

    def _guard(self, method: str) -> None:
        host = self.headers.get("Host", "")
        if host not in self.server.hosts:
            raise TaskError("this server only answers to 127.0.0.1, localhost or [::1] on its own port", 421)
        if method != "GET" and self.headers.get("Origin") != "http://" + host:
            raise TaskError("cross-origin request refused", 403)

    def _has_cookie(self) -> bool:
        for item in self.headers.get("Cookie", "").split(";"):
            name, _, value = item.strip().partition("=")
            if name == self.server.cookie and hmac.compare_digest(value.encode(), self.server.session.encode()):
                return True
        return False

    def _require_cookie(self) -> None:
        if self._has_cookie():
            return
        raise TaskError("open the GUI with the link printed in the terminal", 401)

    def _login(self, token: str) -> None:
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            raise TaskError("this link is not valid for this GUI; use the link printed in the terminal", 401)
        with self.server.login_lock:
            used, self.server.token_used = self.server.token_used, True
        if used and self._has_cookie():
            return self._redirect_home()  # the same browser opening the link again
        if used:
            raise TaskError("this link was already used, and it works only once; to open the GUI in another browser, stop it with Ctrl+C and start it again", 401)
        self._redirect_home("{}={}; HttpOnly; SameSite=Strict; Path=/".format(self.server.cookie, self.server.session))

    def _redirect_home(self, cookie: str = "") -> None:
        self.send_response(303)
        self._security_headers()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Location", "/")  # drops the token from the address bar
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _get_api(self, path: str) -> None:
        tasks = self.server.tasks
        if path == "/api/orchestrators":
            try:
                fleet = check_all(tasks.dotenv)
            except common.ConfigurationError as error:
                raise TaskError(str(error)) from None
            return self._json(200, dataclasses.asdict(fleet))
        if path == "/api/paths":
            return self._json(200, dict(tasks.paths(), reports=self.server.report_dir))
        if path == "/api/tasks":
            return self._json(200, {"tasks": [task.view() for task in tasks.list()]})
        if path == "/api/builds":
            return self._json(200, tasks.builds())
        if path.startswith("/api/builds/"):
            parts = [unquote(part) for part in path.split("/")[3:]]
            if len(parts) != 3:
                raise TaskError("not found", 404)
            found = tasks.site_file(*parts)
            return self._download(found.name, found.read_bytes(), "text/csv; charset=utf-8" if found.suffix == ".csv" else "text/plain; charset=utf-8")
        # /api/runs (the run log tables) comes with gui_tables in step 7; compare and preconfigs with their tasks (R6, step 10).
        task_id, action = _task_path(path)
        if action == "":
            return self._json(200, tasks.get(task_id).view())
        if action == "events":
            return self._events(task_id)
        if action == "result":
            return self._json(200, {"result": tasks.get(task_id).full_result()})
        raise TaskError("not found", 404)

    def _post_api(self, path: str) -> None:
        if self.headers.get_content_type() != "application/json":
            raise TaskError("send JSON (Content-Type: application/json)", 415)
        body = self._body()
        tasks = self.server.tasks
        if path == "/api/tasks":
            return self._json(201, tasks.create(body).view())
        task_id, action = _task_path(path)
        if action == "approve":
            return self._json(202, tasks.approve(task_id, body.get("answers")).view())
        if action == "cancel":
            return self._json(202, tasks.cancel(task_id).view())
        raise TaskError("not found", 404)

    def _body(self) -> Mapping[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            raise TaskError("request body too large or without a length", 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TaskError("request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise TaskError("request body must be a JSON object")
        return body

    def _events(self, task_id: str) -> None:
        """Server-sent events: every state change and printed line of the task, then the stream closes when the task is finished."""
        task = self.server.tasks.get(task_id)
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        sent = 0
        while True:
            with task.changed:
                if sent == len(task.events) and task.state not in FINAL:
                    task.changed.wait(15)
                events, finished = task.events[sent:], task.state in FINAL
            try:
                for event in events:
                    self.wfile.write(b"data: " + dumps(dict(event, task=task.view())) + b"\n\n")
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
            except OSError:
                return  # the browser went away
            sent += len(events)
            if finished and sent == len(task.events):
                return

    def _static(self, path: str) -> None:
        name = "index.html" if path == "/" else path.lstrip("/")
        files = {item.name: item for item in STATIC.iterdir() if item.is_file()}
        if name not in files:
            raise TaskError("not found", 404)
        self._send(200, files[name].read_bytes(), _TYPES.get(files[name].suffix, "application/octet-stream"))

    def _json(self, status: int, value: Any) -> None:
        self._send(status, dumps(value), "application/json")

    def _download(self, name: str, body: bytes, content_type: str) -> None:
        self._send(200, body, content_type, {"Content-Disposition": 'attachment; filename="{}"'.format(name)})

    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Mapping[str, str]] = None) -> None:
        self.send_response(status)
        self._security_headers()
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        for name, value in _HEADERS.items():
            self.send_header(name, value)


def _task_path(path: str) -> Tuple[str, str]:
    """/api/tasks/<id>[/<action>] -> (id, action)."""
    parts = path.split("/")
    if len(parts) not in (4, 5) or parts[:3] != ["", "api", "tasks"] or not parts[3]:
        raise TaskError("not found", 404)
    return parts[3], parts[4] if len(parts) == 5 else ""


def open_browser(url: str) -> None:
    """On WSL, open the Windows browser (wslview, else explorer.exe); elsewhere the system browser. Failures are ignored: the URL is printed."""
    if "microsoft" in platform.release().lower():  # WSL 1 and 2 kernels both name Microsoft
        command = shutil.which("wslview") or shutil.which("explorer.exe")
        if command:
            subprocess.Popen([command, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    webbrowser.open(url)


def folders() -> Dict[str, Optional[str]]:
    """The folders the GUI's runs use, as absolute paths: the .env, preconfig-output/, preconfig-work/ and the run log (None: off).
    The run log folder is fixed in EDGECONNECT_REPORT_DIR here, once at launch, so a run never reads it relative to a folder."""
    report = runlog.report_dir()
    if report is not None:
        os.environ[runlog.REPORT_DIR_ENV] = str(report.resolve())
    return {"dotenv": os.path.abspath(common._find_dotenv()),  # EC_DOTENV, as the launcher sets it; no public way to name it
            "output": str(Path.cwd().resolve() / "preconfig-output"), "work": str(common.WORK_DIR.resolve()),
            "reports": str(report.resolve()) if report is not None else None}


def serve(port: int = 0, browser: bool = True) -> int:
    paths = folders()
    tasks = TaskManager(paths["dotenv"], str(Path(paths["output"]).parent), paths["work"])
    server = GuiServer(tasks, port, paths["reports"])
    lines: List[str] = ["  .env:              {}".format(paths["dotenv"]), "  output:            {}".format(paths["output"]),
                        "  work:              {}".format(paths["work"]), "  run log:           {}".format(paths["reports"] or "off (EDGECONNECT_REPORT_DIR is empty)")]
    print("edgeconnect-preconfig GUI\n\n{}\n\nOpen this link in your browser (it is your sign-in, works once, and must not be shared):\n\n  {}\n\n"
          "Press Ctrl+C to stop.".format("\n".join(lines), server.url), flush=True)
    if browser:
        open_browser(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if any(task.state == "uploading" for task in tasks.list()):
            print("waiting for the upload that is sending to finish ...", file=sys.stderr, flush=True)
        tasks.close()  # no other task starts uploading
        print("GUI stopped.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m toolkit.gui", description="Start the preconfig GUI on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    parser.add_argument("--no-browser", action="store_true", help="only print the link; do not open a browser")
    args = parser.parse_args(argv)
    return serve(args.port, browser=not args.no_browser)
