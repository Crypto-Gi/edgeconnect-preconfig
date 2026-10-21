#!/usr/bin/env bash
# Install or update the EdgeConnect toolkit from main, in one folder (macOS and Linux): you pick
# the repositories (the edgeconnect-auto CLI, the preconfig scripts, the MCP guide skill) and each
# one's agent skill. The same file ships in edgeconnect-orchestrator-automation,
# edgeconnect-preconfig and edgeconnect-mcp. The MCP server itself is never installed here: it runs
# separately (see the edgeconnect-mcp README).
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Crypto-Gi/edgeconnect-orchestrator-automation/main/setup.sh)
#
# Run it again to update. Anything already installed is reported and kept; only what is missing
# is added.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: setup.sh [--repos LIST] [--dir PATH] [--skills home|folder] [--agents LIST] [--refresh-skills] [--yes]

  --repos LIST       repositories to install, by number: 1,2,3 or "all" (default: all)
                     1) edgeconnect-orchestrator-automation  2) edgeconnect-preconfig
                     3) edgeconnect-mcp (guide skill only)
  --dir PATH         install folder (default: the current folder, or ~/edgeconnect when run
                     from your home folder, or $EDGECONNECT_HOME)
  --skills folder    1) skills only in the install folder (default)
  --skills home      2) skills for your user account (~), for every project
  --agents LIST      agents for the skills, comma-separated (e.g. claude-code,codex),
                     instead of choosing them in the skills installer
  --refresh-skills   reinstall skills that are already installed, from main
  --yes              accept the defaults without asking
EOF
}

ROOT_GIVEN="${EDGECONNECT_HOME:-}"
PICK=""
SCOPE=""
REFRESH=0
AGENTS=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repos) PICK="${2:?--repos needs a list such as 1,3 or all}"; shift 2 ;;
    --dir) ROOT_GIVEN="${2:?--dir needs a path}"; shift 2 ;;
    --skills) SCOPE="${2:?--skills needs home or folder}"; shift 2 ;;
    --agents) AGENTS="${2:?--agents needs a list}"; shift 2 ;;
    --refresh-skills) REFRESH=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

BIN="$HOME/.local/bin"
MARK="# edgeconnect setup.sh"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
HAVE_TTY=0
{ : </dev/tty; } 2>/dev/null && HAVE_TTY=1
ask() {  # question, default -> answer (the default when --yes or no terminal)
  local answer=""
  if [ "$ASSUME_YES" -eq 0 ] && [ "$HAVE_TTY" -eq 1 ]; then
    printf '%s [%s]: ' "$1" "$2" >/dev/tty
    IFS= read -r answer </dev/tty || answer=""
  fi
  printf '%s' "${answer:-$2}"
}

if [ -z "$PICK" ] && [ "$ASSUME_YES" -eq 0 ] && [ "$HAVE_TTY" -eq 1 ]; then
  cat >/dev/tty <<'EOF'

Which repositories do you want to install?

+---+--------------------------------------------+-----------------------------+
| # | Repository                                 | What it does                |
+---+--------------------------------------------+-----------------------------+
| 1 | edgeconnect-orchestrator-automation        | edgeconnect-auto command:   |
|   |                                            | bulk firewall policies,     |
|   | Skill:                                     | address/service groups,     |
|   | managing-edgeconnect-firewall-and-objects  | application definitions     |
+---+--------------------------------------------+-----------------------------+
| 2 | edgeconnect-preconfig                      | edgeconnect-preconfig       |
|   |                                            | command: capture appliance, |
|   | Skill:                                     | build YAML template + CSV,  |
|   | building-edgeconnect-preconfig-templates   | validate and upload         |
+---+--------------------------------------------+-----------------------------+
| 3 | edgeconnect-mcp                            | Guide skill for the MCP     |
|   |                                            | server only (the server     |
|   | Skill:                                     | runs in your Docker,        |
|   | edgeconnect-guide                          | not installed here)         |
+---+--------------------------------------------+-----------------------------+

EOF
  PICK="$(ask 'Type numbers separated by commas (e.g. 1,3), or "all"' "all")"
fi
REPOS=""
for n in $(printf '%s' "${PICK:-all}" | tr 'A-Z,' 'a-z '); do
  case "$n" in
    all) names="edgeconnect-orchestrator-automation edgeconnect-preconfig edgeconnect-mcp" ;;
    1) names="edgeconnect-orchestrator-automation" ;;
    2) names="edgeconnect-preconfig" ;;
    3) names="edgeconnect-mcp" ;;
    *) die "choose numbers from 1 to 3 separated by commas, or all; not $n" ;;
  esac
  for name in $names; do
    case " $REPOS " in *" $name "*) ;; *) REPOS="${REPOS:+$REPOS }$name" ;; esac
  done
done
[ -n "$REPOS" ] || die "choose at least one repository"
picked() { case " $REPOS " in *" $1 "*) return 0 ;; esac; return 1; }
NEED_PYTHON=0
if picked edgeconnect-orchestrator-automation || picked edgeconnect-preconfig; then NEED_PYTHON=1; fi
echo "Installing: $REPOS"

command -v git >/dev/null || die "git is required"
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys, venv; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
[ -n "$PYTHON" ] || die "Python 3.9 or later with the venv module is required (on Debian/Ubuntu: apt install python3-venv)"

if [ -n "$ROOT_GIVEN" ]; then
  ROOT="$ROOT_GIVEN"
elif [ "$PWD" != "$HOME" ]; then
  ROOT="$PWD"  # "this folder": where the user ran the script
else
  ROOT="$HOME/edgeconnect"
fi
ROOT="$(ask "Install folder" "$ROOT")"
case "$ROOT" in "~"|"~/"*) ROOT="$HOME${ROOT#\~}" ;; esac
mkdir -p "$ROOT" && ROOT="$(cd "$ROOT" && pwd)" || die "cannot use the folder $ROOT"
if [ -z "$SCOPE" ]; then
  if [ "$ASSUME_YES" -eq 0 ] && [ "$HAVE_TTY" -eq 1 ]; then
    printf '\nWhere should the skills go?\n  1) Only in this folder: %s   (default)\n  2) Your user account (~)\n' "$ROOT" >/dev/tty
  fi
  SCOPE="$(ask "Choose 1 or 2" "1")"
fi
case "$SCOPE" in
  1|folder|project|dir|same) SCOPE=folder ;;
  2|home|global|"~"|"~/") SCOPE=home ;;
  *) die "choose 1 (this folder) or 2 (your user account), not $SCOPE" ;;
esac
PRECONFIG_SCRIPTS="$ROOT/edgeconnect-preconfig/skills/building-edgeconnect-preconfig-templates/scripts"

say "Repositories (main) in $ROOT"
mkdir -p "$ROOT"
for repo in $REPOS; do
  if [ -d "$ROOT/$repo/.git" ]; then
    if [ -n "$(git -C "$ROOT/$repo" status --porcelain --untracked-files=no)" ]; then
      echo "$repo: already cloned, has local changes, not updated (commit or discard them, then run again)"
    else
      git -C "$ROOT/$repo" checkout -q main || die "$repo: could not switch to main"
      git -C "$ROOT/$repo" pull -q --ff-only origin main || die "$repo: could not update main (diverged, or no network)"
      echo "$repo: already cloned, updated to $(git -C "$ROOT/$repo" rev-parse --short HEAD)"
    fi
  else
    git clone -q "https://github.com/Crypto-Gi/$repo.git" "$ROOT/$repo" || die "$repo: clone failed; check your network access to github.com and run again"
    echo "$repo: cloned at $(git -C "$ROOT/$repo" rev-parse --short HEAD)"
  fi
done

if [ "$NEED_PYTHON" -eq 1 ]; then
say "Python environment in $ROOT/.venv"
if [ -x "$ROOT/.venv/bin/python" ]; then echo "already present"; else "$PYTHON" -m venv "$ROOT/.venv"; fi
# The pip that ships with an older Python (21.x on macOS 3.9) cannot install the CLI in editable mode.
"$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
if picked edgeconnect-orchestrator-automation; then
  "$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check -e "$ROOT/edgeconnect-orchestrator-automation"
  echo "edgeconnect-auto $("$ROOT/.venv/bin/python" -c 'import edgeconnect_automation as e; print(e.__version__)') installed"
fi
if picked edgeconnect-preconfig; then
  "$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$ROOT/edgeconnect-preconfig/requirements.txt"
  echo "preconfig scripts $(cat "$ROOT/edgeconnect-preconfig/skills/building-edgeconnect-preconfig-templates/VERSION" 2>/dev/null || echo "(no VERSION file)"): Jinja2 and PyYAML installed"
fi

say "Shared settings in $ROOT/.env"
if [ -e "$ROOT/.env" ]; then
  echo "already exists, kept unchanged"
else
  (umask 077; cat > "$ROOT/.env" <<'EOF'
# Shared by edgeconnect-auto and the preconfig scripts. Never commit or share this file.
# One slot per Orchestrator; the nickname is what you pass to --orchestrator.
orch_1=lab
lab_base_url=https://<ORCHESTRATOR_FQDN_OR_IP>
lab_api_key=<API_KEY>

# More Orchestrators:
# orch_2=prod
# prod_base_url=https://<ORCHESTRATOR_FQDN_OR_IP>
# prod_api_key=<API_KEY>

# Optional per nickname: <nickname>_ca_bundle=/path/to/ca.pem (private CA),
#   <nickname>_api_key_header=X-Auth-Token,
#   <nickname>_tls_sha256=<fingerprint> (self-signed certificate)
# Preconfig: Orchestrators allowed for /validate, comma-separated nicknames.
# EC_VALIDATORS=lab
EOF
  )
  echo "created: replace the placeholders before the first run"
fi
chmod 600 "$ROOT/.env"

say "Commands in $BIN"
mkdir -p "$BIN"
install_command() {  # name; wrapper body on stdin. Never replaces a command this script did not write.
  local target="$BIN/$1" body
  body="$(cat)"
  if [ -e "$target" ] && ! grep -qF "$MARK" "$target"; then
    echo "$1: $target exists and was not written by this script; left unchanged"
    return
  fi
  local state="installed"
  [ -e "$target" ] && state="already installed, refreshed"
  printf '#!/bin/sh\n%s\n%s\n' "$MARK" "$body" > "$target"
  chmod 755 "$target"
  echo "$1: $state ($target)"
}
if picked edgeconnect-orchestrator-automation; then
install_command edgeconnect-auto <<EOF
exec "$ROOT/.venv/bin/edgeconnect-auto" --dotenv "$ROOT/.env" "\$@"
EOF
fi
if picked edgeconnect-preconfig; then
install_command edgeconnect-preconfig <<EOF
# Usage: edgeconnect-preconfig STAGE [options], where STAGE is capture, compare, translate, render, check, upload, ...
case "\${1:-}" in
  ""|-h|--help|*[!a-z_]*)
    echo "usage: edgeconnect-preconfig STAGE [options]; stages:"
    ls "$PRECONFIG_SCRIPTS" | sed -n 's/[.]py\$//p' | grep -v '^common\$'
    exit 2 ;;
esac
script="$PRECONFIG_SCRIPTS/\$1.py"; shift
[ -f "\$script" ] || { echo "unknown stage; run: edgeconnect-preconfig --help" >&2; exit 2; }
EC_DOTENV="\${EC_DOTENV:-$ROOT/.env}" exec "$ROOT/.venv/bin/python" "\$script" "\$@"
EOF
fi
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "$BIN is not on your PATH; add this line to your shell profile: export PATH=\"$BIN:\$PATH\"" ;;
esac
fi  # NEED_PYTHON

if [ "$SCOPE" = home ]; then
  say "Agent skills for your user (~/)"
  SCOPE_FLAG="-g"; SKILLS_CWD="$HOME"
else
  say "Agent skills inside $ROOT"
  SCOPE_FLAG=""; SKILLS_CWD="$ROOT"
fi
if ! command -v npx >/dev/null; then
  echo "Node.js 18 or later is needed to install the skills; after installing it, run setup.sh again"
else
  installed="$(cd "$SKILLS_CWD" && npx -y skills list $SCOPE_FLAG --json 2>/dev/null | "$PYTHON" -c 'import json, sys
try: print("\n".join(item["name"] for item in json.load(sys.stdin)))
except ValueError: pass' || true)"
  for repo in $REPOS; do
    for skill_dir in "$ROOT/$repo"/skills/*/; do
      skill="$(basename "$skill_dir")"
      if [ "$REFRESH" -eq 0 ] && printf '%s\n' "$installed" | grep -qx "$skill"; then
        echo "$skill: already installed (run with --refresh-skills to update it)"
      elif [ -n "$AGENTS" ]; then
        echo "$skill: installing for $AGENTS"
        # shellcheck disable=SC2046  # one -a per agent
        (cd "$SKILLS_CWD" && npx -y skills add "$ROOT/$repo" -s "$skill" $SCOPE_FLAG $(printf -- '-a %s ' $(echo "$AGENTS" | tr ',' ' ')) -y >/dev/null) || die "$skill: the skills installer failed"
        echo "$skill: installed"
      elif [ "$HAVE_TTY" -eq 1 ]; then
        echo "$skill: installing; choose the agents"
        (cd "$SKILLS_CWD" && npx -y skills add "$ROOT/$repo" -s "$skill" $SCOPE_FLAG </dev/tty) || die "$skill: the skills installer failed"
      else
        echo "$skill: no terminal to choose agents; run setup.sh again with --agents claude-code (or your agents)"
      fi
    done
  done
fi

say "Agent instructions in $ROOT"
# AGENTS.md routes an agent started in this folder to the right tool; CLAUDE.md points Claude Code at it.
# A file you have edited is never overwritten: a newer version is written beside it as AGENTS.md.new.
# Written straight to a file, not through "$(cat <<EOF)": bash 3.2 (macOS) misparses an apostrophe in a heredoc inside $( ).
AGENTS_NEW="$ROOT/.AGENTS.md.setup"
cat > "$AGENTS_NEW" <<'EOF'
# EdgeConnect tools: which one to use, and how

You are working with HPE Aruba EdgeConnect SD-WAN through an Orchestrator. Three tools exist; "Installed here" at the end of this file says which ones this folder has. Choose the tool first, using the table below, then follow that tool's skill. This file only routes and sets the answer style; it never replaces a skill's safety rules, so do not restate or relax them.

## How these tools are used

These tools are run by a person. You help by writing the CSV files and the exact commands; the person pastes and runs them in their own terminal and sends you the output.

- You may run read-only steps yourself when you can (validate, plan, dry run, MCP reads). You never run an `edgeconnect-auto` write (`APPLY`) or an MCP write. Preconfig uploads follow the preconfig skill's own flow: only after the user approves its table, with `--approve`.
- Give each command as one block the person can paste as it is, with the Orchestrator named and the folder it must run from. The `edgeconnect-auto` and `edgeconnect-preconfig` commands already find the shared `.env`, so no `--dotenv` is needed.
- For a write, say what to check on the prompt before typing `APPLY` (the Orchestrator name and the list of changes), and ask them to paste the run summary back so you can read it and offer a read back.
- If the person asks you to "just do it" for an `edgeconnect-auto` write, still hand over the command; the typed approval is theirs.

## Step 1: pick the tool

Find the first row that matches what the user wants.

| If the user wants to... | Use | Skill to read first |
|---|---|---|
| create, change or delete **firewall policies** (global or inter-segment), **zones**, **address groups**, **service groups**, **application definitions**, **application groups**, **AppExpress Monitor** entries, or **template-group ACLs**, especially in bulk from a list or spreadsheet | **`edgeconnect-auto`** (command line) | `managing-edgeconnect-firewall-and-objects` |
| **capture** an appliance's configuration, build a **preconfig template**, or **render, check, validate or upload preconfigs** | **`edgeconnect-preconfig <stage>`** (command line) | `building-edgeconnect-preconfig-templates` |
| look something up, list or explain things, troubleshoot, check status or alarms, **read back** what a change did, or do any other Orchestrator task that is not in the two rows above | **MCP server** (tools `list_orchestrators`, `find_operations`, `describe_operation`, `api_get`, `api_write`) | `edgeconnect-guide` |

If a request fits two rows, use the upper one: it has the stronger approval step. If it fits none, or you are unsure which tool owns an object, say so in one line and ask; do not guess.

Examples: "add 200 application definitions from this sheet" -> `edgeconnect-auto`. "allow segment A to reach segment B on TCP 443" -> `edgeconnect-auto`. "build a preconfig for this branch appliance" -> `edgeconnect-preconfig`. "which appliances are unreachable?" -> MCP. "did that firewall change take effect?" -> MCP read back.

## Step 2: follow the tool's approval flow

Each tool has its own way of making sure a person approves a write. Never skip or imitate it.

- **`edgeconnect-auto`:** you write the CSV and run the read-only checks (validate, plan, dry run). The user alone performs the write by typing `APPLY` in their own terminal. You never type it, never ask the user to paste it into chat, and never get around it.
- **`edgeconnect-preconfig`:** capture, translate, render and validate only read from the Orchestrator or write local files. Nothing is uploaded unless the user explicitly approves, and then only with the script's `--approve` option, as the preconfig skill describes.
- **MCP:** reads are free. The first `api_write` call changes nothing: it returns the exact change and a one-time confirmation code. Show that change to the user. Only after they say yes, repeat the identical call with the code. If the server refuses a write, tell the user and stop; do not look for another way in.

## Rules

1. Objects owned by `edgeconnect-auto` or `edgeconnect-preconfig` (see Step 1) are never written with the MCP `api_write` tool, even if it would work.
2. Name the Orchestrator in every command: `--orchestrator <nickname>` for `edgeconnect-auto` and the preconfig scripts, the `orchestrator` argument for MCP. Never rely on a default. If the user has not said which one, ask. `list_orchestrators` (MCP) shows the configured nicknames.
3. After any write by `edgeconnect-auto` or `edgeconnect-preconfig`, offer an MCP read back and report what it actually returned, not only "verified".
4. Do not start an MCP write while an `edgeconnect-auto` run on the same objects is in progress.
5. Never edit `.env` files, API keys, TLS pins or certificate settings. Tell the user the exact line to add and let them add it. Never print or repeat a secret.
6. A more specific instruction from the user in chat wins over this file, but never over a skill's safety rule or a tool's approval step.

## This folder

- `.env`: the one settings file shared by `edgeconnect-auto` and `edgeconnect-preconfig` (Orchestrator nicknames, URLs, API keys, TLS pins). Both commands find it by themselves, so run them from any folder.
- `edgeconnect-orchestrator-automation/` and `edgeconnect-preconfig/`, when installed: the tools' source, kept current by `setup.sh`. Read them, do not edit them.
- Keep your own CSVs, reports and captures in a separate work folder, not in the tool folders. Never commit them or `.env`.
- `edgeconnect-mcp/`, if it was picked, is there only for its guide skill. The MCP server is installed and run separately (for example in Docker) and is not part of this folder. If its tools are not available, say so and continue with the other two; do not try to install or start it.

## How to answer

- Lead with the answer or the result, then the evidence.
- Say which tool you will use, in one line, before you use it.
- Before anything that writes, show the exact command or change and what it affects, then wait for the user.
- Say "verified" only after a read back shows it. If you could not check something, say so.
- End with what you did not check or are unsure about.
- Keep replies short. Ask one question at a time, and only when a wrong guess would change what traffic or configuration is affected.
- If a command fails, show the message as printed and the fix it suggests. Do not retry with different flags to get past a check.
EOF
# Listed from what the folder holds, so a later run that picks fewer repositories keeps the earlier ones.
{
  printf '\n## Installed here\n\n'
  if [ -d "$ROOT/edgeconnect-orchestrator-automation/.git" ]; then echo '- `edgeconnect-auto` and the skill `managing-edgeconnect-firewall-and-objects` (edgeconnect-orchestrator-automation)'; fi
  if [ -d "$ROOT/edgeconnect-preconfig/.git" ]; then echo '- `edgeconnect-preconfig` and the skill `building-edgeconnect-preconfig-templates` (edgeconnect-preconfig)'; fi
  if [ -d "$ROOT/edgeconnect-mcp/.git" ]; then echo '- the skill `edgeconnect-guide` for the MCP server (edgeconnect-mcp); the server itself runs separately'; fi
  printf '\nA tool not listed here is not installed in this folder: do not run its commands. If the user needs it, tell them to run setup.sh again and pick it.\n'
} >> "$AGENTS_NEW"
if [ ! -e "$ROOT/AGENTS.md" ]; then
  mv "$AGENTS_NEW" "$ROOT/AGENTS.md"
  echo "AGENTS.md: created"
elif cmp -s "$AGENTS_NEW" "$ROOT/AGENTS.md"; then
  rm -f "$AGENTS_NEW"
  echo "AGENTS.md: already present, up to date"
elif cmp -s <(sed '/^## Installed here$/,/^A tool not listed here is not installed/d' "$AGENTS_NEW") <(sed '/^## Installed here$/,/^A tool not listed here is not installed/d' "$ROOT/AGENTS.md"); then
  mv "$AGENTS_NEW" "$ROOT/AGENTS.md"  # only the generated list changed: keep it true
  echo "AGENTS.md: already present, its Installed here list updated"
else
  mv "$AGENTS_NEW" "$ROOT/AGENTS.md.new"
  echo "AGENTS.md: already exists and differs, kept unchanged; the current version is in AGENTS.md.new"
fi
if [ ! -e "$ROOT/CLAUDE.md" ]; then
  printf '@AGENTS.md\n' > "$ROOT/CLAUDE.md"
  echo "CLAUDE.md: created (points to AGENTS.md)"
elif grep -qF '@AGENTS.md' "$ROOT/CLAUDE.md"; then
  echo "CLAUDE.md: already present"
else
  echo "CLAUDE.md: already exists, kept unchanged; add the line @AGENTS.md to it so Claude Code reads AGENTS.md"
fi

say "Done: $REPOS"
step=1
if [ "$NEED_PYTHON" -eq 1 ]; then echo "$step. Fill in $ROOT/.env"; step=$((step + 1)); fi
if picked edgeconnect-orchestrator-automation; then echo "$step. Check: edgeconnect-auto --orchestrator <nickname> discovery"; step=$((step + 1)); fi
if picked edgeconnect-preconfig; then echo "$step. Check: edgeconnect-preconfig capture --help"; step=$((step + 1)); fi
echo "$step. Start your agent in $ROOT (it reads AGENTS.md there) and restart it so it loads the skills"
if picked edgeconnect-mcp; then
  echo "The MCP server itself is not installed here; run it as its README describes: https://github.com/Crypto-Gi/edgeconnect-mcp#install"
else
  echo "The MCP server and its guide skill are separate (run setup.sh again and pick 3 for the skill): https://github.com/Crypto-Gi/edgeconnect-mcp"
fi
