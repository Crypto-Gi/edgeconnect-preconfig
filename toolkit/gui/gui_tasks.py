"""GUI tasks (docs/gui/BUILD_PLAN.md step 5): each action is one engine call in its own thread, as a task the browser follows.

  list, preconfigs, build, variables, validate, render, validate_yaml:  queued -> running -> done | failed | rejected
  upload (a sites CSV):  queued -> running (the check) -> awaiting_approval -> uploading -> done | failed | rejected
                                  \\-> done | failed | rejected     \\-> rejected | expired | cancelled
  upload_yaml:           queued -> uploading -> done | failed | rejected   (no approval: decision 0017)

Reads run in parallel. A run on a build folder or one of its site folders holds that appliance's lock (staging folders are
wiped at start and keep_previous renames the folder), and an upload holds its Orchestrator's lock from its check to its end,
so a second one waits as queued. Nothing here applies a preconfig (decision 0008), hence `uploading`, never automation's
`applying`. Only a task not yet uploading can be cancelled; a running read cannot be stopped midway, so it ends cancelled.

Every run gets the absolute .env, output and work folders as its own RunContext (engine.run makes it in the task's thread);
os.environ is never changed here. Files a person imports come in the request as text and are saved, mode 0600, under names
chosen here, in the task's own new folder preconfig-output/runs/<run_id>/.
"""
import itertools
import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

import cli
import common
import engine
from approval import APPROVAL_SECONDS, ApprovalError, GuiApprovals
from fleet import profiles

READS = {"list", "preconfigs", "build", "variables", "validate", "render", "validate_yaml"}
UPLOADS = {"upload", "upload_yaml"}
# compare joins READS with its engine helper (R6 pending).
FINAL = {"done", "failed", "rejected", "expired", "cancelled"}
_OPEN = {"queued", "running", "awaiting_approval"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # a build or site type folder name as the GUI lists it: no path in it


class TaskError(Exception):
    """A task request the GUI refuses; the message is shown as is."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Task:
    def __init__(self, task_id: str, action: str, orchestrator: str, folder: Optional[Path]) -> None:
        self.id, self.action, self.orchestrator, self.folder = task_id, action, orchestrator, folder
        self.state = "queued"
        self.created = time.time()
        self.message = ""
        self.result: Optional[engine.Result] = None
        self.events: List[Dict[str, Any]] = []
        self.answers: Optional[List[str]] = None
        self.deadline: Optional[float] = None
        self.cancelled = False
        self.changed = threading.Condition()

    def set(self, state: str, message: str = "", result: Optional[engine.Result] = None) -> None:
        with self.changed:
            self.state, self.message = state, message
            if result is not None:
                self.result = result
            self.events.append({"state": state, "message": message, "time": time.time()})
            self.changed.notify_all()

    def line(self, text: str) -> None:
        """One printed line of the running command (engine.run scrubbed it), as it comes."""
        with self.changed:
            self.events.append({"line": text, "time": time.time()})
            self.changed.notify_all()

    def progress(self, done: int, text: str) -> None:
        """One request of a build's capture, from its request log, as it is read: the count so far and `GET /path ok`."""
        with self.changed:
            self.events.append({"progress": text, "done": done, "time": time.time()})
            self.changed.notify_all()

    def view(self) -> Dict[str, Any]:
        """The task as the browser sees it; the whole Result (rows, issues, text) comes from result()."""
        found = self.result
        result = None if found is None else {
            "exit_code": found.exit_code, "status": found.status, "counts": found.counts, "approval": found.approval and dict(found.approval, counts={
                "ready": found.counts.get("PLANNED", 0), "invalid": found.counts.get("INVALID", 0), "conflict": found.counts.get("SKIPPED", 0),
                "rows": len(found.rows)}),  # so the page can say "uploads N of M"
            "issues": len(found.issues), "rows": len(found.rows), "files": len(found.files)}
        return {"id": self.id, "action": self.action, "orchestrator": self.orchestrator, "state": self.state, "message": self.message,
                "created": self.created, "deadline": self.deadline, "folder": str(self.folder) if self.folder else None, "result": result}

    def full_result(self) -> Optional[Dict[str, Any]]:
        # shortcut: the whole Result in one answer; gui_tables pages issues and rows in step 7.
        return None if self.result is None else asdict(self.result)


class TaskManager:
    def __init__(self, dotenv: str, out_parent: str, work_dir: str, approval_seconds: float = APPROVAL_SECONDS) -> None:
        """dotenv: the .env every run reads; out_parent: the folder holding preconfig-output/; work_dir: captures. All absolute."""
        self.dotenv, self.out_parent, self.work_dir = (os.path.abspath(str(p)) for p in (dotenv, out_parent, work_dir))
        self.out_root = Path(self.out_parent) / "preconfig-output"
        self.approval_seconds = approval_seconds
        self.approvals = GuiApprovals(seconds=approval_seconds)
        self.tasks: Dict[str, Task] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}
        self.closing = False

    # ------------------------------------------------------------ the browser's calls

    def create(self, request: Mapping[str, Any]) -> Task:
        action = request.get("action")
        if action not in READS | UPLOADS:
            raise TaskError("unknown action {!r}; use one of {}".format(action, ", ".join(sorted(READS | UPLOADS))))
        named = self._orchestrator(request.get("orchestrator"), required=action not in ("variables", "validate", "render"))
        with self._lock:
            task_id = "task-{}".format(next(self._ids))
        imports = action in ("validate", "render", "validate_yaml", "upload", "upload_yaml")
        task = Task(task_id, action, named, self._run_folder() if imports else None)
        try:
            work = self._prepare(task, request)
        except TaskError:
            if task.folder:
                shutil.rmtree(str(task.folder), ignore_errors=True)
            raise
        with self._lock:
            self.tasks[task_id] = task
        task.set("queued")
        threading.Thread(target=work, name=task_id, daemon=True).start()
        return task

    def get(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskError("no task {}".format(task_id), 404)
        return task

    def list(self) -> List[Task]:
        return sorted(self.tasks.values(), key=lambda task: task.created)

    def approve(self, task_id: str, answers: Any) -> Task:
        """Hand the typed confirmations (the Orchestrator nickname) to an upload awaiting approval."""
        task = self.get(task_id)
        if not isinstance(answers, list) or not all(isinstance(answer, str) for answer in answers):
            raise TaskError("answers must be a list of the typed confirmations")
        with task.changed:
            if task.state != "awaiting_approval" or task.answers is not None:
                raise TaskError("task {} is {}, not awaiting approval".format(task_id, task.state), 409)
            task.answers = answers
            task.changed.notify_all()
        return task

    def cancel(self, task_id: str) -> Task:
        task = self.get(task_id)
        with task.changed:
            if task.state not in _OPEN or task.answers is not None:
                raise TaskError("task {} is {} and can no longer be cancelled".format(task_id, task.state), 409)
            task.cancelled = True
            task.changed.notify_all()
        return task

    def builds(self) -> Dict[str, Any]:
        """The earlier builds in preconfig-output/ (not .building-*, .incomplete-* or *.previous-*), newest first, with their site types."""
        found = []
        for folder in sorted(self.out_root.iterdir()) if self.out_root.is_dir() else []:
            if not (folder.is_dir() and _NAME.match(folder.name) and ".previous-" not in folder.name and (folder / "build.json").is_file()):
                continue
            try:
                info = json.loads((folder / "build.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            sites = [site.name[len("site-"):] for site in sorted(folder.glob("site-*"))
                     if site.is_dir() and _NAME.match(site.name) and ".previous-" not in site.name and (site / "{}.jinja2".format(site.name[len("site-"):])).is_file()]
            url = "/api/builds/{}/{{}}/{{}}".format(quote(folder.name))
            found.append({"appliance": folder.name, "site_type": str(info.get("site_type") or ""), "orchestrator": str(info.get("orchestrator") or ""), "built": str(info.get("built") or ""),
                          "site_types": [{"name": name, "template_url": url.format(quote(name), "template"), "csv_url": url.format(quote(name), "csv")} for name in sites]})
        return {"folder": str(self.out_root), "builds": sorted(found, key=lambda build: build["built"], reverse=True)}

    def site_file(self, build: str, site: str, which: str) -> Path:
        """A site type's template or sites CSV (its example.csv until sites.csv is made), for [Download template] and [Download CSV]."""
        folder = self._site({"build": build, "site": site})
        path = folder / ("{}.jinja2".format(site) if which == "template" else "sites.csv")
        if which == "csv" and not path.is_file():
            path = folder / "example.csv"
        if which not in ("template", "csv") or not path.is_file():
            raise TaskError("not found", 404)
        return path

    def build_text(self, build: str, which: str) -> Dict[str, Any]:
        """A build's reference.yaml or summary.md, every secret shown as <SECRET>; where each value is comes from the dry run."""
        folder = self._build({"build": build})
        reference = folder / "reference.yaml"
        if which not in ("reference", "summary") or not reference.is_file():
            raise TaskError("not found", 404)
        text = (reference if which == "reference" else folder / "summary.md").read_text(encoding="utf-8")
        return {"text": engine.hide_secrets(reference, text)}

    def close(self) -> None:
        """Ctrl+C: from now on no task starts uploading (it ends cancelled), and an upload already sending is waited for."""
        with self._lock:
            self.closing = True
        for task in self.list():
            with task.changed:
                while task.state == "uploading":
                    task.changed.wait(1)

    def _start_upload(self, task: Task) -> bool:
        """Move to uploading, unless the GUI is closing; under the lock close() takes, so close() waits for every upload it lets start."""
        with self._lock:
            if self.closing:
                task.set("cancelled", "the GUI is closing; nothing was uploaded")
                return False
            task.set("uploading")
            return True

    def paths(self) -> Dict[str, str]:
        return {"dotenv": self.dotenv, "output": str(self.out_root), "work": self.work_dir}

    # ------------------------------------------------------------ checking a request

    def _orchestrator(self, name: Any, required: bool) -> str:
        if name in (None, "") and not required:
            return ""
        try:
            configured = profiles(common.env_map(self.dotenv))
        except common.ConfigurationError as error:
            raise TaskError(str(error)) from None
        name = name.strip().lower() if isinstance(name, str) else ""
        if name not in configured:
            raise TaskError("unknown Orchestrator {!r}; configured: {}".format(name, ", ".join(configured) or "none"))
        return name

    def _run_folder(self) -> Path:
        """The task's own new folder for what it imports and writes; its name comes from here, never from the browser."""
        runs = self.out_root / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        folder = runs / "gui-{}-{}".format(time.strftime("%Y%m%dT%H%M%S"), secrets.token_hex(4))
        folder.mkdir(mode=0o700)
        return folder

    def _build(self, request: Mapping[str, Any]) -> Path:
        return self._folder(self.out_root, request.get("build"), "build")

    def _site(self, request: Mapping[str, Any]) -> Path:
        name = request.get("site")
        folder = self._folder(self._build(request), "site-{}".format(name) if isinstance(name, str) else None, "site")
        if not (folder / "columns.json").is_file():  # cli.site_folder's refusal: without it the per-cell checks would not run
            raise TaskError("{} has no template (columns.json): give a build folder or a site-<name> folder".format(folder))
        return folder

    @staticmethod
    def _folder(parent: Path, name: Any, what: str) -> Path:
        if not isinstance(name, str) or not _NAME.match(name) or ".previous-" in name:
            raise TaskError("{} must name a {} folder in {}".format(what, what, parent))
        folder = parent / name
        if not folder.is_dir():
            raise TaskError("there is no {} folder {}".format(what, folder), 404)
        return folder

    @staticmethod
    def _text(request: Mapping[str, Any], key: str) -> str:
        text = request.get(key)
        if not isinstance(text, str) or not text.strip():
            raise TaskError("{} is required".format(key))
        return text

    @staticmethod
    def _save(folder: Path, name: str, text: str) -> Path:
        """Save an imported file as sent (no newline translation, so its bytes are what the approval covers), mode 0600."""
        path = folder / name
        with os.fdopen(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def _files(self, task: Task, request: Mapping[str, Any]) -> List[Path]:
        texts = request.get("files")
        if not isinstance(texts, list) or not texts or not all(isinstance(text, str) and text.strip() for text in texts):
            raise TaskError("files must list the text of each YAML file")
        return [self._save(task.folder, "file-{}.yml".format(n), text) for n, text in enumerate(texts, 1)]

    @staticmethod
    def _optional(request: Mapping[str, Any], key: str) -> Optional[str]:
        value = request.get(key)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise TaskError("{} must be text".format(key))
        return value

    def _prepare(self, task: Task, request: Mapping[str, Any]) -> Callable[[], None]:
        """Check the request now (the browser hears about bad input at once) and return the work for the task's thread."""
        context = {"dotenv": self.dotenv, "out_dir": Path(self.out_parent), "work_dir": Path(self.work_dir), "on_line": task.line}
        opt, action, orch = (lambda key: self._optional(request, key)), task.action, task.orchestrator
        if action == "list":
            return self._read(task, [], lambda: engine.list_appliances(orch, opt("search"), **context))
        if action == "preconfigs":  # the list, or with show (a name or id from it) one preconfig's YAML, redacted by the CLI
            return self._read(task, [], lambda: engine.preconfigs(orch, opt("show"), **context))
        if action == "build":
            # The card sends both fields from `list`; the CLI is given `hostname or nepk`, and names the folder `hostName or nePk`.
            # Both come from `list`: nepk always, hostname empty only for a box without one. One box sent once by hostname and once
            # by nePk queues on either identifier; the folder runs lock the key their folder maps to (_keys).
            hostname, nepk = ((request.get(key) if isinstance(request.get(key), str) else "").strip() for key in ("hostname", "nepk"))
            appliance = hostname or nepk
            if not nepk:
                raise TaskError("nepk is required, and hostname unless the appliance has none: send both as the appliance list gives them")
            for value in (hostname, nepk):  # capture.py and the build name folders after them
                if value and (not _NAME.match(value) or ".." in value):
                    raise TaskError("{!r} is not an appliance hostname or nePk as the appliance list shows it".format(value))
            keys = [_identity(orch, "host", hostname), _identity(orch, "ne", nepk)] if hostname else [_identity(orch, "ne", nepk)]
            validate_on = self._orchestrator(opt("validate_on"), required=False) or None
            log = Path(self.work_dir) / "reference" / orch  # where capture() keeps its request log while it reads
            return self._read(task, keys + ["appliance:" + cli.safe_name(appliance).lower()], lambda: _watched(task, log, appliance, lambda: engine.build(
                orch, appliance, out=self.out_root, validate_on=validate_on, target_release=opt("target_release"), target_ecos=opt("target_ecos"), **context)))
        if action == "variables":
            build = self._build(request)
            pick = request.get("pick") or "top10"
            if pick not in ("top10", "top20", "all"):
                raise TaskError("pick must be top10, top20 or all")
            settings, fixed = (self._names(request, key) for key in ("set", "fix"))
            save = request.get("save") is True
            name = opt("name") if save else None
            return self._read(task, self._keys(build), lambda: engine.variables(build, pick, settings, name=name, yes=save, fixed=fixed, dry_run=not save, **context))
        if action == "validate":  # `validate SITE --csv` as cli.validate_rows runs it, its rows written into the task's folder
            site, csv = self._site(request), self._save(task.folder, "sites.csv", self._text(request, "csv_text"))
            built = self._built(site.parent)
            nick = orch or str(built.get("validator") or "")
            return self._read(task, self._keys(site.parent), lambda: engine.render_site(
                site, csv, task.folder / "rendered", nick or None, substitute=nick != built.get("orchestrator"), **context))
        if action == "render":
            template = self._save(task.folder, "template.jinja2", self._text(request, "template_text"))
            csv = self._save(task.folder, "sites.csv", self._text(request, "csv_text"))
            validate_on = self._orchestrator(opt("validate_on"), required=False) or None
            out = task.folder / "rendered"
            return self._read(task, [], lambda: engine.render(template, csv, out, validate_on=validate_on, substitute=request.get("substitute") is True, **context))
        if action == "validate_yaml":
            files = self._files(task, request)
            return self._read(task, [], lambda: engine.validate_yaml(files, orch, opt("name"), opt("serial"), **context))
        if action == "upload_yaml":
            files = self._files(task, request)
            return self._locked(task, ["orchestrator:" + orch], lambda: self._upload_yaml(task, lambda: engine.upload_yaml(files, orch, opt("name"), opt("serial"), **context)))
        # upload: a sites CSV with a saved site folder, or with a template (written into this task's folder)
        if "template_text" in request:
            source: Dict[str, Any] = {"template": self._save(task.folder, "template.jinja2", self._text(request, "template_text")), "out": task.folder}
            keys = []
        else:
            site = self._site(request)
            source, keys = {"site_dir": site, "out": task.folder}, self._keys(site.parent)
        csv = self._save(task.folder, "sites.csv", self._text(request, "csv_text"))
        return self._locked(task, keys + ["orchestrator:" + orch], lambda: self._upload(
            task, lambda approver: engine.upload(csv, orch, approver=approver, **source, **context)))

    @staticmethod
    def _names(request: Mapping[str, Any], key: str) -> List[str]:
        value = request.get(key) or []
        if not isinstance(value, list) or not all(isinstance(item, str) and item and "," not in item for item in value):
            raise TaskError("{} must list column names or paths".format(key))
        return value

    @staticmethod
    def _built(build_folder: Path) -> Dict[str, Any]:
        """The build's build.json: which Orchestrator it was read from and validated on."""
        try:
            info = json.loads((build_folder / "build.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise TaskError("{} has no readable build.json".format(build_folder)) from None
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _keys(build_folder: Path) -> List[str]:
        """The locks of a run on a build folder or its site folders: the folder's, and the build's identity key the folder maps to
        (the CLI names it after the hostname, or the nePk when there is none), so it waits for a build of that box."""
        keys = ["appliance:" + build_folder.name.lower()]
        try:
            info = json.loads((build_folder / "build.json").read_text(encoding="utf-8"))
            box, orch = info.get("appliance") or {}, str(info.get("orchestrator") or "")
            host, nepk = str(box.get("hostName") or "").strip(), str(box.get("nePk") or "").strip()
        except (OSError, UnicodeError, ValueError, AttributeError):  # no readable build.json: the folder lock still holds
            return keys
        return keys + ([_identity(orch, "host", host)] if host else [_identity(orch, "ne", nepk)] if nepk else [])

    # ------------------------------------------------------------ the task's thread

    def _locked(self, task: Task, keys: Sequence[str], body: Callable[[], None]) -> Callable[[], None]:
        """Wait for every lock (in one order, so two tasks never hold one each) while the task can still be cancelled, then run."""
        def work() -> None:
            with self._lock:
                locks = [self._locks.setdefault(key, threading.Lock()) for key in sorted(set(keys))]
            held: List[threading.Lock] = []
            try:
                for lock in locks:
                    while not lock.acquire(timeout=0.2):
                        if task.cancelled:
                            return task.set("cancelled", "cancelled while waiting for another run on the same Orchestrator or folder")
                    held.append(lock)
                if task.cancelled:
                    return task.set("cancelled")
                body()
            except Exception as error:  # the browser hears of every failure; its text may hold a secret, so only the kind is shown
                task.set("failed", "the run stopped on an unexpected error ({})".format(type(error).__name__))
            finally:
                for lock in reversed(held):
                    lock.release()
        return work

    @staticmethod
    def _sealed(task: Task, result: engine.Result) -> engine.Result:
        """What the CLI wrote in the task's folder (rendered YAML) made 0600 too, before the browser hears the run ended; the
        folder is 0700 meanwhile."""
        if task.folder:
            for path in task.folder.rglob("*"):
                if path.is_file():
                    os.chmod(str(path), 0o600)
        return result

    def _read(self, task: Task, keys: Sequence[str], call: Callable[[], engine.Result]) -> Callable[[], None]:
        def body() -> None:
            task.set("running")
            result = self._sealed(task, call())
            if task.cancelled:  # it could not be stopped midway; it is not offered as finished
                return task.set("cancelled", "cancelled; what it wrote is kept", result)
            task.set(result.status, _summary(result), result)
        return self._locked(task, keys, body)

    def _upload_yaml(self, task: Task, call: Callable[[], engine.Result]) -> None:
        if not self._start_upload(task):
            return
        result = self._sealed(task, call())
        task.set(result.status, _summary(result), result)

    def _upload(self, task: Task, call: Callable[[Optional[Callable[[str], Any]]], engine.Result]) -> None:
        task.set("running", "checking the rows; nothing is uploaded yet")
        check = self._sealed(task, call(None))
        if task.cancelled:
            return task.set("cancelled", "cancelled; nothing was uploaded", check)
        if check.approval is None:  # refused, or no row ready to upload: the check is the outcome
            return task.set(check.status, _summary(check), check)
        task.deadline = time.time() + self.approval_seconds  # for the browser's countdown; the wait uses the monotonic clock
        task.set("awaiting_approval", "type the Orchestrator nickname {} to upload the ready rows".format(task.orchestrator), check)
        deadline = time.monotonic() + self.approval_seconds
        with task.changed:
            while task.answers is None and not task.cancelled and time.monotonic() < deadline:
                task.changed.wait(min(1.0, max(deadline - time.monotonic(), 0.0)))
            answers, cancelled = task.answers, task.cancelled
        task.deadline = None
        if cancelled:
            return task.set("cancelled", "cancelled; nothing was uploaded")
        if answers is None:
            return task.set("expired", "no approval within {:g} minutes; nothing was uploaded".format(self.approval_seconds / 60))
        try:
            approver = engine.approve(self.approvals, check.approval, answers)
        except ApprovalError as error:
            return task.set("rejected", "{}; nothing was uploaded".format(error))
        if not self._start_upload(task):
            return
        result = self._sealed(task, call(approver))
        task.set(result.status, _summary(result), result)


def _watched(task: Task, folder: Path, appliance: str, call: Callable[[], engine.Result]) -> engine.Result:
    """Run a build while handing each request of its capture's request log (.incomplete-<appliance>-<stamp>-<pid>/requests.jsonl,
    as capture.py names it) to the browser: only the method, path and status, never a message. The build's appliance lock keeps a
    second capture of it out; the exact name keeps out another box whose name starts with this one's (h1-b for h1)."""
    stop = threading.Event()
    own = re.compile(r"\.incomplete-" + re.escape(appliance) + r"-\d{8}T\d{6}Z-\d+$")

    def watch() -> None:
        log, offset, done = None, 0, 0
        while True:
            finished = stop.is_set()
            try:
                if log is None:
                    logs = [p for p in folder.glob(".incomplete-*/requests.jsonl") if own.match(p.parent.name) and p.stat().st_mtime >= task.created - 1]
                    log = max(logs, key=lambda p: p.stat().st_mtime) if logs else None
                if log is not None:
                    with log.open(encoding="utf-8") as handle:
                        handle.seek(offset)
                        text = handle.read()
                    complete = text[:text.rfind("\n") + 1]
                    offset += len(complete.encode("utf-8"))
                    for line in complete.splitlines():
                        item = json.loads(line)
                        done += 1
                        task.progress(done, "{} {} {}".format(item.get("method"), item.get("path"), item.get("status")))
            except (OSError, ValueError):  # renamed when the capture ends, or a line still being written
                pass
            if finished:
                return
            stop.wait(0.25)
    watcher = threading.Thread(target=watch, name=task.id + "-progress", daemon=True)
    watcher.start()
    try:
        return call()
    finally:
        stop.set()
        watcher.join(2)


def _identity(orchestrator: str, kind: str, value: str) -> str:
    return "appliance:{}:{}:{}".format(orchestrator, kind, value.lower())


def _summary(result: engine.Result) -> str:
    """The task's one-line message: the first issue, else the row counts, else the command's last line (all scrubbed)."""
    if result.issues:
        return str(result.issues[0].get("message", ""))
    if result.counts:
        return ", ".join("{} {}".format(n, outcome) for outcome, n in sorted(result.counts.items()))
    lines = [line for line in result.text.splitlines() if line.strip()]
    return lines[-1] if lines else result.status


def dumps(value: Any) -> bytes:
    return json.dumps(value, default=str, sort_keys=True).encode("utf-8")
