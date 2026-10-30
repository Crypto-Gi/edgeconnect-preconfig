# Installing edgeconnect-preconfig

Back to the [README](../README.md).

## One-line install with `setup.sh`

macOS and Linux; needs git, Python 3.9 or later and, for the skills, Node.js 18 or later:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Crypto-Gi/edgeconnect-preconfig/main/setup.sh)
```

The same `setup.sh` is in all three repositories. It asks a few questions, then does everything else:

1. **Which repositories**: a table of the three, with what each does and the skill it installs; type numbers separated by commas (for example `1,3`) or `all` (the default).
2. **The MCP server** (only if you pick 3): run it locally (stdio) for your agents, or in Docker over HTTPS, which you install by hand with the [MCP install guide](https://github.com/Crypto-Gi/edgeconnect-mcp/blob/main/docs/install.md), or not at all.
3. **Which agents**, once, from a table of 20 (Claude Code, Codex, Cursor, GitHub Copilot, Gemini CLI, OpenCode, Kilo Code, Kiro CLI, Zed, Windsurf, Cline, Goose, Antigravity, Devin, Roo Code, Continue, Amp, Claude Desktop, and any agent that reads `.agents/skills`). Every picked skill is installed for them, also when it was already installed, and the local MCP server is added to them: through [`add-mcp`](https://www.npmjs.com/package/add-mcp) (without auto-approving tool calls), through `devin mcp add` for Devin, or as a printed config to paste for agents neither covers. Before adding the server it asks to continue, because an existing server named `edgeconnect` in those agents is replaced; with `--yes` it adds the server only when you also pass `--mcp stdio`. It prints each agent's MCP settings file as it goes.
4. **The install folder** (default: the folder you run it from, or `~/edgeconnect` from your home folder).
5. **Where the skills and the MCP server go**, one answer for both: only that folder (the default) or your user account (`~`). Agents that keep MCP servers only for the whole user account (Windsurf, Cline, Goose, Antigravity, Claude Desktop) are asked about one by one when you chose the folder: yes adds the server user-wide for that agent, no skips it. With `--yes` they are skipped, and the last screen lists the command to add each one.

Everything lands in the one folder, from `main`: the repositories, for 1 and 2 a Python environment and the `edgeconnect-auto` (1) and `edgeconnect-preconfig` (2) commands in `~/.local/bin`, for a local MCP server its launcher `<folder>/bin/edgeconnect-mcp`, and one shared `.env`. The last screen shows how to fill in `.env` for what you picked: the Orchestrator lines, the API key, a self-signed certificate, `EC_VALIDATORS` for preconfig and MCP writes. It also writes `AGENTS.md` (which tool an agent should use for which task, and what this folder has installed) and a `CLAUDE.md` that points to it, so start your agent there; an `AGENTS.md` that `setup.sh` wrote and you never edited is kept up to date on every run, and one you have edited is never overwritten (the new text goes to `AGENTS.md.new`). Run it again to update. Options for answering without prompts: `--repos 1,3` (or `all`), `--mcp stdio|docker|no`, `--agents 1,2,15` (numbers or names such as `claude-code,codex`), `--dir PATH`, `--skills folder|home`, `--yes`.

Picking 3 installs the MCP repository's skills (`setup.sh` installs every skill a picked repository has: `edgeconnect-guide` and, since edgeconnect-mcp v0.3.0, `edgeconnect-report`) and, if you choose local, the stdio MCP server for your agents; a Docker server is installed by hand, as the [edgeconnect-mcp README](https://github.com/Crypto-Gi/edgeconnect-mcp#install) covers.

## Manual install (no `setup.sh`)

### 1. Clone & Install Python Dependencies
Requires **Python 3.9+** and two packages (`Jinja2` and `PyYAML`; the EdgeConnect Orchestrator REST client is bundled in `scripts/common.py` using Python's standard library):

```bash
git clone https://github.com/Crypto-Gi/edgeconnect-preconfig.git
cd edgeconnect-preconfig
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Without `setup.sh` there is no `edgeconnect-preconfig` command; run the same commands as `python3 toolkit/scripts/cli.py COMMAND ...` inside the virtual environment. The tests need the same packages (`python3 -m unittest discover -s tests`).

### 2. Configure `.env`
Copy `.env.example` to `.env` in your working directory, or set `EC_DOTENV=/path/to/.env`. Without `EC_DOTENV` the scripts use the first `.env` they find: the working directory, then the skill folder and each folder above it (so the install folder's shared `.env` is found). Environment variables of the same name override `.env` values.

```dotenv
# Read-only reference Orchestrator profile
orch_1=main
main_base_url=https://orchestrator.example.com
main_api_key=YOUR_READ_ONLY_API_KEY

# Lab / staging Orchestrator profile (used for /validate and optional upload)
orch_2=lab
lab_base_url=https://lab-orchestrator.example.com
lab_api_key=YOUR_LAB_API_KEY
# Self-signed lab certificate: pin it instead of turning checks off.
# lab_tls_sha256=<SHA-256 fingerprint, e.g. from: openssl x509 -noout -fingerprint -sha256>
# Orchestrators /validate may run on: exact nicknames, comma-separated; no other profile is allowed
EC_VALIDATORS=lab
```

- Each profile is a nickname: `orch_N=<nick>`, `<nick>_base_url` (https only; the path defaults to `/gms/rest`) and `<nick>_api_key`. Optional: `<nick>_tls_sha256` (pin a self-signed certificate; a profile name never turns certificate checks off), `<nick>_ca_bundle=/path/to/ca.pem` (a private CA), `<nick>_api_key_header` (default `X-Auth-Token`).
- `EC_VALIDATORS` lists the nicknames `/validate` may run on (build, variables, compare, validate, render, check), exactly as named in `orch_N`. A nickname is never allowed for what it contains: a profile not listed is refused, and with `EC_VALIDATORS` unset none is allowed. `upload` does not read this list; it sends only with `--approve`.
- `/validate` may need more than a read-only key: in testing, Orchestrator refused it (HTTP 403) for a read-only key, although nothing is saved. A read-only key is enough for capture and `list`.

Every key, its default and the values it accepts: [`.env.example`](../.env.example).
