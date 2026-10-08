"""End-to-end test of the CRM API, the Asterisk hooks and the Twilio webhooks.

    python -m pytest tests -q        (from the intelreach-crm folder)
"""
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, security  # noqa: E402
from app.main import app  # noqa: E402
from app.phone import normalize  # noqa: E402


def sign(path, params):
    url = "https://crm.example.com" + path
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(b"tok", data.encode(), hashlib.sha1).digest()).decode()


def twilio(c, path, params):
    return c.post(path, data=params, headers={"X-Twilio-Signature": sign(path, params)})


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        with db.tx() as con:
            con.execute("INSERT INTO users(email, name, role, pw_hash, sip_ext) VALUES ('a@x.com','Admin','admin',?, '2001')",
                        (security.hash_password("password1"),))
            db.set_setting(con, "twilio_number", "+15550001111")
            db.set_setting(con, "ghl_voice_url", "https://ghl.example.com/voice?x=1&y=2")
        r = client.post("/api/login", json={"email": "a@x.com", "password": "password1"})
        assert r.status_code == 200
        yield client


def test_normalize():
    assert normalize("+44 (20) 7946-0958") == "+442079460958"
    assert normalize("0092 300 1234567") == "+923001234567"
    assert normalize("(202) 555-0123", "+1") == "+12025550123"
    assert normalize("07700 900123", "+44") == "+447700900123"
    assert normalize("12") is None


def test_me_has_sip_and_turn(c):
    me = c.get("/api/me").json()
    assert me["sip"]["ext"] == "2001" and me["sip"]["password"] == "p1"
    assert any("turn:" in str(s["urls"]) for s in me["sip"]["iceServers"])
    assert me["callerId"] == "+15550001111"


def test_bad_login():
    with TestClient(app) as anon:
        assert anon.post("/api/login", json={"email": "a@x.com", "password": "nope"}).status_code == 401
        assert anon.get("/api/me").status_code == 401


def test_contacts_and_import(c):
    r = c.post("/api/contacts", json={"name": "Ali", "phone": "+92 300 1234567", "company": "Acme"})
    assert r.status_code == 200
    assert c.post("/api/contacts", json={"name": "x", "phone": "abc"}).status_code == 400
    csv = "First Name,Last Name,Mobile,Email\nSara,Khan,+971501234567,s@k.com\nBad,Row,12,\nDup,Ali,+923001234567,\n"
    r = c.post("/api/contacts/import", json={"csv": csv}).json()
    assert r["added"] == 1 and r["skipped"] == 2
    items = c.get("/api/contacts?q=khan").json()["items"]
    assert items[0]["name"] == "Sara Khan" and items[0]["phone"] == "+971501234567"


def test_outgoing_call_flow(c):
    cid = c.get("/api/contacts?q=Ali").json()["items"][0]["id"]
    call = c.post("/api/calls", json={"number": "+923001234567", "contact_id": cid}).json()
    # Asterisk asks permission (from 127.0.0.1, no proxy headers)
    r = c.get("/ast/authorize", params={"s": "sek", "ext": "2001", "to": "+923001234567", "call": call["id"]})
    ok, callerid, call_id, maxsec, rec, trunk, dial = r.text.split("|")
    assert ok == "ok" and callerid == "+15550001111" and int(call_id) == call["id"] and int(maxsec) == 3600
    assert rec == "0" and trunk == "crm-trunk-1" and dial == "+923001234567"
    r = c.get("/ast/hangup", params={"s": "sek", "call": call_id, "status": "ANSWER", "answered": "42", "cause": "16"})
    assert r.text == "ok"
    k = c.get(f"/api/calls/{call_id}").json()
    assert k["status"] == "answered" and k["duration"] == 42 and k["contact_id"] == cid
    assert c.patch(f"/api/calls/{call_id}", json={"disposition": "sale", "notes": "Bought"}).status_code == 200
    assert c.get(f"/api/contacts/{cid}").json()["status"] == "customer"


def test_policy_and_guards(c):
    c.put("/api/admin/settings", json={"allowed_prefixes": "+1,+44", "blocked_prefixes": "+4470"})
    assert c.post("/api/calls", json={"number": "+923001234567"}).status_code == 403
    assert c.post("/api/calls", json={"number": "+447012345678"}).status_code == 403
    assert c.post("/api/calls", json={"number": "+442079460958"}).status_code == 200
    assert c.get("/ast/authorize", params={"s": "sek", "ext": "2001", "to": "+923001234567"}).text.startswith("deny|")
    assert c.get("/ast/authorize", params={"s": "sek", "ext": "9999", "to": "+12025550123"}).text == "deny|unknown agent"
    assert c.get("/ast/authorize", params={"s": "bad", "ext": "2001", "to": "+1202"}).status_code == 403
    # through nginx (proxy headers) the hooks do not exist
    assert c.get("/ast/authorize", params={"s": "sek"}, headers={"X-Real-IP": "1.2.3.4"}).status_code == 404
    c.put("/api/admin/settings", json={"allowed_prefixes": "*", "blocked_prefixes": ""})


def test_incoming_answered_by_agent(c):
    c.post("/api/me/heartbeat", json={"available": True, "registered": True})
    p = {"CallSid": "CA1", "From": "+923001234567", "To": "+15550001111"}
    assert c.post("/twilio/voice", data=p, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    r = twilio(c, "/twilio/voice", p)
    assert r.status_code == 200 and "<Sip>" in r.text and "X-CRM-Call=" in r.text
    call_id = r.text.split("X-CRM-Call=")[1].split("<")[0]
    route = c.get("/ast/route", params={"s": "sek", "call": call_id}).text
    target, name, ring, rec = route.split("|")
    assert target == "PJSIP/2001" and name == "Ali" and ring == "20"
    c.get("/ast/hangup", params={"s": "sek", "call": call_id, "status": "ANSWER", "answered": "30",
                                 "peer": "PJSIP/2001-00000012"})
    r = twilio(c, f"/twilio/after-dial?call={call_id}", {"CallSid": "CA1", "DialCallStatus": "completed"})
    assert "<Hangup/>" in r.text
    k = c.get(f"/api/calls/{call_id}").json()
    assert k["status"] == "answered" and k["agent_id"] == 1 and k["direction"] == "in"


def test_incoming_no_answer_goes_to_ghl(c):
    r = twilio(c, "/twilio/voice", {"CallSid": "CA2", "From": "+12025550123", "To": "+15550001111"})
    call_id = r.text.split("X-CRM-Call=")[1].split("<")[0]
    c.get("/ast/route", params={"s": "sek", "call": call_id})
    c.get("/ast/hangup", params={"s": "sek", "call": call_id, "status": "NOANSWER"})
    r = twilio(c, f"/twilio/after-dial?call={call_id}", {"CallSid": "CA2", "DialCallStatus": "no-answer"})
    assert '<Redirect method="POST">https://ghl.example.com/voice?x=1&amp;y=2</Redirect>' in r.text
    assert c.get(f"/api/calls/{call_id}").json()["status"] == "sent-to-ghl"


def test_incoming_no_agents_online(c):
    c.post("/api/me/heartbeat", json={"available": False})
    r = twilio(c, "/twilio/voice", {"CallSid": "CA3", "From": "+12025550199", "To": "+15550001111"})
    call_id = r.text.split("X-CRM-Call=")[1].split("<")[0]
    assert c.get("/ast/route", params={"s": "sek", "call": call_id}).text == "none|"
    # forged / stale call ids never ring anyone
    assert c.get("/ast/route", params={"s": "sek", "call": "99999"}).text == "none|"
    r = twilio(c, f"/twilio/after-dial?call={call_id}", {"CallSid": "CA3", "DialCallStatus": "busy"})
    assert "<Redirect" in r.text
    c.post("/api/me/heartbeat", json={"available": True})


def test_caller_hangs_up_while_ringing(c):
    r = twilio(c, "/twilio/voice", {"CallSid": "CA4", "From": "+12025550188", "To": "+15550001111"})
    call_id = r.text.split("X-CRM-Call=")[1].split("<")[0]
    c.get("/ast/route", params={"s": "sek", "call": call_id})
    c.get("/ast/hangup", params={"s": "sek", "call": call_id, "status": "CANCEL"})
    r = twilio(c, f"/twilio/after-dial?call={call_id}", {"CallSid": "CA4", "DialCallStatus": "canceled"})
    assert "<Redirect" not in r.text
    assert c.get(f"/api/calls/{call_id}").json()["status"] == "missed"


def test_ghl_only_mode(c):
    c.put("/api/admin/settings", json={"inbound_mode": "ghl_only"})
    r = twilio(c, "/twilio/voice", {"CallSid": "CA5", "From": "+12025550177", "To": "+15550001111"})
    assert "<Redirect" in r.text and "<Sip>" not in r.text
    c.put("/api/admin/settings", json={"inbound_mode": "crm_then_ghl"})


def test_agent_management(c):
    r = c.post("/api/admin/users", json={"email": "b@x.com", "name": "Bob", "password": "password2"}).json()
    assert r["sip_ext"] == "2002"
    r = c.post("/api/admin/users", json={"email": "c@x.com", "name": "Cy", "password": "password3"}).json()
    assert r["sip_ext"] is None and r["warning"]
    with TestClient(app) as bob:
        bob.post("/api/login", json={"email": "b@x.com", "password": "password2"})
        assert bob.get("/api/admin/users").status_code == 403
        # agents only see their own calls + unanswered incoming
        assert all(k["agent_id"] in (None, 2) for k in bob.get("/api/calls").json()["items"])


def conf():
    return (Path(config.TRUNKS_DIR) / "pjsip_trunks.conf").read_text()


def test_env_trunks_were_moved_to_settings(c):
    d = c.get("/api/admin/trunks").json()
    tw, tx = d["items"]
    assert (tw["vendor"], tw["host"], tw["username"], tw["endpoint"]) == ("twilio", "crm.pstn.twilio.com", "tw-user", "crm-trunk-1")
    assert tx["vendor"] == "telnyx" and tx["host"] == "sip.telnyx.com"
    assert "tw-pass" not in json.dumps(d) and tw["password"]
    with db.tx() as con:
        assert "tw-pass" not in con.execute("SELECT password FROM sip_trunks WHERE id = 1").fetchone()[0]
    text = conf()
    assert "[crm-trunk-1]" in text and "username=tw-user" in text and "password=tw-pass" in text
    assert "contact=sip:crm.pstn.twilio.com" in text and "set_var=CRM_TRUNK=1" in text


def test_twilio_credentials_from_settings(c):
    c.put("/api/admin/integrations/twilio", json={"account_sid": "ACnew", "auth_token": "second-token-0123456789"})
    items = {i["provider"]: i for i in c.get("/api/admin/integrations").json()["items"]}
    assert items["twilio"]["configured"] and items["twilio"]["account_sid"] == "ACnew"
    assert "second-token" not in json.dumps(items)
    p = {"CallSid": "CA9", "From": "+12025550100"}
    assert not security.twilio_signature_ok("https://crm.example.com/twilio/voice", p, sign("/twilio/voice", p))
    c.put("/api/admin/integrations/twilio", json={"account_sid": "ACtest", "auth_token": "tok"})
    assert security.twilio_signature_ok("https://crm.example.com/twilio/voice", p, sign("/twilio/voice", p))


def test_trunk_validation(c):
    base = {"name": "X", "vendor": "custom", "host": "sip.example.com"}
    for bad in ({"host": "sip.example.com\n[evil]"}, {"host": "<city>.voip.ms"}, {"username": "a;b"},
                {"password": "x\\y"}, {"password": "x\ny"}, {"inbound_ips": "not-an-ip"}, {"vendor": "nope"},
                {"port": 70000}, {"register": True, "username": ""}):
        assert c.post("/api/admin/trunks", json={**base, **bad}).status_code == 400, bad
    assert c.put("/api/admin/settings", json={"default_trunk": "999"}).status_code == 400
    assert c.post("/api/admin/numbers", json={"number": "12"}).status_code == 400
    assert c.post("/api/admin/numbers", json={"number": "+15550002222", "trunk_id": 999}).status_code == 400


def test_second_vendor_trunk_and_numbers(c):
    t = c.post("/api/admin/trunks", json={
        "name": "VoIP.ms", "vendor": "voipms", "host": "atlanta.voip.ms", "username": "123_crm", "password": "p;w",
        "register": True, "inbound_ips": "208.100.60.0/24, 198.51.100.7", "dial_format": "digits"}).json()
    assert t["endpoint"] == "crm-trunk-3" and not t["warning"]
    text = conf()
    assert "password=p\\;w" in text and "match=208.100.60.0/24" in text and "match=198.51.100.7" in text
    assert "[crm-trunk-3-reg]" in text and "client_uri=sip:123_crm@atlanta.voip.ms" in text and "line=yes" in text
    assert (Path(config.TRUNKS_DIR) / "trunk_ips.txt").read_text().split() == ["208.100.60.0/24", "198.51.100.7"]
    # a password left empty on edit keeps the saved one
    assert c.put("/api/admin/trunks/3", json={"name": "VoIP.ms Atlanta", "password": ""}).status_code == 200
    assert "password=p\\;w" in conf() and "VoIP.ms Atlanta" in conf()

    n = c.post("/api/admin/numbers", json={"number": "+1 404 555 0100", "label": "Atlanta", "trunk_id": 3}).json()
    assert c.post("/api/admin/numbers", json={"number": "+14045550100"}).status_code == 409
    # a campaign showing this number leaves through its vendor's trunk, dialled the way that vendor wants
    camp = c.post("/api/campaigns", json={"name": "ATL", "kind": "power", "config": {"caller_id": "+14045550100"}}).json()
    with db.tx() as con:
        call_id = con.execute("INSERT INTO calls(direction, number, agent_id, status, campaign_id) "
                              "VALUES ('out', '+12025550123', 1, 'new', ?)", (camp["id"],)).lastrowid
    r = c.get("/ast/authorize", params={"s": "sek", "ext": "2001", "to": "+12025550123", "call": call_id}).text
    assert r.split("|")[1] == "+14045550100" and r.split("|")[5:] == ["crm-trunk-3", "12025550123"]

    # incoming over the trunk: the DID can arrive as +E.164, plain digits, only in the To header, or not at all
    c.post("/api/me/heartbeat", json={"available": True})
    for q in ({"did": "+14045550100"}, {"did": "14045550100"}, {"did": "123_crm", "to": "<sip:14045550100@1.2.3.4>;tag=x"},
              {"did": "123_crm", "trunk": "3"}):
        r = c.get("/ast/inbound", params={"s": "sek", "src": "+923001234567", **q}).text
        target, name, ring, rec, cid = r.split("|")
        assert target == "PJSIP/2001" and name == "Ali", q
        assert c.get(f"/api/calls/{cid}").json()["direction"] == "in"
        c.get("/ast/hangup", params={"s": "sek", "call": cid, "status": "NOANSWER"})
        assert c.get(f"/api/calls/{cid}").json()["status"] == "missed"
    assert c.get("/ast/inbound", params={"s": "sek", "did": "+19998887777"}).text == "none|"
    assert c.get("/ast/inbound", params={"s": "bad", "did": "+14045550100"}).status_code == 403
    c.put(f"/api/admin/numbers/{n['id']}", json={"number": "+14045550100", "trunk_id": 3, "inbound": "reject"})
    assert c.get("/ast/inbound", params={"s": "sek", "did": "+14045550100"}).text == "none|"

    # default trunk + the agent's caller ID follow the settings
    c.put("/api/admin/settings", json={"default_trunk": "3"})
    assert c.get("/api/me").json()["callerId"] == "+14045550100"
    # a disabled trunk leaves Asterisk and routing falls back to the first enabled one
    c.put("/api/admin/trunks/3", json={"enabled": False})
    assert "crm-trunk-3" not in conf()
    assert c.get("/api/me").json()["callerId"] == "+15550001111"
    # clean up for the other test module
    c.delete(f"/api/admin/numbers/{n['id']}")
    assert c.delete("/api/admin/trunks/3").json()["ok"]
    assert c.get("/api/admin/settings").json()["default_trunk"] == ""
    with db.tx() as con:
        con.execute("DELETE FROM calls WHERE campaign_id = ?", (camp["id"],))
    c.delete(f"/api/campaigns/{camp['id']}")
