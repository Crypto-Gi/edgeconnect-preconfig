# The toolkit cannot apply a preconfig

Status: accepted (owner, 2026-10-22); supersedes 0007

No script in this repository applies a preconfig to an appliance, and none can ask Orchestrator to do it later: `upload.py` always sends `autoApply: false` and has no `--auto-apply` option, and nothing calls `/gms/appliance/preconfiguration/apply`. The toolkit stops at a validated upload; applying is a person's act in Orchestrator. The owner's reason: an AI agent using these scripts must not be able to change a live appliance, whatever it is told.

The lab apply-and-diff tool used to verify the converter now lives outside the repository, in the private lab folder, behind two terminal gates (a fresh code, then `yes`, read only from `/dev/tty`). Its no-network parts, the lab guardrails and the leaf-by-leaf diff, stay here as `tests/harnesses/lab_diff.py`.

Considered: keeping apply behind the two gates in the toolkit (0007), rejected because an agent with shell access can fake a terminal or edit the gate; a personal PIN, not needed once the toolkit has no apply path.

Checked by: `tests/test_no_apply.py` (fails if any script or test calls the apply API or sets autoApply true).
