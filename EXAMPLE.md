# Poseidon API Curl Examples

These examples assume Poseidon is running locally at `http://127.0.0.1:8000`. Use `https://` and `wss://` through the required TLS reverse proxy in production.

## 1) Set Variables

```bash
export POSEIDON_BASE_URL="http://127.0.0.1:8000"
export POSEIDON_API_KEY="replace-with-api-key"
export POSEIDON_ADMIN_TOKEN="replace-with-admin-token"
export VM_NAME="example-vm"
```

## 2) Health Check (No Auth)

```bash
curl -sS "$POSEIDON_BASE_URL/health"
```

## 3) List VMs (`GET /v1/vms`)

```bash
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms"
```

## 3b) List Boot Media (`GET /v1/boot-media`)

```bash
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/boot-media"
```

Include SHA-256 checksums:

```bash
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/boot-media?include_checksum=true"
```

## 3c) Upload Boot Media (`POST /v1/boot-media/upload`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  -F "file=@/path/to/freebsd-installer.iso" \
  "$POSEIDON_BASE_URL/v1/boot-media/upload"
```

Uploads default to a 32 GiB limit controlled by `POSEIDON_MAX_UPLOAD_BYTES`. Use `?overwrite=true` only for media that is not referenced by a VM.

## 3d) Delete Boot Media (`DELETE /v1/boot-media/{file_name}`)

```bash
curl -sS -X DELETE \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/boot-media/freebsd-installer.iso"
```

## 4) VM Info (`GET /v1/vms/{vm_name}`)

```bash
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME"
```

## 5) VM Config (`GET /v1/vms/{vm_name}/config`)

```bash
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/config"
```

## 6) VM Start (`POST /v1/vms/{vm_name}/start`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/start"
```

## 7) VM Restart (`POST /v1/vms/{vm_name}/restart`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/restart"
```

## 8) VM Stop Graceful (`POST /v1/vms/{vm_name}/stop`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/stop"
```

## 9) VM Stop Forced (`POST /v1/vms/{vm_name}/stop?force=true`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/stop?force=true"
```

## 10) VM Configure (`POST /v1/vms/{vm_name}/configure`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"settings": ["memory=4G", "cpu=2"]}' \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/configure"
```

This endpoint writes guest configuration via:

```bash
sysrc -f "/vm/$VM_NAME/$VM_NAME.conf" <key=value>...
```

## 11) VM Console (`POST /v1/vms/{vm_name}/console`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/console"
```

## 12) Interactive VM Console (`WS /v1/vms/{vm_name}/console/ws`)

```bash
websocat -H="X-API-Key: $POSEIDON_API_KEY" \
  "ws://127.0.0.1:8000/v1/vms/$VM_NAME/console/ws"
```

Short-lived headerless alternative:

```bash
websocat \
  "ws://127.0.0.1:8000/v1/vms/$VM_NAME/console/ws?console_token=<token-from-12b>"
```

## 12b) Issue Short-Lived Console Token (`POST /v1/vms/{vm_name}/console/token`)

```bash
curl -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/console/token"
```

## 12c) Interactive VM Console with Console Token

```bash
websocat \
  "ws://127.0.0.1:8000/v1/vms/$VM_NAME/console/ws?console_token=<token-from-12b>"
```

## 13) OpenAPI JSON (`GET /openapi.json`, Admin Token Required)

```bash
curl -sS \
  -H "X-Admin-Token: $POSEIDON_ADMIN_TOKEN" \
  "$POSEIDON_BASE_URL/openapi.json"
```

## 14) Swagger UI HTML (`GET /docs`, Admin Token Required)

```bash
curl -sS \
  -H "X-Admin-Token: $POSEIDON_ADMIN_TOKEN" \
  "$POSEIDON_BASE_URL/docs"
```

## 15) ReDoc HTML (`GET /redocs`, Admin Token Required)

```bash
curl -sS \
  -H "X-Admin-Token: $POSEIDON_ADMIN_TOKEN" \
  "$POSEIDON_BASE_URL/redocs"
```

## 16) Quick Auth Checks

Missing API key (expected 401):

```bash
curl -i "$POSEIDON_BASE_URL/v1/vms"
```

Missing admin token (expected 401):

```bash
curl -i "$POSEIDON_BASE_URL/openapi.json"
```

## 17) Host Summary (`GET /v1/host/summary`)

```bash
curl -sS \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/host/summary"
```

Example response shape (trimmed):

```json
{
  "host": {
    "details": {
      "kern.hostname": "vmhost1.home.lan",
      "kern.osrelease": "14.3-RELEASE",
      "hw.model": "AMD EPYC",
      "hw.machine": "amd64",
      "hw.ncpu": "16"
    },
    "command_results": {
      "uname": {"command": ["uname", "-a"], "return_code": 0, "stdout": "...", "stderr": ""},
      "sysctl": {"command": ["sysctl", "kern.hostname", "kern.osrelease", "hw.model", "hw.machine", "hw.ncpu"], "return_code": 0, "stdout": "...", "stderr": ""}
    }
  },
  "memory": {
    "details": {
      "hw.physmem": "34359738368",
      "vm.stats.vm.v_free_count": "2016572",
      "hw.pagesize": "4096",
      "computed.free_bytes": "8259889152"
    },
    "command_results": {
      "sysctl": {"command": ["sysctl", "hw.physmem", "hw.realmem", "vm.stats.vm.v_free_count", "hw.pagesize"], "return_code": 0, "stdout": "...", "stderr": ""}
    }
  },
  "disk": {
    "details": {
      "filesystems": [
        {
          "filesystem": "zroot/ROOT/default",
          "size": "1.8T",
          "used": "215G",
          "avail": "1.6T",
          "capacity": "12%",
          "mounted_on": "/",
          "size_bytes": 1979120929996,
          "used_bytes": 230854492160,
          "avail_bytes": 1758266431488
        }
      ],
      "totals": {
        "filesystem_count": 7,
        "size_bytes": 3842278901112,
        "used_bytes": 419235725312,
        "avail_bytes": 3290033172480
      },
      "zpools": [
        {
          "NAME": "zroot",
          "SIZE": "1.81T",
          "ALLOC": "215G",
          "FREE": "1.60T",
          "CKPOINT": "-",
          "EXPANDSZ": "-",
          "FRAG": "8%",
          "CAP": "11%",
          "DEDUP": "1.00x",
          "HEALTH": "ONLINE",
          "ALTROOT": "-"
        }
      ],
      "zpool_summary": {
        "pool_count": 1,
        "online_pool_count": 1
      }
    },
    "command_results": {
      "df": {"command": ["df", "-h"], "return_code": 0, "stdout": "...", "stderr": ""},
      "zpool": {"command": ["zpool", "list"], "return_code": 0, "stdout": "...", "stderr": ""}
    }
  },
  "network": {
    "details": {
      "interfaces": [
        {
          "name": "em0",
          "flags": "8863<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST>",
          "mtu": 1500,
          "mac": "00:11:22:33:44:55",
          "status": "active",
          "media": "Ethernet autoselect (1000baseT <full-duplex>)",
          "description": "uplink",
          "addresses": [
            {"family": "inet", "address": "192.168.1.20"},
            {"family": "inet6", "address": "fe80::211:22ff:fe33:4455%em0"}
          ]
        }
      ],
      "interface_summary": {
        "interface_count": 4,
        "up_interface_count": 3
      },
      "routes": {
        "internet": [
          {
            "Destination": "default",
            "Gateway": "192.168.1.1",
            "Flags": "UGS",
            "Netif": "em0",
            "Expire": ""
          }
        ]
      },
      "default_routes": [
        {"family": "internet", "gateway": "192.168.1.1", "netif": "em0"}
      ],
      "route_summary": {"route_count": 21}
    },
    "command_results": {
      "ifconfig": {"command": ["ifconfig", "-a"], "return_code": 0, "stdout": "...", "stderr": ""},
      "netstat": {"command": ["netstat", "-rn"], "return_code": 0, "stdout": "...", "stderr": ""}
    }
  }
}
```

## 17b) Create VM Contract (`POST /v1/vms/create`)

Current behavior:

- `validate_only=true` runs checks-only mode and returns HTTP 200
- validation failures return `status=validation_failed` without creating a VM
- non-validate create returns HTTP 409 on disk hard-gate failure and HTTP 404 for a missing switch
- non-validate create returns HTTP 201 when create/configure/start succeeds

```bash
curl -i -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "app-01",
    "resources": {"cpu": 2, "memory": "4G"},
    "disk": {"size": "50G", "storage_target": "/vm"},
    "network": {"switch": "public", "interface_type": "virtio-net"},
    "boot": {"source_type": "template", "source": "freebsd-14-template"},
    "options": {"start_after_create": true, "validate_only": true}
  }' \
  "$POSEIDON_BASE_URL/v1/vms/create"
```

Create VM from uploaded ISO (attach installer media):

```bash
curl -i -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "freebsd-inst-01",
    "resources": {"cpu": 2, "memory": "4G"},
    "disk": {"size": "40G", "storage_target": "/vm"},
    "network": {"switch": "public", "interface_type": "virtio-net"},
    "boot": {"source_type": "iso", "source": "freebsd-installer.iso"},
    "options": {"start_after_create": true, "validate_only": false}
  }' \
  "$POSEIDON_BASE_URL/v1/vms/create"
```

Create VM from uploaded IMG (boot from disk image):

```bash
curl -i -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "debian-img-01",
    "resources": {"cpu": 2, "memory": "4G"},
    "disk": {"size": "40G", "storage_target": "/vm"},
    "network": {"switch": "public", "interface_type": "virtio-net"},
    "boot": {"source_type": "img", "source": "debian-cloud.img"},
    "options": {"start_after_create": true, "validate_only": false}
  }' \
  "$POSEIDON_BASE_URL/v1/vms/create"
```

Create VM and persist boot autostart in /etc/rc.conf:

```bash
curl -i -sS -X POST \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "vm_name": "freebsd-inst-auto-01",
    "resources": {"cpu": 2, "memory": "4G"},
    "disk": {"size": "40G", "storage_target": "/vm"},
    "network": {"switch": "public", "interface_type": "virtio-net"},
    "boot": {"source_type": "img", "source": "FreeBSD-15.0-RELEASE-amd64-memstick.img"},
    "options": {"start_after_create": true, "validate_only": false, "auto_start_on_boot": true}
  }' \
  "$POSEIDON_BASE_URL/v1/vms/create"
```

## 17c) Delete VM Contract (`DELETE /v1/vms/{vm_name}`)

Current behavior:

- returns HTTP 404 if VM is absent (strict behavior)
- attempts stop then destroy
- returns HTTP 409 if stop fails and `force=false`
- returns HTTP 200 on successful delete

```bash
curl -i -sS -X DELETE \
  -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME?force=false&destroy_disks=true"
```

## 18) VM Configure Key Reference (sysrc -f)

Set target config file:

```bash
CONF="/vm/$VM_NAME/$VM_NAME.conf"
```

Core boot and CPU/memory:

```bash
sysrc -f "$CONF" loader="<value>"
sysrc -f "$CONF" bhyveload_loader="<value>"
sysrc -f "$CONF" bhyveload_args="<value>"
sysrc -f "$CONF" loader_timeout="<value>"
sysrc -f "$CONF" uefi_vars="<value>"
sysrc -f "$CONF" cpu="<value>"
sysrc -f "$CONF" cpu_sockets="<value>"
sysrc -f "$CONF" cpu_cores="<value>"
sysrc -f "$CONF" cpu_threads="<value>"
sysrc -f "$CONF" memory="<value>"
sysrc -f "$CONF" wired_memory="<value>"
sysrc -f "$CONF" hostbridge="<value>"
sysrc -f "$CONF" ignore_bad_msr="<value>"
sysrc -f "$CONF" bhyve_options="<value>"
sysrc -f "$CONF" comports="<value>"
sysrc -f "$CONF" utctime="<value>"
sysrc -f "$CONF" debug="<value>"
sysrc -f "$CONF" uuid="<value>"
sysrc -f "$CONF" ahci_device_limit="<value>"
```

Disk settings:

```bash
sysrc -f "$CONF" disk0_type="<value>"
sysrc -f "$CONF" disk0_dev="<value>"
sysrc -f "$CONF" disk0_name="<value>"
sysrc -f "$CONF" disk0_opts="<value>"
sysrc -f "$CONF" disk0_size="<value>"
```

Network settings:

```bash
sysrc -f "$CONF" network0_type="<value>"
sysrc -f "$CONF" network0_switch="<value>"
sysrc -f "$CONF" network0_device="<value>"
sysrc -f "$CONF" network0_name="<value>"
sysrc -f "$CONF" network0_mac="<value>"
sysrc -f "$CONF" network0_span="<value>"
```

Device passthrough and slots:

```bash
sysrc -f "$CONF" passthru0="<value>"
sysrc -f "$CONF" start_slot="<value>"
sysrc -f "$CONF" install_slot="<value>"
sysrc -f "$CONF" virt_random="<value>"
```

Graphics and console:

```bash
sysrc -f "$CONF" graphics="<value>"
sysrc -f "$CONF" graphics_port="<value>"
sysrc -f "$CONF" graphics_listen="<value>"
sysrc -f "$CONF" graphics_res="<value>"
sysrc -f "$CONF" graphics_wait="<value>"
sysrc -f "$CONF" graphics_vga="<value>"
sysrc -f "$CONF" vnc_password="<value>"
sysrc -f "$CONF" xhci_mouse="<value>"
sysrc -f "$CONF" virt_console0="<value>"
```

Grub options:

```bash
sysrc -f "$CONF" grub_install0="<value>"
sysrc -f "$CONF" grub_install1="<value>"
sysrc -f "$CONF" grub_run0="<value>"
sysrc -f "$CONF" grub_run1="<value>"
sysrc -f "$CONF" grub_run_partition="<value>"
sysrc -f "$CONF" grub_run_dir="<value>"
sysrc -f "$CONF" grub_run_file="<value>"
```

ZFS/hooks/resource controls:

```bash
sysrc -f "$CONF" zfs_dataset_opts="<value>"
sysrc -f "$CONF" zfs_zvol_opts="<value>"
sysrc -f "$CONF" prestart="<value>"
sysrc -f "$CONF" priority="<value>"
sysrc -f "$CONF" limit_pcpu="<value>"
sysrc -f "$CONF" limit_rbps="<value>"
sysrc -f "$CONF" limit_wbps="<value>"
sysrc -f "$CONF" limit_riops="<value>"
sysrc -f "$CONF" limit_wiops="<value>"
```

Numbered keys can be extended for additional devices or entries (for example `disk1_*`, `network1_*`, `passthru1`, `virt_console1`, `grub_run2`).

## 19) Readiness (`GET /ready`)

```bash
curl -sS "$POSEIDON_BASE_URL/ready"
```

## 20) VM Metrics

```bash
curl -sS -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/metrics"

curl -sS -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/metrics"
```

Metrics are host-visible bhyve process and tap-interface values, not guest-internal utilization.

## 21) Networks (`GET /v1/networks`)

```bash
curl -sS -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/networks"
```

## 22) Snapshots and Clone

These operations require a ZFS-backed vm-bhyve datastore. The VM must be stopped unless snapshot creation explicitly uses `force=true`.

```bash
curl -sS -X POST -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"snapshot_name":"clean-install","force":false}' \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/snapshots"

curl -sS -X POST -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"snapshot_name":"clean-install","destroy_newer":false}' \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/snapshots/rollback"

curl -sS -X POST -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"new_vm_name":"example-vm-clone","snapshot_name":"clean-install"}' \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/clone"
```

## 23) VM Disks

```bash
curl -sS -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/disks"

curl -sS -X POST -H "X-API-Key: $POSEIDON_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"device_type":"file","size":"20G"}' \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/disks"

curl -sS -X DELETE -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/vms/$VM_NAME/disks/1"
```

Detach removes configuration keys but intentionally preserves the disk data. `disk0` cannot be detached.

## 24) Operation History

```bash
curl -sS -H "X-API-Key: $POSEIDON_API_KEY" \
  "$POSEIDON_BASE_URL/v1/operations?limit=100"
```
