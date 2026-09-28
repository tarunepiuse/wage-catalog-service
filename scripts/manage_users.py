"""Bootstrap / manage users from the command line (use this to create the first admin).

    python -m scripts.manage_users create <username> [--role admin|user] [--password-stdin]
    python -m scripts.manage_users set-password <username> [--password-stdin]
    python -m scripts.manage_users disable <username>
    python -m scripts.manage_users enable <username>
    python -m scripts.manage_users list
"""

import argparse
import getpass
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from app.auth.users import UserStore
from app.config import BASE_DIR


def _db_path() -> Path:
    # Only the DB path is needed here, so don't require the JWT secret to be configured.
    env = {**dotenv_values(BASE_DIR / ".env"), **os.environ}
    return Path(env.get("WTC_USER_DB_PATH") or BASE_DIR / "data" / "users.db")


def _read_password(from_stdin: bool) -> str:
    if from_stdin:  # for scripted/container bootstrap: echo "$PW" | ... --password-stdin
        pw = sys.stdin.readline().rstrip("\r\n")
    else:
        pw = getpass.getpass("Password (min 12 chars): ")
        if pw != getpass.getpass("Repeat password: "):
            sys.exit("Passwords do not match")
    if len(pw) < 12:
        sys.exit("Password must be at least 12 characters")
    return pw


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("username")
    c.add_argument("--role", choices=["admin", "user"], default="user")
    c.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    sp = sub.add_parser("set-password")
    sp.add_argument("username")
    sp.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("list")
    args = p.parse_args()

    store = UserStore(_db_path())
    if args.cmd == "create":
        try:
            u = store.create(args.username, _read_password(args.password_stdin), args.role)
        except ValueError as e:
            sys.exit(str(e))
        print(f"Created {u.role} '{u.username}'")
    elif args.cmd == "set-password":
        ok = store.set_password(args.username, _read_password(args.password_stdin))
        print("Password updated; existing tokens revoked" if ok else "User not found")
    elif args.cmd in ("disable", "enable"):
        ok = store.set_active(args.username, args.cmd == "enable")
        print(f"User {args.cmd}d" if ok else "User not found")
    else:
        for u in store.list():
            print(f"{u.username:30} {u.role:6} {'active' if u.is_active else 'disabled'}")


if __name__ == "__main__":
    main()
