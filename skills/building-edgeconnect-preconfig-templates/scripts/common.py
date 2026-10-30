"""Self-contained shared helpers: Orchestrator HTTP client, .env profile loader, serial/filename helpers, secret redaction, YAML 1.2 parser."""
import contextlib
import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Set, TextIO, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATES_DIR = SKILL_DIR / "references" / "default-templates"
# SKILL_DIR is <repo>/toolkit, or <repo>/skills/<skill> for the copy the skill ships; the install folder holds the repo.
_INSTALL = (SKILL_DIR.parents[1] if SKILL_DIR.parent.name == "skills" else SKILL_DIR.parent).parent
_DEFAULT_WORK = _INSTALL / "preconfig-work" if (_INSTALL / "preconfig-work").exists() else Path.cwd() / "preconfig-work"
WORK_DIR = Path(os.environ.get("PRECONFIG_WORK", _DEFAULT_WORK))
CATALOGUE = SKILL_DIR / "references" / "endpoint-catalogue.yaml"

# Key names whose non-empty string values are secrets. BGP "community" and tag "key" are deliberately absent.
_SECRET_WORDS = r"passw|passphrase|secret|psk|pre[_-]?shared|(?:private|auth|authentication|api|ike)[_-]?key|token|md5|enable[_-]?pass"
_SECRET_KEY = re.compile(r"(?i)(" + _SECRET_WORDS + ")")
_SERVER_SIBLINGS = ("host", "address", "authport", "acctport", "server", "serverIp")
_PEM = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.S)
_CRYPT = re.compile(r"\$(1|2[aby]?|5|6|y)\$[./A-Za-z0-9$]{8,}")
# [ \t]: a keyword with an empty value must not reach across the line break and redact the next line's first word.
# A filler ("is", "-", "->"; "=>" is caught by (?![:=])) is never the value: the free-text rules below handle "password is Abc1!" and "password => x".
_CLI_SECRET = re.compile(r"(?im)(\b(?:password|passwd|secret|psk|pre-shared-key|passphrase|auth-key|authentication-key|md5|api-key|token)\b[ \t]+"
                         r"(?!(?:is|-+>?)[ \t])(?:\d[ \t]+)?)(?![:=])(\S+)")  # "password = x" is free text, below
# free text (banners, comments, JSON in CLI lines): "pass: x", "secret = x", "adminPassword is x1", "\"apiKey\":\"x\"". The separator
# (':', '=', an arrow, or "is"/"-" before a value holding a digit or a symbol other than .,;:!?, so "password is required." stays)
# keeps "pass-through" untouched. A key is a secret word (_TEXT_WORD: an optional "x_" prefix, a "_key" suffix, a word key may start
# after '-' as in "x-psk"), a quoted name holding a keyed-field word (_SECRET_WORDS, as for fields: "adminPassword", "tokenize"), or an
# unquoted name ending in one ("adminPassword", "private_key"; not "token-lifetime", "pskType", "md5sum"). A JSON number, true, false
# or null is not a secret (fields skip non-strings); a number in prose is ("password: 12345"). A quoted value ends at its closing
# quote; escapes are read as pairs. Inside a \"-quoted value \\\" is an escaped quote and a bare " is allowed only where it cannot
# close a string; a '-quoted value holds no \" and a bare " only where it cannot close a string. After a quoted key, a value quote of
# the same kind ("password":", \"password\":\", 'password':') always opens the value; any other quote that closes a string
# (_NOT_A_CLOSE) never opens a value, so {"banner": "Password: ", "motd": "x"} keeps its next key. After a quoted JSON key a bare value
# ends at ',' or '}', a secret array (one nesting level) becomes one placeholder item and an object is left to its own keys. Other free
# text ends at whitespace, at a backslash run before a quote, or at a quote followed by a delimiter. Every value pattern has disjoint
# alternatives so no input backtracks more than linearly (Python 3.9 has no atomic groups); a key's secret word must sit within 64
# characters of the name's start, which keeps one long word from being rescanned per rule. null/true/false are not secrets.
_TEXT_WORD = (r"(?:\w{0,64}_)?(?:pass(?:word|wd|code|phrase)?|pwd|pw|secret|psk|pre[_-]?shared[_-]?key|auth(?:entication)?[_-]?key|api[_-]?key"
              r"|token)(?:[_-]key)?")
_TEXT_END_WORDS = r"passw(?:or)?d|passphrase|secret|psk|pre[_-]?shared|(?:private|auth|authentication|api|ike|shared|md5)[_-]?key|token|md5"
_KEY_START = r"(?=\w)(?<![\w<])(?P<key>" + _TEXT_WORD
_KEY_QUOTED = r"|(?<=[\"'])(?=[\w-]{0,64}?(?:" + _SECRET_WORDS + r"))[\w-]+(?=\\?[\"'])"
_KEY_UNQUOTED = r"|(?<!-)[\w-]{0,64}?(?:" + _TEXT_END_WORDS + r")(?![\w-])"
_TEXT_KEY = _KEY_START + _KEY_QUOTED + _KEY_UNQUOTED + ")"
_JSON_KEY = _KEY_START + _KEY_QUOTED + ")"  # the rules below that need a quote after the key, and the one that cannot have one,
_FREE_KEY = _KEY_START + _KEY_UNQUOTED + ")"  # skip the other key form's scan
_TEXT_SEP = r"(?:[ \t]*(?:[:=]>?|-+>)[ \t]*|[ \t]+(?:is|-+)[ \t]+(?=\S*?[^\sA-Za-z.,;:!?]))"
# a quote followed by ',' or ':' and then a JSON value, or by '}' or ']' and then more structure, closes a string
_NOT_A_CLOSE = r"(?!\s*(?:[,:]\s*(?:\\?[\"'\[{]|-?\d|true|false|null|$)|[}\]]\s*(?:[,}\]\"]|$)))"
_TEXT_QUOTED = re.compile(r"(?i)(?P<pre>" + _TEXT_KEY + r"(?P<kq>\\?[\"'])?" + _TEXT_SEP + r"(?:(?=(?P=kq))(?P<same>)|)(?P<esc>\\)?(?P<q>\"|'(?P<sq>))(?(same)|"
                          + _NOT_A_CLOSE + r"))(?P<val>(?(esc)(?:\\\\(?:\\[^\n]|[^\\\n])|\\[^\\\n]|\"" + _NOT_A_CLOSE + r"|[^\\\n\"])*?"
                          r"|(?(sq)(?:\\[^\n\"]|\"" + _NOT_A_CLOSE + r"|[^\\\n\"])*?"
                          r"|(?:\\[^\n]|[^\\\n\"])*?)))(?P<end>(?(esc)\\)(?P=q))")
_JSON_STR = r"(?:\\\"(?:\\\\(?:\\[^\n]|[^\\\n])|\\[^\\\n\"]|[^\\\n\"])*\\\"|\"(?:\\[^\n]|[^\\\"\n])*\")"  # \"-quoted: as in _TEXT_QUOTED
_JSON_OTHER = r"[^\[\]{}\"\\\n]"
_TEXT_JSON_ARRAY = re.compile(r"(?i)(?P<pre>" + _JSON_KEY + r"(?P<esc>\\?)\"[ \t]*:[ \t]*\[)(?P<val>(?:" + _JSON_STR + r"|\[(?:" + _JSON_STR
                              + "|" + _JSON_OTHER + r")*\]|" + _JSON_OTHER + r")*)\]")
_TEXT_JSON_BARE = re.compile(r"(?i)(?P<pre>" + _JSON_KEY + r"\\?\"[ \t]*:[ \t]*)(?P<val>[^\s\\\",}{\[][^\s\\\",}]*)")
_TEXT_FREE = re.compile(r"(?i)(?P<pre>" + _FREE_KEY + _TEXT_SEP + r")(?P<val>(?:\\?[\"']" + _NOT_A_CLOSE + r")?(?:[^\s\\\"']|\\+(?![\\\"])|[\"'](?![\s,;:)}\]\"'\\]|$))+)")  # an unclosed quote is taken with the value
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_NOT_SECRET = ("null", "true", "false")


def _keep(value: str) -> bool:
    """An empty value, a placeholder already written, or null/true/false: nothing to redact (leading quotes ignored)."""
    v = value.lstrip("\\\"'")
    return not v or v.startswith("<SECRET:") or v.lower() in _NOT_SECRET


def _redact_text(text: str) -> str:
    def sub(rule: Any, keep: Any, put: Any) -> None:
        nonlocal text
        text = rule.sub(lambda m: m.group(0) if _keep(m["val"]) or keep(m) else m["pre"] + put(m), text)

    sub(_TEXT_QUOTED, lambda m: False, lambda m: "<SECRET:text>" + m["end"])
    sub(_TEXT_JSON_ARRAY, lambda m: '"' not in m["val"], lambda m: m["esc"] + '"<SECRET:text>' + m["esc"] + '"]')
    sub(_TEXT_JSON_BARE, lambda m: _NUMBER.fullmatch(m["val"]), lambda m: "<SECRET:text>")
    sub(_TEXT_FREE, lambda m: False, lambda m: "<SECRET:text>")
    return text


_SERIAL_HEX = re.compile(r"^[0-9A-F]{12}$")


class EdgeConnectError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, method: str = "", path: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path


class ConfigurationError(EdgeConnectError):
    pass


class AuthenticationError(EdgeConnectError):
    pass


class PermissionDeniedError(EdgeConnectError):
    pass


class ValidationError(EdgeConnectError):
    pass


class ResourceNotFoundError(EdgeConnectError):
    pass


class ServerError(EdgeConnectError):
    pass


class TransportError(EdgeConnectError):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    ca_bundle: Optional[str] = None
    api_key_header: str = "X-Auth-Token"
    timeout: float = 30.0
    verify_tls: bool = True
    name: str = ""
    tls_sha256: Optional[str] = None


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


class _PinnedHTTPSHandler(HTTPSHandler):
    """Trust exactly one certificate, by the SHA-256 of its DER form, checked on every connection."""

    def __init__(self, pin: str) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(context=context)
        self._pinned_context = context
        self._pin = pin

    def https_open(self, req: Any) -> Any:
        pin = self._pin

        class PinnedConnection(HTTPSConnection):
            def connect(self) -> None:
                super().connect()
                actual = hashlib.sha256(self.sock.getpeercert(binary_form=True) or b"").hexdigest()
                if not hmac.compare_digest(actual, pin):
                    self.close()
                    raise ssl.SSLError("server certificate does not match the configured SHA-256 pin")

        return self.do_open(PinnedConnection, req, context=self._pinned_context)


class ApiClient:
    """Minimal standard-library EdgeConnect Orchestrator REST client with redirect refusal and write counting."""

    def __init__(self, config: Config, opener: Any = None) -> None:
        self.config = config
        self.write_requests = 0
        if opener is not None:
            self._opener = opener
        elif config.tls_sha256:
            self._opener = build_opener(_PinnedHTTPSHandler(config.tls_sha256), _RefuseRedirect)
        else:
            if not config.verify_tls:
                ctx = ssl._create_unverified_context()
            else:
                ctx = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else ssl.create_default_context()
            self._opener = build_opener(HTTPSHandler(context=ctx), _RefuseRedirect)

    def get(self, path: str, query: Optional[Mapping[str, Any]] = None, expected_status: Tuple[int, ...] = (200,)) -> Any:
        return self.request("GET", path, query=query, expected_status=expected_status, expect_json=True)

    def request(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        expected_status: Tuple[int, ...] = (200,),
        expect_json: bool = True,
        accept: str = "application/json",
        read_only: bool = False,
    ) -> Any:
        verb = method.upper()
        if verb != "GET" and not read_only:
            self.write_requests += 1
        url = self.config.base_url.rstrip("/") + "/" + path.lstrip("/")
        if query:
            pairs = [(str(k), str(v)) for k, v in query.items() if v is not None]
            if pairs:
                url = url + ("&" if "?" in url else "?") + urlencode(pairs)
        headers = {
            self.config.api_key_header: self.config.api_key,
            "Accept": accept,
        }
        data: Optional[bytes] = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=verb)
        try:
            with self._opener.open(req, timeout=self.config.timeout) as resp:
                status = getattr(resp, "status", resp.getcode())
                raw = resp.read()
        except HTTPError as err:
            status = err.code
            raw = err.read() if hasattr(err, "read") else b""
            msg = self._extract_error_message(raw, status)
            if status not in expected_status:
                self._raise_http(status, msg, verb, path)
        except (URLError, ssl.SSLError, OSError) as err:
            raise TransportError("transport error on {} {}: {}".format(verb, path, err), method=verb, path=path) from err

        if status not in expected_status:
            self._raise_http(status, self._extract_error_message(raw, status), verb, path)
        if not expect_json:
            return raw
        text = (raw or b"").decode("utf-8", "replace").strip()
        return json.loads(text) if text else None

    @staticmethod
    def _extract_error_message(raw: bytes, status: int) -> str:
        text = (raw or b"").decode("utf-8", "replace").strip()
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("error"):
                    return "HTTP {}: {}".format(status, parsed["error"])
            except Exception:
                pass
            return "HTTP {}: {}".format(status, text)
        return "HTTP {}".format(status)

    @staticmethod
    def _raise_http(status: int, message: str, method: str, path: str) -> None:
        if status == 401:
            raise AuthenticationError(message, status=status, method=method, path=path)
        if status == 403:
            raise PermissionDeniedError(message, status=status, method=method, path=path)
        if status == 404:
            raise ResourceNotFoundError(message, status=status, method=method, path=path)
        if status in (400, 409, 422):
            raise ValidationError(message, status=status, method=method, path=path)
        if status in (301, 302, 303, 307, 308):
            raise TransportError(message, status=status, method=method, path=path)
        raise ServerError(message, status=status, method=method, path=path)


def parse_dotenv(path: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return result
    for number, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError("invalid dotenv line {}".format(number))
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {"'", '"'} and len(value) >= 2 and value[-1] == value[0]:
            value = value[1:-1]
        result[key] = value
    return result


def _find_dotenv() -> str:
    if os.environ.get("EC_DOTENV"):
        return os.environ["EC_DOTENV"]
    for candidate in [Path.cwd() / ".env", SKILL_DIR / ".env"] + [p / ".env" for p in SKILL_DIR.parents]:
        if candidate.is_file():
            return str(candidate)
    return str(Path.cwd() / ".env")


def _load_env_map(dotenv_path: Optional[str] = None) -> Dict[str, str]:
    raw = parse_dotenv(dotenv_path or _find_dotenv())
    merged = {k.lower().replace("-", "_"): (v.replace("-", "_") if k.lower().startswith("orch_") else v) for k, v in raw.items()}
    for k, v in os.environ.items():
        merged[k.lower().replace("-", "_")] = v
    return merged


def configured_validators(dotenv_path: Optional[str] = None) -> Set[str]:
    """Return allowed validator nicknames: exactly those listed in EC_VALIDATORS in .env, else none.

    No name pattern allows a profile by itself (docs/decisions/0011).
    """
    return validators_in(_load_env_map(dotenv_path))


def validators_in(env: Mapping[str, str]) -> Set[str]:
    """The EC_VALIDATORS nicknames of a parsed .env map (_load_env_map), lower-cased, with - and _ spellings."""
    explicit = env.get("ec_validators") or ""  # no orch_validators alias: the automation CLI refuses unnumbered orch_ keys in a shared .env
    out: Set[str] = set()
    for part in explicit.split(","):
        if part.strip():
            out.add(part.strip().lower())
            out.add(part.strip().lower().replace("_", "-"))
            out.add(part.strip().lower().replace("-", "_"))
    return out


VALIDATORS = configured_validators()


@dataclass
class RunContext:
    """What one command run reads instead of module values, so runs sharing a process (the GUI) each keep their own.

    A field left None means what a run without a context gets: the .env found as always, EC_VALIDATORS from it, WORK_DIR,
    and preconfig-output in the current folder. The values are read when a command needs them, as without a context.
    """
    dotenv: Optional[str] = None           # .env path
    env: Optional[Dict[str, str]] = None   # that .env parsed (_load_env_map); None: read from the file when needed, as without a context
    validators: Optional[Set[str]] = None  # lower-case nicknames /validate may run on; None: EC_VALIDATORS of that .env
    work_dir: Optional[Path] = None        # where captures and downloaded default templates are kept
    out_dir: Optional[Path] = None         # the folder whose preconfig-output/ is the default output
    ignore_ownership: bool = False         # translate --ignore-template-ownership
    recorder: Any = None                   # structured results of the run (cli-requests R1)
    approver: Optional[Callable[..., Any]] = None  # upload approval hook (cli-requests R2)
    source: str = "cli"


_RUN: "contextvars.ContextVar[Optional[RunContext]]" = contextvars.ContextVar("edgeconnect_preconfig_run", default=None)


@contextlib.contextmanager
def running(context: RunContext) -> Iterator[RunContext]:
    """Run the block (and the threads' own code inside it) with this context; a thread starts with none."""
    token = _RUN.set(context)
    try:
        yield context
    finally:
        _RUN.reset(token)


def run_context() -> Optional[RunContext]:
    return _RUN.get()


def run_dotenv() -> Optional[str]:
    """The run's .env, or None to find it as always."""
    context = _RUN.get()
    return context.dotenv if context else None


def env_map(dotenv_path: Optional[str] = None) -> Dict[str, str]:
    """The parsed .env: the one given, else the run's map or file, else the one found as always."""
    context = _RUN.get()
    if dotenv_path is None and context and context.env is not None:
        return context.env
    return _load_env_map(dotenv_path or run_dotenv())


def validators() -> Set[str]:
    context = _RUN.get()
    if context is None:
        return VALIDATORS
    if context.validators is None:
        context.validators = validators_in(env_map()) if context.dotenv or context.env is not None else VALIDATORS
    return context.validators


def work_dir() -> Path:
    context = _RUN.get()
    return context.work_dir if context and context.work_dir else WORK_DIR


def output_root() -> Path:
    """The default output parent: preconfig-output/ in the current folder, or in the run's out_dir."""
    context = _RUN.get()
    return (context.out_dir if context and context.out_dir else Path.cwd()) / "preconfig-output"


class Recorder:
    """A run's structured results, in the order they were made, next to its printed text (cli-requests R1)."""

    def __init__(self) -> None:
        self.results: List[Dict[str, Any]] = []

    def add(self, kind: str, **data: Any) -> None:
        self.results.append(dict(data, kind=kind))


def record(kind: str, **data: Any) -> None:
    """Keep a result for the run's recorder; without a context or a recorder nothing is kept."""
    context = _RUN.get()
    if context is not None and context.recorder is not None:
        context.recorder.add(kind, **data)


class Problem(str):
    """A problem message that also says what it is about (cli-requests R3); it prints exactly as the message."""
    column: Optional[str]
    value: Any

    def __new__(cls, message: str, column: Optional[str] = None, value: Any = None) -> "Problem":
        made = str.__new__(cls, message)
        made.column, made.value = column, value
        return made


def problem_data(row: Optional[int], problem: str, secrets: Any = ()) -> Dict[str, Any]:
    """A problem as data: row, column and value when the message carries them, and the message; for a secret column the value
    is <SECRET> and a message that could hold the cell is replaced, so the record never holds a secret (the printed text may
    show fewer problems than are recorded)."""
    column, value, message = getattr(problem, "column", None), getattr(problem, "value", None), str(problem)
    if column in secrets:
        if value not in (None, "", "<SECRET>"):  # the message may quote the cell escaped or trimmed, so no replace() is safe
            message = "{}: <SECRET> (a secret column's message is not recorded)".format(column)
        value = "<SECRET>"
    return {"row": row, "column": column, "value": value, "message": message}


_OUTPUT: "contextvars.ContextVar[Optional[Tuple[TextIO, TextIO]]]" = contextvars.ContextVar("edgeconnect_preconfig_output", default=None)
_ROUTING = threading.Lock()


class RoutedStream:
    """Stands in for sys.stdout or sys.stderr: a write goes to the run's capture (captured_output) when it has one, else to the
    stream it replaced, so runs in other threads keep printing where they did."""

    def __init__(self, stream: TextIO, index: int) -> None:
        self.stream, self.index = stream, index

    def target(self) -> TextIO:
        capture = _OUTPUT.get()
        return capture[self.index] if capture is not None else self.stream

    def write(self, text: str) -> int:
        target = self.target()
        return target.write(text) if target is not None else len(text)  # no stream (pythonw): print() writes nothing, as before

    def flush(self) -> None:
        target = self.target()
        if target is not None:
            target.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


@contextlib.contextmanager
def captured_output(out: TextIO, err: Optional[TextIO] = None) -> Iterator[TextIO]:
    """Send this run's stdout (and stderr, to err or to out) writes there; other threads and runs are not affected.

    contextlib.redirect_stdout swaps the stream for the whole process, so a parallel run's lines would land in this capture."""
    with _ROUTING:
        for index, name in enumerate(("stdout", "stderr")):
            if not isinstance(getattr(sys, name), RoutedStream):
                setattr(sys, name, RoutedStream(getattr(sys, name), index))
    token = _OUTPUT.set((out, err or out))
    try:
        yield out
    finally:
        _OUTPUT.reset(token)


def client_for(nickname: str, dotenv_path: Optional[str] = None) -> ApiClient:
    """Build an ApiClient for a named Orchestrator profile in .env (the run's, unless one is given)."""
    env = env_map(dotenv_path)
    norm = nickname.strip().lower().replace("-", "_")
    base_url = env.get("{}_base_url".format(norm))
    api_key = env.get("{}_api_key".format(norm))
    if not base_url or not api_key:
        profiles = sorted(v for k, v in env.items() if re.match(r"^orch_\d+$", k) and v)
        raise ConfigurationError("unknown or incomplete Orchestrator profile {!r} (configured: {})".format(nickname, ", ".join(profiles) or "none"))
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("base URL for {!r} must be an https:// URL".format(nickname))
    path = parsed.path.rstrip("/") or "/gms/rest"
    normalized_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    ca_bundle = env.get("{}_ca_bundle".format(norm))
    verify_raw = env.get("{}_verify_tls".format(norm), "").strip().lower()
    # A self-signed lab Orchestrator is trusted by pinning its certificate, never because of its profile name.
    pin = re.sub(r"[\s:]", "", env.get("{}_tls_sha256".format(norm), "")).lower() or None
    if pin is not None and not re.fullmatch(r"[0-9a-f]{64}", pin):
        raise ConfigurationError("{}_tls_sha256 must be the certificate's SHA-256 fingerprint: 64 hex digits, colons allowed".format(norm))
    cfg = Config(
        base_url=normalized_url,
        api_key=api_key,
        ca_bundle=ca_bundle,
        api_key_header=env.get("{}_api_key_header".format(norm)) or "X-Auth-Token",
        verify_tls=verify_raw not in ("false", "0", "no"),
        name=norm,
        tls_sha256=pin,
    )
    return ApiClient(cfg)


def normalize_serial(value: Optional[str]) -> Optional[str]:
    """Normalize an appliance serial number (e.g. '00-1B-BC-46-2D-3D' -> '001BBC462D3D') and validate 12 hex chars."""
    if value is None:
        return None
    cleaned = re.sub(r"[\s\-:]", "", str(value)).upper()
    if not cleaned:
        return None
    if not _SERIAL_HEX.match(cleaned):
        raise ValueError("serial_number {!r} must be 12 hexadecimal characters (e.g. 001BBC462D3D or 00-1B-BC-46-2D-3D)".format(value))
    return cleaned


def resolve_output_filename(row: Mapping[str, Any], index: int = 1) -> Tuple[str, str]:
    """Resolve (preconfig_name, filename_with_yml) for a CSV row.

    Priority:
      1. row['preconfig_name'] or row['filename']
      2. '<hostname>_preconfig' if row['hostname'] is non-empty
      3. 'site_<index>_preconfig'
    Rejects path separators ('/', '\\', '..') so CSV cells cannot write outside the output directory.
    """
    raw = str(row.get("preconfig_name") or row.get("filename") or "").strip()
    if raw:
        base = re.sub(r"\.(?:yml|yaml)$", "", raw, flags=re.I).strip()
    else:
        host = str(row.get("hostname") or row.get("applianceInfo.hostname") or "").strip()
        base = "{}_preconfig".format(host) if host else "site_{}_preconfig".format(index)
    if not base or "/" in base or "\\" in base or ".." in base or any(ord(c) < 32 for c in base):
        raise ValueError("invalid preconfig_name/filename {!r}: must be a simple filename without path separators".format(raw or base))
    return base, base + ".yml"


def _release_key(release: str) -> Tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(release)))


def find_default_template(release: str) -> Tuple[Optional[Path], str]:
    """(template, why) for an Orchestrator release (docs/decisions/0014): the exact build, else the newest template of the same
    major.minor, else the nearest older release, else the nearest newer; never None only because the exact build is missing."""
    have: Dict[str, Path] = {}
    for folder in (work_dir() / "reference" / "default-templates", DEFAULT_TEMPLATES_DIR):  # bundled wins
        for p in folder.glob("orchestrator-*.yml"):
            have[p.stem[len("orchestrator-"):]] = p
    if release in have:
        return have[release], "the exact template for Orchestrator release {}".format(release)
    key = _release_key(release)
    family = [r for r in have if _release_key(r)[:2] == key[:2]]
    if family:
        r = max(family, key=_release_key)
        return have[r], "no template for {}: using {}, the newest of the same {} family".format(release, r, ".".join(map(str, key[:2])))
    older = [r for r in have if _release_key(r) < key]
    newer = [r for r in have if _release_key(r) > key]
    if older or newer:
        r = max(older, key=_release_key) if older else min(newer, key=_release_key)
        return have[r], "no template for {} or its family: using {}, the nearest {} release (release {} is untested)".format(release, r, "older" if older else "newer", release)
    return None, "no default templates in {}".format(DEFAULT_TEMPLATES_DIR)


def redact_secrets(value: Any, path: str = "$") -> Tuple[Any, List[str]]:
    """Return (copy with secrets replaced by <SECRET:path>, list of redacted paths)."""
    found: List[str] = []

    def walk(item: Any, where: str, secret_key: bool) -> Any:
        if isinstance(item, dict):
            server = any(s in item for s in _SERVER_SIBLINGS)  # e.g. NAC/RADIUS server: its "key" is a shared secret
            return {k: walk(v, "{}.{}".format(where, k), bool(_SECRET_KEY.search(str(k))) or (k == "key" and server)) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v, "{}[{}]".format(where, i), secret_key) for i, v in enumerate(item)]
        if isinstance(item, str) and item:
            if secret_key:
                found.append(where)
                return "<SECRET:{}>".format(where)
            text = _CLI_SECRET.sub(lambda m: m.group(1) + "<SECRET:text>", _redact_text(_CRYPT.sub("<SECRET:hash>", _PEM.sub("<SECRET:pem>", item))))
            if text != item:
                found.append(where)
            return text
        return item

    return walk(value, path, False), found


def toolkit_version() -> str:
    """'1.1.0 (commit 456f08b, 2026-10-21)' from VERSION and, in a git checkout, the last commit; local edits to the toolkit are flagged."""
    version = (SKILL_DIR / "VERSION").read_text().strip() if (SKILL_DIR / "VERSION").is_file() else "unknown"
    git = ["git", "-C", str(SKILL_DIR)]
    try:
        commit = subprocess.run(git + ["log", "-1", "--format=%h, %cd", "--date=short"], capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run(git + ["status", "--porcelain", "--", "."], capture_output=True, text=True, timeout=10).stdout.splitlines()
        # capture.py caches new releases' default templates as new files: the toolkit working, not an edit (an edited bundled one still counts)
        edited = [line for line in status if not (line.startswith("?? ") and "references/default-templates/" in line)]
    except (OSError, subprocess.SubprocessError):  # no git, or not a checkout: VERSION alone
        commit, edited = "", ""
    return version + (" (commit {})".format(commit) if commit else "") + (", WITH LOCAL EDITS to the toolkit" if edited else "")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def parse_yaml(text: str) -> Any:
    """YAML 1.2 core-schema scalars. PyYAML's default (1.1) turns 1:1 into 61, 08034 into an octal error, and yes/no/on/off
    into booleans, whereas Orchestrator's parser reads `importRouteTarget: 1:1` and `zipCode: 08034` as strings."""
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is not installed for {}: run `{} -m pip install -r requirements.txt`".format(sys.executable, sys.executable))

    class Core(yaml.SafeLoader):
        pass

    Core.yaml_implicit_resolvers = {k: [(t, r) for t, r in v if t not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float")]
                                    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    Core.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    Core.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"))
    Core.add_implicit_resolver("tag:yaml.org,2002:float", re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"), list("-+0123456789."))
    return yaml.load(text, Loader=Core)


def load_yaml(path: Path) -> Any:
    return parse_yaml(path.read_text(encoding="utf-8"))


def paths_of(doc: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(doc, dict):
        out: Dict[str, Any] = {}
        for key, item in doc.items():
            out.update(paths_of(item, "{}.{}".format(prefix, key) if prefix else str(key)))
        return out or {prefix: {}}
    if isinstance(doc, list):
        out = {}
        for index, item in enumerate(doc):
            out.update(paths_of(item, "{}[{}]".format(prefix, index)))
        return out or {prefix: []}
    return {prefix: doc}


def changed_paths(a: Any, b: Any) -> List[str]:
    pa, pb = paths_of(a), paths_of(b)
    return sorted(p for p in set(pa) | set(pb) if p not in pa or p not in pb or pa[p] != pb[p] or type(pa[p]) is not type(pb[p]))

