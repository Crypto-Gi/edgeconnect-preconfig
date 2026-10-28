"""Self-contained shared helpers: Orchestrator HTTP client, .env profile loader, serial/filename helpers, secret redaction, YAML 1.2 parser."""
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
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
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
_SECRET_KEY = re.compile(r"(?i)(passw|passphrase|secret|psk|pre_?shared|private_?key|auth_?key|authentication_?key|api_?key|token|md5|ike_?key|enable_?pass)")
_SERVER_SIBLINGS = ("host", "address", "authport", "acctport", "server", "serverIp")
_PEM = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.S)
_CRYPT = re.compile(r"\$(1|2[aby]?|5|6|y)\$[./A-Za-z0-9$]{8,}")
# [ \t]: a keyword with an empty value must not reach across the line break and redact the next line's first word
_CLI_SECRET = re.compile(r"(?im)(\b(?:password|passwd|secret|psk|pre-shared-key|passphrase|auth-key|authentication-key|md5|api-key|token)\b[ \t]+(?:\d[ \t]+)?)(?![:=])(\S+)")  # "password = x" is free text, below
# free text (banners, comments, JSON in CLI lines): "pass: x", "secret = x", "\"password\":\"x\"". The ':' or '=' keeps
# "pass-through" and "token-lifetime" untouched. A quoted value is taken up to its closing quote; a bare value after a quoted JSON key
# ends at ',' or '}'; other free text ends at whitespace, a quote or a backslash. null/true/false are not secrets.
_TEXT_KEY = r"(?<!<)\b(?:pass(?:word|wd|code|phrase)?|pwd|pw|secret|psk|pre-shared-key|auth-key|authentication-key|api-key|token)"
_TEXT_QUOTED = re.compile(r"(?i)(" + _TEXT_KEY + r"(?:\\?\")?[ \t]*[:=][ \t]*(\\?\"))(.+?)(\2)")
_TEXT_JSON_BARE = re.compile(r"(?i)(" + _TEXT_KEY + r"\\?\"[ \t]*:[ \t]*)([^\s\\\",}]+)")
_TEXT_FREE = re.compile(r"(?i)(" + _TEXT_KEY + r"[ \t]*[:=][ \t]*)([^\s\\\"]+)")
_NOT_SECRET = ("null", "true", "false")


def _redact_text(text: str) -> str:
    keep = lambda v: v.startswith("<SECRET:") or v.lower() in _NOT_SECRET
    text = _TEXT_QUOTED.sub(lambda m: m.group(0) if keep(m.group(3)) else m.group(1) + "<SECRET:text>" + m.group(4), text)
    text = _TEXT_JSON_BARE.sub(lambda m: m.group(0) if keep(m.group(2)) else m.group(1) + "<SECRET:text>", text)
    return _TEXT_FREE.sub(lambda m: m.group(0) if keep(m.group(2)) else m.group(1) + "<SECRET:text>", text)


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
    """Return allowed validator nicknames: exactly those listed in EC_VALIDATORS (or orch_validators) in .env, else none.

    No name pattern allows a profile by itself (docs/decisions/0011).
    """
    env = _load_env_map(dotenv_path)
    explicit = env.get("ec_validators") or env.get("orch_validators") or ""
    out: Set[str] = set()
    for part in explicit.split(","):
        if part.strip():
            out.add(part.strip().lower())
            out.add(part.strip().lower().replace("_", "-"))
            out.add(part.strip().lower().replace("-", "_"))
    return out


VALIDATORS = configured_validators()


def client_for(nickname: str, dotenv_path: Optional[str] = None) -> ApiClient:
    """Build an ApiClient for a named Orchestrator profile in .env."""
    env = _load_env_map(dotenv_path)
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
    for folder in (WORK_DIR / "reference" / "default-templates", DEFAULT_TEMPLATES_DIR):  # bundled wins
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
            text = _CLI_SECRET.sub(lambda m: m.group(1) + "<SECRET:text>", _CRYPT.sub("<SECRET:hash>", _PEM.sub("<SECRET:pem>", item)))
            text = _redact_text(text)
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

