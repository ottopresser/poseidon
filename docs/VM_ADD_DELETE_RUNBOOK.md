# VM Add/Delete Operational Runbook

This runbook defines a repeatable process for creating and deleting VMs.

Status: current operational guidance. Items explicitly marked planned are not implemented by the API.

## Scope

- Applies to day-2 VM operations on Poseidon-managed hosts.
- Covers operator workflow, safety gates, and expected outcomes.

## Policy Defaults

- CPU capacity check: soft gate (warn only).
- Memory capacity check: soft gate (warn only).
- Disk capacity check: hard gate (block if insufficient).
- Network availability check: hard gate (block if unavailable).
- Delete absent VM behavior: strict failure (not success).

Rationale:

- CPU and memory are shared/overcommitted in this environment.
- Disk and network failures are structural and should stop provisioning.
- Deleting a non-existent VM should surface an explicit failure state.

## Add VM Workflow

## 1) Request Intake

- Record required inputs:
  - vm_name
  - cpu, memory
  - disk size and storage target
  - network/switch assignment
  - boot source (template, image, or ISO)
- Confirm requester, owner, environment, and purpose.

## 2) Pre-Checks

- Validate API auth and host reachability.
- Confirm vm_name policy (allowed characters and uniqueness).
- Check host summary and evaluate:
  - Requested CPU against host CPU count and requested memory against current free pages: warning only by default.
  - Disk free space: block on insufficient space.
  - Network switch/bridge existence: block on missing network.

## 3) Provisioning

- Create VM artifacts on host (definition, disk, boot wiring).
- Apply configuration values.
- Start VM.

## 4) Validation

- Verify VM appears in VM list.
- Verify VM info reports expected runtime/config.
- Optional console validation for boot readiness.

## 5) Finalization

- In the operator's external change record, record completion metadata:
  - created_by
  - created_at
  - host
  - final config snapshot
- Return operation result as succeeded or failed.

## Add VM Failure Handling

- Name already exists: fail and stop.
- Disk/network hard-gate failure: fail and stop.
- CPU/memory soft-gate exceedance: continue with warning.
- Partial provisioning failure:
  - cleanup partial artifacts when safe
  - mark operation failed
  - create cleanup follow-up if needed

## Delete VM Workflow

## 1) Request Intake

- Record required inputs:
  - vm_name
  - stop strategy (graceful first, optional force)
  - storage disposition (destroy or retain)
- Confirm owner approval and retention policy.

## 2) Pre-Checks

- Verify VM exists.
- If VM is absent: fail with not-found outcome.
- Confirm backup/snapshot requirements are met.

## 3) Stop and Destroy

- Attempt graceful stop.
- If allowed and needed, force stop after timeout.
- Remove VM definition and apply storage disposition.

## 4) Validation

- Confirm VM no longer appears in VM list/info.
- Review the API's `disks_destroyed` result. A false value means the VM definition was removed but disk destruction was not confirmed.

## 5) Finalization

- In the operator's external change record, record deletion metadata:
  - deleted_by
  - deleted_at
  - reason
  - storage_disposition
- Return operation result as succeeded or failed.

## Delete VM Failure Handling

- VM absent at start: fail with not-found.
- Stop failure with no force permission: fail and escalate.
- Partial delete failure:
  - flag orphan cleanup task
  - return explicit failure details

## Operational States

Suggested state labels:

- pending
- validating
- provisioning
- running
- deleting
- deleted
- failed

## Current Audit Records

- Every HTTP request records an operation ID, timestamp, method, path, status, duration, and client address.
- `GET /v1/operations` returns the newest bounded records.
- Set `POSEIDON_AUDIT_LOG` to persist JSONL records across restarts.
- `POSEIDON_AUDIT_MAX_RECORDS` controls the in-memory limit and defaults to 1000.

## Planned Audit Detail

- Record each create/delete phase start and completion.
- Associate subprocess return codes and stderr with the parent operation ID.
