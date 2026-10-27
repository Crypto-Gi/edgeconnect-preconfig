# A release without its own template uses its family's

Status: accepted (owner, 2026-10-27)

The converter needs the target Orchestrator release's default preconfig template (`references/default-templates/orchestrator-<release>.yml`) for the allow list (`docs/decisions/0013`), section order and the documented option limits. It used to stop when the exact build was missing unless `capture.py` could download it, so an Orchestrator on 9.7.1.42060 whose key could not read the template, or a new 9.8, could not be converted for. On 2026-10-27 the owner ruled that baseline templates live in the repository and that all builds of one release family count as one: a 9.7.1 template serves every 9.7.x, and a release the toolkit has not been tested with is converted with the current template rather than refused.

`common.find_default_template` picks: the exact build (bundled or downloaded by capture); else the newest bundled template with the same major.minor; else the nearest older release, noted as untested; else the nearest newer one. Only an empty template folder stops the build. `summary.md` and `ledger.json` (`default_template`) record the file used and why.

Checked by: `tests/test_templates.py`, `tests/test_capture.py::TranslateMissingTemplateTest`.
