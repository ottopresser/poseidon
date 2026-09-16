#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a valid Poseidon API key.")
    parser.add_argument(
        "--bytes",
        type=int,
        default=32,
        help="Number of random bytes before URL-safe encoding",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print only the key value",
    )
    args = parser.parse_args()

    if args.bytes < 16:
        raise SystemExit("--bytes must be at least 16")

    api_key = secrets.token_urlsafe(args.bytes)
    if args.raw:
        print(api_key)
        return

    print(f"export POSEIDON_API_KEY=\"{api_key}\"")


if __name__ == "__main__":
    main()
