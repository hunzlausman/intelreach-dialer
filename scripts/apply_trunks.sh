#!/usr/bin/env bash
# Applies Admin → SIP trunks: reloads PJSIP and opens 5060/udp to the signalling IPs set per trunk.
# Run as root by intelreach-crm-trunks.path whenever the CRM rewrites pjsip_trunks.conf
# (the CRM itself never runs as root). Safe to run by hand: sudo bash scripts/apply_trunks.sh
set -euo pipefail
DIR=${CRM_TRUNKS_DIR:-/etc/intelreach-crm/asterisk}
STATE=/var/lib/intelreach-crm-trunks
STATIC=/etc/intelreach-crm/sip_ips.static          # Twilio / Telnyx ranges from install.sh – never removed here
mkdir -p "$STATE"; chmod 700 "$STATE"

asterisk -rx "module reload res_pjsip.so" >/dev/null
asterisk -rx "module reload res_pjsip_outbound_registration.so" >/dev/null 2>&1 || true
logger -t intelreach-crm "SIP trunks reloaded"

command -v ufw >/dev/null && ufw status | grep "Status: active" >/dev/null || exit 0
# only well-formed IPv4 addresses / ranges, whatever the file contains
valid() { grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$' "$1" 2>/dev/null | sort -u || true; }
new=$(valid "$DIR/trunk_ips.txt")
old=$(valid "$STATE/applied")
keep=$(tr ' ' '\n' < "$STATIC" 2>/dev/null | sort -u || true)
for net in $old; do
  grep -qxF "$net" <<<"$new" && continue
  grep -qxF "$net" <<<"$keep" && continue
  ufw delete allow proto udp from "$net" to any port 5060 >/dev/null || true
done
for net in $new; do ufw allow proto udp from "$net" to any port 5060 comment 'CRM SIP trunk' >/dev/null; done
printf '%s\n' $new > "$STATE/applied"
