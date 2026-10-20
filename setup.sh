#!/usr/bin/env bash
# Install or update the EdgeConnect toolkit: the edgeconnect-auto CLI, the preconfig scripts and
# their two agent skills, all from main, in one folder (macOS and Linux). The same file ships in
# edgeconnect-orchestrator-automation, edgeconnect-preconfig and edgeconnect-mcp. The MCP server
# and its guide skill are installed separately (see the edgeconnect-mcp README).
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Crypto-Gi/edgeconnect-orchestrator-automation/main/setup.sh)
#
# Run it again to update. Anything already installed is reported and kept; only what is missing
# is added.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: setup.sh [--dir PATH] [--skills home|folder] [--agents LIST] [--refresh-skills] [--yes]

  --dir PATH         install folder (default ~/edgeconnect, or $EDGECONNECT_HOME)
  --skills home      install the skills for your user, for every project (default)
  --skills folder    install the skills inside the install folder only
  --agents LIST      agents for the skills, comma-separated (e.g. claude-code,codex),
                     instead of choosing them in the skills installer
  --refresh-skills   reinstall skills that are already installed, from main
  --yes              accept the defaults without asking
EOF
}

ROOT="${EDGECONNECT_HOME:-$HOME/edgeconnect}"
SCOPE=""
REFRESH=0
AGENTS=""
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) ROOT="${2:?--dir needs a path}"; shift 2 ;;
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
REPOS="edgeconnect-orchestrator-automation edgeconnect-preconfig"

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

command -v git >/dev/null || die "git is required"
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys, venv; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
[ -n "$PYTHON" ] || die "Python 3.9 or later with the venv module is required (on Debian/Ubuntu: apt install python3-venv)"

ROOT="$(ask "Install folder" "$ROOT")"
case "$ROOT" in "~"|"~/"*) ROOT="$HOME${ROOT#\~}" ;; esac
if [ -z "$SCOPE" ]; then
  SCOPE="$(ask "Install the skills for your user in ~/ (home) or only inside $ROOT (folder)?" "home")"
fi
case "$SCOPE" in
  home|global|"~"|"~/") SCOPE=home ;;
  folder|project|dir|same) SCOPE=folder ;;
  *) die "--skills must be home or folder, not $SCOPE" ;;
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

say "Python environment in $ROOT/.venv"
if [ -x "$ROOT/.venv/bin/python" ]; then echo "already present"; else "$PYTHON" -m venv "$ROOT/.venv"; fi
# The pip that ships with an older Python (21.x on macOS 3.9) cannot install the CLI in editable mode.
"$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -q --disable-pip-version-check -e "$ROOT/edgeconnect-orchestrator-automation" -r "$ROOT/edgeconnect-preconfig/requirements.txt"
echo "edgeconnect-auto $("$ROOT/.venv/bin/python" -c 'import edgeconnect_automation as e; print(e.__version__)'), Jinja2 and PyYAML installed"

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
install_command edgeconnect-auto <<EOF
exec "$ROOT/.venv/bin/edgeconnect-auto" --dotenv "$ROOT/.env" "\$@"
EOF
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
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "$BIN is not on your PATH; add this line to your shell profile: export PATH=\"$BIN:\$PATH\"" ;;
esac

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

say "Done"
echo "1. Fill in $ROOT/.env"
echo "2. Check: edgeconnect-auto --orchestrator <nickname> discovery"
echo "3. Restart your agent so it loads the skills"
echo "The MCP server and its guide skill are separate: https://github.com/Crypto-Gi/edgeconnect-mcp"
