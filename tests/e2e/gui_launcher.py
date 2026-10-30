"""Start the preconfig GUI server in this process with the Orchestrator faked, for the browser tests (docs/gui/BUILD_PLAN.md step 11).

The fake is the engine tests' (tests/test_gui_engine.py `Echoing`, over the golden Orchestrator of tests/test_golden.py): it lists
h1 and h2, answers /validate and holds the golden preconfigs. A build reads the golden synthetic capture instead of an appliance,
and a compared site reads the same capture under its own name, so compare gives it a row and its own values.
Nothing leaves this computer. The .env, output, work and run-log folders are made inside `folder`.

    python3 tests/e2e/gui_launcher.py     # prints the sign-in link, for a look by hand; Ctrl+C stops it
"""
import copy
import dataclasses
import functools
import itertools
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_gui_tasks  # noqa: E402,F401  guard-ok: puts toolkit/scripts and toolkit/gui on sys.path, as the other GUI tests
from test_gui_tasks import ENV  # noqa: E402  guard-ok: the GUI tests' .env (lab is the validator)
from test_gui_engine import Echoing  # noqa: E402  guard-ok: the engine tests' fake Orchestrator
from test_golden import PRECONFIGS, write_capture  # noqa: E402  guard-ok: the golden preconfigs and synthetic capture
import capture  # noqa: E402  guard-ok: imported after the sys.path insert
import cli  # noqa: E402  guard-ok: imported after the sys.path insert
import fleet  # noqa: E402  guard-ok: imported after the sys.path insert
import gui_server  # noqa: E402  guard-ok: imported after the sys.path insert
import upload  # noqa: E402  guard-ok: imported after the sys.path insert
import validate  # noqa: E402  guard-ok: imported after the sys.path insert
from gui_tasks import TaskManager  # noqa: E402  guard-ok: imported after the sys.path insert


@dataclasses.dataclass(frozen=True)
class Config:
    """What fleet.check_all replaces on a client (its timeout); the golden fake's SimpleNamespace is not a dataclass."""
    base_url: str
    timeout: float = 5.0


def start(folder: Path):
    """The server, serving in a thread, and the patches holding the fake; stop with `stop(server, patches)`."""
    folder = Path(folder).resolve()
    dotenv = folder / ".env"
    dotenv.write_text(ENV, encoding="utf-8")
    orch = Echoing()
    orch.config = Config(orch.config.base_url)
    orch.existing[:] = copy.deepcopy(PRECONFIGS)
    numbers = itertools.count(1)

    def reading(orchestrator, appliance, role, *args, **kwargs):
        """capture.capture: the golden capture of h1; a compared site (role sample) h2 reads it as h2 would, HA peers swapped."""
        made = write_capture(folder / "work" / "cap-{}".format(next(numbers)))
        if role != "reference" and appliance.lower() in ("h2", "8.ne"):
            for path in [made / "run.json"] + sorted((made / "capture").glob("*.json")):
                swap = {'"h1"': '"h2"', "9.NE": "8.NE", "8.NE": "9.NE", '"role": "reference"': '"role": "sample"'}
                text = path.read_text(encoding="utf-8")
                path.write_text(re.sub("|".join(map(re.escape, swap)), lambda found: swap[found.group(0)], text), encoding="utf-8")
        return made

    environ = {k: v for k, v in os.environ.items() if k.upper() != "EC_VALIDATORS" and not k.lower().endswith("_gui_preconfig_uploads")}
    environ["EDGECONNECT_REPORT_DIR"] = str(folder / "reports")  # so "See what changed" has runs
    patches = [mock.patch.dict(os.environ, environ, clear=True),
               mock.patch.object(capture, "capture", side_effect=reading),
               mock.patch.object(cli, "client_for", return_value=orch), mock.patch.object(validate, "client_for", return_value=orch),
               mock.patch.object(upload, "client_for", return_value=orch),
               mock.patch.object(gui_server, "check_all", functools.partial(fleet.check_all, client_factory=lambda name: orch))]
    for patch in patches:
        patch.start()
    server = gui_server.GuiServer(TaskManager(str(dotenv), str(folder), str(folder / "work")), 0, str(folder / "reports"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, patches


def stop(server, patches) -> None:
    server.shutdown()
    server.server_close()
    for patch in reversed(patches):
        patch.stop()


if __name__ == "__main__":
    server, patches = start(Path(tempfile.mkdtemp()))
    print(server.url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stop(server, patches)
