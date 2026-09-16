# API Contract: Add and Delete VMs

Status: implemented behavior. The filename is retained for compatibility with existing links.

## Design Goals

- Provide explicit create and delete APIs.
- Keep behavior predictable for automation and operators.
- Encode policy defaults:
  - CPU/memory checks are warnings (soft gate).
  - Disk/network checks are blocking (hard gate).
  - Deleting an absent VM returns not-found failure.

## Authentication

- Requires X-API-Key on all /v1 endpoints.

## Boot Media Endpoints

- GET /v1/boot-media
  - Lists uploaded .iso and .img files from the host boot media directory.
- POST /v1/boot-media/upload
  - Uploads .iso or .img files for later VM create use.
  - Defaults to a 32 GiB limit configured by POSEIDON_MAX_UPLOAD_BYTES.
  - Supports overwrite=true only while the media is not referenced by a VM.
- DELETE /v1/boot-media/{file_name}
  - Deletes unreferenced boot media.

## Endpoint: Create VM

- Method: POST
- Path: /v1/vms/create

## Request Body

```json
{
  "vm_name": "app-01",
  "resources": {
    "cpu": 2,
    "memory": "4G"
  },
  "disk": {
    "size": "50G",
    "storage_target": "/vm"
  },
  "network": {
    "switch": "public",
    "interface_type": "virtio-net"
  },
  "boot": {
    "source_type": "iso",
    "source": "freebsd-installer.iso"
  },
  "options": {
    "start_after_create": true,
    "validate_only": false
  }
}
```

## Create Response (Success)

HTTP 201

```json
{
  "operation": "create_vm",
  "status": "succeeded",
  "vm_name": "app-01",
  "warnings": [
    {
      "code": "CAPACITY_CPU_SOFT",
      "message": "Requested CPU exceeds host CPU count; overcommit warning."
    }
  ],
  "checks": {
    "cpu": {"gate": "soft", "result": "warn"},
    "memory": {"gate": "soft", "result": "warn"},
    "disk": {"gate": "hard", "result": "pass"},
    "network": {"gate": "hard", "result": "pass"}
  },
  "result": {
    "created": true,
    "started": true
  }
}
```

## Create Response (Validation Only)

HTTP 200

```json
{
  "operation": "create_vm",
  "status": "validated",
  "vm_name": "app-01",
  "checks": {
    "cpu": {"gate": "soft", "result": "warn"},
    "memory": {"gate": "soft", "result": "pass"},
    "disk": {"gate": "hard", "result": "pass"},
    "network": {"gate": "hard", "result": "pass"}
  },
  "result": {
    "created": false,
    "started": false
  }
}
```

When a hard check fails, HTTP remains 200, `status` is `validation_failed`, and the failed check is returned without creating a VM.

## Create Response Errors

- HTTP 400: invalid payload or policy violation.
- HTTP 401: missing/invalid API key.
- HTTP 404: referenced network/template not found.
- HTTP 409: VM already exists.
- HTTP 413: boot-media upload exceeds the configured limit.
- HTTP 422: schema validation error.
- HTTP 500: host or command failure.

## Endpoint: Delete VM

- Method: DELETE
- Path: /v1/vms/{vm_name}

## Query Parameters

- force: bool (default false)
- destroy_disks: bool (default false)

Example:

```text
DELETE /v1/vms/app-01?force=false&destroy_disks=true
```

## Delete Response (Success)

HTTP 200

```json
{
  "operation": "delete_vm",
  "status": "succeeded",
  "vm_name": "app-01",
  "warnings": [],
  "result": {
    "stopped": true,
    "definition_deleted": true,
    "disks_destroyed": true
  }
}
```

## Delete Response Errors

- HTTP 401: missing/invalid API key.
- HTTP 404: VM not found (strict behavior, no success on absent VM).
- HTTP 409: VM cannot be stopped/deleted due to current state.
- HTTP 500: host or command failure.

## Idempotency and Semantics

- Create is non-idempotent for duplicate names; duplicates return 409.
- Delete is strict by default; missing VM returns 404.
- If `vm destroy -d` is unsupported and definition-only fallback succeeds, `disks_destroyed` is false.

## Future Considerations

- Should create support asynchronous execution with operation IDs?
- Should disk creation support multiple disks at initial create time?
- Should network permit multiple NICs during create?
- Should destroy_disks default be true in non-production environments?
