#!/bin/sh
# Symlink the ClaudeLiveBridge Remote Script into Live's User Library.
#   scripts/install_remote_script.sh            install (symlink)
#   scripts/install_remote_script.sh --copy     copy instead, if Live ignores the symlink
#   scripts/install_remote_script.sh --remove   uninstall
# Restart Live afterwards; it only loads Remote Scripts at startup.
set -eu

NAME=ClaudeLiveBridge
SRC="$(cd "$(dirname "$0")/.." && pwd)/RemoteScript/$NAME"
DEST_DIR="${LIVE_USER_LIBRARY:-$HOME/Music/Ableton/User Library}/Remote Scripts"
DEST="$DEST_DIR/$NAME"

case "${1:-}" in
    --remove)
        rm -rf "$DEST"
        echo "Removed $DEST"
        exit 0 ;;
    --copy)
        mkdir -p "$DEST_DIR"
        rm -rf "$DEST"
        mkdir "$DEST"
        cp "$SRC"/*.py "$DEST"/
        echo "Copied $SRC -> $DEST" ;;
    "")
        mkdir -p "$DEST_DIR"
        if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
            echo "$DEST exists and is not a symlink; remove it or use --copy" >&2
            exit 1
        fi
        ln -sfn "$SRC" "$DEST"
        echo "Linked $DEST -> $SRC" ;;
    *)
        echo "usage: $0 [--copy|--remove]" >&2
        exit 2 ;;
esac

echo "Next: restart Live, then Settings > Link, Tempo & MIDI > Control Surface: $NAME (Input/Output: None)."
