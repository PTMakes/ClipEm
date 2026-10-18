#!/usr/bin/env bash
# Creates ClipEm's Ed25519 release signing key (once) and stamps its public
# half into everything that ships it:
#   - App/App/Shared/Installer/signing_public_key.txt
#   - App/App/Shared/Installer/Scripts/ClipEmSetupLinux.sh
# (the Windows installer bundles signing_public_key.txt at build time).
#
# The private key is written OUTSIDE the repo, to $CLIPEM_SIGNING_KEY or
# ~/.config/clipem/signing-key.pem (mode 600). Back it up somewhere safe and
# offline (e.g. a password manager): losing it means shipping a new public
# key, and every existing install then needs a manual reinstall to trust
# releases again. Never commit it.
#
# Usage:
#   ./generate-signing-key.sh               create the key (refuses to overwrite) and stamp
#   ./generate-signing-key.sh --stamp-only  re-stamp the public key from an existing key
#   ./generate-signing-key.sh --help        show this help and exit
set -euo pipefail

case "${1:-}" in
  -h|--help)
    # This file's header comment, up to the first line of code.
    sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'
    exit 0 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEY_PATH="${CLIPEM_SIGNING_KEY:-$HOME/.config/clipem/signing-key.pem}"
INSTALLER_DIR="$SCRIPT_DIR/../App/App/Shared/Installer"
PUBLIC_KEY_FILE="$INSTALLER_DIR/signing_public_key.txt"
LINUX_INSTALLER="$INSTALLER_DIR/Scripts/ClipEmSetupLinux.sh"

if [[ "${1:-}" != "--stamp-only" ]]; then
  if [[ -e "$KEY_PATH" ]]; then
    echo "A signing key already exists at $KEY_PATH - refusing to overwrite it." >&2
    echo "Use --stamp-only to re-stamp its public key." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$KEY_PATH")"
  chmod 700 "$(dirname "$KEY_PATH")"
  (umask 077 && openssl genpkey -algorithm ed25519 -out "$KEY_PATH")
  echo "Created private key: $KEY_PATH  (back this up offline; never commit it)"
fi

PUBLIC_KEY="$(openssl pkey -in "$KEY_PATH" -pubout -outform DER | tail -c 32 | base64 -w0)"

printf '%s\n' "$PUBLIC_KEY" > "$PUBLIC_KEY_FILE"
sed -i "s|^CLIPEM_SIGNING_PUBLIC_KEY=\".*\"|CLIPEM_SIGNING_PUBLIC_KEY=\"$PUBLIC_KEY\"|" "$LINUX_INSTALLER"

python3 "$SCRIPT_DIR/release/release_common.py" check-key
echo "Stamped public key $PUBLIC_KEY into:"
echo "  $PUBLIC_KEY_FILE"
echo "  $LINUX_INSTALLER"
echo "Commit those two files (App) before the next release."
