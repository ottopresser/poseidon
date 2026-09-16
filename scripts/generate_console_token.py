#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security import issue_console_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a short-lived Poseidon console token.")
    parser.add_argument("--vm-name", required=True, help="VM name encoded into the token")
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help="Optional token lifetime in seconds",
    )
    parser.add_argument(
        "--env",
        default="POSEIDON_CONSOLE_TOKEN",
        help="Environment variable name shown in export format",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print only the token value",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.vm_name):
        raise SystemExit("--vm-name must match the Poseidon VM name policy")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.env):
        raise SystemExit("--env must be a valid shell environment variable name")

    try:
        token, ttl_seconds, expires_at = issue_console_token(args.vm_name, ttl_seconds=args.ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        detail = getattr(exc, "detail", None)
        raise SystemExit(detail or str(exc)) from exc

    if args.raw:
        print(token)
        return

    print(f'export {args.env}="{token}"')
    print(f"# vm={args.vm_name} ttl_seconds={ttl_seconds} expires_at_epoch={expires_at}")


if __name__ == "__main__":
    main()