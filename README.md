# IntelReach Calling CRM

A standalone calling CRM (contacts, browser softphone, call log, outcomes, agents) at
**https://dialer.intelreach.com**. It uses the **Asterisk that already runs on 76.13.179.69** and the **same Twilio
number that is connected to GoHighLevel**.

* **Outgoing calls (international):** agent's browser → Asterisk → **any SIP trunk** (Twilio, Telnyx, Plivo,
  SignalWire, Vonage, VoIP.ms or any other SIP provider – as many as you like) → PSTN, with a number from that
  provider (or your Twilio number) as caller ID. Trunks and numbers are set in **Admin → SIP trunks**.
* **Numbers from any provider:** calls to them arrive over their trunk and ring the CRM agents.
* **Incoming calls to the Twilio number:** Twilio → CRM webhook → rings **every online CRM agent** → if nobody
  answers in *N* seconds (or nobody is online) the call is **handed to GHL exactly as before**.
* **GHL keeps working:** its outgoing calls, SMS and the number stay in GHL. If the CRM server is down, Twilio
  automatically uses GHL's webhook (it is set as the number's fallback URL).

```
                       ┌──────────── outgoing ─────────────┐
 Agent browser ──WSS──► Asterisk (pbx.intelreach.com) ──UDP 5060──► Twilio Elastic SIP Trunk ──► +44…, +92…, +971…
   (dialer.intelreach.com)      ▲       │ CURL 127.0.0.1:8040
                             │       ▼
                             │   CRM (FastAPI + SQLite): who may call where, caller ID, who rings, call log
                             │       ▲
 Caller ──► Twilio number ──►│ POST /twilio/voice ──► <Dial><Sip> to Asterisk ──► rings agents 2001+
                             │       └─ no answer ──► <Redirect> to GHL's original webhook ──► GHL
```

### About "ring both"
A Twilio number can only send a call to **one** webhook, and GHL's webhook wants to control the call itself, so
CRM and GHL cannot ring at the *same* second. The closest that works reliably: **CRM agents first (default 20 s),
then GHL**. You can change it in *Admin → Settings → Incoming calls*: "Ring CRM agents, then GHL" / "CRM agents
only" / "GHL only".

### What changes on the server (nothing existing is modified)
| Item | Change |
|---|---|
| Asterisk | new files `pjsip_crm.conf`, `pjsip_crm_agents.conf`, `extensions_crm.conf` + one `#include` line at the end of `pjsip.conf` and `extensions.conf`. New contexts only; classroom users (1001, 1002, 1100+) are untouched. |
| Asterisk | new UDP transport on **5060**, used only by the SIP providers |
| Firewall | 5060/udp opened **only to the SIP providers' signalling IPs** (RTP 10000–20000 is already open) |
| nginx | new site `dialer.intelreach.com` (existing sites untouched) |
| New service | `intelreach-crm` (127.0.0.1:8040), data in `/var/lib/intelreach-crm/crm.db` |

---

## Step 1 – DNS (GoDaddy)
Add: **A record**, name `crm`, value `76.13.179.69`. Check: `nslookup dialer.intelreach.com`.

## Step 2 – Twilio console (same account GHL uses)

**2a. Elastic SIP Trunk – for outgoing calls**
1. *Elastic SIP Trunking → Trunks → Create new SIP Trunk* → name `IntelReach-CRM`.
2. **Termination** → *Termination SIP URI*: e.g. `intelreach-crm` → you get `intelreach-crm.pstn.twilio.com`.
3. **Termination → Authentication**:
   * *IP Access Control Lists* → new list `intelreach-server` with `76.13.179.69/32`.
   * *Credential Lists* → new list with a username + strong password (write both down).
4. **Origination**: leave empty. Incoming calls reach the CRM through the webhook instead.
5. **Numbers**: ⚠️ **do NOT add your GHL number to the trunk.** That would take incoming calls away from GHL.
   You can still use it as caller ID, because it is a number in the same account.

**2b. Geo permissions – which countries you may call**
*Voice → Settings → Geo permissions → **Elastic SIP Trunking** tab* (separate from Programmable Voice):
tick the countries you call. Leave high-risk / premium destinations off unless you need them.

**2c. API credentials**
*Account → API keys & tokens*: copy the **Account SID** and **Auth Token**. You paste them into the CRM in Step 4
(**Admin → Integrations → Twilio**). The CRM uses them to point the number's voice webhook at the CRM (remembering
GHL's), to check Twilio's webhook signatures and for Twilio AI calls.

**Other providers** (Telnyx, Plivo, SignalWire, Vonage, VoIP.ms, …): create a SIP trunk / credentials connection
there that allows `76.13.179.69`, and note the SIP server, username and password. For incoming calls, send the
numbers to `sip:<number>@76.13.179.69:5060` (or let the trunk register – tick *Register* in the CRM).

## Step 3 – Install on the server
```bash
ssh root@76.13.179.69
git clone https://github.com/hunzlausman/intelreach-dialer.git /root/intelreach-crm
cd /root/intelreach-crm
bash deploy/install.sh          # creates /etc/intelreach-crm.env (check PUBLIC_IP / CRM_DOMAIN), installs everything
```
Twilio keys, SIP trunks and numbers are **not** in the `.env` any more – you set them in the CRM (Step 4).
Older installs that still have `TWILIO_SIP_*` / `TELNYX_SIP_*` in the `.env`: those trunks are copied into
*Admin → SIP trunks* on the first start, and the Twilio SID/token keep working until you fill in
*Admin → Integrations → Twilio*.

Updating later (after a `git pull`):
```bash
cd /root/intelreach-crm && git pull && bash deploy/install.sh
```
If the installer says *"The new UDP transport needs a full Asterisk restart"*, do it when no class or call is
running: `systemctl restart asterisk`.

Create the first admin:
```bash
cd /opt/intelreach-crm && set -a && . /etc/intelreach-crm.env && set +a && \
  sudo -u intelreach-crm -E venv/bin/python scripts/create_admin.py you@example.com "Your Name"
```

## Step 4 – Credentials, trunks, numbers
1. Open **https://dialer.intelreach.com** and sign in.
2. **Admin → Integrations → Twilio**: paste the Account SID and Auth Token → **Save** → **Test**.
   For live captions, recording transcripts and AI agents also add a speech-to-text key – **AssemblyAI** or
   **Deepgram** (Deepgram key with the *Member* role or higher) – and pick it in **Admin → Settings → Speech-to-text**.
   Each AI agent can override that choice.
3. **Admin → SIP trunks → + Trunk**: pick the provider, enter the SIP server (e.g. `intelreach-crm.pstn.twilio.com`),
   username and password. Add one per provider/account. Asterisk picks the change up within a few seconds
   (`intelreach-crm-trunks.path` reloads PJSIP and opens the firewall to the IPs you list for incoming calls).
4. **Admin → SIP trunks → + Number**: add each number you own and the trunk it belongs to. A number is the caller
   ID on its trunk; campaigns can choose a number (and with it, its provider). Pick the default trunk in
   **Admin → Settings**.
5. **Admin → Twilio number** → enter the GHL number (`+1…`) → **Connect**.

**AI agents on your SIP trunks:** create a *Custom* AI agent with **Phone carrier → Your SIP trunk**. Its calls
go out through the trunk of its caller ID (or the default trunk); Asterisk hands the audio to the CRM's voice
engine over AudioSocket (`127.0.0.1:8045`, `chan_audiosocket` – `install.sh` loads it). For incoming calls, set a
number to *AI agent answers* or *Ring the CRM agents, then the AI agent*. A transfer target of `sip:agents` rings
your online CRM agents. Telnyx Call Control / Twilio API carriers keep working for agents that use them.
   The CRM saves GHL's current webhook (shown as *GHL fallback*) and points the number at
   `https://dialer.intelreach.com/twilio/voice`.
6. **Admin → Settings**: set the default country code, the allowed countries (e.g. `+1,+44,+92,+971` or `*`),
   the ring time and the longest call length.
7. **Admin → Agents → + Agent**: each agent gets an email/password and their own phone line (2001, 2002 …).

To undo: **Admin → Twilio → Give incoming back to GHL**. This puts GHL's webhook back exactly as it was.

## Step 5 – Test
1. **Audio path:** in the phone panel click **audio test** (dials 600). You should hear yourself. If not, it's the
   browser ↔ Asterisk media path (same checks as the classroom phone).
2. **Outgoing:** call your mobile in another country (`+44…`, `+92…`). Your mobile should show the Twilio number.
3. **Incoming:** tick **Taking calls**, then call the Twilio number from a mobile. The CRM rings with the contact's
   name. Don't answer: after the ring time GHL takes the call as usual.
4. Check **Calls**: every call has a status (Answered / No answer / Sent to GHL …), talk time, outcome and notes.

Live debugging:
```bash
asterisk -rvvv                  # then:  pjsip set logger on
journalctl -u intelreach-crm -f
asterisk -rx "pjsip show endpoints" | grep crm-trunk      # one per enabled trunk (crm-trunk-<id>)
asterisk -rx "pjsip show registrations"                  # trunks with "Register" ticked
journalctl -u intelreach-crm-trunks -n 20                # last trunk reloads / firewall updates
```

## Troubleshooting
| Symptom | Check |
|---|---|
| Phone pill "Phone error / refused" | `asterisk -rx "pjsip show endpoint 2001"` exists? Re-run `install.sh` (it regenerates the agent lines and reloads PJSIP). |
| Outgoing: "Call not allowed" | *Admin → Settings* allowed/blocked prefixes, caller ID connected. Asterisk log shows `CRM refused: deny|…`. |
| Outgoing: "no SIP trunk configured" | *Admin → SIP trunks*: at least one trunk must be enabled. |
| Outgoing: fails right away, 403 / 407 | The trunk's username/password in *Admin → SIP trunks* and the IP allow-list at the provider (76.13.179.69). Twilio: IP ACL **and** credential list, plus Geo permission on the **Elastic SIP Trunking** tab. |
| Trunk change not in Asterisk | `systemctl status intelreach-crm-trunks.path` must be active; run `bash /opt/intelreach-crm/scripts/apply_trunks.sh` by hand. |
| Calls to a provider's number don't ring | The number is in *Admin → SIP trunks → Numbers* with *Ring the CRM agents*; the provider sends it to `sip:<number>@76.13.179.69:5060` from an IP listed on the trunk (or the trunk registers). |
| Outgoing: connects but no audio | `ufw status` – 10000:20000/udp open; `rtp set debug on`. Twilio media comes from 168.86.128.0/18. |
| Incoming never rings the CRM | *Admin → Twilio* must say "incoming → CRM first". If GHL re-saved the number it may have put its webhook back: click **Re-connect**. Agent must tick *Taking calls* and the line must show *ready*. |
| Incoming rings, but Twilio logs "SIP 404/403" | `pjsip set logger on`: the INVITE must come from a Twilio IP in `[twilio-inbound-identify]`. Twilio may add ranges: update `pjsip_crm.conf` + the ufw rules (see https://www.twilio.com/docs/sip-trunking/ip-addresses). |
| GHL no longer receives calls | *Admin → Twilio → GHL fallback* must show GHL's URL. If it is empty, re-save the number inside GHL, then **Re-connect** here. |

## Security
* SIP 5060 is reachable only from Twilio's / Telnyx's IPs and the IPs you list per trunk. Agent phones only reach the PSTN through the CRM's checks (logged-in
  agent, allowed country, caller ID set, max call length). Twilio's Geo Permissions apply on top.
* `/ast/*` hooks: localhost only, with a shared secret (nginx returns 404 for them).
* Twilio webhooks are verified with the `X-Twilio-Signature` header.
* Twilio tokens and trunk passwords are stored encrypted (`CRM_SECRET_KEY`); the browser never gets them back.
  Trunk values are validated strictly before they are written into Asterisk's config.
* Consider adding `fail2ban` for Asterisk and a Twilio usage trigger (*Usage → Triggers*) as a spend alarm.

## Files
| Path | Purpose |
|---|---|
| `app/` | FastAPI backend: `api.py` (contacts, calls), `admin.py` (agents, settings, SIP trunks + numbers, Twilio connect), `trunks.py` (trunk config + call routing), `hooks.py` (Twilio webhooks + Asterisk CURL hooks) |
| `static/` | Web app (no build step): `app.js` UI, `phone.js` softphone (JsSIP) |
| `asterisk/` | `pjsip_crm.conf` (transport, Twilio inbound, agent templates; includes the CRM-written trunks), `extensions_crm.conf` (dialplan) |
| `scripts/` | `gen_agent_pool.py` (agent SIP lines), `create_admin.py`, `apply_trunks.sh` (PJSIP reload + firewall, run as root) |
| `deploy/` | `install.sh`, nginx site, systemd units (CRM + trunk watcher), env example |
| `tests/` | `python -m pytest tests` – API, Twilio and Asterisk hook flows |
