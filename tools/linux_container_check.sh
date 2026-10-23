#!/usr/bin/env bash
# Run the supported Linux/Python 3.12 checks in a privileged Ubuntu 24.04 container, as CI does.
#
#   tools/linux_container_check.sh unit      # full unittest suite (default)
#   tools/linux_container_check.sh release   # suite-independent release gate: clean wheels, Chromium journey, axe audits
#
# The container needs --privileged only so Bubblewrap can create user namespaces; parser isolation stays
# in required mode. The checkout is mounted read-only and the *committed* HEAD is cloned inside, so
# uncommitted edits (and Windows CRLF working-tree bytes) are never what gets tested. The release mode
# downloads Node, Playwright and Chromium. This is local validation, not a remote CI run.
set -euo pipefail
mode="${1:-unit}"
case "$mode" in unit|release) ;; *) echo "usage: $0 [unit|release]" >&2; exit 2;; esac
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mount="$repo"
if command -v cygpath >/dev/null 2>&1; then mount="$(cygpath -w "$repo")"; export MSYS_NO_PATHCONV=1; fi
inner="$(mktemp)"
trap 'rm -f "$inner"' EXIT
cat > "$inner" <<'INNER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mode="$1"
apt-get update -qq >/dev/null
pkgs="bubblewrap libseccomp2 libsqlite3-0 python3.12-venv git"
[ "$mode" = release ] && pkgs="$pkgs curl ca-certificates"
apt-get install -y -qq $pkgs >/dev/null
if [ "$mode" = release ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  apt-get install -y -qq nodejs >/dev/null
fi
git config --global --add safe.directory /src
git clone -q /src /work && cd /work
echo "testing commit $(git rev-parse --short HEAD)"
/usr/bin/python3.12 -m venv /venv
export PATH=/venv/bin:$PATH
python -c "import sqlite3"
python -m pip install -q --require-hashes -r tools/release-build-requirements.txt -r tools/release-pdf-requirements.txt
python -m pip install -q --no-build-isolation --no-deps -e .
if [ "$mode" = unit ]; then
  python -m unittest discover -s tests -t .
else
  npm install --prefix /ui --no-save --package-lock=false playwright@1.62.1 axe-core@4.11.0 >/dev/null 2>&1
  /ui/node_modules/.bin/playwright install --with-deps chromium >/dev/null 2>&1
  PLAYWRIGHT_MODULE=/ui/node_modules/playwright AXE_MODULE=/ui/node_modules/axe-core \
    python tools/check_release.py --output /tmp/dasre-release --browser --accessibility \
    || { tail -40 /tmp/dasre-release/logs/browser.log 2>/dev/null; exit 1; }
fi
INNER
docker run --rm --privileged -v "$mount:/src:ro" -v "$(cygpath -w "$inner" 2>/dev/null || echo "$inner"):/inner.sh:ro" \
  ubuntu:24.04 bash /inner.sh "$mode"
