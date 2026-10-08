"""The few Twilio REST calls the CRM needs: find the number, point its voice
webhook at the CRM (remembering GHL's), and put it back."""
import httpx2 as httpx
from fastapi import HTTPException

from . import vault

BASE = "https://api.twilio.com/2010-04-01/Accounts"


def _client():
    sid, token = vault.twilio_creds()
    if not (sid and token):
        raise HTTPException(400, "Twilio Account SID / Auth Token missing (Admin → Integrations → Twilio)")
    return sid, httpx.Client(auth=(sid, token), timeout=15)


def _check(r):
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise HTTPException(502, f"Twilio: {msg}")
    return r.json()


def find_number(phone):
    sid, client = _client()
    with client as c:
        data = _check(c.get(f"{BASE}/{sid}/IncomingPhoneNumbers.json", params={"PhoneNumber": phone}))
    nums = data.get("incoming_phone_numbers") or []
    if not nums:
        raise HTTPException(404, f"{phone} is not a number in Twilio account {sid}")
    return nums[0]


def get_application(app_sid):
    sid, client = _client()
    with client as c:
        return _check(c.get(f"{BASE}/{sid}/Applications/{app_sid}.json"))


def update_number(pn_sid, **fields):
    sid, client = _client()
    with client as c:
        return _check(c.post(f"{BASE}/{sid}/IncomingPhoneNumbers/{pn_sid}.json", data=fields))
