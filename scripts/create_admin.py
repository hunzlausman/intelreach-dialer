#!/usr/bin/env python3
"""Create (or reset the password of) a CRM admin.

    cd /opt/intelreach-crm && set -a && . /etc/intelreach-crm.env && set +a && \
    venv/bin/python scripts/create_admin.py you@example.com "Your Name"
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db, security  # noqa: E402
from app.admin import free_ext  # noqa: E402

if len(sys.argv) < 3:
    sys.exit(__doc__)
email, name = sys.argv[1], sys.argv[2]
pw = sys.argv[3] if len(sys.argv) > 3 else getpass.getpass("Password (min 8 characters): ")
if len(pw) < 8:
    sys.exit("Password too short")

db.init()
with db.tx() as con:
    u = con.execute("SELECT id, sip_ext FROM users WHERE email = ?", (email,)).fetchone()
    if u:
        con.execute("UPDATE users SET pw_hash = ?, role = 'admin', active = 1, sip_ext = COALESCE(sip_ext, ?) WHERE id = ?",
                    (security.hash_password(pw), free_ext(con), u["id"]))
        print(f"Updated {email} (admin)")
    else:
        ext = free_ext(con)
        con.execute("INSERT INTO users(email, name, role, pw_hash, sip_ext) VALUES (?, ?, 'admin', ?, ?)",
                    (email, name, security.hash_password(pw), ext))
        print(f"Created admin {email} with phone line {ext or '(none – generate the agent pool first)'}")
