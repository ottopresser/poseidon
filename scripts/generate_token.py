#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a cryptographically strong token.")
    parser.add_argument(
        "--env",
        default="POSEIDON_ADMIN_TOKEN",
        help="Environment variable name shown in export format",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=32,
        help="Number of random bytes before URL-safe encoding",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print only the token value",
    )
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.env):
        raise SystemExit("--env must be a valid shell environment variable name")
    if args.bytes < 16:
        raise SystemExit("--bytes must be at least 16")

    token = secrets.token_urlsafe(args.bytes)
    if args.raw:
        print(token)
        return

    print(f"export {args.env}=\"{token}\"")


if __name__ == "__main__":
    main()
