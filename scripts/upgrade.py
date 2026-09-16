#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import re
import shlex
import subprocess
from pathlib import Path

if __package__:
    from .service_templates import RCD_SCRIPT_TEMPLATE, RUNNER_SCRIPT_TEMPLATE
else:
    from service_templates import RCD_SCRIPT_TEMPLATE, RUNNER_SCRIPT_TEMPLATE


def validate_inputs(service_name: str, release_root: str, release_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", service_name):
        raise SystemExit(
            "Service name must start with a letter and contain only letters, digits, and underscores."
        )
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", release_root):
        raise SystemExit(
            "Release root must be an absolute path containing only letters, digits, '.', '_', '-', and '/'."
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_id):
        raise SystemExit(
            "Release ID must start with a letter or digit and contain only letters, digits, '.', '_', and '-'."
        )


def validate_server(server: str) -> None:
    target_pattern = r"(?:[A-Za-z0-9_.-]+@)?(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:]+\])"
    if not re.fullmatch(target_pattern, server) or server.startswith("-"):
        raise SystemExit(f"Invalid upgrade target: {server}")


def parse_servers_from_file(servers_path: Path) -> list[str]:
    return [
        line.strip()
        for line in servers_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def run_command(
    command: list[str],
    dry_run: bool,
    *,
    input_text: str | None = None,
) -> None:
    printable = " ".join(shlex.quote(part) for part in command)
    print(printable)
    if not dry_run:
        try:
            subprocess.run(command, input=input_text, text=True, check=True)
        except subprocess.CalledProcessError as error:
            raise SystemExit(
                f"Command failed with exit status {error.returncode}: {printable}"
            ) from None


def run_remote_shell(server: str, script: str, dry_run: bool) -> None:
    if dry_run:
        print(f"ssh {shlex.quote(server)} sh -s <<'POSEIDON_UPGRADE' ... POSEIDON_UPGRADE")
        return
    run_command(["ssh", server, "sh", "-s"], False, input_text=script)


def preflight_script(
    service_name: str,
    release_root: str,
    release_dir: str,
    allow_nonlocal_bind: bool,
    prepare_release_root: bool = True,
) -> str:
    service = shlex.quote(service_name)
    root = shlex.quote(release_root)
    release = shlex.quote(release_dir)
    allow_nonlocal = "yes" if allow_nonlocal_bind else "no"
    prepare = f"mkdir -p {root}" if prepare_release_root else ""
    return f"""set -eu
[ "$(id -u)" -eq 0 ] || {{ echo "Upgrade requires root on the target" >&2; exit 1; }}
command -v python3 >/dev/null
python3 -m venv --help >/dev/null
python3 -m ensurepip --version >/dev/null
command -v rsync >/dev/null
command -v sysrc >/dev/null
command -v service >/dev/null
command -v fetch >/dev/null
command -v vm >/dev/null
[ -x /usr/local/etc/rc.d/{service} ] || {{ echo "Missing rc.d service: {service}" >&2; exit 1; }}
current_dir=$(sysrc -n {service_name}_dir)
[ -n "$current_dir" ] && [ -d "$current_dir" ] || {{ echo "Configured service directory is missing" >&2; exit 1; }}
for key in {service_name}_api_key {service_name}_admin_token {service_name}_console_token_secret; do
    [ -n "$(sysrc -n "$key" 2>/dev/null || true)" ] || {{ echo "Missing required rc.conf key: $key" >&2; exit 1; }}
done
bind_host=$(sysrc -n {service_name}_host 2>/dev/null || true)
case "$bind_host" in
    127.0.0.1|localhost|::1) ;;
    *) [ "{allow_nonlocal}" = yes ] || {{ echo "Refusing non-local service bind: $bind_host" >&2; exit 1; }} ;;
esac
[ ! -e {release} ] || {{ echo "Release already exists: {release_dir}" >&2; exit 1; }}
{prepare}
"""


def bootstrap_script(service_name: str, release_dir: str) -> str:
    release = shlex.quote(release_dir)
    return f"""set -eu
cd {release}
python3 -m venv .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m compileall -q app scripts
.venv/bin/python - <<'PY'
from app.main import app
schema = app.openapi()
required_paths = {{"/health", "/v1/vms", "/v1/vms/create"}}
assert required_paths <= schema["paths"].keys()
PY
sh -n scripts/run_poseidon.sh
sh -n scripts/{service_name}.rcd
"""


def promotion_script(service_name: str, release_dir: str) -> str:
    service = shlex.quote(service_name)
    release = shlex.quote(release_dir)
    return f"""set -eu
old_dir=$(sysrc -n {service_name}_dir)
rc_path=/usr/local/etc/rc.d/{service_name}
rc_backup={release}/.poseidon-previous-rcd
cp -p "$rc_path" "$rc_backup"
rollback()
{{
    echo "Upgrade failed; restoring $old_dir" >&2
    cp -p "$rc_backup" "$rc_path"
    sysrc {service_name}_dir="$old_dir" >/dev/null
    service {service} restart
}}
wait_for_health()
{{
    attempts=0
    while [ "$attempts" -lt 30 ]; do
        if fetch -qo - "$health_url" 2>/dev/null | grep -F '"status":"ok"' >/dev/null; then
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    return 1
}}
if ! install -m 755 {release}/scripts/{service_name}.rcd "$rc_path"; then
    rollback
    exit 1
fi
if ! sysrc {service_name}_dir={release} >/dev/null; then
    rollback
    exit 1
fi
if ! service {service} restart; then
    rollback
    exit 1
fi
bind_host=$(sysrc -n {service_name}_host 2>/dev/null || true)
port=$(sysrc -n {service_name}_port 2>/dev/null || echo 8000)
case "$bind_host" in
    ::1) health_url="http://[::1]:$port/health" ;;
    127.0.0.1|localhost) health_url="http://$bind_host:$port/health" ;;
    *) health_url="http://127.0.0.1:$port/health" ;;
esac
if ! wait_for_health; then
    rollback
    exit 1
fi
printf '%s\n' "$old_dir" > {release}/.poseidon-previous-release
echo "Upgrade succeeded: $old_dir -> {release_dir}"
"""


def upgrade_server(
    server: str,
    local_repo: Path,
    service_name: str,
    release_root: str,
    release_id: str,
    allow_nonlocal_bind: bool,
    dry_run: bool,
    preflight_only: bool = False,
) -> None:
    release_dir = f"{release_root.rstrip('/')}/{release_id}"
    run_remote_shell(
        server,
        preflight_script(
            service_name,
            release_root,
            release_dir,
            allow_nonlocal_bind,
            prepare_release_root=not preflight_only,
        ),
        dry_run,
    )
    if preflight_only:
        print(f"Preflight passed: {server}")
        return
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
            f"{server}:{release_dir}/",
        ],
        dry_run,
    )
    runner = RUNNER_SCRIPT_TEMPLATE.format(
        remote_dir=release_dir,
        host="127.0.0.1",
        port=8000,
    )
    rc_script = RCD_SCRIPT_TEMPLATE.format(
        service_name=service_name,
        remote_dir=release_dir,
        host="127.0.0.1",
        port=8000,
        run_user="root",
    )
    upload_runner = (
        f"cat > {shlex.quote(f'{release_dir}/scripts/run_poseidon.sh')} <<'POSEIDON_RUNNER'\n"
        f"{runner}POSEIDON_RUNNER\n"
        f"cat > {shlex.quote(f'{release_dir}/scripts/{service_name}.rcd')} <<'POSEIDON_RCD'\n"
        f"{rc_script}POSEIDON_RCD\n"
        f"chmod 755 {shlex.quote(f'{release_dir}/scripts/run_poseidon.sh')}\n"
    )
    run_remote_shell(server, upload_runner, dry_run)
    run_remote_shell(server, bootstrap_script(service_name, release_dir), dry_run)
    run_remote_shell(server, promotion_script(service_name, release_dir), dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Safely upgrade existing Poseidon FreeBSD services with automatic rollback."
    )
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument(
        "--server",
        action="append",
        help="Upgrade one target; repeat to specify multiple targets",
    )
    targets.add_argument("--servers-file", default="scripts/servers.conf")
    parser.add_argument("--service-name", default="poseidon")
    parser.add_argument("--release-root", default="/usr/local/poseidon-releases")
    parser.add_argument(
        "--release-id",
        default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    parser.add_argument(
        "--allow-nonlocal-bind",
        action="store_true",
        help="Allow upgrade when the service is not bound to localhost",
    )
    parser.add_argument(
        "--all-servers",
        action="store_true",
        help="Allow --apply to upgrade more than one target in a single run",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--dry-run", action="store_true", help="Print actions (default)")
    execution.add_argument("--apply", action="store_true", help="Perform the upgrade")
    execution.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run read-only prerequisite checks over SSH without uploading or changing files",
    )
    args = parser.parse_args()
    dry_run = not (args.apply or args.preflight_only)
    validate_inputs(args.service_name, args.release_root, args.release_id)

    repo_root = Path(__file__).resolve().parent.parent
    if args.server:
        servers = args.server
    else:
        servers_path = (repo_root / args.servers_file).resolve()
        if not servers_path.exists():
            parser.error(f"Servers file not found: {servers_path}")
        servers = parse_servers_from_file(servers_path)
        if not servers:
            parser.error(f"No servers found in {servers_path}")
    for server in servers:
        validate_server(server)
    if args.apply and len(servers) > 1 and not args.all_servers:
        parser.error(
            "Refusing to upgrade multiple servers without --all-servers; use --server for a canary first."
        )

    for server in servers:
        print(f"\n=== Upgrading {server} to {args.release_id} ===")
        upgrade_server(
            server,
            repo_root,
            args.service_name,
            args.release_root,
            args.release_id,
            args.allow_nonlocal_bind,
            dry_run,
            args.preflight_only,
        )


if __name__ == "__main__":
    main()