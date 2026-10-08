"""Phone number clean-up and the international dialling policy."""
import re

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize(raw, default_country="+1"):
    """'+44 (20) 7946-0958' / '0044…' / '2025550123' -> '+442079460958'. None if invalid."""
    s = (raw or "").strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if plus:
        num = "+" + digits
    elif digits.startswith("00"):
        num = "+" + digits[2:]
    else:
        cc = re.sub(r"\D", "", default_country or "")
        # national numbers written with a leading trunk 0 (e.g. UK 07700…)
        num = "+" + cc + digits.lstrip("0") if cc else "+" + digits
    return num if E164.match(num) else None


def _prefixes(text):
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip()]


def check_allowed(number, settings):
    """Returns '' if the number may be dialled, otherwise the reason."""
    for p in _prefixes(settings.get("blocked_prefixes")):
        if number.startswith(p):
            return f"Calls to {p} are blocked"
    allowed = _prefixes(settings.get("allowed_prefixes") or "*")
    if "*" in allowed or any(number.startswith(p) for p in allowed):
        return ""
    return "This country is not enabled for calling (Admin → Settings → Allowed countries)"


def timezone_for(number):
    """Best-guess IANA time zone of a phone number ('' if unknown)."""
    try:
        import phonenumbers
        from phonenumbers import timezone as pn_tz
        zones = pn_tz.time_zones_for_number(phonenumbers.parse(number))
        return zones[0] if zones and zones[0] != "Etc/Unknown" else ""
    except Exception:
        return ""
