"""Copy the toolkit into the skill folder and rebuild dist/.

toolkit/ (VERSION, scripts/, references/) is the only source. The skill ships its own copy so it works where it is
installed alone (npx skills add, a Claude Desktop upload); SKILL.md is the skill's own file.

    python3 tools/build_skill.py           copy toolkit/ into the skill and rebuild dist/*.zip and dist/*.skill
    python3 tools/build_skill.py --check   exit 1, listing the differences, if the copy or dist/ is out of date
"""
import argparse
import filecmp
import io
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
TOOLKIT = ROOT / "toolkit"
NAME = "building-edgeconnect-preconfig-templates"
SKILL = ROOT / "skills" / NAME
DIST = [ROOT / "dist" / (NAME + ".zip"), ROOT / "dist" / (NAME + ".skill")]
COPIED = ["VERSION", "scripts", "references"]


def _files(folder: Path) -> Dict[str, Path]:
    """Relative path -> file, without Python caches."""
    return {str(p.relative_to(folder)): p for p in sorted(folder.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc" and p.name != ".DS_Store"}


def _wanted() -> Dict[str, Path]:
    """Every file the skill must hold: SKILL.md and the toolkit copy."""
    out = {"SKILL.md": SKILL / "SKILL.md"}
    for item in COPIED:
        src = TOOLKIT / item
        out.update({item: src} if src.is_file() else {item + "/" + k: v for k, v in _files(src).items()})
    return out


def _zip_bytes(files: Dict[str, Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(NAME + "/" + rel, date_time=(2026, 1, 1, 0, 0, 0))  # fixed time: the same files give the same archive
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[rel].read_bytes())
    return buffer.getvalue()


def check() -> List[str]:
    problems = []
    have, want = _files(SKILL), _wanted()
    problems += ["missing in the skill: " + rel for rel in sorted(set(want) - set(have))]
    problems += ["not in toolkit/: skills/{}/{}".format(NAME, rel) for rel in sorted(set(have) - set(want))]
    problems += ["differs from toolkit/: skills/{}/{}".format(NAME, rel) for rel in sorted(set(want) & set(have))
                 if rel != "SKILL.md" and not filecmp.cmp(str(want[rel]), str(have[rel]), shallow=False)]
    expected = _zip_bytes(want)
    problems += ["out of date: " + str(d.relative_to(ROOT)) for d in DIST if not d.is_file() or d.read_bytes() != expected]
    return problems


def build() -> None:
    for item in COPIED:
        target = SKILL / item
        if target.is_dir():
            shutil.rmtree(str(target))
        elif target.exists():
            target.unlink()
        src = TOOLKIT / item
        if src.is_dir():
            shutil.copytree(str(src), str(target), ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        else:
            shutil.copy2(str(src), str(target))
    data = _zip_bytes(_wanted())
    for d in DIST:
        d.parent.mkdir(exist_ok=True)
        d.write_bytes(data)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report whether the skill copy and dist/ are up to date")
    if parser.parse_args(argv).check:
        problems = check()
        print("\n".join(problems) if problems else "skill copy and dist/ are up to date")
        return 1 if problems else 0
    build()
    print("copied toolkit/ into skills/{} and rebuilt {}".format(NAME, ", ".join(str(d.relative_to(ROOT)) for d in DIST)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
