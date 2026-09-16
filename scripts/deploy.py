#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path

if __package__:
    from .service_templates import RCD_SCRIPT_TEMPLATE, RUNNER_SCRIPT_TEMPLATE
else:
    from service_templates import RCD_SCRIPT_TEMPLATE, RUNNER_SCRIPT_TEMPLATE


def parse_servers_from_file(servers_path: Path) -> list[str]:
    lines = servers_path.read_text(encoding="utf-8").splitlines()
    servers: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        servers.append(stripped)

    return servers


def validate_shell_inputs(service_name: str, remote_dir: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", service_name):
        raise SystemExit("Service name must start with a letter and contain only letters, digits, and underscores.")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", remote_dir):
        raise SystemExit("Remote directory must be an absolute path containing only letters, digits, '.', '_', '-', and '/'.")


def validate_server(server: str) -> None:
    target_pattern = r"(?:[A-Za-z0-9_.-]+@)?(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:]+\])"
    if not re.fullmatch(target_pattern, server) or server.startswith("-"):
        raise SystemExit(f"Invalid deployment target: {server}")


def apply_ssh_user(servers: list[str], ssh_user: str | None) -> list[str]:
    if not ssh_user:
        return servers
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", ssh_user):
        raise SystemExit("SSH user contains unsupported characters.")
    targets: list[str] = []
    for server in servers:
        host = server.split("@", 1)[-1]
        targets.append(f"{ssh_user}@{host}")
    return targets


def select_ssh_user(servers: list[str], configured_user: str | None, non_interactive: bool) -> str | None:
    if configured_user or non_interactive or not sys.stdin.isatty():
        return configured_user
    embedded_users = {server.split("@", 1)[0] for server in servers if "@" in server}
    default_user = embedded_users.pop() if len(embedded_users) == 1 else "root"
    selected = input(f"SSH login user [{default_user}]: ").strip()
    return selected or default_user


def load_credentials(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    credentials: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {
            "POSEIDON_API_KEY",
            "POSEIDON_ADMIN_TOKEN",
            "POSEIDON_CONSOLE_TOKEN_SECRET",
        } and value:
            credentials[key] = value
    return credentials


def write_credentials(path: Path, credentials: dict[str, str]) -> None:
    if any("\n" in value or "\r" in value for value in credentials.values()):
        raise SystemExit("Deployment credentials cannot contain newlines.")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{key}={value}\n" for key, value in credentials.items())
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as credential_file:
        credential_file.write(content)
    path.chmod(0o600)


def resolve_credentials(
    api_key: str | None,
    admin_token: str | None,
    console_token_secret: str | None,
    credentials_path: Path,
) -> tuple[str, str, str, bool]:
    stored = load_credentials(credentials_path)
    if credentials_path.exists():
        credentials_path.chmod(0o600)
    values = {
        "POSEIDON_API_KEY": api_key or stored.get("POSEIDON_API_KEY"),
        "POSEIDON_ADMIN_TOKEN": admin_token or stored.get("POSEIDON_ADMIN_TOKEN"),
        "POSEIDON_CONSOLE_TOKEN_SECRET": console_token_secret
        or stored.get("POSEIDON_CONSOLE_TOKEN_SECRET"),
    }
    generated = False
    for key, value in values.items():
        if not value:
            values[key] = secrets.token_urlsafe(48)
            generated = True
    if generated or not credentials_path.exists():
        write_credentials(credentials_path, {key: str(value) for key, value in values.items()})
    return (
        str(values["POSEIDON_API_KEY"]),
        str(values["POSEIDON_ADMIN_TOKEN"]),
        str(values["POSEIDON_CONSOLE_TOKEN_SECRET"]),
        generated,
    )


def run_command(command: list[str], dry_run: bool) -> None:
    printable = " ".join(shlex.quote(part) for part in command)
    print(printable)
    if not dry_run:
        subprocess.run(command, check=True)


def run_remote_shell(server: str, script: str, dry_run: bool) -> None:
    run_command(["ssh", server, "sh", "-s"], dry_run) if dry_run else subprocess.run(
        ["ssh", server, "sh", "-s"],
        input=script,
        text=True,
        check=True,
    )


def upload_text_file(server: str, remote_path: str, content: str, dry_run: bool) -> None:
    heredoc = "POSEIDON_EOF"
    script = (
        f"cat > {shlex.quote(remote_path)} <<'{heredoc}'\n"
        f"{content}\n"
        f"{heredoc}\n"
    )
    if dry_run:
        print(f"ssh {shlex.quote(server)} sh -s <<'EOF' ... write {remote_path} ... EOF")
        return
    subprocess.run(["ssh", server, "sh", "-s"], input=script, text=True, check=True)


def deploy_to_server(server: str, local_repo: Path, remote_dir: str, dry_run: bool) -> None:
    remote_dir_quoted = shlex.quote(remote_dir)

    mkdir_command = f"mkdir -p {remote_dir_quoted}"
    run_command(["ssh", server, "sh", "-c", shlex.quote(mkdir_command)], dry_run)

    run_command(
        [
            "rsync",
            "-az",
            "--delete",
            "--exclude",
            ".git",
            "--exclude",
            ".venv",
            "--exclude",
            ".svn",
            "--exclude",
            "__pycache__",
            "--exclude",
            "*.py[co]",
            "--exclude",
            "tests",
            "--exclude",
            ".pytest_cache",
            f"{local_repo}/",
            f"{server}:{remote_dir}/",
        ],
        dry_run,
    )

    bootstrap = (
        f"cd {remote_dir_quoted} && "
        "python3 -m venv .venv && "
        ".venv/bin/python -m pip install --upgrade pip && "
        ".venv/bin/python -m pip install -r requirements.txt"
    )

    run_command(["ssh", server, "sh", "-c", shlex.quote(bootstrap)], dry_run)


def install_freebsd_service(
    server: str,
    remote_dir: str,
    service_name: str,
    api_key: str,
    admin_token: str,
    console_token_secret: str,
    dry_run: bool,
) -> None:
    rc_path = f"/usr/local/etc/rc.d/{service_name}"
    launcher_path = f"{remote_dir}/scripts/run_poseidon.sh"
    rc_script = RCD_SCRIPT_TEMPLATE.format(
        service_name=service_name,
        remote_dir=remote_dir,
        host="127.0.0.1",
        port=8000,
        run_user="root",
    )
    launcher_script = RUNNER_SCRIPT_TEMPLATE.format(
        remote_dir=remote_dir,
        host="127.0.0.1",
        port=8000,
    )

    upload_text_file(server, rc_path, rc_script, dry_run)
    upload_text_file(server, launcher_path, launcher_script, dry_run)
    run_command(["ssh", server, f"chmod 755 {shlex.quote(rc_path)} {shlex.quote(launcher_path)}"], dry_run)

    api_key_arg = shlex.quote(f"{service_name}_api_key={api_key}")
    admin_token_arg = shlex.quote(f"{service_name}_admin_token={admin_token}")
    console_token_arg = shlex.quote(f"{service_name}_console_token_secret={console_token_secret}")
    rcconf_script = f"""
set -eu
sysrc -f /etc/rc.conf {service_name}_enable=\"YES\"
sysrc -f /etc/rc.conf {service_name}_dir=\"{remote_dir}\"
sysrc -f /etc/rc.conf {service_name}_host=\"127.0.0.1\"
sysrc -f /etc/rc.conf {service_name}_port=\"8000\"
sysrc -f /etc/rc.conf {api_key_arg}
sysrc -f /etc/rc.conf {admin_token_arg}
sysrc -f /etc/rc.conf {console_token_arg}
service {service_name} restart || service {service_name} start
"""
    run_remote_shell(server, rcconf_script, dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy Poseidon to all servers listed in a servers.conf file."
    )
    parser.add_argument(
        "--servers-file",
        default="scripts/servers.conf",
        help="Path to newline-delimited server list file",
    )
    parser.add_argument(
        "--remote-dir",
        default="/usr/local/poseidon",
        help="Remote deployment directory",
    )
    parser.add_argument("--ssh-user", help="SSH login user applied to every target")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable the SSH-user prompt; use users from servers.conf or --ssh-user",
    )
    parser.add_argument(
        "--credentials-file",
        default="~/.config/poseidon/deploy.env",
        help="Mode-0600 file used to save and reuse generated deployment credentials",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing (default)",
    )
    execution.add_argument(
        "--apply",
        action="store_true",
        help="Perform deployment and service changes",
    )
    parser.add_argument(
        "--service-name",
        default="poseidon",
        help="FreeBSD rc.d service name",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("POSEIDON_API_KEY"),
        help="API key to write into /etc/rc.conf for the service",
    )
    parser.add_argument(
        "--admin-token",
        default=os.getenv("POSEIDON_ADMIN_TOKEN"),
        help="Admin token to write into /etc/rc.conf for docs/openapi access",
    )
    parser.add_argument(
        "--console-token-secret",
        default=os.getenv("POSEIDON_CONSOLE_TOKEN_SECRET"),
        help="Console token secret to write into /etc/rc.conf for short-lived WS token signing",
    )
    parser.add_argument(
        "--skip-freebsd-service",
        action="store_true",
        help="Skip rc.d service installation and rc.conf updates",
    )
    args = parser.parse_args()
    args.dry_run = not args.apply
    validate_shell_inputs(args.service_name, args.remote_dir)
    if args.dry_run:
        args.api_key = args.api_key or "dry-run-api-key"
        args.admin_token = args.admin_token or "dry-run-admin-token"
        args.console_token_secret = args.console_token_secret or "dry-run-console-token-secret"
    elif not args.skip_freebsd_service:
        credentials_path = Path(args.credentials_file).expanduser()
        (
            args.api_key,
            args.admin_token,
            args.console_token_secret,
            generated_credentials,
        ) = resolve_credentials(
            args.api_key,
            args.admin_token,
            args.console_token_secret,
            credentials_path,
        )
        if generated_credentials:
            print(f"Generated missing credentials and saved them to {credentials_path} (mode 0600).")

    repo_root = Path(__file__).resolve().parent.parent
    servers_path = (repo_root / args.servers_file).resolve()

    if not servers_path.exists():
        raise SystemExit(
            f"Servers file not found: {servers_path}. Create it with one server per line, for example 'root@10.0.0.10'."
        )

    servers = parse_servers_from_file(servers_path)
    if not servers:
        raise SystemExit(
            f"No servers found in {servers_path}. Add one server per line, for example 'root@10.0.0.10'."
        )

    ssh_user = select_ssh_user(servers, args.ssh_user, args.non_interactive)
    servers = apply_ssh_user(servers, ssh_user)
    for server in servers:
        validate_server(server)

    for server in servers:
        print(f"\\n=== Deploying to {server} ===")
        deploy_to_server(server, repo_root, args.remote_dir, args.dry_run)
        if not args.skip_freebsd_service:
            install_freebsd_service(
                server=server,
                remote_dir=args.remote_dir,
                service_name=args.service_name,
                api_key=args.api_key,
                admin_token=args.admin_token,
                console_token_secret=args.console_token_secret,
                dry_run=args.dry_run,
            )


if __name__ == "__main__":
    main()
