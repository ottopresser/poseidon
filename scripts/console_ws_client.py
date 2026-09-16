#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
import termios
import tty
from contextlib import suppress
from urllib.parse import urlencode

import websockets


async def _read_stdin(loop: asyncio.AbstractEventLoop, file_descriptor: int) -> bytes:
    result = loop.create_future()

    def read_ready() -> None:
        try:
            result.set_result(os.read(file_descriptor, 1024))
        except OSError as exc:
            result.set_exception(exc)

    loop.add_reader(file_descriptor, read_ready)
    try:
        return await result
    finally:
        loop.remove_reader(file_descriptor)


async def _bridge_console(ws_url: str, api_key: str | None) -> None:
    stop_event = asyncio.Event()
    additional_headers = {"X-API-Key": api_key} if api_key else None

    async with websockets.connect(ws_url, max_size=None, extra_headers=additional_headers) as ws:
        loop = asyncio.get_running_loop()

        async def _recv_loop() -> None:
            while not stop_event.is_set():
                try:
                    message = await ws.recv()
                except websockets.ConnectionClosed:
                    stop_event.set()
                    return

                if isinstance(message, str):
                    data = message.encode("utf-8", errors="replace")
                else:
                    data = message

                os.write(sys.stdout.fileno(), data)

        async def _send_loop() -> None:
            while not stop_event.is_set():
                data = await _read_stdin(loop, sys.stdin.fileno())
                if not data:
                    stop_event.set()
                    return

                # Ctrl-] closes local client session.
                if data == b"\x1d":
                    stop_event.set()
                    return

                await ws.send(data)

        recv_task = asyncio.create_task(_recv_loop())
        send_task = asyncio.create_task(_send_loop())

        done, pending = await asyncio.wait(
            {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        for task in done:
            with suppress(asyncio.CancelledError):
                await task


def _build_ws_url(
    host: str,
    port: int,
    vm_name: str,
    api_key: str | None,
    console_token: str | None,
    secure: bool,
    plain: bool,
) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", vm_name):
        raise ValueError("vm_name must match the Poseidon VM name policy.")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535.")
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise ValueError("host contains unsupported characters.")

    scheme = "wss" if secure else "ws"
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    query_params: dict[str, str] = {}
    if console_token:
        query_params["console_token"] = console_token
    elif api_key:
        pass
    else:
        raise ValueError("Either api_key or console_token is required.")
    if plain:
        query_params["plain"] = "1"
    query = urlencode(query_params)
    query_suffix = f"?{query}" if query else ""
    return f"{scheme}://{url_host}:{port}/v1/vms/{vm_name}/console/ws{query_suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive Poseidon VM console client")
    parser.add_argument("--host", required=True, help="Poseidon host")
    parser.add_argument("--port", type=int, default=8000, help="Poseidon port")
    parser.add_argument("--vm", required=True, help="VM name")
    parser.add_argument("--api-key", help="Poseidon API key")
    parser.add_argument("--console-token", help="Short-lived console token")
    parser.add_argument("--secure", action="store_true", help="Use wss://")
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Request plain output mode for line-oriented clients",
    )
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("This client requires a TTY on stdin/stdout.", file=sys.stderr)
        return 2

    if not args.api_key and not args.console_token:
        print("Either --api-key or --console-token is required.", file=sys.stderr)
        return 2

    ws_url = _build_ws_url(
        host=args.host,
        port=args.port,
        vm_name=args.vm,
        api_key=args.api_key,
        console_token=args.console_token,
        secure=args.secure,
        plain=args.plain,
    )

    old_settings = termios.tcgetattr(sys.stdin.fileno())

    def _handle_sigint(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_sigint)

    try:
        tty.setraw(sys.stdin.fileno())
        asyncio.run(_bridge_console(ws_url, args.api_key))
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
