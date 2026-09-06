#!/bin/bash
# Sign the generated shortcut on Viktor's Mac and drop the result next to it.
#
# Why this exists: Apple signs shortcuts with `shortcuts sign`, which is
# macOS-only, and iOS 15 removed the "Allow Untrusted Shortcuts" setting, so an
# unsigned file cannot be installed at all. Everything else was checked and
# ruled out on 2026-09-05: scaxyz/shortcut-signing-server needs macOS itself,
# HubSign is a hosted instance of it, RoutineHub's Remote Sign needs a Mac over
# SSH, and iOS 15+ cannot sign on-device.
#
# The Mac sits behind WireGuard at 10.3.5.2, and only the wireguard pod's
# network namespace has a route to it, so ssh goes through a socat relay in an
# ephemeral container. The private key never leaves the devvm.
#
# Prerequisites:
#   - the Mac is up on the tunnel   (kubectl -n wireguard exec deploy/wireguard
#                                     -c wireguard -- wg show wg0)
#   - a relay container exists:
#       kubectl debug -n wireguard <pod> --image=alpine:3.20 \
#         --target=wireguard -c relay1 -- sleep 900
#       kubectl -n wireguard exec <pod> -c relay1 -- apk add --no-cache socat
#
# Usage: tools/sign_shortcut.sh [pod-name]
set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
UNSIGNED="$HERE/backend/static/download-to-calibre.shortcut"
SIGNED="$HERE/backend/static/download-to-calibre.signed.shortcut"

POD="${1:-$(kubectl -n wireguard get pod -l app=wireguard -o jsonpath='{.items[0].metadata.name}')}"
HOST=viktorbarzin@10.3.5.2
RELAY="kubectl -n wireguard exec -i $POD -c relay1 -- socat - TCP:10.3.5.2:22"
SSH_OPTS=(-o "ProxyCommand=$RELAY" -o StrictHostKeyChecking=accept-new
          -o ConnectTimeout=20 -o BatchMode=yes)

python3 "$HERE/tools/build_shortcut.py" "$UNSIGNED"

echo "== uploading unsigned ($(stat -c%s "$UNSIGNED") bytes) =="
# base64 over the ssh channel, because scp has no route of its own here.
# macOS base64 wants -i/-o rather than positional arguments.
base64 -w0 "$UNSIGNED" | ssh "${SSH_OPTS[@]}" "$HOST" \
  'cat > /tmp/dtc.b64 && base64 -d -i /tmp/dtc.b64 -o /tmp/dtc-unsigned.shortcut'

echo "== signing (mode: anyone) =="
ssh "${SSH_OPTS[@]}" "$HOST" \
  'rm -f /tmp/dtc-signed.shortcut &&
   shortcuts sign --mode anyone --input /tmp/dtc-unsigned.shortcut --output /tmp/dtc-signed.shortcut'

echo "== downloading signed =="
ssh "${SSH_OPTS[@]}" "$HOST" 'base64 -i /tmp/dtc-signed.shortcut' | tr -d '\n' | base64 -d > "$SIGNED"

ssh "${SSH_OPTS[@]}" "$HOST" 'rm -f /tmp/dtc.b64 /tmp/dtc-unsigned.shortcut /tmp/dtc-signed.shortcut'

head -c 4 "$SIGNED" | grep -q AEA1 || { echo "not an Apple Encrypted Archive"; exit 1; }
echo "wrote $SIGNED ($(stat -c%s "$SIGNED") bytes, AEA1)"
