# Poseidon

Poseidon is a Python 3 API backend for managing bhyve virtual machines on FreeBSD.

## Scope

Poseidon provides a stable Python interface over the `vm` command line toolchain instead of exposing raw shell commands directly to higher layers.

Primary goals:

- manage VM lifecycle
- support FreeBSD, Linux, and UEFI-based boot flows
- model VM definitions and runtime state in Python
- isolate command building, execution, and error mapping behind typed services
- keep FreeBSD and bhyve specifics in dedicated infrastructure components
- API endpoints only, no UI
- FreeBSD service included to run Poseidon from /etc/rc.conf
- support direct deployment to listed servers in scripts/servers.conf

## FreeBSD Host Requirements (bhyve + Poseidon)

For a base host that will run bhyve VMs and the Poseidon API, install and enable the following.

1. Install required packages.

```bash
pkg update
pkg install -y vm-bhyve bhyve-firmware grub2-bhyve rsync python311 py311-pip git ca_root_nss
rm -f /usr/local/bin/python3
ln -s /usr/local/bin/python3.11 /usr/local/bin/python3
```

2. Enable required services and kernel modules.

```bash
sysrc vm_enable="YES"
sysrc vm_dir="/vm"
sysrc vm_list=""
sysrc vm_delay="5"
```

3. Initialize vm-bhyve and default switch.

```bash
vm init
vm switch create public
vm switch add public em0
```

4. Verify required binaries are available.

```bash
command -v vm bhyve grub-bhyve rsync python3
```

Notes:

- `bhyve` is part of FreeBSD base, while `vm-bhyve`, `grub2-bhyve`, and `bhyve-firmware` come from packages.
- If your external NIC is not `em0`, replace it in the `vm switch add` command.
- Poseidon deployment targets are maintained in `scripts/servers.conf` (one host per line).

## Operational Docs

- [docs/VM_ADD_DELETE_RUNBOOK.md](docs/VM_ADD_DELETE_RUNBOOK.md) - step-by-step operational checklist for VM add/delete.
- [docs/API_CONTRACT_ADD_DELETE_DRAFT.md](docs/API_CONTRACT_ADD_DELETE_DRAFT.md) - implemented API contract and future considerations.

## Current API

Implemented endpoints:

- `GET /health`
- `GET /v1/vms` -> wraps `vm list`
- `GET /v1/boot-media` -> list uploaded `.iso` and `.img` files under the boot media directory
- `POST /v1/boot-media/upload` -> upload `.iso` or `.img` into the boot media directory for VM create workflows
- `DELETE /v1/boot-media/{file_name}` -> delete uploaded `.iso` or `.img` from the boot media directory
- `POST /v1/vms/create` -> create VM with checks-only mode when `validate_only=true` and full create/configure/start when `validate_only=false`
- `POST /v1/vms/{vm_name}/start` -> wraps `vm start <vm_name>`
- `POST /v1/vms/{vm_name}/restart` -> wraps `vm restart <vm_name>`
- `POST /v1/vms/{vm_name}/stop?force=false` -> wraps `vm stop <vm_name>` (or `vm stop -f <vm_name>`)
- `DELETE /v1/vms/{vm_name}?force=false&destroy_disks=false` -> strict `404` if VM is absent, then stop and destroy VM (use `force=true` when needed)
- `POST /v1/vms/{vm_name}/detach-media` -> removes file-backed installer disk entries from VM config and cleans up any symlinks in the VM directory
- `POST /v1/vms/{vm_name}/configure` -> wraps non-interactive `sysrc -f /vm/<vm_name>/<vm_name>.conf <key=value>...`
- `GET /v1/vms/{vm_name}/config` -> reads and parses `/vm/<vm_name>/<vm_name>.conf`
- `GET /v1/vms/{vm_name}/ip` -> resolves configured VM MAC addresses through the host IPv4 ARP table
- `POST /v1/vms/{vm_name}/console` -> wraps `vm console <vm_name>`
- `POST /v1/vms/{vm_name}/console/token` -> issues short-lived token for WebSocket console
- `WS /v1/vms/{vm_name}/console/ws` -> interactive console bridge to `vm console <vm_name>`
- `GET /v1/vms/{vm_name}` -> wraps `vm info <vm_name>`
- `GET /v1/host/summary` -> host-only summary for CPU/OS, memory, disk, and networking
- `GET /ready` -> dependency readiness for vm-bhyve, VM/switch listing, and the VM root
- `GET /v1/vms/metrics` -> host-visible CPU, resident memory, uptime, and tap counters for all VMs
- `GET /v1/vms/{vm_name}/metrics` -> host-visible metrics for one VM
- `GET /v1/networks` -> vm-bhyve switch details and host interface inventory
- `GET|POST /v1/vms/{vm_name}/snapshots` -> list or create ZFS snapshots
- `POST /v1/vms/{vm_name}/snapshots/rollback` -> roll back a stopped VM to a ZFS snapshot
- `DELETE /v1/vms/{vm_name}/snapshots/{snapshot_name}` -> delete a ZFS snapshot
- `POST /v1/vms/{vm_name}/clone` -> clone a stopped ZFS-backed VM
- `GET|POST /v1/vms/{vm_name}/disks` -> list disks or create and attach a disk to a stopped VM
- `DELETE /v1/vms/{vm_name}/disks/{disk_index}` -> detach a non-root disk while preserving its data
- `GET /v1/operations` -> bounded HTTP operation history

`GET /v1/host/summary` includes both:

- parsed compact details for disk and networking (filesystems, zpool list, interfaces, routes, default routes)
- raw command output under each section's `command_results` for full fidelity

Authentication and logging:

- `/v1/*` endpoints require `X-API-Key` header
- The API key is a root-equivalent administrative credential because VM configuration supports vm-bhyve hooks.
- API key source is `POSEIDON_API_KEY` environment variable on the server
- WebSocket console accepts either `X-API-Key` or short-lived `X-Console-Token`/`console_token`; API keys are not accepted in query strings.
- short-lived console token signing secret source is `POSEIDON_CONSOLE_TOKEN_SECRET`
- optional token TTL source is `POSEIDON_CONSOLE_TOKEN_TTL_SECONDS` (default `60`, allowed range `5..3600`)
- boot media upload limit source is `POSEIDON_MAX_UPLOAD_BYTES` (default `34359738368`, or 32 GiB)
- optional persistent audit JSONL path is `POSEIDON_AUDIT_LOG`; in-memory history defaults to 1000 records and is controlled by `POSEIDON_AUDIT_MAX_RECORDS`
- `/docs`, `/redocs`, and `/openapi.json` require an admin token via `X-Admin-Token` header or `admin_token` query parameter
- admin token source is `POSEIDON_ADMIN_TOKEN` environment variable on the server
- request logging is enabled with method, path, status, duration, and client address

Create and media behavior:

- VM names are 1-64 characters, start with a letter or digit, and may contain letters, digits, `.`, `_`, and `-`.
- `disk.storage_target` is an absolute filesystem path used for the create capacity check and must be on the same filesystem as the configured VM root.
- Uploaded non-memstick IMG files are intentionally shared writable disks. Poseidon blocks deletion and overwrite while a VM references the image.
- Production service installs bind to `127.0.0.1` and require a TLS reverse proxy for remote HTTP and WebSocket access.
- VM metrics are host-visible values. Guest-internal CPU and memory utilization require an agent inside each guest.
- Guest IP lookup returns `ip_address: null` until the host has learned the VM's address through ARP; each configured interface and any matching addresses are included in `interfaces`.
- Snapshot, rollback, and clone operations require a ZFS-backed vm-bhyve datastore. Rollback, clone, and disk changes require a stopped VM.

## Local Development

1. Create and activate a virtual environment.
2. Install dependencies.
3. Start the API.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open API docs at `http://127.0.0.1:8000/docs`.

Set an API key before calling `/v1/*` endpoints:

```bash
export POSEIDON_API_KEY="replace-with-a-strong-key"
export POSEIDON_ADMIN_TOKEN="replace-with-a-different-strong-admin-token"
export POSEIDON_CONSOLE_TOKEN_SECRET="replace-with-a-different-strong-secret"
```

Generate valid tokens:

```bash
python scripts/generate_token.py --env POSEIDON_API_KEY
python scripts/generate_token.py --env POSEIDON_ADMIN_TOKEN
python scripts/generate_token.py --env POSEIDON_CONSOLE_TOKEN_SECRET
```

Generate a short-lived console token for a specific VM:

```bash
python scripts/generate_console_token.py --vm-name <vm_name>
```

Dedicated API key generator:

```bash
python scripts/generate_api_key.py
```

Or print token values only:

```bash
python scripts/generate_token.py --raw
```

Curl examples are centralized in `EXAMPLE.md`, including VM lifecycle, boot media, host summary, docs endpoints, create/delete contract calls, and auth error checks.

Use that file as the canonical command reference.

`ram=` and `cpus=` are accepted as compatibility aliases and are translated to `memory=` and `cpu=` before calling `sysrc`.

Interactive VM console stream over WebSocket (optional local test/debug client):

```bash
python scripts/console_ws_client.py \
	--host 127.0.0.1 \
	--port 8000 \
	--vm <vm_name> \
	--api-key "$POSEIDON_API_KEY"
```

This client uses raw TTY mode and behaves more like a real terminal than generic line-oriented WebSocket tools.

This script is optional and is not required by the Poseidon API service at runtime.

`Ctrl-]` exits the local client session.

Recommended browser-safe flow:

1. Mint a short-lived console token on your backend (or trusted BFF) using the curl example in `EXAMPLE.md`.

2. Use returned `token` as `console_token` in the WebSocket URL:

```bash
python scripts/console_ws_client.py \
	--host 127.0.0.1 \
	--port 8000 \
	--vm <vm_name> \
	--console-token "<token-from-endpoint>"
```

Alternative line-oriented client (wscat):

```bash
wscat -H "X-API-Key: $POSEIDON_API_KEY" -c "ws://127.0.0.1:8000/v1/vms/<vm_name>/console/ws"
```

Short-lived token variant for browser-safe flows:

```bash
wscat -c "ws://127.0.0.1:8000/v1/vms/<vm_name>/console/ws?console_token=<token-from-console-token-endpoint>"
```

Remote machine requirement for console access:

- Remote Linux machines that need interactive VM console access must have a WebSocket client installed.
- Recommended: install `wscat` (Node.js client), which avoids Rust/cargo toolchain issues on older distros.

```bash
sudo apt update
sudo apt install -y nodejs npm
sudo npm install -g wscat
```

Verify installation:

```bash
wscat --version
```

Connect to the Poseidon VM console over WebSocket:

```bash
wscat -H "X-API-Key: $POSEIDON_API_KEY" -c "ws://127.0.0.1:8000/v1/vms/<vm_name>/console/ws"
```

Browser-friendly docs access:

```text
http://127.0.0.1:8000/docs?admin_token=<admin-token>
http://127.0.0.1:8000/redocs?admin_token=<admin-token>
```

When using browser access, keep the `admin_token` in the docs URL so Swagger UI/ReDoc can also fetch `/openapi.json` with the same token.
Use query-token docs URLs only on localhost or behind the required TLS proxy because URLs may be retained in browser history and proxy logs.

## Deployment Targets

Deploy host list is stored in `scripts/servers.conf` with one host per line.

Example:

```text
root@vmhost1.home.lan
root@vmhost2.home.lan
# root@vmhost3.home.lan
```

## Deploy To Servers

Deployment uses `scripts/servers.conf` (one target per line, `#` for comments) and deploys Poseidon over SSH.

Dry run:

```bash
python scripts/deploy.py --dry-run
```

Dry run with an alternate server list file:

```bash
python scripts/deploy.py --servers-file scripts/servers.staging.conf --dry-run
```

Deploy (includes FreeBSD rc.d service install and `/etc/rc.conf` updates):

```bash
python scripts/deploy.py --apply
```

In an interactive terminal, the deployer asks for the SSH login user. OpenSSH handles any SSH password prompt directly; Poseidon never reads or stores the password. Full service installation requires a root SSH login because Poseidon manages root-level vm-bhyve and rc.d state.

If credentials are not supplied through arguments, environment variables, or an existing credentials file, the deployer generates the API key, admin token, and console-token signing secret. It saves them to `~/.config/poseidon/deploy.env` with mode `0600` and reuses them on later deployments.

Deploy using generated environment variables and `/usr/local/poseidon`:

```bash
eval "$(python scripts/generate_api_key.py)"
eval "$(python scripts/generate_token.py --env POSEIDON_ADMIN_TOKEN)"
eval "$(python scripts/generate_token.py --env POSEIDON_CONSOLE_TOKEN_SECRET)"
python scripts/deploy.py --apply --remote-dir /usr/local/poseidon
```

What deployment does on each server:

1. Creates the remote directory.
2. Syncs project files with `rsync`.
3. Creates `.venv` and installs dependencies from `requirements.txt` using `.venv/bin/python -m pip` (shell-agnostic; no `activate` required).
4. Installs `/usr/local/etc/rc.d/poseidon`.
5. Adds required `poseidon_*` values to `/etc/rc.conf`.
6. Restarts or starts the `poseidon` service.

Useful options:

- `--apply` to perform changes; without it, deployment only prints a preview
- `--ssh-user root` to bypass the interactive SSH-user prompt
- `--non-interactive` for CI or scripted deployment using users from `servers.conf` or `--ssh-user`
- `--credentials-file /secure/path/poseidon.env` to change where generated credentials are stored
- `--service-name myposeidon` to change rc.d service name
- `--skip-freebsd-service` to deploy code only without rc.d/rc.conf changes

## Upgrade Existing Production Servers

Use `scripts/upgrade.py` for an existing rc.d installation. It preserves credentials and the current release, stages dependencies in an isolated directory, validates the staged application, switches `poseidon_dir`, waits up to 30 seconds for `/health`, and restores the previous directory automatically if restart or health validation fails.

Preview a single-server canary without contacting it:

```bash
python scripts/upgrade.py --server root@vmhost1.home.lan
```

Run read-only prerequisite checks on the canary over SSH:

```bash
python scripts/upgrade.py --server root@vmhost1.home.lan --preflight-only
```

This checks root access, required commands, Python `venv`/`ensurepip`, the installed rc.d service, required credentials, local-only binding, the current service directory, and release-path availability. It does not create directories, upload files, install packages, restart services, or change rc.conf.

Apply the canary after reviewing the preview:

```bash
python scripts/upgrade.py --server root@vmhost1.home.lan --apply
```

After validating the canary, upgrade all targets from `scripts/servers.conf`:

```bash
python scripts/upgrade.py --apply --all-servers
```

The default release root is `/usr/local/poseidon-releases`; use `--release-root /opt/poseidon-releases` when appropriate. The upgrader refuses legacy rc.d scripts that cannot switch release directories and refuses non-local bind addresses unless `--allow-nonlocal-bind` is explicitly supplied. Old release directories are retained for rollback and must be cleaned up manually after the new release is proven stable.

## Run Directly On VM Hosts (Step By Step)

Use this when you want to run Poseidon directly on each FreeBSD VM host without using the deployment script.

1. Log in to a VM host.

```bash
ssh root@vmhost1.home.lan
```

2. Install required packages if missing.

```bash
pkg update
pkg install -y python3 rsync
```

3. Copy or check out Poseidon code on the host.

```bash
mkdir -p /opt/poseidon
cd /opt/poseidon
# Use your source-control checkout or file-transfer workflow here.
```

4. Create Python environment and install dependencies.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

5. Generate and set runtime tokens.

```bash
python scripts/generate_token.py --env POSEIDON_API_KEY
python scripts/generate_token.py --env POSEIDON_ADMIN_TOKEN
python scripts/generate_token.py --env POSEIDON_CONSOLE_TOKEN_SECRET
```

Run the printed `export ...` lines, then persist values in `/etc/rc.conf` as:

```bash
echo 'poseidon_enable="YES"' >> /etc/rc.conf
echo 'poseidon_dir="/opt/poseidon"' >> /etc/rc.conf
echo 'poseidon_host="127.0.0.1"' >> /etc/rc.conf
echo 'poseidon_port="8000"' >> /etc/rc.conf
echo 'poseidon_api_key="<paste-api-key>"' >> /etc/rc.conf
echo 'poseidon_admin_token="<paste-admin-token>"' >> /etc/rc.conf
echo 'poseidon_console_token_secret="<paste-console-token-secret>"' >> /etc/rc.conf
```

6. Install rc.d service and launcher from this repo.

```bash
python scripts/install_freebsd_service.py --dry-run
python scripts/install_freebsd_service.py
```

The installer writes these files and updates `/etc/rc.conf` automatically:

- `/usr/local/etc/rc.d/poseidon`
- `/opt/poseidon/scripts/run_poseidon.sh`

7. Validate API and docs access.

Use the validation curl commands in `EXAMPLE.md`.

## Uninstall FreeBSD Service

To remove the local FreeBSD service, rc.conf entries, and launcher script:

```bash
python scripts/uninstall_freebsd_service.py --dry-run
python scripts/uninstall_freebsd_service.py
```

Useful options:

- `--service-name myposeidon` for non-default service names
- `--keep-launcher` to keep `/opt/poseidon/scripts/run_poseidon.sh`
- `--rc-conf /path/to/rc.conf` for custom rc.conf locations

Notes:

- Requires SSH access from your workstation to each server.
- Requires `python3` and `rsync` on remote hosts.
- rc.d install requires root privileges on target hosts.
- Place a TLS reverse proxy in front of `127.0.0.1:8000` before exposing Poseidon remotely.
- On FreeBSD, dependencies intentionally use plain `uvicorn` (not `uvicorn[standard]`) to avoid Rust build failures from optional watcher packages.
- Python versions below 3.14 use `pydantic==1.10.15` to avoid Rust-based `pydantic-core` builds on FreeBSD. Python 3.14 and newer use Pydantic 2 because Pydantic 1 does not support those interpreters.
- If a host uses `csh`/`tcsh` as default shell, force POSIX shell for manual remote commands, for example: `ssh root@vmhost1.home.lan "sh -lc 'cd /usr/local/poseidon && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'"`.
