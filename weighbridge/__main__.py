"""Command line: python -m weighbridge <command>

  init        create the database and the first owner/admin account
  run         start the app (open http://127.0.0.1:8080 in a browser)
  sniff       show what the weight indicator sends on a COM port
  add-user    add a user from the command line
  verify      check the audit log has not been tampered with
  backup      write a database backup now
"""
from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import audit, security
from .config import load_config
from .db import connect, init_db, transaction
from .services.weighing import now_iso


def _ask_password(cfg, username: str) -> str:
    min_len = int(cfg["security"]["password_min_length"])
    while True:
        pw = getpass.getpass(f"Password for {username} (min {min_len} characters): ")
        problems = security.password_problems(pw, min_len, username)
        if problems:
            print("  " + " ".join(problems))
            continue
        if getpass.getpass("Same password again: ") != pw:
            print("  The passwords don't match.")
            continue
        return pw


def _add_user(cfg, username: str, full_name: str, role: str, password: str, must_change: bool) -> None:
    conn = connect(cfg.db_path)
    try:
        with transaction(conn):
            conn.execute("INSERT INTO users(username, full_name, role, pw_hash, must_change_pw, created_at) "
                         "VALUES (?,?,?,?,?,?)", (username, full_name, role, security.hash_password(password),
                                                  int(must_change), now_iso(cfg)))
            audit.record(conn, now_iso(cfg), None, "user.created", "user", username, {"role": role, "via": "cli"})
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weighbridge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.toml (default: next to the program)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("run")
    sn = sub.add_parser("sniff")
    sn.add_argument("--port", required=True, help="e.g. COM3")
    sn.add_argument("--baud", type=int, help="test only this baud rate")
    au = sub.add_parser("add-user")
    au.add_argument("username")
    au.add_argument("--name", default="")
    au.add_argument("--role", choices=security.ROLES, default="operator")
    sub.add_parser("verify")
    sub.add_parser("backup")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "sniff":
        from .indicator import sniff
        sniff(args.port, (args.baud,) if args.baud else (9600, 4800, 2400, 19200, 1200))
        return 0

    init_db(cfg.db_path)

    if args.cmd == "init":
        conn = connect(cfg.db_path)
        has_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        print(f"Database: {cfg.db_path}")
        if has_users:
            print("Users already exist. Use add-user to add more.")
            return 0
        print("Create the owner/admin account. You will set up two-step login at first sign-in.")
        username = input("Admin username [owner]: ").strip().lower() or "owner"
        full_name = input("Full name: ").strip() or username
        _add_user(cfg, username, full_name, "admin", _ask_password(cfg, username), must_change=False)
        print(f"Done. Start the app with:  python -m weighbridge run   and log in as {username}.")
        return 0

    if args.cmd == "add-user":
        name = args.name or input("Full name: ").strip() or args.username
        _add_user(cfg, args.username.lower(), name, args.role, _ask_password(cfg, args.username), must_change=True)
        print(f"User {args.username} added as {args.role}. They must change the password at first login.")
        return 0

    if args.cmd == "verify":
        conn = connect(cfg.db_path)
        ok, count, bad = audit.verify_chain(conn)
        head = audit.chain_head(conn)
        conn.close()
        if ok:
            print(f"OK: {count} audit records, chain intact. Head {head}")
            return 0
        print(f"TAMPERED: chain breaks at audit record {bad}.")
        return 2

    if args.cmd == "backup":
        from .services.backup import backup_now
        print("Backup written:", backup_now(cfg))
        return 0

    if args.cmd == "run":
        import uvicorn
        from .web.app import create_app
        host, port = cfg["server"]["host"], int(cfg["server"]["port"])
        if host not in ("127.0.0.1", "localhost"):
            print(f"WARNING: listening on {host}. Other computers on the network can reach the login page. "
                  "Only do this behind the site firewall, and preferably with HTTPS.")
        uvicorn.run(create_app(cfg), host=host, port=port, log_level="info", proxy_headers=False,
                    server_header=False)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
