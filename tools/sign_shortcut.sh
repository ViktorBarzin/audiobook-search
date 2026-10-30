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
#         --target=wireguard -c relayN -- sleep 900   (then RELAY_CONTAINER=relayN)
#       kubectl -n wireguard exec <pod> -c relayN -- apk add --no-cache socat
#
# Usage: tools/sign_shortcut.sh [pod-name]
#        MAC_HOST=viktorbarzin@<mac-address> tools/sign_shortcut.sh
set -eu

HERE="$(cd "$(dirname "$0")/.." && pwd)"
# Sign every variant. They differ only in name and in whose Kindle address the
# import question asks for; neither address is baked in.
VARIANTS=("" "anca")

POD="${1:-}"
if [ -z "${MAC_HOST:-}" ] && [ -z "$POD" ]; then
  POD="$(kubectl -n wireguard get pod -l app=wireguard -o jsonpath='{.items[0].metadata.name}')"
fi
HOST=viktorbarzin@10.3.5.2
# The relay container name changes each time one is created, since an
# ephemeral container cannot be removed from a live pod and its name must
# be unique. Override with RELAY_CONTAINER.
RELAY_CONTAINER="${RELAY_CONTAINER:-relay1}"
RELAY="kubectl -n wireguard exec -i $POD -c $RELAY_CONTAINER -- socat - TCP:10.3.5.2:22"
SSH_OPTS=(-o "ProxyCommand=$RELAY" -o StrictHostKeyChecking=accept-new
          -o ConnectTimeout=20 -o BatchMode=yes)
# When the Mac is on the London LAN the devvm reaches it directly, with no
# relay: MAC_HOST=viktorbarzin@192.168.8.194 (homelab ios doctor names the
# current address, which rotates).
if [ -n "${MAC_HOST:-}" ]; then
  HOST="$MAC_HOST"
  SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o BatchMode=yes)
fi

for variant in "${VARIANTS[@]}"; do
  stem="download-to-calibre${variant:+-$variant}"
  UNSIGNED="$HERE/backend/static/$stem.shortcut"
  SIGNED="$HERE/backend/static/$stem.signed.shortcut"
  python3 "$HERE/tools/build_shortcut.py" "$UNSIGNED" $variant

echo "== uploading unsigned $stem ($(stat -c%s "$UNSIGNED") bytes) =="
# base64 over the ssh channel, because scp has no route of its own here.
# macOS base64 wants -i/-o rather than positional arguments.
base64 -w0 "$UNSIGNED" | ssh "${SSH_OPTS[@]}" "$HOST" \
  'cat > /tmp/dtc.b64 && /usr/bin/base64 -d -i /tmp/dtc.b64 -o /tmp/dtc-unsigned.shortcut'

echo "== signing (mode: anyone) =="
ssh "${SSH_OPTS[@]}" "$HOST" \
  'rm -f /tmp/dtc-signed.shortcut &&
   shortcuts sign --mode anyone --input /tmp/dtc-unsigned.shortcut --output /tmp/dtc-signed.shortcut'

echo "== downloading signed =="
ssh "${SSH_OPTS[@]}" "$HOST" '/usr/bin/base64 -i /tmp/dtc-signed.shortcut' | tr -d '\n' | base64 -d > "$SIGNED"

ssh "${SSH_OPTS[@]}" "$HOST" 'rm -f /tmp/dtc.b64 /tmp/dtc-unsigned.shortcut /tmp/dtc-signed.shortcut'

  head -c 4 "$SIGNED" | grep -q AEA1 || { echo "not an Apple Encrypted Archive"; exit 1; }
  echo "wrote $SIGNED ($(stat -c%s "$SIGNED") bytes, AEA1)"
done
