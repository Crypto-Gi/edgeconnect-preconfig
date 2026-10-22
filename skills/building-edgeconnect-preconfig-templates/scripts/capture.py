#!/usr/bin/env python3
"""Stages 0-1: read-only capture of one appliance's configuration, live and cached, from one Orchestrator.

GET requests only, on every Orchestrator. Secrets are redacted before anything is written.
Output: <work>/<reference|samples>/<orchestrator>/<nePk>/<UTC timestamp>/
  run.json  capture-index.json  requests.jsonl  capture/<id>.<live|cached|orchestrator|backup>.json
"""
import argparse
import base64
import binascii
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CATALOGUE, DEFAULT_TEMPLATES_DIR, WORK_DIR, client_for, load_yaml, parse_yaml, redact_secrets, utc_stamp, write_json  # noqa: E402

STOP_ERRORS = ("AuthenticationError", "TransportError")  # 401, redirect, TLS, network; a 403 is one read the key may not make
LABELS = {"cached": ("cached", "live"), "fromGms": ("cached", "live")}


class Stop(Exception):
    pass


class Reader:
    """The only network path in this script: GET, raw body, JSON-decoded by us so empty/false bodies are kept."""

    def __init__(self, client: Any, log: Path, pause: float) -> None:
        self.client, self.log, self.pause = client, log, pause

    def get(self, path: str, query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        record: Dict[str, Any] = {"method": "GET", "path": path, "query": query or {}, "time": utc_stamp()}
        try:
            body = self.client.request("GET", path, query=query, expected_status=(200, 204), expect_json=False)
            text = (body or b"").decode("utf-8", "replace")
            try:
                data = json.loads(text) if text.strip() else None
            except json.JSONDecodeError:
                data = text
            record.update(status="ok", bytes=len(body or b""))
            return {"ok": True, "data": data}
        except Exception as error:  # classified below; never retried here
            name = type(error).__name__
            record.update(status="error", error=name, http=getattr(error, "status", None), message=str(error)[:300])
            if name in STOP_ERRORS:
                raise Stop("{} on GET {}: {}".format(name, path, str(error)[:200])) from None
            return {"ok": False, "error": name, "http": getattr(error, "status", None), "message": str(error)[:300]}
        finally:
            with self.log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            if self.client.write_requests:
                raise Stop("write counter is {}; capture is read-only".format(self.client.write_requests))
            time.sleep(self.pause)


def find_appliance(appliances: List[Dict[str, Any]], wanted: str) -> Dict[str, Any]:
    hits = [a for a in appliances if wanted in (str(a.get("nePk") or a.get("id")), a.get("hostName"))]
    if len(hits) != 1:
        raise Stop("appliance {!r}: {} matches by nePk or exact hostname".format(wanted, len(hits)))
    return hits[0]


def capture(orchestrator: str, appliance: str, role: str, pause: float, out_root: Path, only: Optional[List[str]] = None, reuse: bool = False) -> Path:
    client = client_for(orchestrator)
    stamp = utc_stamp()
    tmp = out_root / ".incomplete-{}-{}-{}".format(appliance, stamp, os.getpid())
    (tmp / "capture").mkdir(parents=True, exist_ok=True)
    reader = Reader(client, tmp / "requests.jsonl", pause)
    try:

        versions = reader.get("/gms/versions")  # a 403 comes back as not ok: no release, the rest of the capture goes on
        release = versions["data"].get("current") if versions["ok"] and isinstance(versions["data"], dict) else None
        if release and not only and not (DEFAULT_TEMPLATES_DIR / "orchestrator-{}.yml".format(release)).is_file():
            def_tmpl = reader.get("/gms/appliance/preconfiguration/default")
            text = def_tmpl["data"] if def_tmpl["ok"] else None
            if isinstance(text, dict):  # 10.0.0.40625 answers {"configData": "<base64 of the YAML>", ...}
                try:
                    text = base64.b64decode("".join(str(text.get("configData") or "").split()), validate=True).decode("utf-8")
                except (TypeError, binascii.Error, UnicodeDecodeError):
                    text = None
            try:
                usable = isinstance(text, str) and isinstance(parse_yaml(text), dict)
            except Exception:  # not YAML at all
                usable = False
            if usable:
                DEFAULT_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
                (DEFAULT_TEMPLATES_DIR / "orchestrator-{}.yml".format(release)).write_text(text, encoding="utf-8")
            else:
                print("warning: could not cache the default preconfig template of Orchestrator release {}; translate.py --target-release {} "
                      "needs {}".format(release, release, DEFAULT_TEMPLATES_DIR / "orchestrator-{}.yml".format(release)), file=sys.stderr)
        listing = reader.get("/appliance")
        if not listing["ok"]:
            raise Stop("cannot list appliances: {}".format(listing))
        target = find_appliance(listing["data"], appliance)
        nepk = str(target.get("nePk") or target.get("id"))
        reach = reader.get("/reachability/gms", {"nePk": nepk})
        reachable = bool(reach["ok"] and isinstance(reach["data"], dict) and int(reach["data"].get("state", 2)) in (1, 4))
        index: List[Dict[str, Any]] = []
        if reach.get("error") == "PermissionDeniedError":  # unknown, not unreachable: try the live reads and report the refusal (decision 0009)
            reachable = None
            index.append({"id": "reachability", "path": "/reachability/gms", "label": "orchestrator", "query": {"nePk": nepk}, "sections": [],
                          "status": "denied", "error": reach["error"], "http": reach["http"], "message": reach["message"]})

        # Opt-in only: a re-capture after an apply must re-read Orchestrator-scope state (associations, ids-state), never reuse it.
        orch_cache: Dict[str, Any] = {}
        for prev in sorted(out_root.glob("*.NE/*Z/capture/*.orchestrator.json")) if reuse else []:
            if time.time() - prev.stat().st_mtime < 3600:
                try:
                    orch_cache[prev.name] = json.loads(prev.read_text(encoding="utf-8")).get("data")
                except Exception:
                    pass

        secret_paths: Dict[str, List[str]] = {}
        for entry in load_yaml(CATALOGUE)["endpoints"]:
            if only and entry["id"] not in only:
                continue
            base = dict(entry.get("params") or {})
            if entry["scope"] == "appliance":
                base["nePk"] = nepk
            source = entry["source"]
            if source == "single":
                variants = [("orchestrator", base)]
            elif source in ("live", "backup"):
                variants = [(source, base if (reachable is not False or source == "backup") else None)]
            else:
                cached_label, live_label = LABELS[source]
                variants = [(cached_label, dict(base, **{source: "true"}))]
                variants.append((live_label, dict(base, **{source: "false"})) if reachable is not False else (live_label, None))
            for label, query in variants:
                item = {"id": entry["id"], "path": entry["path"], "label": label, "query": query, "sections": entry.get("sections", [])}
                if query is None:
                    item.update(status="unavailable", reason="appliance unreachable; live read not attempted")
                    index.append(item)
                    continue
                name = "{}.{}.json".format(entry["id"], label)
                if entry["scope"] == "orchestrator" and name in orch_cache and role == "sample":
                    payload = orch_cache[name]
                    item["reused"] = True
                else:
                    result = reader.get(entry["path"], query)
                    if not result["ok"]:  # a 403 (the key's RBAC roles refuse this read) is "denied": translate reports it, never silently
                        item.update(status="denied" if result["error"] == "PermissionDeniedError" else "error", error=result["error"], http=result["http"], message=result["message"])
                        index.append(item)
                        continue
                    payload = result["data"]
                    if entry.get("reduce") == "latest-backup" and isinstance(payload, list) and payload:
                        latest = max(payload, key=lambda b: int(b.get("backupTime") or 0))
                        payload = {k: latest.get(k) for k in ("id", "backupTime", "swVersion", "fileName", "runningConfig")}
                        payload["backupCount"] = len(result["data"])
                data, secrets = redact_secrets(payload)
                write_json(tmp / "capture" / name, {
                    "orchestrator": orchestrator, "release": release, "nePk": nepk, "label": label,
                    "path": entry["path"], "query": query, "captured_at": utc_stamp(), "data": data})
                if secrets:
                    secret_paths[name] = secrets
                item.update(status="ok", file="capture/" + name, empty=data in (None, [], {}, ""))
                index.append(item)

        run = {
            "stage": "capture", "orchestrator": orchestrator, "release": release, "role": role,
            "appliance": {"nePk": nepk, "hostName": target.get("hostName"), "model": target.get("model"),
                          "softwareVersion": target.get("softwareVersion"), "state": target.get("state")},
            "reachable": reachable, "started": stamp, "finished": utc_stamp(), "only": only or None, "reused_orchestrator_reads": reuse,
            "counts": {s: sum(1 for i in index if i["status"] == s) for s in ("ok", "error", "unavailable")},
            "write_requests": client.write_requests, "secret_paths": secret_paths,
        }
        write_json(tmp / "capture-index.json", index)
        write_json(tmp / "run.json", run)
    except BaseException:  # a stopped capture leaves no half-written folder behind
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    final = out_root / nepk / stamp
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(final)
    return final


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--orchestrator", required=True, help=".env profile nickname, e.g. prod or lab")
    parser.add_argument("--appliance", required=True, help="nePk (e.g. 0.NE) or exact hostname")
    parser.add_argument("--role", required=True, choices=("reference", "sample"))
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between requests (default 0.2)")
    parser.add_argument("--only", nargs="*", help="catalogue ids to capture (default: all)")
    parser.add_argument("--reuse-orchestrator-reads", action="store_true",
                        help="sample role: reuse Orchestrator-scope reads from captures of the last hour (bulk corpus runs; never after an apply)")
    args = parser.parse_args(argv)
    out_root = WORK_DIR / ("reference" if args.role == "reference" else "samples") / args.orchestrator
    try:
        final = capture(args.orchestrator, args.appliance, args.role, args.pause, out_root, args.only, args.reuse_orchestrator_reads)
    except Stop as stop:
        print("STOPPED: {}".format(stop), file=sys.stderr)
        return 2
    run = json.loads((final / "run.json").read_text())
    print("captured {} {} ({}) release {} reachable={} ok={ok} error={error} unavailable={unavailable} write_requests={w}".format(
        args.orchestrator, run["appliance"]["nePk"], run["appliance"]["hostName"], run["release"], run["reachable"], w=run["write_requests"], **run["counts"]))
    print(final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
