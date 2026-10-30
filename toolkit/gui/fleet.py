"""Fleet status (docs/gui/BUILD_PLAN.md step 4): check every Orchestrator in .env at once and say green, amber or red, with a plain reason.

The same checks, timings and fields as the automation GUI's fleet.py, so an Orchestrator looks the same in both GUIs, but over
preconfig's own .env reader and client, so the GUI sees exactly what the CLI sees.
Green: connected and the key works (a 403 on GET /gms/versions is still green, as `list` treats it).
Amber: usable, with one thing to know (slow or busy).
Red: not usable as configured (bad settings, no answer, certificate, DNS, refused, key refused, server error).
Import it after putting toolkit/scripts and toolkit/gui on sys.path.
"""
import dataclasses
import os
from pathlib import Path
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlsplit

import common
from common import AuthenticationError, ConfigurationError, EdgeConnectError, PermissionDeniedError, ServerError, TransportError
from engine import uploads_setting
from upload import orchestrator_identity

GREEN, AMBER, RED = "green", "amber", "red"
CHECK_SECONDS = 5.0
SLOW_SECONDS = 2.0
SERVER_TEXT = 100
_SLOT = re.compile(r"^orch_(\d+)$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_NICKNAME = re.compile(r"^[a-z0-9_]+$")  # automation's config.py: a nickname, and the names it reserves
_RESERVED = {"default", "orchestrator", "edgeconnect", "ec"}
_REFUSE = "the automation tools (edgeconnect-auto) stop at this key: "


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
    warnings: List[str] = dataclasses.field(default_factory=list)  # .env keys the automation tools would refuse, by key name only


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
    path = dotenv_path or common.run_dotenv() or common._find_dotenv()  # no public way to name the .env the CLI would find
    raw = _raw_dotenv(path)
    file_keys = {key.lower().replace("-", "_") for key, _ in raw}
    # A *_BASE_URL only in the environment may be another program's: it is named only with its _api_key beside it.
    unlisted = sorted({key[:-len("_base_url")] for key in env if key.endswith("_base_url") and (key in file_keys or key[:-len("base_url")] + "api_key" in env)}
                      - set(names))
    note = _not_listed_note(unlisted, file_keys)

    def check(name: str) -> Status:
        try:
            with common.running(context):
                status = _check(name, env, context.validators, timeout, client_factory, clock)
        except Exception:  # one profile never takes the panel down; the error's text may hold a secret, so it is not shown
            status = Status(name, _origin(env.get("{}_base_url".format(name), "")), RED,
                            "the check stopped on an unexpected error; nothing is known about this Orchestrator", validator=name in context.validators)
        return dataclasses.replace(status, reason=_mask(status.reason, [env.get("{}_api_key".format(name), "")]))

    keys = [value for key, value in env.items() if key.endswith("_api_key")]
    note, warnings = _mask(note, keys), [_mask(warning, keys) for warning in automation_refusals(raw, os.environ)]
    if not names:
        return Fleet([], unlisted, note, warnings)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return Fleet(list(pool.map(check, names)), unlisted, note, warnings)


def _server_text(text: Any, env: Mapping[str, str], name: str) -> str:
    """Text the Orchestrator sent, as shown: the key masked and at most SERVER_TEXT characters, whatever a server sends."""
    return _mask(str(text), [env.get("{}_api_key".format(name), "")])[:SERVER_TEXT]


def _mask(text: str, keys: List[str]) -> str:
    """Free text with every API key replaced; the fixed fields (status, url, id...) are never rewritten."""
    for key in keys:
        text = text.replace(key, "<API_KEY>") if key else text
    return text


def _not_listed_note(unlisted: List[str], file_keys: Set[str]) -> str:
    """Why these nicknames are not listed and how to list them; automation's single-Orchestrator names are reserved as nicknames."""
    def where(group: List[str]) -> str:
        return ".env" if all(name + "_base_url" in file_keys for name in group) else ".env or the environment"
    plain, reserved = [n for n in unlisted if n not in _RESERVED], [n for n in unlisted if n in _RESERVED]
    notes = []
    if plain:
        notes.append("{} {} settings in {} but no orch_N line, so {} not listed; add an orch_<N>=<nickname> line to list one".format(
            ", ".join(plain), "has" if len(plain) == 1 else "have", where(plain), "it is" if len(plain) == 1 else "they are"))
    if reserved:
        notes.append("{}: single-Orchestrator settings in {}, not listed, and their names are reserved; copy them to a nickname of your own, "
                     "for example orch_<N>=main with main_base_url and main_api_key".format(", ".join(reserved), where(reserved)))
    return ". ".join(notes)


def _raw_dotenv(path: str) -> List[Tuple[str, str]]:
    """Every key = value line of the .env in file order, repeats included, the value as written with its quotes, split into lines
    as both toolkits' parse_dotenv split them."""
    found: List[Tuple[str, str]] = []
    if not Path(path).is_file():
        return found
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        line = line[7:].lstrip() if line.startswith("export ") else line
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            found.append((key.strip(), value.strip()))
    return found


def automation_refusals(raw: List[Tuple[str, str]], environ: Mapping[str, str]) -> List[str]:
    """The keys of the .env (_raw_dotenv) and the environment that make every edgeconnect-auto command stop (its config.py), named
    by key, never by value: unnumbered orch_ keys, then unclosed quotes in .env, then in slot order nicknames it refuses or sees twice.
    """
    quoted = {"'", '"'}
    merged = {key.lower(): (key, value[1:-1] if len(value) >= 2 and value[0] in quoted and value[-1] == value[0] else value) for key, value in raw}
    merged.update({key.lower(): (key + " (in the environment)", value) for key, value in sorted(environ.items())})  # automation: the environment wins
    found = []
    for lower, (key, _) in merged.items():
        if lower.startswith("orch_") and lower != "orch_default" and not _SLOT.match(lower):
            found.append(key + ": " + _REFUSE + "an orch_ key must be numbered (orch_<N>), or be orch_default")
    for key, value in raw:  # every line: automation stops at the first unclosed quote, even on a key repeated later
        if value[:1] in quoted and (len(value) < 2 or value[-1] != value[0]):
            found.append(key + ": " + _REFUSE + "its quote is not closed")
    owners: Dict[str, str] = {}
    slots = sorted((int(match.group(1)), key, value.strip().lower()) for lower, (key, value) in merged.items() for match in [_SLOT.match(lower)] if match and value.strip())
    for _, key, name in slots:
        if not _NICKNAME.match(name) or name in _RESERVED:
            found.append(key + ": " + _REFUSE + "a nickname uses letters, digits or _ only, and is not default, ec, edgeconnect or orchestrator")
        elif name in owners:
            found.append(key + ": " + _REFUSE + "this nickname is already used by " + owners[name])
        else:
            owners[name] = key
    return found


def _check(name: str, env: Mapping[str, str], validators: Set[str], timeout: float, client_factory: Callable[[str], Any], clock: Callable[[], float]) -> Status:
    url = _origin(env.get("{}_base_url".format(name), ""))
    validator = name in validators
    try:
        try:
            netloc = urlsplit(env.get("{}_base_url".format(name), "").strip()).netloc
        except ValueError:
            raise ConfigurationError("the base URL cannot be read as a URL") from None
        if "@" in netloc:  # client_for keeps it, and urllib then fails naming it
            raise ConfigurationError("the base URL must not hold a user name or password")
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
            release = _server_text((versions.get("current") if isinstance(versions, dict) else None) or "?", env, name)
        except PermissionDeniedError:  # a key without that right still lists appliances (cli.py list)
            release = ""
        server_id = _server_text(orchestrator_identity(client)["serial"] or "", env, name)
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
    uploads = uploads_setting(name, env)  # as the upload gate reads it
    if uploads == "bad":
        raise ConfigurationError("{}_gui_preconfig_uploads must be on or off".format(name))
    return {"label": env.get("{}_gui_label".format(name), ""), "color": color, "writes": uploads == "on"}


def _origin(base_url: str) -> str:
    """scheme://host[:port], without any user:password@ part of the URL."""
    try:
        parts = urlsplit(base_url.strip())
    except ValueError:  # for example an unclosed [ of an IPv6 address; never raises, as the error fallback calls it
        return ""
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
