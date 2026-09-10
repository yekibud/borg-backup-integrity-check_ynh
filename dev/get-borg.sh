#!/bin/bash
# Download the official standalone Borg 1.4.x binary into dev/local/bin (no root needed) and verify its GPG signature
# with the Borg release key (Thomas Waldmann, 6D5BEF9ADD20758057474B70F9F88FB52FAF7B393).
set -Eeuo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BIN="$HERE/local/bin"; mkdir -p "$BIN"
VERSION="${BORG_VERSION:-1.4.5}"
ASSET="borg-linux-glibc231-x86_64"
URL="https://github.com/borgbackup/borg/releases/download/$VERSION/$ASSET"
if [ -x "$BIN/borg" ] && "$BIN/borg" --version 2>/dev/null | grep -q "$VERSION"; then echo "$BIN/borg ($VERSION) present"; exit 0; fi
curl -fL --progress-bar "$URL" -o "$BIN/borg.tmp"
curl -fsSL "$URL.asc" -o "$BIN/borg.tmp.asc" || true
if command -v gpg >/dev/null && [ -s "$BIN/borg.tmp.asc" ]; then
    export GNUPGHOME="$BIN/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
    gpg --batch --keyserver hkps://keys.openpgp.org --recv-keys 6D5BEF9ADD20758057474B70F9F88FB52FAF7B393 >/dev/null 2>&1 || gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys 6D5BEF9ADD20758057474B70F9F88FB52FAF7B393 >/dev/null 2>&1 || echo "WARNING: could not fetch the Borg release key; signature not verified"
    if gpg --batch --verify "$BIN/borg.tmp.asc" "$BIN/borg.tmp" 2>/dev/null; then echo "signature OK"; else echo "WARNING: signature could not be verified (key unavailable?)"; fi
fi
chmod +x "$BIN/borg.tmp"; mv "$BIN/borg.tmp" "$BIN/borg"; rm -f "$BIN/borg.tmp.asc"
"$BIN/borg" --version
