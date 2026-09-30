#!/bin/sh
# Local macOS capture UI. No dependencies downloaded, no model/broker startup.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
agent_dir=$(dirname "$script_dir")
personal_root=${VIBE_PERSONAL_HOME:-"$HOME/Library/Application Support/VibeTradingPersonal"}
mkdir -p "$personal_root"
chmod 700 "$personal_root"
if [ "$(uname -s)" = Darwin ] && command -v swiftc >/dev/null 2>&1; then
    if swiftc -module-cache-path "$personal_root/swift-cache" "$agent_dir/src/personal_web/ocr.swift" -o "$personal_root/local-ocr"; then
        export PYTHONPATH="$agent_dir${PYTHONPATH:+:$PYTHONPATH}"
        exec python3 -m src.personal_web --data-dir "$personal_root" --ocr-binary "$personal_root/local-ocr" "$@"
    fi
fi
export PYTHONPATH="$agent_dir${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m src.personal_web --data-dir "$personal_root" "$@"
