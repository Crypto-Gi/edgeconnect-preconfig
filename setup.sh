#!/usr/bin/env bash
# Install or update the EdgeConnect toolkit from main, in one folder (macOS and Linux): you pick
# the repositories (the edgeconnect-auto CLI, the preconfig scripts, the MCP guide skill) and each
# one's agent skill, plus, if you want it, the MCP server run locally (stdio) for the agents you
# choose. The same file ships in edgeconnect-orchestrator-automation, edgeconnect-preconfig and
# edgeconnect-mcp. A Docker (HTTPS) MCP server is installed by hand from the edgeconnect-mcp guide.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Crypto-Gi/edgeconnect-orchestrator-automation/main/setup.sh)
#
# Run it again to update. Anything already installed is reported and kept; only what is missing
# is added.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: setup.sh [--repos LIST] [--mcp stdio|docker|no] [--agents LIST] [--dir PATH]
                [--skills home|folder] [--yes]

  --repos LIST       repositories to install, by number: 1,2,3 or "all" (default: all)
                     1) edgeconnect-orchestrator-automation  2) edgeconnect-preconfig
                     3) edgeconnect-mcp (guide skill, and the server if --mcp stdio)
  --mcp stdio        with 3: also run the MCP server locally for your agents (default)
  --mcp docker       with 3: you run it in Docker over HTTPS; print the install guide link
  --mcp no           with 3: guide skill only
  --dir PATH         install folder (default: the current folder, or ~/edgeconnect when run
                     from your home folder, or $EDGECONNECT_HOME)
  --skills folder    1) skills and MCP server only in the install folder (default)
  --skills home      2) skills and MCP server for your user account (~), in every project
  --agents LIST      agents for the skills and the MCP server, asked once: numbers from the
                     agent table or names (claude-code,codex,cursor,...), comma-separated
                     (default: 1, Claude Code). Every picked skill is (re)installed for them.
  --yes              accept the defaults without asking
EOF
}

ROOT_GIVEN="${EDGECONNECT_HOME:-}"
PICK=""
MCP=""
MCP_AGENTS=""
SCOPE=""
AGENTS=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repos) PICK="${2:?--repos needs a list such as 1,3 or all}"; shift 2 ;;
    --mcp) MCP="${2:?--mcp needs stdio, docker or no}"; shift 2 ;;
    --mcp-agents) AGENTS="${2:?--mcp-agents needs a list}"; shift 2 ;;  # 1.2.7 name, kept as an alias of --agents
    --dir) ROOT_GIVEN="${2:?--dir needs a path}"; shift 2 ;;
    --skills) SCOPE="${2:?--skills needs home or folder}"; shift 2 ;;
    --agents) AGENTS="${2:?--agents needs a list}"; shift 2 ;;
    --refresh-skills) shift ;;  # no longer needed: skills are always installed for the agents you choose
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

BIN="$HOME/.local/bin"
export NPM_CONFIG_UPDATE_NOTIFIER=false  # keep npx output to what this script prints
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

MCP_GUIDE="https://github.com/Crypto-Gi/edgeconnect-mcp/blob/main/docs/install.md"
MCP_MODE=no
MCP_AGENT_LIST=""
if picked edgeconnect-mcp; then
  if [ -z "$MCP" ]; then
    if [ "$ASSUME_YES" -eq 0 ] && [ "$HAVE_TTY" -eq 1 ]; then
      printf '\nInstall the MCP server too?\n  1) Yes, local (stdio) for your agents   (default)\n  2) No, I run it in Docker over HTTPS\n  3) No\n' >/dev/tty
    fi
    MCP="$(ask "Choose 1, 2 or 3" "1")"
  fi
  case "$MCP" in
    1|stdio|local|yes) MCP_MODE=stdio ;;
    2|docker|https|http) MCP_MODE=docker ;;
    3|no|none) MCP_MODE=no ;;
    *) die "choose 1 (local stdio), 2 (Docker over HTTPS) or 3 (no), not $MCP" ;;
  esac
fi
if [ "$MCP_MODE" = no ] && [ -n "$MCP" ] && ! picked edgeconnect-mcp; then
  die "--mcp needs repository 3 (edgeconnect-mcp) in the pick"
fi

# One agent question for every skill and the MCP server. Columns: number|name|skills agent|MCP route|MCP agent
# MCP route: folder = add-mcp in this folder or user-wide; user = add-mcp user-wide only; devin = devin mcp add;
# paste = no installer, the config is printed; - = no MCP. Skills agent - = the agent has no skills.
AGENT_TABLE='1|Claude Code|claude-code|folder|claude-code
2|Codex|codex|folder|codex
3|Cursor|cursor|folder|cursor
4|GitHub Copilot in VS Code|github-copilot|folder|vscode
5|GitHub Copilot CLI|github-copilot|folder|github-copilot-cli
6|Gemini CLI|gemini-cli|folder|gemini-cli
7|OpenCode|opencode|folder|opencode
8|Kilo Code|kilo|folder|kilo-code
9|Kiro CLI|kiro-cli|folder|kiro-cli
10|Zed|zed|folder|zed
11|Windsurf|windsurf|user|windsurf
12|Cline|cline|user|cline
13|Goose|goose|user|goose
14|Antigravity|antigravity|user|antigravity
15|Devin|devin|devin|devin
16|Roo Code|roo|paste|-
17|Continue|continue|paste|-
18|Amp|amp|paste|-
19|Claude Desktop|-|user|claude-desktop
20|Other agents that read .agents/skills|universal|paste|-'
if [ -z "$AGENTS" ] && [ "$ASSUME_YES" -eq 0 ] && [ "$HAVE_TTY" -eq 1 ]; then
  {
    printf '\nWhich agents should get the skills%s? (asked once, used for everything)\n\n' "$( [ "$MCP_MODE" = stdio ] && echo ' and the MCP server')"
    line='+----+-------------------------------------------+--------+-----------------------------+'
    echo "$line"; printf '| %-2s | %-41s | %-6s | %-27s |\n' '#' 'Agent' 'Skills' 'MCP server'; echo "$line"
    printf '%s\n' "$AGENT_TABLE" | while IFS='|' read -r num name skill route _; do
      case "$route" in folder) mcp="this folder or user-wide" ;; user) mcp="user-wide only" ;; devin) mcp="devin mcp add" ;; paste) mcp="config printed to paste" ;; *) mcp="-" ;; esac
      printf '| %-2s | %-41s | %-6s | %-27s |\n' "$num" "$name" "$( [ "$skill" = - ] && echo no || echo yes)" "$mcp"
    done
    echo "$line"; echo
  } >/dev/tty
  AGENTS="$(ask 'Type numbers separated by commas (e.g. 1,2,15), or "all"' "1")"
fi
SKILL_AGENTS=""; MCP_FOLDER=""; MCP_USER=""; MCP_DEVIN=0; MCP_PASTE=""; AGENT_NAMES=""
add_word() { case " $(eval echo \"\$$1\") " in *" $2 "*) ;; *) eval "$1=\"\${$1:+\$$1 }$2\"" ;; esac; }
for token in $(printf '%s' "${AGENTS:-1}" | tr 'A-Z,' 'a-z '); do
  [ "$token" = all ] && token="$(printf '%s\n' "$AGENT_TABLE" | cut -d'|' -f1 | tr '\n' ' ')"
  for t in $token; do
    row="$(printf '%s\n' "$AGENT_TABLE" | awk -F'|' -v t="$t" '$1 == t || $3 == t || $5 == t { print; exit }')"
    if [ -z "$row" ]; then
      case "$t" in *[!a-z0-9-]*|"") die "unknown agent $t: use numbers from the table or agent names such as claude-code,codex" ;; esac
      row="0|$t|$t|paste|-"  # another agent the skills installer knows; its MCP config is printed
    fi
    IFS='|' read -r _ name skill route mcp <<EOF
$row
EOF
    add_word AGENT_NAMES "$(printf '%s' "$name" | tr ' ' '_')"
    [ "$skill" != - ] && add_word SKILL_AGENTS "$skill"
    case "$route" in
      folder) add_word MCP_FOLDER "$mcp" ;;
      user) add_word MCP_USER "$mcp" ;;
      devin) MCP_DEVIN=1 ;;
      paste) add_word MCP_PASTE "$(printf '%s' "$name" | tr ' ' '_')" ;;
    esac
  done
done
echo "Agents: $(printf '%s' "$AGENT_NAMES" | sed 's/ /, /g' | tr '_' ' ')"

command -v git >/dev/null || die "git is required"
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys, venv; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
if [ -z "$PYTHON" ] && { [ "$NEED_PYTHON" -eq 1 ] || [ "$MCP_MODE" = stdio ]; }; then
  die "Python 3.9 or later with the venv module is required (on Debian/Ubuntu: apt install python3-venv)"
fi

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
    what="the skills"; [ "$MCP_MODE" = stdio ] && what="the skills and the MCP server"
    printf '\nWhere should %s go?\n  1) Only in this folder: %s   (default)\n  2) Your user account (~), in every project\n' "$what" "$ROOT" >/dev/tty
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
fi  # NEED_PYTHON

if [ "$NEED_PYTHON" -eq 1 ] || [ "$MCP_MODE" = stdio ]; then

say "Shared settings in $ROOT/.env"
if [ -e "$ROOT/.env" ]; then
  echo "already exists, kept unchanged"
else
  (umask 077; cat > "$ROOT/.env" <<'EOF'
# Shared by edgeconnect-auto, the preconfig scripts and the local MCP server. Never commit or share this file.
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
# MCP server: writes stay off unless you turn them on; options in
#   https://github.com/Crypto-Gi/edgeconnect-mcp#configuration
EOF
  )
  echo "created: replace the placeholders before the first run"
fi
chmod 600 "$ROOT/.env"

say "Commands"
install_command() {  # name [folder, default $BIN]; wrapper body on stdin. Never replaces a command this script did not write.
  local target="${2:-$BIN}/$1" body
  mkdir -p "${2:-$BIN}"
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
if [ "$MCP_MODE" = stdio ]; then
  # The server needs only the standard library: the toolkit's Python when there is one, else the system one.
  if [ -x "$ROOT/.venv/bin/python" ]; then MCP_PY="$ROOT/.venv/bin/python"; else MCP_PY="$(command -v "$PYTHON")"; fi
# In the install folder, not ~/.local/bin: each folder's agents start that folder's server and .env.
install_command edgeconnect-mcp "$ROOT/bin" <<EOF
# Runs the EdgeConnect MCP server (stdio) from $ROOT/edgeconnect-mcp with the shared .env.
PYTHONPATH="$ROOT/edgeconnect-mcp/src\${PYTHONPATH:+:\$PYTHONPATH}" exec "$MCP_PY" -m edgeconnect_mcp --dotenv "\${EDGECONNECT_DOTENV:-$ROOT/.env}" "\$@"
EOF
fi
if [ "$NEED_PYTHON" -eq 1 ]; then
  case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "$BIN is not on your PATH; add this line to your shell profile: export PATH=\"$BIN:\$PATH\"" ;;
  esac
fi
fi  # .env and commands

if [ "$SCOPE" = home ]; then
  say "Agent skills for your user (~/)"
  SCOPE_FLAG="-g"; SKILLS_CWD="$HOME"
else
  say "Agent skills inside $ROOT"
  SCOPE_FLAG=""; SKILLS_CWD="$ROOT"
fi
HAVE_NPX=0; command -v npx >/dev/null && HAVE_NPX=1
if [ -z "$SKILL_AGENTS" ]; then
  echo "none of the agents you picked takes skills"
elif [ "$HAVE_NPX" -eq 0 ]; then
  echo "Node.js 18 or later is needed to install the skills; after installing it, run setup.sh again"
else
  # shellcheck disable=SC2046  # one -a per agent
  AGENT_FLAGS="$(printf -- '-a %s ' $SKILL_AGENTS)"
  for repo in $REPOS; do
    for skill_dir in "$ROOT/$repo"/skills/*/; do
      skill="$(basename "$skill_dir")"
      # Installed or not, every picked skill goes to every chosen agent: that also updates it from main.
      # shellcheck disable=SC2086
      (cd "$SKILLS_CWD" && npx -y skills add "$ROOT/$repo" -s "$skill" $SCOPE_FLAG $AGENT_FLAGS -y >/dev/null) || die "$skill: the skills installer failed"
      echo "$skill: installed for $SKILL_AGENTS"
    done
  done
fi

if [ "$MCP_MODE" = docker ]; then
  say "MCP server"
  echo "You run it in Docker over HTTPS: install it manually with the repo guide: $MCP_GUIDE"
elif [ "$MCP_MODE" = stdio ]; then
  MCP_CMD="$ROOT/bin/edgeconnect-mcp"
  if [ "$SCOPE" = home ]; then say "MCP server for your agents, in every project"; else say "MCP server for your agents, in $ROOT"; fi
  # Agents that keep MCP servers only for the whole user account go user-wide even for "this folder":
  # add-mcp would otherwise move every agent of the call user-wide.
  if [ "$SCOPE" = home ]; then for a in $MCP_FOLDER; do add_word MCP_USER "$a"; done; MCP_FOLDER=""; fi
  if [ -n "$MCP_FOLDER$MCP_USER" ]; then
    echo "Adding edgeconnect ($MCP_CMD) to:"
    [ -n "$MCP_FOLDER" ] && echo "  this folder: $MCP_FOLDER"
    [ -n "$MCP_USER" ] && echo "  your user account, every project: $MCP_USER"
    echo "An existing MCP server named edgeconnect in those agents is replaced."
    go="$(ask "Continue" "Y")"
    case "$go" in
      [Nn]*) echo "skipped; to add it later: npx -y add-mcp $MCP_CMD -n edgeconnect -a <agent> (add -g for user-wide)" ;;
      *)
        if [ "$HAVE_NPX" -eq 0 ]; then
          echo "Node.js 18 or later is needed for this step; after installing it, run setup.sh again"
        else
          # No --auto-approve: the agents still ask before each MCP tool call.
          # shellcheck disable=SC2046,SC2086
          if [ -n "$MCP_FOLDER" ]; then
            (cd "$ROOT" && npx -y add-mcp "$MCP_CMD" -n edgeconnect $(printf -- '-a %s ' $MCP_FOLDER) -y >/dev/null) || die "add-mcp failed for $MCP_FOLDER"
            echo "added for this folder: $MCP_FOLDER (some agents, such as Claude Code, ask you once to approve it)"
          fi
          # shellcheck disable=SC2046,SC2086
          if [ -n "$MCP_USER" ]; then
            (cd "$ROOT" && npx -y add-mcp "$MCP_CMD" -n edgeconnect -g $(printf -- '-a %s ' $MCP_USER) -y >/dev/null) || die "add-mcp failed for $MCP_USER"
            echo "added for your user account: $MCP_USER"
          fi
        fi ;;
    esac
  fi
  if [ "$MCP_DEVIN" -eq 1 ]; then
    DEVIN_SCOPE=local; [ "$SCOPE" = home ] && DEVIN_SCOPE=user
    if ! command -v devin >/dev/null; then
      echo "Devin: the devin command is not installed; after installing it, run in $ROOT: devin mcp add -s $DEVIN_SCOPE edgeconnect -- $MCP_CMD"
    else
      DEVIN_OWN="$ROOT/.devin/mcp_config.local.json"; [ "$SCOPE" = home ] && DEVIN_OWN="$HOME/.config/devin/mcp_config.json"
      if [ -f "$DEVIN_OWN" ] && grep -q '"edgeconnect"' "$DEVIN_OWN"; then
        echo "Devin: already has edgeconnect in $DEVIN_OWN, left unchanged"
      elif (cd "$ROOT" && devin mcp add -s "$DEVIN_SCOPE" edgeconnect -- "$MCP_CMD" >/dev/null 2>&1); then
        echo "Devin: added ($DEVIN_OWN)"
      else
        echo "Devin: devin mcp add failed; run it yourself in $ROOT: devin mcp add -s $DEVIN_SCOPE edgeconnect -- $MCP_CMD"
      fi
      # Devin also reads other agents' MCP files (.mcp.json, Windsurf's...): show the entry it resolves.
      seen="$(cd "$ROOT" && devin mcp get edgeconnect 2>/dev/null | sed -n 's/^ *Command: //p' | head -1)"
      if [ -n "$seen" ] && [ "$seen" != "$MCP_CMD" ]; then
        echo "  note: Devin resolves edgeconnect to $seen, from another agent's settings; remove that entry if Devin should use this folder's server"
      fi
    fi
  fi
  if [ -n "$MCP_PASTE" ]; then
    echo "$(printf '%s' "$MCP_PASTE" | sed 's/ /, /g' | tr '_' ' '): no installer for these; add this to the agent's MCP settings (where most agents keep them: https://github.com/Crypto-Gi/edgeconnect-mcp/blob/main/docs/clients.md):"
    printf '  {"mcpServers": {"edgeconnect": {"command": "%s"}}}\n' "$MCP_CMD"
  fi
  if [ -n "$MCP_USER" ] || { [ "$MCP_DEVIN" -eq 1 ] && [ "$SCOPE" = home ]; }; then
    echo "User-wide entries run the server from $ROOT: moving or deleting that folder breaks them (run setup.sh again from the new place)."
  fi
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
- `edgeconnect-mcp/`, if it was picked: its guide skill and, when "Installed here" lists it, the MCP server that your agent starts through `bin/edgeconnect-mcp` with the shared `.env`. Otherwise the MCP server runs separately (for example in Docker). If its tools are not available, say so and continue with the other two; do not try to install or start it.

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
LIST_AUTO='- `edgeconnect-auto` and the skill `managing-edgeconnect-firewall-and-objects` (edgeconnect-orchestrator-automation)'
LIST_PRECONFIG='- `edgeconnect-preconfig` and the skill `building-edgeconnect-preconfig-templates` (edgeconnect-preconfig)'
LIST_MCP='- the skill `edgeconnect-guide` for the MCP server (edgeconnect-mcp); the server itself runs separately'
LIST_MCP_LOCAL='- the skill `edgeconnect-guide` and the MCP server, run locally by `bin/edgeconnect-mcp` with the shared `.env` (edgeconnect-mcp)'
LIST_END='A tool not listed here is not installed in this folder: do not run its commands. If the user needs it, tell them to run setup.sh again and pick it.'
{
  printf '\n## Installed here\n\n'
  if [ -d "$ROOT/edgeconnect-orchestrator-automation/.git" ]; then printf '%s\n' "$LIST_AUTO"; fi
  if [ -d "$ROOT/edgeconnect-preconfig/.git" ]; then printf '%s\n' "$LIST_PRECONFIG"; fi
  if [ -d "$ROOT/edgeconnect-mcp/.git" ]; then
    if [ -x "$ROOT/bin/edgeconnect-mcp" ]; then printf '%s\n' "$LIST_MCP_LOCAL"; else printf '%s\n' "$LIST_MCP"; fi
  fi
  printf '\n%s\n' "$LIST_END"
} >> "$AGENTS_NEW"
section() { sed -n '/^## Installed here$/,/^A tool not listed here is not installed/p' "$1"; }
rest() { sed '/^## Installed here$/,/^A tool not listed here is not installed/d' "$1"; }
LIST_LINES="$ROOT/.AGENTS.md.lines"
printf '%s\n' '## Installed here' '' "$LIST_AUTO" "$LIST_PRECONFIG" "$LIST_MCP" "$LIST_MCP_LOCAL" "$LIST_END" > "$LIST_LINES"
if [ ! -e "$ROOT/AGENTS.md" ]; then
  mv "$AGENTS_NEW" "$ROOT/AGENTS.md"
  echo "AGENTS.md: created"
elif cmp -s "$AGENTS_NEW" "$ROOT/AGENTS.md"; then
  rm -f "$AGENTS_NEW"
  echo "AGENTS.md: already present, up to date"
elif cmp -s <(rest "$AGENTS_NEW") <(rest "$ROOT/AGENTS.md") && ! section "$ROOT/AGENTS.md" | grep -qvxF -f "$LIST_LINES"; then
  mv "$AGENTS_NEW" "$ROOT/AGENTS.md"  # only the generated list changed, and nobody edited it: keep it true
  echo "AGENTS.md: already present, its Installed here list updated"
else
  mv "$AGENTS_NEW" "$ROOT/AGENTS.md.new"
  echo "AGENTS.md: already exists and differs, kept unchanged; the current version is in AGENTS.md.new"
fi
rm -f "$LIST_LINES"
if [ ! -e "$ROOT/CLAUDE.md" ]; then
  printf '@AGENTS.md\n' > "$ROOT/CLAUDE.md"
  echo "CLAUDE.md: created (points to AGENTS.md)"
elif grep -qF '@AGENTS.md' "$ROOT/CLAUDE.md"; then
  echo "CLAUDE.md: already present"
else
  echo "CLAUDE.md: already exists, kept unchanged; add the line @AGENTS.md to it so Claude Code reads AGENTS.md"
fi

say "Done: $REPOS"
if [ "$NEED_PYTHON" -eq 1 ] || [ "$MCP_MODE" = stdio ]; then
  cat <<EOF

Next: fill in $ROOT/.env, the one settings file for everything you installed.
  Open it:   open -e "$ROOT/.env"   (macOS)    or    nano "$ROOT/.env"

  For each Orchestrator, three lines. The nickname (here "lab") is what you type after
  --orchestrator, and what your agent names:
    orch_1=lab
    lab_base_url=https://<Orchestrator address>
    lab_api_key=<REST API key>
  A second Orchestrator gets orch_2=<nickname> and its own two lines.

  API key: create one in Orchestrator, as HPE's API Key page shows:
    https://arubanetworking.hpe.com/techdocs/sdwan/docs/orch/orchestrator/server/api-key/
    Read-Only is enough for reads; writes need a key that may write.
  Self-signed certificate: add lab_tls_sha256=<fingerprint>. Get the fingerprint, and check it
    on the Orchestrator itself:
    openssl s_client -connect <Orchestrator address>:443 </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256
  Private CA instead: lab_ca_bundle=/path/to/ca.pem
EOF
  if picked edgeconnect-preconfig; then cat <<'EOF'

  Preconfig: name the Orchestrators that may check rendered files with /validate, a lab one:
    EC_VALIDATORS=lab
EOF
  fi
  if [ "$MCP_MODE" = stdio ]; then cat <<'EOF'

  MCP server: read-only until you allow writes for an Orchestrator:
    lab_mcp_writes=true
  Writes stay two-step (the agent shows the change and asks first). Every option:
    https://github.com/Crypto-Gi/edgeconnect-mcp#configuration
EOF
  fi
  echo
  echo "Then check:"
  if picked edgeconnect-orchestrator-automation; then echo "  edgeconnect-auto --orchestrator lab discovery"; fi
  if picked edgeconnect-preconfig; then echo "  edgeconnect-preconfig capture --help"; fi
  if [ "$MCP_MODE" = stdio ]; then echo "  restart your agents and ask: list my EdgeConnect orchestrators"; fi
fi
echo
echo "Start your agent in $ROOT (it reads AGENTS.md there), and restart it so it loads the skills and MCP servers."
if [ "$MCP_MODE" = docker ]; then
  echo "The MCP server runs in your Docker: install it manually with the repo guide: $MCP_GUIDE"
elif picked edgeconnect-mcp && [ "$MCP_MODE" = no ]; then
  echo "The MCP server is not installed (run setup.sh again, pick 3 and then 1 to run it locally, or see $MCP_GUIDE)"
elif ! picked edgeconnect-mcp; then
  echo "The MCP server and its guide skill are separate (run setup.sh again and pick 3): https://github.com/Crypto-Gi/edgeconnect-mcp"
fi
