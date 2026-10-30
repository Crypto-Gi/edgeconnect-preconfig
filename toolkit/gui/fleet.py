"""Fleet status (docs/gui/BUILD_PLAN.md step 4): check every Orchestrator in .env at once and say green, amber or red, with a plain reason.

The same checks, timings and fields as the automation GUI's fleet.py, so an Orchestrator looks the same in both GUIs, but over
preconfig's own .env reader and client, so the GUI sees exactly what the CLI sees.
Green: connected and the key works (a 403 on GET /gms/versions is still green, as `list` treats it).
Amber: usable, with one thing to know (slow or busy).
Red: not usable as configured (bad settings, no answer, certificate, DNS, refused, key refused, server error).
Import it after putting toolkit/scripts and toolkit/gui on sys.path.
"""
import dataclasses
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Set
from urllib.parse import urlsplit

import common
from common import AuthenticationError, ConfigurationError, EdgeConnectError, PermissionDeniedError, ServerError, TransportError
from upload import orchestrator_identity

GREEN, AMBER, RED = "green", "amber", "red"
CHECK_SECONDS = 5.0
SLOW_SECONDS = 2.0
_SLOT = re.compile(r"^orch_(\d+)$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ON, _OFF = {"on", "true", "yes", "1"}, {"off", "false", "no", "0", ""}  # as the automation GUI reads <nickname>_gui_writes


@dataclasses.dataclass(frozen=True)
class Status:
    name: str
    url: str
    status: str
    reason: str
    release: str = ""
    id: str = ""            # the serial from /gmsserver/info; "" when it is a placeholder or cannot be read: known by its URL
    label: str = ""
    color: str = ""
    writes: bool = False    # GUI uploads are on: <nickname>_gui_preconfig_uploads (the automation field name, so both GUIs read alike)
    validator: bool = False  # /validate may run here: listed in EC_VALIDATORS
    seconds: float = 0.0


@dataclasses.dataclass(frozen=True)
class Fleet:
    orchestrators: List[Status]
    not_listed: List[str]  # nicknames with settings in .env but no orch_N line, as the CLI's profile list leaves them out
    note: str = ""


def profiles(env: Mapping[str, str]) -> List[str]:
    """The orch_N nicknames of a parsed .env (common.env_map), in slot order, lower-cased as client_for reads them."""
    slots = sorted((int(match.group(1)), value.strip().lower()) for key, value in env.items() for match in [_SLOT.match(key)] if match and value.strip())
    return list(dict.fromkeys(name for _, name in slots))


def check_all(
    dotenv_path: Optional[str],
    timeout: float = CHECK_SECONDS,
    client_factory: Callable[[str], Any] = common.client_for,
    clock: Callable[[], float] = time.monotonic,
) -> Fleet:
    """One Status per orch_N profile, in slot order; all are checked in parallel, each read gets one try of `timeout` seconds."""
    env = common.env_map(dotenv_path)
    # A thread starts without the caller's run context, so each worker runs inside one holding this .env.
    context = common.RunContext(dotenv=dotenv_path or common.run_dotenv(), env=env, validators=common.validators_in(env))
    names = profiles(env)
    unlisted = sorted({key[:-len("_base_url")] for key in env if key.endswith("_base_url")} - set(names))
    note = "{} {} settings in .env but no orch_N line, so {} not listed; add an orch_<N>=<nickname> line to list one".format(
        ", ".join(unlisted), "has" if len(unlisted) == 1 else "have", "it is" if len(unlisted) == 1 else "they are") if unlisted else ""

    def check(name: str) -> Status:
        with common.running(context):
            status = _check(name, env, context.validators, timeout, client_factory, clock)
        key = env.get("{}_api_key".format(name))
        if not key:
            return status
        return dataclasses.replace(status, **{field: value.replace(key, "<API_KEY>") for field, value in dataclasses.asdict(status).items() if isinstance(value, str)})

    if not names:
        return Fleet([], unlisted, note)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return Fleet(list(pool.map(check, names)), unlisted, note)


def _check(name: str, env: Mapping[str, str], validators: Set[str], timeout: float, client_factory: Callable[[str], Any], clock: Callable[[], float]) -> Status:
    url = _origin(env.get("{}_base_url".format(name), ""))
    validator = name in validators
    try:
        gui = _gui_settings(name, env)
        client = client_factory(name)
    except ConfigurationError as error:
        return Status(name, url, RED, "fix the settings: {}".format(error), validator=validator)
    url = _origin(client.config.base_url)
    client.config = dataclasses.replace(client.config, timeout=timeout)  # the client makes one try per request
    gui["validator"] = validator
    start = clock()
    try:
        try:
            versions = client.get("/gms/versions")
            release = str((versions.get("current") if isinstance(versions, dict) else None) or "?")
        except PermissionDeniedError:  # a key without that right still lists appliances (cli.py list)
            release = ""
        server_id = orchestrator_identity(client)["serial"] or ""
    except ServerError as error:
        if error.status == 429:  # preconfig's client reports 429 as a server error
            return Status(name, url, AMBER, "Orchestrator is busy (HTTP 429); check again shortly", **gui)
        return Status(name, url, RED, "Orchestrator answered with a server error (HTTP {})".format(error.status), **gui)
    except AuthenticationError as error:
        return Status(name, url, RED, "the API key was refused (HTTP {})".format(error.status), **gui)
    except TransportError as error:
        return Status(name, url, RED, _transport_reason(error, timeout), **gui)
    except EdgeConnectError as error:
        return Status(name, url, RED, str(error), **gui)
    except ValueError:
        return Status(name, url, RED, "the answer to GET /gms/versions is not JSON", **gui)
    seconds = round(clock() - start, 2)
    found = dict(release=release, id=server_id, seconds=seconds, **gui)
    connected = "connected (release {})".format(release) if release else "connected (release unknown: GET /gms/versions refused)"
    if seconds > SLOW_SECONDS:
        return Status(name, url, AMBER, "{}, but slow: {} s to answer".format(connected, seconds), **found)
    # Cloud-hosted Orchestrators have no unique serial number; their fixed address is their identity, so that is no warning.
    return Status(name, url, GREEN, connected + ("" if server_id else "; no unique serial number, so it is known by its address"), **found)


def _gui_settings(name: str, env: Mapping[str, str]) -> Dict[str, Any]:
    color = env.get("{}_gui_color".format(name), "")
    if color and not _COLOR.match(color):
        raise ConfigurationError("{}_gui_color must be a colour like #1f6feb".format(name))
    uploads = env.get("{}_gui_preconfig_uploads".format(name), "").strip().lower()
    if uploads not in _ON | _OFF:
        raise ConfigurationError("{}_gui_preconfig_uploads must be on or off".format(name))
    return {"label": env.get("{}_gui_label".format(name), ""), "color": color, "writes": uploads in _ON}


def _origin(base_url: str) -> str:
    """scheme://host[:port], without any user:password@ part of the URL."""
    parts = urlsplit(base_url.strip())
    return "{}://{}".format(parts.scheme, parts.netloc.rpartition("@")[2]) if parts.netloc else ""


def _transport_reason(error: TransportError, timeout: float) -> str:
    cause = error.__cause__
    cause = getattr(cause, "reason", cause)  # URLError wraps the socket error
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return "no answer within {:g} s".format(timeout)
    if isinstance(cause, ssl.SSLError):
        return "the certificate was not accepted; check the CA bundle or the certificate pin"
    if isinstance(cause, socket.gaierror):
        return "the address could not be found (DNS)"
    if isinstance(cause, ConnectionRefusedError):
        return "the connection was refused"
    return str(error)
