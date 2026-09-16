#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
from pathlib import Path

if __package__:
    from .service_templates import RCD_SCRIPT_TEMPLATE, RUNNER_SCRIPT_TEMPLATE
else:
    from service_templates import RCD_SCRIPT_TEMPLATE, RUNNER_SCRIPT_TEMPLATE


def run_command(command: list[str], dry_run: bool) -> None:
    printable = " ".join(shlex.quote(part) for part in command)
    print(printable)
    if not dry_run:
        subprocess.run(command, check=True)


def set_rc_conf_value(rc_conf: Path, key: str, value: str, dry_run: bool) -> None:
    sensitive = key.endswith(("_api_key", "_admin_token", "_console_token_secret"))
    display_value = "<redacted>" if sensitive else value
    line = f'{key}="{display_value}"'
    print(f"set rc.conf key: {line}")
    command = ["sysrc", "-f", str(rc_conf), f"{key}={value}"]
    if sensitive:
        if not dry_run:
            subprocess.run(command, check=True)
        return
    run_command(command, dry_run)


def write_file(path: Path, content: str, mode: int, dry_run: bool) -> None:
    print(f"write file: {path}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)


def validate_shell_inputs(
    service_name: str,
    remote_dir: str,
    host: str,
    port: int,
    run_user: str,
) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", service_name):
        raise SystemExit("Service name must start with a letter and contain only letters, digits, and underscores.")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", remote_dir):
        raise SystemExit("Remote directory must be an absolute path containing only letters, digits, '.', '_', '-', and '/'.")
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise SystemExit("Host contains unsupported characters.")
    if port < 1 or port > 65535:
        raise SystemExit("Port must be between 1 and 65535.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", run_user):
        raise SystemExit("Run user contains unsupported characters.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Poseidon FreeBSD rc.d service on the local host.")
    parser.add_argument("--remote-dir", default="/opt/poseidon", help="Poseidon project directory")
    parser.add_argument("--service-name", default="poseidon", help="rc.d service name")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--run-user", default="root", help="Service run user")
    parser.add_argument("--api-key", default=os.getenv("POSEIDON_API_KEY"), help="POSEIDON_API_KEY value")
    parser.add_argument("--admin-token", default=os.getenv("POSEIDON_ADMIN_TOKEN"), help="POSEIDON_ADMIN_TOKEN value")
    parser.add_argument(
        "--console-token-secret",
        default=os.getenv("POSEIDON_CONSOLE_TOKEN_SECRET"),
        help="POSEIDON_CONSOLE_TOKEN_SECRET value",
    )
    parser.add_argument("--rc-conf", default="/etc/rc.conf", help="Path to rc.conf")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files")
    args = parser.parse_args()
    validate_shell_inputs(args.service_name, args.remote_dir, args.host, args.port, args.run_user)
    if not args.dry_run and (not args.api_key or not args.admin_token or not args.console_token_secret):
        parser.error(
            "API key, admin token, and console token secret are required via arguments or POSEIDON_* environment variables."
        )
    if args.dry_run:
        args.api_key = args.api_key or "dry-run-api-key"
        args.admin_token = args.admin_token or "dry-run-admin-token"
        args.console_token_secret = args.console_token_secret or "dry-run-console-token-secret"

    rc_conf = Path(args.rc_conf)
    rc_path = Path(f"/usr/local/etc/rc.d/{args.service_name}")
    launcher_path = Path(args.remote_dir) / "scripts" / "run_poseidon.sh"

    rc_script = RCD_SCRIPT_TEMPLATE.format(
        service_name=args.service_name,
        remote_dir=args.remote_dir,
        host=args.host,
        port=args.port,
        run_user=args.run_user,
    )
    launcher_script = RUNNER_SCRIPT_TEMPLATE.format(
        remote_dir=args.remote_dir,
        host=args.host,
        port=args.port,
    )

    write_file(rc_path, rc_script, mode=0o755, dry_run=args.dry_run)
    write_file(launcher_path, launcher_script, mode=0o755, dry_run=args.dry_run)

    set_rc_conf_value(rc_conf, f"{args.service_name}_enable", "YES", args.dry_run)
    set_rc_conf_value(rc_conf, f"{args.service_name}_dir", args.remote_dir, args.dry_run)
    set_rc_conf_value(rc_conf, f"{args.service_name}_host", args.host, args.dry_run)
    set_rc_conf_value(rc_conf, f"{args.service_name}_port", str(args.port), args.dry_run)
    set_rc_conf_value(rc_conf, f"{args.service_name}_api_key", args.api_key, args.dry_run)
    set_rc_conf_value(rc_conf, f"{args.service_name}_admin_token", args.admin_token, args.dry_run)
    set_rc_conf_value(
        rc_conf,
        f"{args.service_name}_console_token_secret",
        args.console_token_secret,
        args.dry_run,
    )

    run_command(["sh", "-c", f"service {args.service_name} restart || service {args.service_name} start"], args.dry_run)


if __name__ == "__main__":
    main()
