#!/bin/bash
# Removes the OS-level boot/login autostart entry created by Setup's
# autostart step, without re-running the rest of Setup.
set -euo pipefail

case "${1:-}" in
    -h|--help)
        cat <<'USAGE'
Usage: UninstallAutostartLinux.sh

Removes this ClipEm folder's login autostart entry and its application menu
entry, without running Setup. Entries that start another ClipEm folder are
left alone. Safe to run any time.

  -h, --help  Show this help and exit.
USAGE
        exit 0
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${SCRIPT_DIR}/App/Linux/python/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "No python3 found - run Setup_Linux.sh (or App/Setup-Uninstall_Scripts/Setup_Linux.sh) at least once first."
    exit 1
fi

exec "$PYTHON_BIN" "${SCRIPT_DIR}/App/Shared/Installer/uninstall_autostart.py"
