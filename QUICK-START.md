# Poseidon Quick Start

This installs Poseidon as a FreeBSD `rc.d` service on one bhyve host. Replace `vmhost1.home.lan`, `em0`, and `/path/to/poseidon` with values for your environment.

Poseidon binds to `127.0.0.1:8000` by default. Do not expose it remotely without a TLS reverse proxy that supports HTTP and WebSocket traffic.

## 1. Prepare the FreeBSD host

Host preparation is required for both automatic and manual installation. Log in as `root` and install the required packages:

```sh
ssh root@vmhost1.home.lan
pkg update
pkg install -y vm-bhyve bhyve-firmware grub2-bhyve rsync python311 py311-pip git ca_root_nss
rm -f /usr/local/bin/python3
ln -s /usr/local/bin/python3.11 /usr/local/bin/python3
```

Enable and initialize vm-bhyve:

```sh
sysrc vm_enable="YES"
sysrc vm_dir="/vm"
sysrc vm_list=""
sysrc vm_delay="5"
vm init
vm switch create public
vm switch add public em0
```

If the external network interface is not `em0`, substitute the correct interface. Verify the prerequisites:

```sh
command -v vm bhyve grub-bhyve rsync python3
```

## 2. Choose an installation method

Use the automatic method for normal installations. It copies Poseidon, creates the virtual environment, installs dependencies, generates and stores credentials, installs the `rc.d` service, updates `/etc/rc.conf`, and starts Poseidon.

Use the manual method when the deployment script cannot be run from a workstation or when each installation step must be performed directly on the host.

### Automatic installation (recommended)

Run these commands from the workstation containing the Poseidon checkout. The workstation must have Python 3, SSH, and rsync installed and must be able to log in to the host as `root`.

Create a one-host deployment file:

```sh
cd /path/to/poseidon
printf '%s\n' 'root@vmhost1.home.lan' > scripts/servers.local.conf
```

Preview the complete deployment and review every target and command:

```sh
python3 scripts/deploy.py \
  --servers-file scripts/servers.local.conf \
  --non-interactive \
  --dry-run
```

Apply the deployment only after the preview is correct:

```sh
python3 scripts/deploy.py \
  --servers-file scripts/servers.local.conf \
  --non-interactive \
  --apply
```

The default remote directory is `/usr/local/poseidon`. If credentials were not supplied, the deployer generates them and saves them locally in `~/.config/poseidon/deploy.env` with mode `0600`. Keep this file secure; the API key grants root-equivalent VM administration access. The same credentials are reused on later deployments.

Continue to [Verify the installation](#3-verify-the-installation).

### Manual installation

Use the remaining steps instead of the automatic installation above.

#### Copy Poseidon to the host

Exit the host, then run this from the workstation containing the Poseidon checkout:

```sh
rsync -az \
  --exclude .git \
  --exclude .svn \
  --exclude .venv \
  --exclude __pycache__ \
  /path/to/poseidon/ root@vmhost1.home.lan:/opt/poseidon/
```

#### Install Poseidon on the host

Log back in to the host, create the virtual environment, and install dependencies:

```sh
ssh root@vmhost1.home.lan
cd /opt/poseidon
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Generate three independent secrets in the current shell:

```sh
eval "$(python3 scripts/generate_token.py --env POSEIDON_API_KEY)"
eval "$(python3 scripts/generate_token.py --env POSEIDON_ADMIN_TOKEN)"
eval "$(python3 scripts/generate_token.py --env POSEIDON_CONSOLE_TOKEN_SECRET)"
```

Keep these values secure. The API key grants root-equivalent VM administration access.

Preview the service installation, then install it:

```sh
python3 scripts/install_freebsd_service.py --dry-run
python3 scripts/install_freebsd_service.py
```

The installer writes `/usr/local/etc/rc.d/poseidon` and `/opt/poseidon/scripts/run_poseidon.sh`, stores the service settings in `/etc/rc.conf`, and starts Poseidon.

## 3. Verify the installation

Log in to the host and check the service and unauthenticated health endpoints:

```sh
ssh root@vmhost1.home.lan
service poseidon status
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/ready
```

Load the API key persisted for the service and verify an authenticated request. This works for either installation method:

```sh
POSEIDON_API_KEY="$(sysrc -n poseidon_api_key)"
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  http://127.0.0.1:8000/v1/vms
```

Open the local API documentation with the admin token generated during installation:

```text
http://127.0.0.1:8000/docs?admin_token=<POSEIDON_ADMIN_TOKEN>
```

Because Poseidon listens only on localhost, use an SSH tunnel to open the documentation from the workstation before a TLS reverse proxy is configured:

```sh
ssh -L 8000:127.0.0.1:8000 root@vmhost1.home.lan
```

For remote access, configure the required TLS reverse proxy first and use `https://` for HTTP and `wss://` for VM console WebSockets. Additional API examples are in [EXAMPLE.md](EXAMPLE.md).

## Troubleshooting

Service logs are written to `/var/log/poseidon.log`:

```sh
tail -f /var/log/poseidon.log
```

Confirm the service starts after a reboot:

```sh
service poseidon restart
service poseidon status
```