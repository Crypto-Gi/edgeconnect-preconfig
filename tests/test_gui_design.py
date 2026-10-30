"""The GUI's colours meet WCAG AA for text (4.5:1) in both themes, and the page loads only files that are bundled.

Copied from edgeconnect-orchestrator-automation 7af5e9d, tests/test_gui_design.py; only the static folder differs."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "toolkit" / "gui" / "static"
TEXT = ("text", "muted", "signal", "gate", "ok", "bad")


def tokens(block):
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block))


def themes():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    dark = tokens(re.search(r":root \{(.*?)\n\}", css, re.S).group(1))
    media = tokens(re.search(r':root:not\(\[data-theme="dark"\]\) \{(.*?)\}', css, re.S).group(1))
    chosen = tokens(re.search(r':root\[data-theme="light"\] \{(.*?)\}', css, re.S).group(1))
    return dark, media, chosen


def contrast(first, second):
    def luminance(colour):
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class DesignTests(unittest.TestCase):
    def test_text_colours_meet_wcag_aa_in_both_themes(self):
        dark, light, chosen = themes()
        self.assertEqual(light, chosen, "the system light theme and the chosen light theme must match")
        failures = []
        for name, theme in (("dark", dark), ("light", light)):
            pairs = [(fg, bg) for fg in TEXT for bg in ("ink", "panel")] + [("on-signal", "signal")]
            for fg, bg in pairs:
                ratio = contrast(theme[fg], theme[bg])
                if ratio < 4.5:
                    failures.append("{} {} on {}: {:.2f}".format(name, fg, bg, ratio))
        self.assertEqual(failures, [])

    def test_page_uses_only_bundled_files(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        references = re.findall(r'(?:href|src)="([^"#]+)"', html) + re.findall(r"url\(([^)]+)\)", css)
        self.assertTrue(references)
        self.assertEqual([ref for ref in references if not (STATIC / ref).is_file()], [])
        self.assertNotIn("<script>", html)  # the CSP allows only script files from this server


if __name__ == "__main__":
    unittest.main()
