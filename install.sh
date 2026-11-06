#!/usr/bin/env bash
# Install or update only this repository, edgeconnect-preconfig, with its agent skill: install-all.sh with --repos fixed.
# Every other option of install-all.sh passes through (--yes, --agents, --dir, --skills, --mcp).
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/mirwasay/edgeconnect-preconfig/main/install.sh)
#
# Run it again to update. To install several repositories, run install-all.sh instead.
set -euo pipefail
REPO=edgeconnect-preconfig
NUMBER=2
for arg in "$@"; do
  case "$arg" in
    --repos|--repos=*) printf 'error: install.sh installs only %s; to choose repositories, run install-all.sh\n' "$REPO" >&2; exit 2 ;;
  esac
done
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -f "$here/install-all.sh" ]; then
  exec bash "$here/install-all.sh" --repos "$NUMBER" "$@"
fi
script="$(mktemp)"
trap 'rm -f "$script"' EXIT
curl -fsSL "https://raw.githubusercontent.com/mirwasay/$REPO/main/install-all.sh" -o "$script" || { echo "error: could not download install-all.sh; check your network access to github.com" >&2; exit 1; }
bash "$script" --repos "$NUMBER" "$@"
