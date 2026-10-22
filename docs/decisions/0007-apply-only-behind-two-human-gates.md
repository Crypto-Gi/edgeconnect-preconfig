# Apply only behind two human gates

Status: superseded by 0008 (owner, 2026-10-22)

The toolkit stops at upload. The CLI has no apply command. The two paths that can still apply a preconfig to an appliance, `upload.py --auto-apply` (Orchestrator applies it by itself when the appliance appears) and the lab harness `tests/harnesses/apply_and_diff.py`, first call `common.human_gate`: a person types a fresh random code shown for this run, then `yes` after a warning that lists the Orchestrator, the appliances and the preconfigs. Answers are read only from the controlling terminal (`/dev/tty`), never from stdin, an option or an environment variable, so piped input cannot pass the gates and a run with no terminal stops before anything is sent. The owner's reason: applying changes live appliances, and an AI agent must never be the one that decides it.

Upload itself needs only `--approve`, as before.

Considered: a personal PIN stored as a hash (stops an agent that fakes a terminal, at the cost of setup); an upload key typed per run. Not adopted for now; revisit if an agent is ever seen driving a pseudo-terminal.

Checked by: `tests/test_apply_gate.py`.
