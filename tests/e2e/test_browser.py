"""The GUI in a real browser (docs/gui/BUILD_PLAN.md step 11), over the in-process server of gui_launcher.py and its fake Orchestrator.

- The smoke test goes from an appliance to written YAML files as a person would: Find appliances, build h1, tick one more setting,
  Save template and CSV (the browser downloads both), choose those two files on "Make the preconfig files", Create the YAML files,
  and download the written file from the row table; it must equal the file in the run's folder.
- The axe check runs axe-core on every screen (home and fleet, runs, run, pick with the compare panel open, build, inventory,
  make, yaml), the save dialog and the compare Table with each site's own values, in the light and the dark theme, and fails on any serious or critical violation, and on any
  serious or critical result axe could not decide ("incomplete") unless KNOWN_INCOMPLETE names it with its reason.

Skipped unless EDGECONNECT_BROWSER_TESTS=1, so the normal suite needs no browser; the CI browser job sets it. With it set, a
playwright that does not import is an error and a missing AXE_CORE_JS (the path of a pinned axe.min.js) fails the axe check. EDGECONNECT_BROWSER_PATH names a Chromium binary instead of Playwright's own.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ON = os.environ.get("EDGECONNECT_BROWSER_TESTS") == "1"
if ON:  # the browser job: a broken playwright install must fail here, never turn into a skip
    from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
AXE = os.environ.get("AXE_CORE_JS", "")
# axe results it could not decide ("incomplete") that are serious or critical fail the check like violations, except the
# elements named here (rule -> CSS selector -> reason). axe cannot work out the colour under a translucent background; these
# contrasts were computed from the app.css tokens instead (WCAG formula, both themes).
KNOWN_INCOMPLETE = {"color-contrast": {
    ".bar *": "the sticky top bar is 86% --ink over the page; over --ink or --panel every text colour on it is 4.66:1 or more",
    ".yaml li.on .tag, .yaml li.hover .tag": "--signal on --glow over --ink: 4.85:1 (light), 5.98:1 (dark)",
    "#pick-table td.differs > [aria-hidden=true]": "the decorative ≠ (axe: only non-text characters); --gate on --panel, which tests/test_gui_design.py checks",
}}
# axe on the whole page: [violations, incomplete], an incomplete result's nodes that KNOWN_INCOMPLETE names left out
AXE_RUN = """async (known) => {
  const result = await axe.run(document);
  const unknown = (rule) => (node) => !(known[rule.id] || []).some((selector) => {
    const element = document.querySelector(node.target[0]);
    return element && element.matches(selector);
  });
  return [result.violations, result.incomplete.map((rule) => Object.assign(rule, { nodes: rule.nodes.filter(unknown(rule)) })).filter((rule) => rule.nodes.length)];
}"""
SCREENS = ("home", "build", "pick", "make", "yaml", "inventory", "runs", "run")
TIMEOUT = 30000  # ms; a build against the fake takes a few seconds
if ON:
    expect.set_options(timeout=TIMEOUT)


@unittest.skipUnless(ON, "browser tests: set EDGECONNECT_BROWSER_TESTS=1 (and install playwright)")  # guard-ok: the normal suite needs no browser; CI's browser job sets it
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gui_launcher  # only here: it imports the GUI test fixtures, which a skipped module does not need
        cls.launcher = gui_launcher
        cls.tmp = Path(tempfile.mkdtemp())
        cls.server, cls.patches = gui_launcher.start(cls.tmp)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=os.environ.get("EDGECONNECT_BROWSER_PATH") or None)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.launcher.stop(cls.server, cls.patches)
        shutil.rmtree(str(cls.tmp), ignore_errors=True)

    def open(self, **options):
        """A new browser signed in with the launch link; the link works once, so later pages reuse its cookie."""
        context = self.browser.new_context(accept_downloads=True, reduced_motion="reduce", viewport={"width": 1280, "height": 900}, **options)
        self.addCleanup(context.close)
        if not hasattr(type(self), "cookies"):
            page = context.new_page()
            page.goto(self.server.url)
            type(self).cookies = context.cookies()
        else:
            context.add_cookies(self.cookies)
            page = context.new_page()
            page.goto(self.server.url.split("?")[0])
        page.set_default_timeout(TIMEOUT)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "script errors on the page"))
        expect(page.locator("#fleet")).to_have_attribute("aria-busy", "false")
        expect(page.locator("#orch-status")).to_have_text("● green")
        return page

    def downloads(self, page, count, action):
        """Run action and save the `count` files the browser downloads; returns their paths."""
        got = []
        page.on("download", lambda download: got.append(download))
        action()
        for _ in range(TIMEOUT // 100):
            if len(got) >= count:
                break
            page.wait_for_timeout(100)
        self.assertEqual(len(got), count, [d.suggested_filename for d in got])
        folder = Path(tempfile.mkdtemp(dir=str(self.tmp)))
        paths = []
        for download in got:
            paths.append(folder / download.suggested_filename)
            download.save_as(str(paths[-1]))
        return paths

    def build_h1(self, page):
        page.goto(page.url.split("#")[0] + "#/build")
        page.get_by_role("button", name="Find appliances").click()
        page.get_by_role("radio", name="Build from h1").check()
        page.get_by_role("button", name="Build", exact=True).click()
        expect(page.locator("#build-result-title")).to_be_visible()
        expect(page.get_by_text("Saved h1, validated on lab.")).to_be_visible()

    def test_smoke_from_an_appliance_to_written_yaml_files(self):
        page = self.open()
        page.get_by_role("link", name="Build a template from an appliance").click()
        self.build_h1(page)
        expect(page.locator("#view-build .summary")).not_to_have_text("Loading summary.md…")

        page.get_by_role("link", name="Choose what changes per site").click()
        status = page.locator("#view-pick > div.panel > p[role=status]")
        expect(status).to_contain_text("per site:")
        before = int(status.inner_text().split()[0])
        box = page.locator("#pick-sections input[type=checkbox]:not(:checked)").first.get_attribute("data-pick")
        page.locator('[data-pick="{}"]'.format(box)).check()  # the screen is drawn again after each dry run: the box is found by its setting
        page.wait_for_function("([el, n]) => parseInt(el.textContent) > n", arg=[status.element_handle(), before])
        expect(page.locator('[data-pick="{}"]'.format(box))).to_be_checked()

        page.get_by_role("button", name="Save template and CSV").click()
        page.locator("#pick-save-name").fill("smoke")
        template, sheet = sorted(self.downloads(page, 2, lambda: page.get_by_role("button", name="Save", exact=True).click()), key=lambda p: p.suffix, reverse=True)
        self.assertEqual((template.name, sheet.name), ("smoke.jinja2", "sites.csv"))
        site = self.tmp / "preconfig-output" / "h1" / "site-smoke"
        self.assertEqual(template.read_bytes(), (site / "smoke.jinja2").read_bytes())
        self.assertEqual(sheet.read_bytes(), (site / "sites.csv").read_bytes())
        page.get_by_role("dialog", name="Save template and CSV").get_by_role("button", name="Close").click()

        page.locator("#view-pick").get_by_role("link", name="← All tasks").click()
        page.get_by_role("link", name="Make the preconfig files").click()
        page.locator("#make-template").set_input_files(str(template))
        page.locator("#make-csv").set_input_files(str(sheet))
        expect(page.locator("#make-choice-create")).to_be_checked()
        page.get_by_role("button", name="Make the files").click()
        result = page.locator("#view-make section[aria-labelledby=make-result-title]")
        expect(result.get_by_role("status").first).to_contain_text("Done")
        expect(result).to_contain_text("every cell was checked")
        link = result.locator("table a[download]").first
        expect(link).to_have_text("h1_preconfig.yml")
        written, = self.downloads(page, 1, link.click)
        newest = max((self.tmp / "preconfig-output" / "runs").glob("*/rendered/h1_preconfig.yml"), key=lambda p: p.stat().st_mtime)  # this run's
        self.assertEqual(written.read_bytes(), newest.read_bytes())
        self.assertIn(b"hostname: \"h1\"", written.read_bytes())

    def test_axe_finds_no_serious_or_critical_violation_on_any_screen_in_either_theme(self):
        self.assertTrue(AXE and Path(AXE).is_file(), "set AXE_CORE_JS to the path of a pinned axe.min.js (now: {!r})".format(AXE))
        page = self.open(bypass_csp=True)  # axe-core is injected as an inline script, which the GUI's CSP refuses
        page.add_script_tag(content=Path(AXE).read_text(encoding="utf-8"))
        found = {}

        def check(screen):
            for theme in ("light", "dark"):
                page.evaluate("theme => applyTheme(theme)", theme)
                page.wait_for_timeout(50)
                results = page.evaluate(AXE_RUN, {rule: list(selectors) for rule, selectors in KNOWN_INCOMPLETE.items()})
                found[screen, theme] = [{"id": v["id"], "impact": v["impact"], "incomplete": kind == 1, "nodes": [n["target"] + [n.get("failureSummary", "")] for n in v["nodes"]][:5]}
                                        for kind, listed in enumerate(results) for v in listed]

        expect(page.locator("#builds-text")).to_contain_text("newest first")
        check("home")
        self.build_h1(page)  # its log and summary.md shown
        check("build")

        page.goto(page.url.split("#")[0] + "#/compare/h1")
        expect(page.locator("#view-pick > div.panel > p[role=status]")).to_contain_text("per site:")
        page.get_by_role("button", name="Find sites").click()
        expect(page.get_by_role("checkbox", name="Compare with h2")).to_be_visible()
        expect(page.locator("details.pick-compare")).to_have_attribute("open", "")
        check("pick")
        page.get_by_role("button", name="Save template and CSV").click()
        page.locator("#pick-save-name").fill("axe")
        page.get_by_role("button", name="Save", exact=True).click()
        saving = page.get_by_role("dialog", name="Save template and CSV")
        expect(saving).to_contain_text("Saved site-axe")
        check("save dialog")
        saving.get_by_role("button", name="Close").click()
        page.get_by_role("checkbox", name="Compare with h2").check()
        page.get_by_role("button", name="Compare with the ticked sites").click()
        expect(page.locator("#pick-table th.site")).to_have_text("h2")  # the Table tab, h2's own values beside h1's
        expect(page.locator("#pick-table td.site.differs").first).to_be_visible()
        check("compare table")

        site = [self.tmp / "preconfig-output" / "h1" / "site-axe"]
        page.goto(page.url.split("#")[0] + "#/make")
        page.locator("#make-template").set_input_files(str(next(site[0].glob("*.jinja2"))))
        page.locator("#make-csv").set_input_files(str(site[0] / "sites.csv"))
        page.get_by_role("button", name="Make the files").click()
        expect(page.locator("#view-make table a[download]").first).to_be_visible()
        check("make")

        page.goto(page.url.split("#")[0] + "#/yaml")
        page.locator("#yaml-files").set_input_files(str(next((site[0] / "rendered").glob("*.yml"))))
        page.get_by_role("button", name="Check the files").click()
        expect(page.locator("#view-yaml table a[download], #view-yaml table td").first).to_be_visible()
        check("yaml")

        page.goto(page.url.split("#")[0] + "#/inventory")
        page.get_by_role("button", name="Show the YAML of br-1").click()
        expect(page.locator("#inv-yaml")).to_contain_text("hostname: br-1")
        expect(page.locator("#inv-apps tr").first).to_be_visible()
        check("inventory")

        page.goto(page.url.split("#")[0] + "#/runs")
        first = page.locator("#runs-table a[href^='#/runs/']").first
        expect(first).to_be_visible()
        check("runs")
        first.click()
        expect(page.locator("#run-facts dt").first).to_be_visible()
        check("run")

        counts = {"{} {}".format(*key): [sum(not v["incomplete"] for v in value), sum(v["incomplete"] for v in value)] for key, value in found.items()}
        print("\naxe [violations, incomplete] per screen and theme: " + json.dumps(counts), file=sys.stderr)
        if os.environ.get("AXE_REPORT"):
            Path(os.environ["AXE_REPORT"]).write_text(json.dumps({"{} {}".format(*k): v for k, v in found.items()}, indent=1), encoding="utf-8")
        bad = {"{} {}".format(*key): [v for v in value if v["impact"] in ("serious", "critical")] for key, value in found.items()}
        self.assertEqual({key: value for key, value in bad.items() if value}, {})
        self.assertEqual(sorted({screen for screen, _ in found}), sorted(SCREENS + ("save dialog", "compare table")))


if __name__ == "__main__":
    unittest.main()
