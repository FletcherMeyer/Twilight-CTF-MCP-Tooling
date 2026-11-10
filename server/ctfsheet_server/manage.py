"""Admin CLI.

    python -m ctfsheet_server.manage gen-key
    python -m ctfsheet_server.manage init-db
    python -m ctfsheet_server.manage rotate-keys        # after prepending a new key to CTFSHEET_DATA_KEYS
    python -m ctfsheet_server.manage reset-password USERNAME
    python -m ctfsheet_server.manage list-teams
"""
from __future__ import annotations

import argparse
import getpass
import sys

from cryptography.fernet import Fernet


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ctfsheet-manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-key", help="print a new Fernet data-encryption key")
    sub.add_parser("init-db", help="create/upgrade the database schema")
    sub.add_parser("rotate-keys", help="re-encrypt all data with the first key in CTFSHEET_DATA_KEYS")
    rp = sub.add_parser("reset-password", help="set a user's password")
    rp.add_argument("username")
    sub.add_parser("list-teams")
    args = ap.parse_args(argv)

    if args.cmd == "gen-key":
        print(Fernet.generate_key().decode())
        return 0

    from .crypto import box, hash_secret
    from .db import connect, init_db

    init_db()
    if args.cmd == "init-db":
        print("Database ready.")
        return 0

    conn = connect()
    if args.cmd == "rotate-keys":
        b = box()
        n = 0
        for table, cols in {"teams": ["agent_md"], "challenges": ["description", "notes"],
                            "flags": ["value"], "artifacts": ["content"]}.items():
            for row in conn.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall():
                new = [b.rotate(row[c]) for c in cols]
                conn.execute(f"UPDATE {table} SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                             (*new, row["id"]))
                n += 1
        conn.commit()
        print(f"Re-encrypted {n} rows. You may now remove old keys from CTFSHEET_DATA_KEYS.")
    elif args.cmd == "reset-password":
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat: ") or len(pw) < 10:
            print("Passwords differ or are shorter than 10 characters.", file=sys.stderr)
            return 1
        cur = conn.execute("UPDATE users SET pw_hash = ? WHERE username = ?", (hash_secret(pw), args.username))
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
                     (args.username,))
        conn.commit()
        print("Updated." if cur.rowcount else "No such user.")
    elif args.cmd == "list-teams":
        for r in conn.execute("SELECT t.id, t.name, COUNT(u.id) AS members FROM teams t "
                              "LEFT JOIN users u ON u.team_id = t.id GROUP BY t.id ORDER BY t.id"):
            print(f"{r['id']:>4}  {r['name']:<40} {r['members']} members")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
