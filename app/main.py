import asyncio
from contextlib import suppress
import logging
import os
import signal
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.audit import OperationAuditStore
from app.models import (
    BootMediaDeleteResponse,
    BootMediaListResponse,
    BootMediaUploadResponse,
    CommandResult,
    ConsoleTokenResponse,
    HostSummaryResponse,
    NetworkInventoryResponse,
    OperationHistoryResponse,
    ReadinessResponse,
    SnapshotName,
    VmCapacityCheckResult,
    VmCloneRequest,
    VmCloneResponse,
    VmCreateChecks,
    VmCreateRequest,
    VmCreateResponse,
    VmConfigResponse,
    VmConfigureRequest,
    VmDeleteResponse,
    VmDetachMediaResponse,
    VmDiskAddRequest,
    VmDiskDetachResponse,
    VmDiskInventoryResponse,
    VmOperationWarning,
    VmActionResponse,
    VmInfoResponse,
    VmListItem,
    VmListResponse,
    VmMetricsListResponse,
    VmMetricsResponse,
    VmName,
    VmSnapshotListResponse,
    VmSnapshotRequest,
    VmSnapshotRollbackRequest,
)
from app.security import issue_console_token, verify_admin_token, verify_api_key, verify_console_token
from app.services.vm_cli import VmCliError, VmCliService

app = FastAPI(
    title="Poseidon API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
vm_service = VmCliService()
audit_store = OperationAuditStore()
vm_router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])

logger = logging.getLogger("poseidon.api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

CREATE_VM_RESPONSES = {
    201: {
        "description": "VM created successfully.",
        "content": {
            "application/json": {
                "example": {
                    "operation": "create_vm",
                    "status": "succeeded",
                    "vm_name": "app-01",
                    "warnings": [],
                    "checks": {
                        "cpu": {"gate": "soft", "result": "pass"},
                        "memory": {"gate": "soft", "result": "pass"},
                        "disk": {"gate": "hard", "result": "pass"},
                        "network": {"gate": "hard", "result": "pass"},
                    },
                    "result": {"created": True, "started": True},
                }
            }
        },
    },
    404: {
        "description": "Referenced dependency not found (for example network switch or uploaded boot media).",
        "content": {
            "application/json": {
                "example": {"detail": "Boot media not found: freebsd-installer.iso"}
            }
        },
    },
    409: {
        "description": "Create request failed hard-gate checks (disk/network) or VM already exists.",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Create request failed hard-gate checks: disk=fail network=pass"
                }
            }
        },
    },
}

DELETE_VM_RESPONSES = {
    404: {
        "description": "VM not found.",
        "content": {
            "application/json": {
                "example": {"detail": "VM not found: app-01"}
            }
        },
    },
    409: {
        "description": "Delete request conflicts with current state or safety requirements.",
        "content": {
            "application/json": {
                "example": {
                    "detail": "Unable to stop VM before destroy: vm may still be running. Retry with force=true."
                }
            }
        },
    },
}


def _command_result(result) -> CommandResult:
    return CommandResult(
        command=result.command,
        return_code=result.return_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _command_result_map(results: dict[str, object]) -> dict[str, CommandResult]:
    return {name: _command_result(result) for name, result in results.items()}


def _int_from(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _require_vm_exists(vm_name: str) -> None:
    try:
        exists = vm_service.vm_exists(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not exists:
        raise HTTPException(status_code=404, detail=f"VM not found: {vm_name}")


def _require_vm_stopped(vm_name: str) -> None:
    _require_vm_exists(vm_name)
    try:
        state = vm_service.vm_state(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not state or not state.strip().lower().startswith("stopped"):
        raise HTTPException(
            status_code=409,
            detail=f"VM must be confirmed stopped for this operation; current state: {state or 'unknown'}",
        )


def _build_create_checks(payload: VmCreateRequest) -> tuple[VmCreateChecks, list[VmOperationWarning], bool]:
    summary = vm_service.host_summary()
    warnings: list[VmOperationWarning] = []
    hard_failed = False

    host_details = summary.get("host", {}).get("details", {})
    memory_details = summary.get("memory", {}).get("details", {})

    requested_cpu = payload.resources.cpu
    host_ncpu = _int_from(host_details.get("hw.ncpu"))
    cpu_result = "pass"
    if host_ncpu is None:
        cpu_result = "warn"
        warnings.append(
            VmOperationWarning(
                code="CAPACITY_CPU_UNKNOWN",
                message="Unable to determine host CPU count; treating CPU check as soft warning.",
            )
        )
    elif requested_cpu > host_ncpu:
        cpu_result = "warn"
        warnings.append(
            VmOperationWarning(
                code="CAPACITY_CPU_SOFT",
                message="Requested CPU exceeds host CPU count; overcommit warning.",
            )
        )

    requested_memory_bytes = vm_service.parse_human_size_to_bytes(payload.resources.memory)
    host_free_bytes = _int_from(memory_details.get("computed.free_bytes"))
    memory_result = "pass"
    if requested_memory_bytes is None:
        memory_result = "warn"
        warnings.append(
            VmOperationWarning(
                code="CAPACITY_MEMORY_PARSE",
                message="Requested memory format could not be parsed; memory check is warning only.",
            )
        )
    elif host_free_bytes is None:
        memory_result = "warn"
        warnings.append(
            VmOperationWarning(
                code="CAPACITY_MEMORY_UNKNOWN",
                message="Unable to determine free memory; memory check is warning only.",
            )
        )
    elif requested_memory_bytes > host_free_bytes:
        memory_result = "warn"
        warnings.append(
            VmOperationWarning(
                code="CAPACITY_MEMORY_SOFT",
                message="Requested memory exceeds currently free memory; overcommit warning.",
            )
        )

    requested_disk_bytes = vm_service.parse_human_size_to_bytes(payload.disk.size)
    disk_avail_bytes = vm_service.storage_available_bytes(payload.disk.storage_target)
    disk_result = "pass"
    if requested_disk_bytes is None:
        disk_result = "fail"
        hard_failed = True
    elif disk_avail_bytes is None:
        disk_result = "fail"
        hard_failed = True
    elif requested_disk_bytes > disk_avail_bytes:
        disk_result = "fail"
        hard_failed = True

    _, switch_list = vm_service.list_switches()
    switch_names = {name for name in switch_list if name}

    network_result = "pass"
    if not switch_names or payload.network.switch not in switch_names:
        network_result = "fail"
        hard_failed = True

    checks = VmCreateChecks(
        cpu=VmCapacityCheckResult(gate="soft", result=cpu_result),
        memory=VmCapacityCheckResult(gate="soft", result=memory_result),
        disk=VmCapacityCheckResult(gate="hard", result=disk_result),
        network=VmCapacityCheckResult(gate="hard", result=network_result),
    )
    return checks, warnings, hard_failed


def _raise_create_failure(vm_name: str, status_code: int, detail: str) -> None:
    rollback_errors: list[str] = []

    try:
        vm_service.stop_vm(vm_name, force=True)
    except VmCliError as exc:
        rollback_errors.append(f"stop: {exc}")

    try:
        vm_service.remove_vm_autostart_on_boot(vm_name)
    except VmCliError as exc:
        rollback_errors.append(f"autostart: {exc}")

    try:
        destroy_result, disks_destroyed = vm_service.destroy_vm(
            vm_name,
            force=True,
            destroy_disks=True,
        )
        if destroy_result.return_code != 0:
            rollback_errors.append(
                "destroy: " + (destroy_result.stderr.strip() or destroy_result.stdout.strip() or "command failed")
            )
        elif not disks_destroyed:
            rollback_errors.append("destroy: VM definition removed but disk destruction was unavailable")
    except VmCliError as exc:
        rollback_errors.append(f"destroy: {exc}")

    if rollback_errors:
        detail = f"{detail} Rollback incomplete: {'; '.join(rollback_errors)}"
    else:
        detail = f"{detail} The partially created VM was rolled back."

    raise HTTPException(status_code=status_code, detail=detail)


def _openapi_url_for_request(request: Request) -> str:
    admin_token = request.query_params.get("admin_token")
    if not admin_token:
        return "/openapi.json"

    token = quote(admin_token, safe="")
    return f"/openapi.json?admin_token={token}"


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", response_model=ReadinessResponse)
def readiness(response: Response) -> ReadinessResponse:
    checks = vm_service.readiness_checks()
    ready = all(bool(check["ready"]) for check in checks)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(verify_admin_token)])
def openapi_json() -> JSONResponse:
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    return JSONResponse(schema)


@app.get("/docs", include_in_schema=False, dependencies=[Depends(verify_admin_token)])
def docs_ui(request: Request):
    return get_swagger_ui_html(
        openapi_url=_openapi_url_for_request(request),
        title=f"{app.title} - Swagger UI",
    )


@app.get("/redocs", include_in_schema=False, dependencies=[Depends(verify_admin_token)])
def redocs_ui(request: Request):
    return get_redoc_html(
        openapi_url=_openapi_url_for_request(request),
        title=f"{app.title} - ReDoc",
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    client = request.client.host if request.client else "unknown"
    try:
        response = await call_next(request)
        response_status = response.status_code
        return response
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "%s %s status=%s duration_ms=%s client=%s",
            request.method,
            request.url.path,
            response_status,
            duration_ms,
            client,
        )
        audit_store.record(
            method=request.method,
            path=request.url.path,
            status_code=response_status,
            duration_ms=duration_ms,
            client=client,
        )


@vm_router.get("/vms", response_model=VmListResponse)
def vm_list() -> VmListResponse:
    try:
        result, items = vm_service.list_vms()
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    response_items = [VmListItem(fields=item) for item in items]
    return VmListResponse(raw=result.stdout, items=response_items)


@vm_router.get("/boot-media", response_model=BootMediaListResponse)
def boot_media_list(include_checksum: bool = False) -> BootMediaListResponse:
    try:
        media_dir, items = vm_service.list_boot_media(include_checksum=include_checksum)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return BootMediaListResponse(media_dir=media_dir, items=items)


@vm_router.post("/boot-media/upload", response_model=BootMediaUploadResponse, status_code=201)
def boot_media_upload(file: UploadFile = File(...), overwrite: bool = False) -> BootMediaUploadResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Upload must include a file name ending in .iso or .img.")

    try:
        media_dir, item = vm_service.upload_boot_media(file.filename, file.file, overwrite=overwrite)
    except VmCliError as exc:
        detail = str(exc)
        if detail.startswith("Boot media already exists:"):
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail.startswith("Boot media is in use by VM:"):
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail.startswith("Boot media exceeds maximum upload size"):
            raise HTTPException(status_code=413, detail=detail) from exc
        if detail.startswith("Boot media upload is empty"):
            raise HTTPException(status_code=400, detail=detail) from exc
        if detail.startswith("Only .iso and .img") or detail.startswith("Invalid media filename"):
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=500, detail=detail) from exc
    finally:
        file.file.close()

    return BootMediaUploadResponse(media_dir=media_dir, item=item)


@vm_router.delete("/boot-media/{file_name}", response_model=BootMediaDeleteResponse)
def boot_media_delete(file_name: str) -> BootMediaDeleteResponse:
    try:
        media_dir, clean_name, target_path = vm_service.delete_boot_media(file_name)
    except VmCliError as exc:
        detail = str(exc)
        if detail.startswith("Boot media not found:"):
            raise HTTPException(status_code=404, detail=detail) from exc
        if detail.startswith("Boot media is in use by VM:"):
            raise HTTPException(status_code=409, detail=detail) from exc
        if detail.startswith("Only .iso and .img") or detail.startswith("Invalid media filename"):
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=500, detail=detail) from exc

    return BootMediaDeleteResponse(
        media_dir=media_dir,
        file_name=clean_name,
        file_path=target_path,
        deleted=True,
    )


@vm_router.post("/vms/create", response_model=VmCreateResponse, status_code=201, responses=CREATE_VM_RESPONSES)
def vm_create(payload: VmCreateRequest, response: Response) -> VmCreateResponse:
    source_type = payload.boot.source_type.strip().lower()
    source_name = payload.boot.source.strip()
    source_ext = os.path.splitext(source_name)[1].lower()

    try:
        if vm_service.vm_exists(payload.vm_name):
            raise HTTPException(status_code=409, detail=f"VM already exists: {payload.vm_name}")
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if source_type == "iso" and source_ext != ".iso":
        raise HTTPException(
            status_code=400,
            detail="boot.source must reference a .iso file when boot.source_type=iso.",
        )

    if source_type == "img" and source_ext != ".img":
        raise HTTPException(
            status_code=400,
            detail="boot.source must reference a .img file when boot.source_type=img.",
        )

    if source_type == "iso" or source_type == "img":
        try:
            media_path = vm_service.resolve_boot_media_path(payload.boot.source)
        except VmCliError as exc:
            detail = str(exc)
            if detail.startswith("Boot media not found:"):
                raise HTTPException(status_code=404, detail=detail) from exc
            if detail.startswith("Only .iso and .img") or detail.startswith("Invalid media filename"):
                raise HTTPException(status_code=400, detail=detail) from exc
            raise HTTPException(status_code=500, detail=detail) from exc
    else:
        media_path = ""

    try:
        checks, warnings, hard_failed = _build_create_checks(payload)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if payload.options.validate_only:
        response.status_code = status.HTTP_200_OK
        return VmCreateResponse(
            operation="create_vm",
            status="validation_failed" if hard_failed else "validated",
            vm_name=payload.vm_name,
            warnings=warnings,
            checks=checks,
            result={"created": False, "started": False},
        )

    if hard_failed:
        if checks.network.result == "fail":
            raise HTTPException(
                status_code=404,
                detail=f"Network switch or interface not found: {payload.network.switch}",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Create request failed hard-gate checks: disk={checks.disk.result} network={checks.network.result}",
        )

    try:
        create_result = vm_service.create_vm(
            vm_name=payload.vm_name,
            source_type=source_type,
            source=payload.boot.source,
        )
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if create_result.return_code != 0:
        create_error = create_result.stderr.strip() or create_result.stdout.strip()
        normalized_error = create_error.lower()
        if "already exists" in normalized_error:
            create_status = 409
        elif "not found" in normalized_error or "does not exist" in normalized_error:
            create_status = 404
        else:
            create_status = 500
        raise HTTPException(status_code=create_status, detail=create_error)

    img_boot_name = ""
    if source_type == "img":
        try:
            img_boot_name = vm_service.link_boot_media_into_vm(payload.vm_name, media_path)
        except VmCliError as exc:
            _raise_create_failure(payload.vm_name, 500, str(exc))

    configure_settings = [
        f"memory={payload.resources.memory}",
        f"cpu={payload.resources.cpu}",
        f"network0_switch={payload.network.switch}",
        f"network0_type={payload.network.interface_type}",
    ]

    if source_type == "iso":
        configure_settings.extend(
            [
                "loader=uefi",
                f"disk0_size={payload.disk.size}",
                "cd0_type=ahci-cd",
                f"cd0_name={media_path}",
            ]
        )
    elif source_type == "img":
        image_name = os.path.basename(source_name).lower()
        if "memstick" in image_name:
            # disk0 = root target disk (blank, installer writes here)
            # disk1 = installer media (boot source, detach after install)
            configure_settings.extend(
                [
                    "loader=uefi",
                    "disk0_type=virtio-blk",
                    "disk0_name=disk0.img",
                    f"disk0_size={payload.disk.size}",
                    "disk1_type=ahci-hd",
                    "disk1_dev=file",
                    f"disk1_name={img_boot_name}",
                ]
            )
        else:
            configure_settings.extend(
                [
                    "disk0_type=virtio-blk",
                    "disk0_dev=file",
                    f"disk0_name={img_boot_name}",
                ]
            )
    else:
        configure_settings.append(f"disk0_size={payload.disk.size}")

    try:
        configure_result = vm_service.configure_vm(payload.vm_name, settings=configure_settings)
    except VmCliError as exc:
        _raise_create_failure(payload.vm_name, 500, str(exc))

    if configure_result.return_code != 0:
        _raise_create_failure(
            payload.vm_name,
            500,
            configure_result.stderr.strip() or configure_result.stdout.strip() or "VM configuration failed.",
        )

    if payload.options.auto_start_on_boot:
        try:
            vm_service.ensure_vm_autostart_on_boot(payload.vm_name)
        except VmCliError as exc:
            _raise_create_failure(payload.vm_name, 500, str(exc))

    started = False
    if payload.options.start_after_create:
        try:
            start_result = vm_service.start_vm(payload.vm_name)
        except VmCliError as exc:
            _raise_create_failure(payload.vm_name, 500, str(exc))

        if start_result.return_code != 0:
            _raise_create_failure(
                payload.vm_name,
                500,
                start_result.stderr.strip() or start_result.stdout.strip() or "VM start failed.",
            )

        try:
            post_start_state = vm_service.vm_state(payload.vm_name)
        except VmCliError as exc:
            _raise_create_failure(payload.vm_name, 500, str(exc))

        if not vm_service.is_active_vm_state(post_start_state):
            state_text = post_start_state or "unknown"
            _raise_create_failure(
                payload.vm_name,
                409,
                f"VM did not reach an active state after start. Current state: {state_text}",
            )

        started = True

    try:
        created_exists = vm_service.vm_exists(payload.vm_name)
    except VmCliError as exc:
        _raise_create_failure(payload.vm_name, 500, f"Unable to verify created VM: {exc}")
    if not created_exists:
        _raise_create_failure(payload.vm_name, 500, "VM create command succeeded but the VM was not found afterward.")

    return VmCreateResponse(
        operation="create_vm",
        status="succeeded",
        vm_name=payload.vm_name,
        warnings=warnings,
        checks=checks,
        result={"created": True, "started": started},
    )


@vm_router.delete("/vms/{vm_name}", response_model=VmDeleteResponse, responses=DELETE_VM_RESPONSES)
def vm_delete(vm_name: VmName, force: bool = False, destroy_disks: bool = False) -> VmDeleteResponse:
    _require_vm_exists(vm_name)

    try:
        stop_result = vm_service.stop_vm(vm_name, force=force)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    stopped = stop_result.return_code == 0
    if not stopped:
        try:
            current_state = vm_service.vm_state(vm_name)
        except VmCliError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if current_state is None or vm_service.is_active_vm_state(current_state):
            raise HTTPException(
                status_code=409,
                detail="Unable to confirm that the VM stopped before destroy. Retry with force=true or inspect its state.",
            )
        stopped = True

    try:
        destroy_result, disks_destroyed = vm_service.destroy_vm(
            vm_name,
            force=force,
            destroy_disks=destroy_disks,
        )
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if destroy_result.return_code != 0:
        raise HTTPException(status_code=500, detail=destroy_result.stderr.strip() or destroy_result.stdout.strip())

    try:
        delete_verified = not vm_service.vm_exists(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=f"VM destroy succeeded but deletion could not be verified: {exc}") from exc
    if not delete_verified:
        raise HTTPException(status_code=500, detail="VM destroy command succeeded but the VM is still present.")

    delete_warnings: list[VmOperationWarning] = []
    try:
        vm_service.remove_vm_autostart_on_boot(vm_name)
    except VmCliError as exc:
        delete_warnings.append(
            VmOperationWarning(
                code="AUTOSTART_CLEANUP_FAILED",
                message=f"VM was deleted, but its autostart entry could not be removed: {exc}",
            )
        )

    return VmDeleteResponse(
        operation="delete_vm",
        status="succeeded",
        vm_name=vm_name,
        warnings=delete_warnings,
        result={
            "stopped": stopped,
            "definition_deleted": True,
            "disks_destroyed": disks_destroyed,
        },
    )


@vm_router.post("/vms/{vm_name}/start", response_model=VmActionResponse)
def vm_start(vm_name: VmName) -> VmActionResponse:
    _require_vm_exists(vm_name)
    try:
        result = vm_service.start_vm(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())

    return VmActionResponse(vm_name=vm_name, action="start", result=_command_result(result))


@vm_router.post("/vms/{vm_name}/restart", response_model=VmActionResponse)
def vm_restart(vm_name: VmName) -> VmActionResponse:
    _require_vm_exists(vm_name)
    try:
        result = vm_service.restart_vm(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())

    return VmActionResponse(vm_name=vm_name, action="restart", result=_command_result(result))


@vm_router.post("/vms/{vm_name}/stop", response_model=VmActionResponse)
def vm_stop(vm_name: VmName, force: bool = False) -> VmActionResponse:
    _require_vm_exists(vm_name)
    try:
        result = vm_service.stop_vm(vm_name, force=force)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())

    return VmActionResponse(vm_name=vm_name, action="stop", result=_command_result(result))


@vm_router.post("/vms/{vm_name}/configure", response_model=VmActionResponse)
def vm_configure(vm_name: VmName, payload: VmConfigureRequest | None = None) -> VmActionResponse:
    _require_vm_exists(vm_name)
    settings = None
    if payload:
        settings = payload.settings or payload.options

    if not settings:
        raise HTTPException(
            status_code=400,
            detail="Request body must include at least one VM setting (for example: settings=[\"memory=4G\", \"cpu=2\"]).",
        )

    try:
        result = vm_service.configure_vm(vm_name, settings=settings)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.return_code != 0:
        raise HTTPException(status_code=400, detail=result.stderr.strip() or result.stdout.strip())

    return VmActionResponse(vm_name=vm_name, action="configure", result=_command_result(result))


@vm_router.post("/vms/{vm_name}/detach-media", response_model=VmDetachMediaResponse)
def vm_detach_media(vm_name: VmName) -> VmDetachMediaResponse:
    _require_vm_exists(vm_name)

    try:
        result = vm_service.detach_installer_media(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return VmDetachMediaResponse(
        vm_name=vm_name,
        removed_keys=result["removed_keys"],
        removed_media=result["removed_media"],
        config_updated=result["config_updated"],
    )


@vm_router.post("/vms/{vm_name}/console", response_model=VmActionResponse)
def vm_console(vm_name: VmName) -> VmActionResponse:
    _require_vm_exists(vm_name)
    try:
        result = vm_service.console_vm(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())

    return VmActionResponse(vm_name=vm_name, action="console", result=_command_result(result))


@vm_router.post("/vms/{vm_name}/console/token", response_model=ConsoleTokenResponse)
def vm_console_token(vm_name: VmName) -> ConsoleTokenResponse:
    _require_vm_exists(vm_name)
    token, ttl_seconds, expires_at = issue_console_token(vm_name)
    return ConsoleTokenResponse(
        vm_name=vm_name,
        token=token,
        expires_in_seconds=ttl_seconds,
        expires_at_epoch=expires_at,
        ws_path=f"/v1/vms/{vm_name}/console/ws",
    )


@vm_router.get("/vms/metrics", response_model=VmMetricsListResponse)
def vm_metrics_list() -> VmMetricsListResponse:
    try:
        _output, items = vm_service.list_vm_metrics()
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return VmMetricsListResponse(items=items)


@vm_router.get("/vms/{vm_name}/metrics", response_model=VmMetricsResponse)
def vm_metrics(vm_name: VmName) -> VmMetricsResponse:
    try:
        item = vm_service.vm_metrics(vm_name)
    except VmCliError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404 if detail.startswith("VM not found:") else 500,
            detail=detail,
        ) from exc
    return VmMetricsResponse(**item)


@vm_router.get("/networks", response_model=NetworkInventoryResponse)
def network_inventory() -> NetworkInventoryResponse:
    try:
        inventory = vm_service.network_inventory()
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return NetworkInventoryResponse(
        switches=inventory["switches"],
        interfaces=inventory["interfaces"],
        command_results=_command_result_map(inventory["command_results"]),
    )


@vm_router.post("/vms/{vm_name}/snapshots", response_model=VmActionResponse)
def vm_snapshot(vm_name: VmName, payload: VmSnapshotRequest) -> VmActionResponse:
    if payload.force:
        _require_vm_exists(vm_name)
    else:
        _require_vm_stopped(vm_name)
    try:
        result = vm_service.snapshot_vm(vm_name, payload.snapshot_name, payload.force)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())
    return VmActionResponse(vm_name=vm_name, action="snapshot", result=_command_result(result))


@vm_router.get("/vms/{vm_name}/snapshots", response_model=VmSnapshotListResponse)
def vm_snapshots(vm_name: VmName) -> VmSnapshotListResponse:
    _require_vm_exists(vm_name)
    try:
        items = vm_service.list_vm_snapshots(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return VmSnapshotListResponse(vm_name=vm_name, items=items)


@vm_router.post("/vms/{vm_name}/snapshots/rollback", response_model=VmActionResponse)
def vm_snapshot_rollback(
    vm_name: VmName,
    payload: VmSnapshotRollbackRequest,
) -> VmActionResponse:
    _require_vm_stopped(vm_name)
    try:
        result = vm_service.rollback_vm_snapshot(
            vm_name,
            payload.snapshot_name,
            payload.destroy_newer,
        )
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())
    return VmActionResponse(vm_name=vm_name, action="snapshot_rollback", result=_command_result(result))


@vm_router.delete("/vms/{vm_name}/snapshots/{snapshot_name}", response_model=VmActionResponse)
def vm_snapshot_delete(vm_name: VmName, snapshot_name: SnapshotName) -> VmActionResponse:
    _require_vm_stopped(vm_name)
    try:
        result = vm_service.delete_vm_snapshot(vm_name, snapshot_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())
    return VmActionResponse(vm_name=vm_name, action="snapshot_delete", result=_command_result(result))


@vm_router.post("/vms/{vm_name}/clone", response_model=VmCloneResponse, status_code=201)
def vm_clone(vm_name: VmName, payload: VmCloneRequest) -> VmCloneResponse:
    _require_vm_stopped(vm_name)
    try:
        if vm_service.vm_exists(payload.new_vm_name):
            raise HTTPException(status_code=409, detail=f"VM already exists: {payload.new_vm_name}")
        result = vm_service.clone_vm(vm_name, payload.new_vm_name, payload.snapshot_name)
    except HTTPException:
        raise
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())
    try:
        clone_exists = vm_service.vm_exists(payload.new_vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=f"Clone succeeded but verification failed: {exc}") from exc
    if not clone_exists:
        raise HTTPException(status_code=500, detail="Clone command succeeded but the new VM was not found.")
    return VmCloneResponse(
        source_vm_name=vm_name,
        new_vm_name=payload.new_vm_name,
        result=_command_result(result),
    )


@vm_router.get("/vms/{vm_name}/disks", response_model=VmDiskInventoryResponse)
def vm_disks(vm_name: VmName) -> VmDiskInventoryResponse:
    _require_vm_exists(vm_name)
    try:
        items = vm_service.vm_disk_inventory(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return VmDiskInventoryResponse(vm_name=vm_name, items=items)


@vm_router.post("/vms/{vm_name}/disks", response_model=VmActionResponse, status_code=201)
def vm_disk_add(vm_name: VmName, payload: VmDiskAddRequest) -> VmActionResponse:
    _require_vm_stopped(vm_name)
    try:
        result = vm_service.add_vm_disk(vm_name, payload.device_type, payload.size)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.return_code != 0:
        raise HTTPException(status_code=409, detail=result.stderr.strip() or result.stdout.strip())
    return VmActionResponse(vm_name=vm_name, action="disk_add", result=_command_result(result))


@vm_router.delete("/vms/{vm_name}/disks/{disk_index}", response_model=VmDiskDetachResponse)
def vm_disk_detach(vm_name: VmName, disk_index: int) -> VmDiskDetachResponse:
    _require_vm_stopped(vm_name)
    try:
        removed_keys = vm_service.detach_vm_disk(vm_name, disk_index)
    except VmCliError as exc:
        detail = str(exc)
        status_code = 404 if detail.startswith("Disk not found:") else 409
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return VmDiskDetachResponse(
        vm_name=vm_name,
        disk_index=disk_index,
        removed_keys=removed_keys,
    )


@vm_router.get("/operations", response_model=OperationHistoryResponse)
def operation_history(limit: int = Query(100, ge=1, le=1000)) -> OperationHistoryResponse:
    return OperationHistoryResponse(items=audit_store.list_records(limit))


@vm_router.get("/vms/{vm_name}", response_model=VmInfoResponse)
def vm_info(vm_name: VmName) -> VmInfoResponse:
    _require_vm_exists(vm_name)
    try:
        result = vm_service.info_vm(vm_name)
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if result.return_code != 0:
        raise HTTPException(status_code=500, detail=result.stderr.strip() or result.stdout.strip())

    return VmInfoResponse(vm_name=vm_name, result=_command_result(result))


@vm_router.get("/vms/{vm_name}/config", response_model=VmConfigResponse)
def vm_config(vm_name: VmName) -> VmConfigResponse:
    try:
        config_path, config = vm_service.read_vm_config(vm_name)
    except VmCliError as exc:
        detail = str(exc)
        status_code = 404 if detail.startswith("VM config not found:") else 500
        raise HTTPException(status_code=status_code, detail=detail) from exc

    return VmConfigResponse(vm_name=vm_name, config_path=config_path, config=config)


@vm_router.get("/host/summary", response_model=HostSummaryResponse)
def host_summary() -> HostSummaryResponse:
    try:
        summary = vm_service.host_summary()
    except VmCliError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return HostSummaryResponse(
        host={
            "details": summary["host"]["details"],
            "command_results": _command_result_map(summary["host"]["command_results"]),
        },
        memory={
            "details": summary["memory"]["details"],
            "command_results": _command_result_map(summary["memory"]["command_results"]),
        },
        disk={
            "details": summary["disk"]["details"],
            "command_results": _command_result_map(summary["disk"]["command_results"]),
        },
        network={
            "details": summary["network"]["details"],
            "command_results": _command_result_map(summary["network"]["command_results"]),
        },
    )


app.include_router(vm_router)


@app.websocket("/v1/vms/{vm_name}/console/ws")
async def vm_console_ws(websocket: WebSocket, vm_name: VmName) -> None:
    api_key = websocket.headers.get("x-api-key")
    console_token = websocket.headers.get("x-console-token") or websocket.query_params.get("console_token")
    plain_mode = (websocket.query_params.get("plain") or "").lower() in {"1", "true", "yes"}
    try:
        if console_token:
            verify_console_token(console_token, vm_name)
        else:
            verify_api_key(api_key)
    except HTTPException as exc:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(exc.detail))
        return

    try:
        vm_exists = await asyncio.to_thread(vm_service.vm_exists, vm_name)
    except VmCliError as exc:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason=str(exc))
        return
    if not vm_exists:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=f"VM not found: {vm_name}")
        return

    await websocket.accept()

    command = [vm_service.command, "console", vm_name]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        await websocket.send_text(f"Command not found: {vm_service.command}")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    async def _forward_stream(stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(1024)
            if not chunk:
                return

            if plain_mode:
                # Coalesce nearby serial output into fewer websocket frames so
                # line-oriented clients do not render arbitrary read boundaries.
                pending = bytearray(chunk)
                reached_eof = False
                while True:
                    try:
                        extra = await asyncio.wait_for(stream.read(1024), timeout=0.02)
                    except asyncio.TimeoutError:
                        break

                    if not extra:
                        reached_eof = True
                        break

                    pending.extend(extra)

                text = pending.decode("utf-8", errors="replace")
                text = text.replace("\x00", "")
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                with suppress(WebSocketDisconnect, RuntimeError):
                    async with websocket_send_lock:
                        await websocket.send_text(text)

                if reached_eof:
                    return

                continue

            text = chunk.decode("utf-8", errors="replace")
            with suppress(WebSocketDisconnect, RuntimeError):
                async with websocket_send_lock:
                    await websocket.send_text(text)

    async def _terminate_console_process() -> None:
        if process.returncode is not None:
            return

        # Try graceful shutdown first.
        with suppress(ProcessLookupError):
            if process.pid and hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)

        if process.returncode is not None:
            return

        # Escalate to hard kill to avoid hanging service restarts.
        with suppress(ProcessLookupError):
            if process.pid and hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)

    websocket_send_lock = asyncio.Lock()
    stdout_task = asyncio.create_task(_forward_stream(process.stdout))
    stderr_task = asyncio.create_task(_forward_stream(process.stderr))
    process_wait_task = asyncio.create_task(process.wait())

    try:
        while True:
            receive_task = asyncio.create_task(websocket.receive())
            done, _pending = await asyncio.wait(
                {receive_task, process_wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if process_wait_task in done:
                receive_task.cancel()
                with suppress(asyncio.CancelledError):
                    await receive_task
                with suppress(RuntimeError):
                    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                break

            message = receive_task.result()
            message_type = message.get("type")

            if message_type == "websocket.disconnect":
                break

            text_payload = message.get("text")
            if text_payload is not None and process.stdin is not None:
                # Most websocket clients send a text frame without a terminal newline.
                # vm console expects carriage return for Enter on serial terminals.
                payload = text_payload
                if not payload.endswith("\n") and not payload.endswith("\r"):
                    payload += "\r\n" if plain_mode else "\r"
                process.stdin.write(payload.encode("utf-8", errors="replace"))
                await process.stdin.drain()
                continue

            binary_payload = message.get("bytes")
            if binary_payload is not None and process.stdin is not None:
                process.stdin.write(binary_payload)
                await process.stdin.drain()
    except WebSocketDisconnect:
        pass
    finally:
        if process.stdin is not None:
            with suppress(BrokenPipeError):
                process.stdin.close()

        await _terminate_console_process()
        with suppress(asyncio.CancelledError):
            await process_wait_task

        stdout_task.cancel()
        stderr_task.cancel()
        with suppress(asyncio.CancelledError):
            await stdout_task
        with suppress(asyncio.CancelledError):
            await stderr_task
