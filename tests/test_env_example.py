import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "toolkit" / "scripts"
GUI = ROOT / "toolkit" / "gui"
# env.get("key"), os.environ.get("KEY"), os.environ["KEY"], os.getenv("KEY"); "{}_suffix" is a per-nickname key.
READ = re.compile(r"""(?:\benv\.get|\benviron\.get|\bgetenv)\(\s*["']([^"']+)["']|\benviron\[\s*["']([^"']+)["']\s*\]""")


def keys_read_by_scripts():
    keys = set()
    for script in list(SCRIPTS.glob("*.py")) + list(GUI.glob("*.py")):
        for match in READ.finditer(script.read_text(encoding="utf-8")):
            keys.add(match.group(1) or match.group(2))
    return keys


class EnvExampleDocumentsEveryKey(unittest.TestCase):
    """Every .env or environment key the scripts and the GUI read appears as KEY= (commented or not) in .env.example."""

    def test_every_key_read_is_in_env_example(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        keys = keys_read_by_scripts()
        self.assertGreaterEqual(len(keys), 8, "the key scan found too little; check the READ pattern")
        missing = []
        for key in sorted(keys):
            if key.startswith("{}"):
                pattern = r"(?im)^#?\s*(?:export\s+)?\w+{}=".format(re.escape(key[2:]))
            else:
                pattern = r"(?im)^#?\s*(?:export\s+)?{}=".format(re.escape(key))
            if not re.search(pattern, example):
                missing.append(key.replace("{}", "<nickname>"))
        self.assertEqual(missing, [], "keys read by scripts/ or gui/ but not in .env.example")


if __name__ == "__main__":
    unittest.main()
