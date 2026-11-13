#!/usr/bin/env bash
# Box side: install a Forward Guidance notes tarball (from tools/forward_guidance_md.py --tar) into
# Snowball's own vault folder. Only "Forward Guidance/..." .md paths are accepted; nothing else is touched.
# Usage: fg_vault_install.sh /workspace/uploads/fg-notes-YYYY-MM-DD.tgz ["/home/box/obsidian/vault/00 Inbox/Agents/Snowball"]
set -euo pipefail
TGZ="$1"
DEST="${2:-/home/box/obsidian/vault/00 Inbox/Agents/Snowball}"
[ -f "$TGZ" ] || { echo "no tarball $TGZ"; exit 2; }
[ -d "$DEST" ] || { echo "no vault folder $DEST"; exit 2; }
tar tzf "$TGZ" | while IFS= read -r n; do
  case "$n" in
    "Forward Guidance/"*.md) case "$n" in *..*) echo "bad path $n"; exit 3;; esac ;;
    *) echo "refusing path $n"; exit 3 ;;
  esac
done
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
tar xzf "$TGZ" -C "$TMP"
if grep -rEiq 'api[_-]?key|secret|passphrase|password|PRIVATE KEY|@icloud\.com|@gmail\.com' "$TMP"; then
  echo "secret-like or email pattern found; not installed"; exit 4
fi
cp -r "$TMP/Forward Guidance" "$DEST/"
find "$TMP/Forward Guidance" -name '*.md' | sed "s#^$TMP/#$DEST/#"
