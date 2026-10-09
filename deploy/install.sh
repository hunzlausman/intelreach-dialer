#!/usr/bin/env bash
# Installs / updates the IntelReach Calling CRM on the IntelReach server.
# Safe to run again (after editing /etc/intelreach-crm.env or a git pull).
#
#   cd /root/intelreach-crm && sudo bash deploy/install.sh
#
# What it touches:
#   /opt/intelreach-crm, /var/lib/intelreach-crm, /etc/intelreach-crm*  (new)
#   /etc/asterisk: adds pjsip_crm.conf, pjsip_crm_agents.conf, extensions_crm.conf
#                  and ONE #include line at the end of pjsip.conf / extensions.conf
#   /etc/intelreach-crm/asterisk: SIP trunks written by the CRM (Admin → SIP trunks),
#                  applied by the root unit intelreach-crm-trunks.path
#   ufw: 5060/udp from Twilio's / Telnyx's signalling ranges + the IPs set per trunk
#   nginx: new site crm.<domain> (existing sites untouched)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/intelreach-crm
ENVF=/etc/intelreach-crm.env
AST=/etc/asterisk
SVC=intelreach-crm
TRUNKS_DIR=/etc/intelreach-crm/asterisk
TELNYX_SIGNALLING="192.76.120.10 64.16.250.10 185.246.41.140 185.246.41.141 103.115.244.145 103.115.244.146"
TWILIO_SIGNALLING="54.172.60.0/23 34.203.250.0/23 54.244.51.0/24 54.171.127.192/26 52.215.127.0/24 35.156.191.128/25 3.122.181.0/24 54.65.63.192/26 3.112.80.0/24 54.169.127.128/26 3.1.77.0/24 54.252.254.64/26 3.104.90.0/24 177.71.206.192/26 18.228.249.0/24"

ok()   { echo -e "\e[32m✔\e[0m $*"; }
warn() { echo -e "\e[33m!\e[0m $*"; }
step() { echo -e "\n\e[1;34m==> $*\e[0m"; }
die()  { echo -e "\e[31m✘ $*\e[0m" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "Run as root (sudo bash deploy/install.sh)"

step "1/8 Settings ($ENVF)"
if [ ! -f "$ENVF" ]; then
  cp "$REPO/deploy/crm.env.example" "$ENVF"
  sed -i "s/^CRM_AST_SECRET=.*/CRM_AST_SECRET=$(openssl rand -hex 24)/" "$ENVF"
  sed -i "s|^CRM_SECRET_KEY=.*|CRM_SECRET_KEY=$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')|" "$ENVF"
  if [ -f /etc/intelreach.env ] && grep -q '^TURN_SECRET=' /etc/intelreach.env; then
    sed -i "s/^TURN_SECRET=.*/$(grep '^TURN_SECRET=' /etc/intelreach.env)/" "$ENVF"
  fi
  chmod 640 "$ENVF"
  ok "created $ENVF"
fi
set -a; . "$ENVF"; set +a
missing=""
# Twilio keys and SIP trunks are set in the CRM (Admin → Integrations / SIP trunks), not here
for v in CRM_AST_SECRET PUBLIC_IP; do
  [ -n "${!v:-}" ] || missing="$missing $v"
done
[ -z "$missing" ] || die "Fill in$missing in $ENVF (see README step 2), then run this again."
CRM_PORT=${CRM_PORT:-8040}
CRM_DOMAIN=${CRM_DOMAIN:-dialer.intelreach.com}
CRM_RECORDINGS=${CRM_RECORDINGS:-/var/spool/asterisk/monitor/crm}
grep -q '^CRM_SECRET_KEY=.\+' "$ENVF" || die "CRM_SECRET_KEY is empty in $ENVF – generate one: python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())'"
ok "domain $CRM_DOMAIN, port $CRM_PORT"

step "2/8 Packages"
python3 -c 'import venv, ensurepip' 2>/dev/null || { apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null; }
command -v rsync >/dev/null || apt-get install -y -qq rsync >/dev/null
id "$SVC" >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin "$SVC"
ok "python $(python3 -V | cut -d' ' -f2), user $SVC"

step "3/8 App -> $APP"
mkdir -p "$APP" /var/lib/intelreach-crm/media/vm /etc/intelreach-crm "$CRM_RECORDINGS"
chown asterisk:asterisk "$CRM_RECORDINGS"; chmod 2750 "$CRM_RECORDINGS"
usermod -aG asterisk "$SVC"          # read call recordings
rsync -a --delete --exclude venv --exclude __pycache__ "$REPO/app" "$REPO/static" "$REPO/scripts" "$REPO/requirements.txt" "$APP/"
[ -x "$APP/venv/bin/pip" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R "$SVC:$SVC" /var/lib/intelreach-crm
chown root:"$SVC" "$ENVF"; chmod 640 "$ENVF"
git -C "$REPO" rev-parse --short HEAD > "$APP/VERSION" 2>/dev/null || echo unknown > "$APP/VERSION"
# the CRM writes the trunk config here, Asterisk reads it (setgid: new files get group asterisk)
mkdir -p "$TRUNKS_DIR"
[ -f "$TRUNKS_DIR/pjsip_trunks.conf" ] || echo "; no SIP trunks yet (Admin → SIP trunks)" > "$TRUNKS_DIR/pjsip_trunks.conf"
touch "$TRUNKS_DIR/trunk_ips.txt"
chown -R "$SVC:asterisk" "$TRUNKS_DIR"; chmod 2750 "$TRUNKS_DIR"; chmod 640 "$TRUNKS_DIR"/*
echo "$TWILIO_SIGNALLING $TELNYX_SIGNALLING" > /etc/intelreach-crm/sip_ips.static
ok "installed"

step "4/8 Agent phone lines (SIP 2001+)"
python3 "$REPO/scripts/gen_agent_pool.py" --from "${POOL_FROM:-2001}" --count "${POOL_COUNT:-50}" \
  --conf "$AST/pjsip_crm_agents.conf" --json /etc/intelreach-crm/agent_pool.json
chown root:"$SVC" /etc/intelreach-crm/agent_pool.json; chmod 640 /etc/intelreach-crm/agent_pool.json

step "5/8 Asterisk (trunk + dialplan)"
# another transport already listening on UDP 5060? (ours is transport-udp-crm)
if asterisk -rx "pjsip show transports" | grep -E 'udp .*:5060' | grep -v transport-udp-crm | grep . >/dev/null; then
  die "Another PJSIP UDP transport already uses port 5060 – remove it or move it to another port first."
fi
render() {
  sed -e "s|@PUBLIC_IP@|$PUBLIC_IP|g" -e "s|@TRUNKS_DIR@|$TRUNKS_DIR|g" -e "s|@AI_PORT@|${CRM_AUDIOSOCKET_PORT:-8045}|g" \
      -e "s|@CRM_PORT@|$CRM_PORT|g" -e "s|@CRM_SECRET@|$CRM_AST_SECRET|g" -e "s|@REC_DIR@|$CRM_RECORDINGS|g" "$1" > "$2"
}
render "$REPO/asterisk/pjsip_crm.conf" "$AST/pjsip_crm.conf"
render "$REPO/asterisk/extensions_crm.conf" "$AST/extensions_crm.conf"
rm -f "$AST/pjsip_crm_telnyx.conf"      # older versions: the Telnyx trunk now lives in Admin → SIP trunks
chmod 640 "$AST/pjsip_crm.conf" "$AST/pjsip_crm_agents.conf" "$AST/extensions_crm.conf"
for f in pjsip extensions; do
  if ! grep -q "^#include ${f}_crm.conf" "$AST/$f.conf"; then
    cp -a "$AST/$f.conf" "$AST/$f.conf.bak-crm-$(date +%Y%m%d%H%M%S)"
    printf '\n#include %s_crm.conf\n' "$f" >> "$AST/$f.conf"
    ok "added #include ${f}_crm.conf to $f.conf (backup saved)"
  fi
done
chown -R asterisk:asterisk "$AST"
# CURL() = func_curl.so, which needs res_curl.so loaded first
sed -i -E '/^noload\s*=>\s*(func_curl|res_curl)\.so/d' "$AST/modules.conf"
MODDIR=$(asterisk -rx "core show settings" | awk -F': *' '/Module directory/ {print $2}' | tr -d '[:space:]')
MODDIR=${MODDIR:-/usr/lib/asterisk/modules}
if [ ! -f "$MODDIR/func_curl.so" ] && command -v apt-get >/dev/null && dpkg -s asterisk >/dev/null 2>&1; then
  apt-get install -y -qq asterisk-modules >/dev/null || true       # Debian/Ubuntu packaged Asterisk
fi
# a shared library the modules need (usually libcurl) missing → install it
if ldd "$MODDIR/res_curl.so" "$MODDIR/func_curl.so" 2>/dev/null | grep 'libcurl.*not found' >/dev/null && command -v apt-get >/dev/null; then
  apt-get install -y -qq libcurl4 >/dev/null || true
fi
RES_LOAD=$(asterisk -rx "module load res_curl.so" 2>&1 || true)
CURL_LOAD=$(asterisk -rx "module load func_curl.so" 2>&1 || true)
if ! asterisk -rx "module show like func_curl" | grep func_curl >/dev/null; then
  echo "  module directory: $MODDIR"
  ls -l "$MODDIR"/func_curl.so "$MODDIR"/res_curl.so 2>&1 | sed 's/^/  /'
  echo "  missing libraries:"; ldd "$MODDIR/res_curl.so" "$MODDIR/func_curl.so" 2>&1 | grep 'not found' | sed 's/^/    /' || true
  echo "  loaded:"; asterisk -rx "module show like curl" | sed 's/^/    /'
  echo "  res_curl: $RES_LOAD"
  echo "  asterisk says: $CURL_LOAD"
  echo "  details: grep -i curl /var/log/asterisk/messages* | tail"
  die "func_curl is not loaded – the CRM dialplan needs CURL(). Packaged Asterisk: apt install asterisk-modules. \
Built from source: apt install libcurl4-openssl-dev, then in the source folder ./configure && make menuselect \
(enable func_curl + res_curl) && make && make install, and run this script again."
fi
ok "func_curl loaded"
# AI agents over the SIP trunks: Dial(AudioSocket/…) + call files from the CRM
asterisk -rx "module load res_audiosocket.so" >/dev/null 2>&1 || true      # chan_audiosocket needs it first
asterisk -rx "module load chan_audiosocket.so" >/dev/null 2>&1 || true
if asterisk -rx "module show like chan_audiosocket" | grep chan_audiosocket >/dev/null; then
  ok "chan_audiosocket loaded (AI agents on SIP trunks)"
else
  warn "chan_audiosocket is not available – AI agents can't use the SIP trunks (Telnyx/Twilio API calls still work)"
fi
SPOOL=${CRM_AST_SPOOL:-/var/spool/asterisk/outgoing}
mkdir -p "$SPOOL"; chown asterisk:asterisk "$SPOOL"; chmod 2770 "$SPOOL"
SPOOL_TMP=${CRM_AST_SPOOL_TMP:-/var/spool/asterisk/crm-tmp}      # call files are written here, then moved in
mkdir -p "$SPOOL_TMP"; chown "$SVC:asterisk" "$SPOOL_TMP"; chmod 2770 "$SPOOL_TMP"
rm -f "$SPOOL"/.crm-ai-*.call
chmod g+x "$(dirname "$SPOOL")"
ok "CRM may start AI calls ($SPOOL)"
asterisk -rx "module reload res_pjsip.so" >/dev/null
asterisk -rx "dialplan reload" >/dev/null
if asterisk -rx "pjsip show transports" | grep transport-udp-crm >/dev/null; then
  ok "transport-udp-crm loaded"
else
  warn "The new UDP transport needs a full Asterisk restart (ends live calls/classes):  systemctl restart asterisk"
fi
asterisk -rx "pjsip show endpoint twilio-inbound" | grep -i "twilio-inbound" >/dev/null && ok "twilio-inbound endpoint ok"

step "6/8 Firewall: SIP 5060/udp from the SIP providers only"
if command -v ufw >/dev/null && ufw status | grep "Status: active" >/dev/null; then
  for net in $TWILIO_SIGNALLING; do ufw allow proto udp from "$net" to any port 5060 comment 'Twilio SIP' >/dev/null; done
  # Telnyx SIP signalling (in-dialog requests / incoming calls on a Telnyx trunk)
  for net in $TELNYX_SIGNALLING; do ufw allow proto udp from "$net" to any port 5060 comment 'Telnyx SIP' >/dev/null; done
  ok "ufw rules added (RTP 10000-20000/udp is already open; other providers' IPs: Admin → SIP trunks)"
else
  warn "ufw not active – allow 5060/udp ONLY from: $TWILIO_SIGNALLING $TELNYX_SIGNALLING + your trunks' IPs"
fi

step "7/8 Service"
cp "$REPO/deploy/intelreach-crm.service" "$REPO/deploy/intelreach-crm-trunks.service" \
   "$REPO/deploy/intelreach-crm-trunks.path" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now intelreach-crm-trunks.path
systemctl enable -q "$SVC"
systemctl restart "$SVC"
for i in $(seq 1 20); do curl -fsS "http://127.0.0.1:$CRM_PORT/api/health" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "http://127.0.0.1:$CRM_PORT/api/health" >/dev/null || die "CRM did not start: journalctl -u $SVC -n 50"
ok "running on 127.0.0.1:$CRM_PORT – version $(cat "$APP/VERSION")"
bash "$APP/scripts/apply_trunks.sh" && ok "SIP trunks applied ($(grep -c '^\[crm-trunk-[0-9]*\]$' "$TRUNKS_DIR/pjsip_trunks.conf" || true) endpoints)"

step "8/8 nginx + certificate for $CRM_DOMAIN"
SITE=/etc/nginx/sites-available/intelreach-crm.conf
CERT=/etc/letsencrypt/live/$CRM_DOMAIN/fullchain.pem
# port 443: an nginx stream SNI router forwarding to 127.0.0.1:8443, or nginx's normal https sites?
if grep -rqsE '(proxy_pass|server)[[:space:]]+127\.0\.0\.1:8443' /etc/nginx --exclude=intelreach-crm.conf; then
  SSL_LISTEN="127.0.0.1:8443 ssl http2"
else
  SSL_LISTEN="443 ssl http2"
fi
site() { sed -e "s|@CRM_DOMAIN@|$CRM_DOMAIN|g" -e "s|@CRM_PORT@|$CRM_PORT|g" -e "s|@SSL_LISTEN@|$SSL_LISTEN|g"   "$REPO/deploy/nginx-crm.conf" > "$SITE"; }
site
if [ ! -f "$CERT" ]; then
  sed -i '/#SSL_START/,/#SSL_END/d' "$SITE"          # port 80 only until the certificate exists
  ln -sf "$SITE" /etc/nginx/sites-enabled/
  nginx -t -q && systemctl reload nginx
  certbot certonly --webroot -w /var/www/html -d "$CRM_DOMAIN" --agree-tos --register-unsafely-without-email --non-interactive \
    || die "certbot failed – does $CRM_DOMAIN point to $PUBLIC_IP yet? (README step 1)"
  site
fi
ln -sf "$SITE" /etc/nginx/sites-enabled/
nginx -t -q || die "nginx config test failed: nginx -t"
systemctl reload nginx
ok "https://$CRM_DOMAIN (nginx listens on $SSL_LISTEN)"

echo
echo "Done. Next:"
echo "  1. First admin (once):"
echo "     cd $APP && set -a && . $ENVF && set +a && sudo -u $SVC -E venv/bin/python scripts/create_admin.py you@example.com 'Your Name'"
echo "  2. Open https://$CRM_DOMAIN -> Admin -> Integrations -> Twilio (Account SID + Auth Token)"
echo "  3. Admin -> SIP trunks -> add your trunks (Twilio, Telnyx, Plivo, …) and their numbers"
echo "  4. Admin -> Twilio number -> enter your number -> Connect (incoming calls to it via the CRM)"
