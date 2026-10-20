#!/usr/bin/env bash
# Install the EdgeConnect command-line tools and agent skills in one folder (macOS and Linux).
# The same file is in all three repositories; the MCP server itself is not installed by it.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Crypto-Gi/edgeconnect-preconfig/main/setup.sh)
#
# What it does, asking before each choice (-y accepts every default):
#   1. clones or updates the repositories at main into one folder (default ~/edgeconnect)
#   2. builds one Python environment and one shared .env, and adds the commands
#      edgeconnect-auto and edgeconnect-preconfig to ~/.local/bin
#   3. installs the agent skills with the `skills` CLI, either for your whole account (~)
#      or for the current project folder only; skills that are already installed are left alone
# Run it again at any time: whatever is already in place is reported and skipped.
#
# Options: -y, --yes            use the default for every question (the same as no terminal)
#          --dir DIR            install folder (or set EDGECONNECT_HOME)
#          --scope global|project   where the skills go: ~ or the current folder
#          --update             refresh skills that are already installed
#          --skip-skills        tools only
# Without a terminal (or with -y) the skills go to Claude Code and the shared ~/.agents/skills folder
# (Codex and other agents); with a terminal the skills CLI shows its own agent picker.
#          -h, --help
set -euo pipefail

MARK="# edgeconnect setup.sh"
BIN="$HOME/.local/bin"
GITHUB="https://github.com/Crypto-Gi"
TOOLS="edgeconnect-orchestrator-automation edgeconnect-preconfig"
SKILL_REPOS="edgeconnect-orchestrator-automation edgeconnect-preconfig edgeconnect-mcp"  # the MCP repo only supplies its guide skill

YES=0; ROOT="${EDGECONNECT_HOME:-}"; SCOPE=""; UPDATE=0; SKIP_SKILLS=0
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --dir) ROOT="${2:?--dir needs a folder}"; shift ;;
    --scope) SCOPE="${2:?--scope needs global or project}"; shift ;;
    --update) UPDATE=1 ;;
    --skip-skills) SKIP_SKILLS=1 ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
case "$SCOPE" in ""|global|project) ;; *) echo "--scope must be global or project" >&2; exit 2 ;; esac

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
HAVE_TTY=0; if [ "$YES" -eq 0 ] && { : </dev/tty; } 2>/dev/null; then HAVE_TTY=1; fi
ask() {  # ask "question" default -> answer (the default when -y or no terminal)
  local answer=""
  if [ "$HAVE_TTY" -eq 1 ]; then
    printf '%s [%s]: ' "$1" "$2" >/dev/tty
    read -r answer </dev/tty || answer=""
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

say "Where to install"
[ -n "$ROOT" ] || ROOT="$(ask "Install folder for the tools, shared .env and skill sources" "$HOME/edgeconnect")"
case "$ROOT" in "~"*) ROOT="$HOME${ROOT#\~}" ;; esac
mkdir -p "$ROOT"
ROOT="$(cd "$ROOT" && pwd)"
echo "using $ROOT"

say "Repositories (main) in $ROOT"
repos="$TOOLS"; [ "$SKIP_SKILLS" -eq 1 ] || repos="$SKILL_REPOS"
for repo in $repos; do
  if [ -d "$ROOT/$repo/.git" ]; then
    if [ -n "$(git -C "$ROOT/$repo" status --porcelain --untracked-files=no)" ]; then
      echo "$repo: local changes, not updated (commit or discard them, then run again)"
    else
      git -C "$ROOT/$repo" checkout -q main || die "$repo: could not switch to main"
      git -C "$ROOT/$repo" pull -q --ff-only origin main || die "$repo: could not update main (diverged, or no access to GitHub)"
      echo "$repo: updated to $(git -C "$ROOT/$repo" rev-parse --short HEAD)"
    fi
  else
    git clone -q "$GITHUB/$repo.git" "$ROOT/$repo" || die "$repo: clone failed; check your network access to github.com and run again"
    echo "$repo: cloned at $(git -C "$ROOT/$repo" rev-parse --short HEAD)"
  fi
done

say "Python environment in $ROOT/.venv"
if [ -x "$ROOT/.venv/bin/python" ]; then echo "environment already exists, updating packages"; else "$PYTHON" -m venv "$ROOT/.venv"; fi
"$ROOT/.venv/bin/python" -m pip install -q --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -q -e "$ROOT/edgeconnect-orchestrator-automation" -r "$ROOT/edgeconnect-preconfig/requirements.txt"
echo "ready: edgeconnect-auto $("$ROOT/.venv/bin/python" -c 'import edgeconnect_automation as e; print(e.__version__)'), Jinja2 and PyYAML"

say "Shared settings in $ROOT/.env"
if [ -e "$ROOT/.env" ]; then
  echo "already there, kept as it is"
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
PRECONFIG_SCRIPTS="$ROOT/edgeconnect-preconfig/skills/building-edgeconnect-preconfig-templates/scripts"
mkdir -p "$BIN"
install_command() {  # name; wrapper body on stdin. Never replaces a command this script did not write.
  local target="$BIN/$1" body
  body="$(cat)"
  if [ -e "$target" ] && ! grep -qF "$MARK" "$target"; then
    echo "$1: $target exists and was not written by this script; left unchanged"
    return
  fi
  printf '#!/bin/sh\n%s\n%s\n' "$MARK" "$body" > "$target"
  chmod 755 "$target"
  echo "$1 -> $target"
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

if [ "$SKIP_SKILLS" -eq 0 ]; then
  say "Agent skills"
  export DISABLE_TELEMETRY=1
  # One bundle folder, because the skills CLI does not follow symlinks and shows one agent picker per source.
  BUNDLE="$ROOT/skills"
  rm -rf "$BUNDLE"; mkdir -p "$BUNDLE"
  for repo in $SKILL_REPOS; do
    for dir in "$ROOT/$repo"/skills/*/; do
      [ -f "${dir}SKILL.md" ] && cp -R "${dir%/}" "$BUNDLE/"
    done
  done
  names="$(ls "$BUNDLE")"
  if ! command -v npx >/dev/null; then
    echo "Node.js 18 or later is needed for the skills installer; after installing it, run this script again."
  else
    if [ -z "$SCOPE" ]; then
      echo "Where should the skills go?"
      echo "  1) global: your account (~), every project can use them"
      echo "  2) project: only this folder ($PWD)"
      case "$(ask "Choose 1 or 2" 1)" in 2|p|project) SCOPE=project ;; *) SCOPE=global ;; esac
    fi
    gflag="-g"; [ "$SCOPE" = project ] && gflag=""
    installed="$(npx -y skills list $gflag --json 2>/dev/null | "$ROOT/.venv/bin/python" -c 'import json,sys
try: print(" ".join(s["name"] for s in json.load(sys.stdin)))
except Exception: pass' || true)"
    missing=""
    for name in $names; do
      case " $installed " in
        *" $name "*)
          if [ "$UPDATE" -eq 1 ]; then echo "$name: already installed, refreshing"; missing="$missing $name"
          else echo "$name: already installed (run with --update to refresh it)"; fi ;;
        *) echo "$name: not installed yet"; missing="$missing $name" ;;
      esac
    done
    if [ -z "$missing" ]; then
      echo "nothing to install"
    else
      args=""; for name in $missing; do args="$args -s $name"; done
      if [ "$HAVE_TTY" -eq 1 ]; then
        # shellcheck disable=SC2086
        npx -y skills add "$BUNDLE" $gflag $args </dev/tty || echo "the skills installer stopped; run this script again to finish"
      else
        # shellcheck disable=SC2086
        npx -y skills add "$BUNDLE" $gflag $args -a claude-code -a codex -y </dev/null || echo "the skills installer failed; run this script again to finish"
      fi
    fi
  fi
fi

say "Done"
echo "1. Fill in $ROOT/.env"
echo "2. Check: edgeconnect-auto --orchestrator <nickname> discovery"
echo "3. Restart your agent so it loads the skills"
echo "Related repositories: $GITHUB/edgeconnect-mcp (MCP server, not installed by this script), $GITHUB/edgeconnect-orchestrator-automation, $GITHUB/edgeconnect-preconfig"
