# upload --yaml uploads without --approve; validate --yaml is the dry run

Status: accepted (owner, 2026-10-28)

The owner asked to upload preconfig YAML files that are known to work, without the template and CSV path. `upload --yaml FILE|DIR ...` runs the checks a CSV row gets (YAML shape, `<SECRET:` placeholders, Orchestrator `/validate`, name and serial conflicts) and uploads every file that passes with no confirmation step: no `--approve`, no typed confirmation. The owner's reason: "uploading a file is not dangerous; applying is dangerous and we are not doing it." `validate --yaml` makes the same checks and never writes. The CSV path keeps `--approve` unchanged; `--approve` given with `--yaml` is accepted and changes nothing. Every upload still sends `autoApply: false` and nothing applies (`docs/decisions/0008`). An agent asks the user before it runs `upload --yaml`.

Checked by: `tests/test_upload_yaml.py`, `tests/test_no_apply.py`.
