#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from pathlib import Path


def run_command(command: list[str], dry_run: bool, check: bool = True) -> None:
    printable = " ".join(shlex.quote(part) for part in command)
    print(printable)
    if not dry_run:
        subprocess.run(command, check=check)


def remove_file(path: Path, dry_run: bool) -> None:
    if path.exists():
        print(f"remove file: {path}")
        if not dry_run:
            path.unlink()
    else:
        print(f"skip missing file: {path}")


def remove_rc_conf_keys(rc_conf: Path, keys: list[str], dry_run: bool) -> None:
    for key in keys:
        run_command(["sysrc", "-f", str(rc_conf), "-x", key], dry_run=dry_run, check=False)


def validate_shell_inputs(service_name: str, remote_dir: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", service_name):
        raise SystemExit("Service name must start with a letter and contain only letters, digits, and underscores.")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", remote_dir):
        raise SystemExit("Remote directory must be an absolute path containing only letters, digits, '.', '_', '-', and '/'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Uninstall Poseidon FreeBSD rc.d service from the local host.")
    parser.add_argument("--service-name", default="poseidon", help="rc.d service name")
    parser.add_argument("--remote-dir", default="/opt/poseidon", help="Poseidon project directory")
    parser.add_argument("--rc-conf", default="/etc/rc.conf", help="Path to rc.conf")
    parser.add_argument("--keep-launcher", action="store_true", help="Do not remove run_poseidon.sh")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files")
    args = parser.parse_args()
    validate_shell_inputs(args.service_name, args.remote_dir)

    rc_conf = Path(args.rc_conf)
    rc_script = Path(f"/usr/local/etc/rc.d/{args.service_name}")
    launcher_script = Path(args.remote_dir) / "scripts" / "run_poseidon.sh"

    run_command(["service", args.service_name, "stop"], dry_run=args.dry_run, check=False)

    keys = [
        f"{args.service_name}_enable",
        f"{args.service_name}_dir",
        f"{args.service_name}_host",
        f"{args.service_name}_port",
        f"{args.service_name}_api_key",
        f"{args.service_name}_admin_token",
        f"{args.service_name}_console_token_secret",
    ]
    remove_rc_conf_keys(rc_conf, keys, dry_run=args.dry_run)

    remove_file(rc_script, dry_run=args.dry_run)
    if args.keep_launcher:
        print(f"keep launcher file: {launcher_script}")
    else:
        remove_file(launcher_script, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
