"""compare on fixed synthetic captures (tests/fixtures/compare/) gives fixed outputs: exit code, columns, sites.csv rows and notes.

The goldens are the contract other front ends (the GUI) build on. After an intended change, regenerate them with
`UPDATE_GOLDEN=1 python -m unittest tests.test_compare_golden` and review the diff of tests/fixtures/compare/golden/.
Offline: capture is replaced by the fixture captures, /validate by the offline checks (check.run and render without --validate).
"""
import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "toolkit" / "scripts"))
import check  # noqa: E402
import cli  # noqa: E402
import render  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "compare"
RELEASE = "9.7.1.42046"
SCENARIOS = {  # name -> partner captures (typed as their hostnames), and the answers typed when it is not --yes
    "identical-sites": {"partners": ["identical"]},
    "scalar-differences": {"partners": ["scalars"]},
    "layout-differs-column": {"partners": ["zone"]},
    "layout-differs-fixed": {"partners": ["zone"], "answers": ["edit", "6", "", "y"]},  # 6: deploymentInterfaces[lan0].zone in the tick list
    "partner-only-route": {"partners": ["partner-only"]},
    "base-only-route": {"partners": ["base-only"]},
    "dhcp-relay-vs-server": {"partners": ["relay"]},
    "broken-partner": {"partners": ["broken"]},
    "base-as-partner": {"partners": [], "typed": ["branch-a"]},
}


def offline_checked(folder, site, validator, substitute):
    result, _ = cli.quiet(check.run, folder, site, None, None, False)
    return ["{}: {}".format(n, c["detail"]) for n, c in result["checks"].items() if not c["ok"]]


def compare(tmp: Path, spec: dict) -> dict:
    caps = tmp / "captures"
    shutil.copytree(FIXTURES / "captures", caps)
    build = tmp / "build"
    cli.translate_into(build, caps / "base", "branch-a", RELEASE, [])
    run = json.loads((caps / "base" / "run.json").read_text())
    (build / "build.json").write_text(json.dumps({"orchestrator": "example", "validator": "example-lab", "appliance": run["appliance"], "capture": str(caps / "base"),
                                                   "target_release": RELEASE, "target_ecos": None, "site_type": "branch-a"}))
    typed = spec.get("typed") or ["branch-b"] * len(spec["partners"])
    folders = iter(caps / p for p in spec["partners"])
    answers = iter(spec.get("answers", []))
    argv = ["compare", str(build)] + typed + (["--name", "branch"] if spec.get("answers") else ["--yes", "--name", "branch"])
    with mock.patch("capture.capture", side_effect=lambda *a, **k: next(folders)), mock.patch.object(cli, "checked", offline_checked), \
            mock.patch.object(cli, "validate_rows", lambda folder, csv_path, info, nick: cli.quiet(render.main, ["--site-dir", str(folder), "--csv", str(csv_path)])[0]), \
            mock.patch.dict(cli.VERBS, {"compare": lambda rest: cli.cmd_compare(rest, lambda prompt: next(answers))}), \
            contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(out):
        code = cli.main(argv)
    site = build / "site-branch"
    result = {"exit": code, "notes": [line for line in out.getvalue().splitlines() if line.startswith(("Note:", "Warning:", "error:"))]}
    if (site / "columns.json").is_file():
        result["columns"] = list(json.loads((site / "columns.json").read_text())["columns"])
        with (site / "sites.csv").open(newline="") as handle:
            result["rows"] = list(csv.DictReader(handle))
    return result


class Golden(unittest.TestCase):
    def test_compare_outputs_match_the_goldens(self):
        for name, spec in SCENARIOS.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                got = compare(Path(tmp), spec)
                path = FIXTURES / "golden" / "{}.json".format(name)
                if os.environ.get("UPDATE_GOLDEN"):
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(json.dumps(got, indent=1) + "\n")
                self.assertEqual(got, json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
