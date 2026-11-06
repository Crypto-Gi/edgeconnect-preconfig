#!/usr/bin/env bash
# setup.sh is now install-all.sh. This forwarder keeps old links and re-runs working; it passes every option through.
# The same file is in all three repositories.
set -euo pipefail
echo "setup.sh is now install-all.sh"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -f "$here/install-all.sh" ]; then
  exec bash "$here/install-all.sh" "$@"
fi
script="$(mktemp)"
trap 'rm -f "$script"' EXIT
curl -fsSL "https://raw.githubusercontent.com/mirwasay/edgeconnect-preconfig/main/install-all.sh" -o "$script" || { echo "error: could not download install-all.sh; check your network access to github.com" >&2; exit 1; }
bash "$script" "$@"
