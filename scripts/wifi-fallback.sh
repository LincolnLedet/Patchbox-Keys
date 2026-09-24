#!/bin/bash
# wifi-fallback — if the Pi can't join a known WiFi network on boot, bring up its
# own access point so the padmap web UI stays reachable at http://172.24.1.1:8080
#
# SSID: patchbox   password: <set in NetworkManager profile "pb-hotspot">
# The password is NOT stored here — it lives in the NM connection profile on the
# device. Create/set it with: nmcli con modify pb-hotspot wifi-sec.psk '<yourpw>'

HOTSPOT="pb-hotspot"
IFACE="wlan0"
TIMEOUT="${TIMEOUT:-45}"   # seconds to wait for a saved network to connect
DRYRUN="${DRYRUN:-0}"

log() { echo "wifi-fallback: $*"; }

active_con() {
    nmcli -t -f DEVICE,STATE,CONNECTION device status \
        | awk -F: -v i="$IFACE" '$1==i && $2=="connected" {print $3}'
}

# Give NetworkManager a moment to come up and start associating.
nm-online -s -t 15 >/dev/null 2>&1

for _ in $(seq 1 "$TIMEOUT"); do
    con="$(active_con)"
    if [ -n "$con" ] && [ "$con" != "$HOTSPOT" ]; then
        log "connected to '$con' — hotspot not needed"
        exit 0
    fi
    if [ "$con" = "$HOTSPOT" ]; then
        log "hotspot already active"
        exit 0
    fi
    sleep 1
done

log "no WiFi after ${TIMEOUT}s — starting hotspot '$HOTSPOT' (SSID patchbox)"
if [ "$DRYRUN" = "1" ]; then
    log "DRYRUN: would run: nmcli con up $HOTSPOT"
    exit 0
fi
nmcli con up "$HOTSPOT" && log "hotspot up — web UI at http://172.24.1.1:8080"
