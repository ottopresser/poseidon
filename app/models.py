from typing import Annotated, Any, List, Literal, Optional

import pydantic
from pydantic import BaseModel, Field


if pydantic.VERSION.startswith("1."):
    from pydantic import constr

    VmName = constr(regex=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    StoragePath = constr(strip_whitespace=True, regex=r"^/")
    ResourceSize = constr(
        strip_whitespace=True,
        regex=r"^(?:[1-9][0-9]*(?:\.[0-9]+)?|0+\.0*[1-9][0-9]*)[KMGTP]?(?:I?B)?$",
    )
    NonEmptyText = constr(strip_whitespace=True, min_length=1)
    SnapshotName = constr(regex=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
else:
    from pydantic import StringConstraints

    VmName = Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
    ]
    StoragePath = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^/")]
    ResourceSize = Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^(?:[1-9][0-9]*(?:\.[0-9]+)?|0+\.0*[1-9][0-9]*)[KMGTP]?(?:I?B)?$",
        ),
    ]
    NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    SnapshotName = Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"),
    ]


class CommandResult(BaseModel):
    command: List[str]
    return_code: int
    stdout: str
    stderr: str


class VmListItem(BaseModel):
    fields: dict[str, str]


class VmListResponse(BaseModel):
    raw: str
    items: list[VmListItem]


class VmActionResponse(BaseModel):
    vm_name: str
    action: str
    result: CommandResult


class VmInfoResponse(BaseModel):
    vm_name: str
    result: CommandResult


class VmConfigResponse(BaseModel):
    vm_name: str
    config_path: str
    config: dict[str, str]


class HostSummarySection(BaseModel):
    details: dict[str, Any]
    command_results: dict[str, CommandResult]


class HostSummaryResponse(BaseModel):
    host: HostSummarySection
    memory: HostSummarySection
    disk: HostSummarySection
    network: HostSummarySection


class VmCreateResources(BaseModel):
    cpu: int = Field(..., gt=0)
    memory: ResourceSize


class VmCreateDisk(BaseModel):
    size: ResourceSize
    storage_target: StoragePath


class VmCreateNetwork(BaseModel):
    switch: VmName
    interface_type: Literal["virtio-net", "e1000"] = "virtio-net"


class VmCreateBoot(BaseModel):
    source_type: Literal["template", "iso", "img"]
    source: NonEmptyText


class VmCreateOptions(BaseModel):
    start_after_create: bool = True
    validate_only: bool = False
    auto_start_on_boot: bool = False


class VmCreateRequest(BaseModel):
    vm_name: VmName
    resources: VmCreateResources
    disk: VmCreateDisk
    network: VmCreateNetwork
    boot: VmCreateBoot
    options: VmCreateOptions = Field(default_factory=VmCreateOptions)


class VmOperationWarning(BaseModel):
    code: str
    message: str


class VmCapacityCheckResult(BaseModel):
    gate: str
    result: str


class VmCreateChecks(BaseModel):
    cpu: VmCapacityCheckResult
    memory: VmCapacityCheckResult
    disk: VmCapacityCheckResult
    network: VmCapacityCheckResult


class VmCreateOutcome(BaseModel):
    created: bool
    started: bool


class VmCreateResponse(BaseModel):
    operation: str
    status: str
    vm_name: str
    warnings: list[VmOperationWarning] = Field(default_factory=list)
    checks: VmCreateChecks
    result: VmCreateOutcome


class VmDeleteOutcome(BaseModel):
    stopped: bool
    definition_deleted: bool
    disks_destroyed: bool


class VmDeleteResponse(BaseModel):
    operation: str
    status: str
    vm_name: str
    warnings: list[VmOperationWarning] = Field(default_factory=list)
    result: VmDeleteOutcome


class BootMediaItem(BaseModel):
    file_name: str
    file_path: str
    size_bytes: int
    modified_epoch: int
    checksum_sha256: str | None = None


class BootMediaListResponse(BaseModel):
    media_dir: str
    items: list[BootMediaItem]


class BootMediaUploadResponse(BaseModel):
    media_dir: str
    item: BootMediaItem


class BootMediaDeleteResponse(BaseModel):
    media_dir: str
    file_name: str
    file_path: str
    deleted: bool


class VmDetachMediaResponse(BaseModel):
    vm_name: str
    removed_keys: list[str]
    removed_media: list[str]
    config_updated: bool


class VmConfigureRequest(BaseModel):
    settings: Optional[List[str]] = None

    # Backward-compatible alias for older clients.
    options: Optional[List[str]] = None


class ConsoleTokenResponse(BaseModel):
    vm_name: str
    token_type: str = "poseidon-console-token"
    token: str
    expires_in_seconds: int
    expires_at_epoch: int
    ws_path: str


class ReadinessCheck(BaseModel):
    name: str
    ready: bool
    detail: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: list[ReadinessCheck]


class VmNetworkMetrics(BaseModel):
    interface: str
    switch: str | None = None
    rx_bytes: int | None = None
    tx_bytes: int | None = None
    rx_packets: int | None = None
    tx_packets: int | None = None


class VmMetricsResponse(BaseModel):
    vm_name: str
    state: str
    configured_cpu: int | None = None
    configured_memory: str | None = None
    cpu_percent: float | None = None
    resident_memory_bytes: int | None = None
    uptime: str | None = None
    network: list[VmNetworkMetrics] = Field(default_factory=list)
    scope_note: str


class VmMetricsListResponse(BaseModel):
    items: list[VmMetricsResponse]


class NetworkInventoryResponse(BaseModel):
    switches: list[dict[str, str]]
    interfaces: list[dict[str, Any]]
    command_results: dict[str, CommandResult]


class VmSnapshotRequest(BaseModel):
    snapshot_name: SnapshotName | None = None
    force: bool = False


class VmSnapshotRollbackRequest(BaseModel):
    snapshot_name: SnapshotName
    destroy_newer: bool = False


class VmSnapshotListResponse(BaseModel):
    vm_name: str
    items: list[str]


class VmCloneRequest(BaseModel):
    new_vm_name: VmName
    snapshot_name: SnapshotName | None = None


class VmCloneResponse(BaseModel):
    source_vm_name: str
    new_vm_name: str
    result: CommandResult


class VmDiskAddRequest(BaseModel):
    device_type: Literal["file", "zvol", "sparse-zvol"] = "file"
    size: ResourceSize


class VmDiskItem(BaseModel):
    index: int
    name: str
    device_type: str
    emulation: str | None = None
    options: str | None = None


class VmDiskInventoryResponse(BaseModel):
    vm_name: str
    items: list[VmDiskItem]


class VmDiskDetachResponse(BaseModel):
    vm_name: str
    disk_index: int
    removed_keys: list[str]
    data_preserved: bool = True


class OperationRecord(BaseModel):
    operation_id: str
    timestamp_epoch: int
    method: str
    path: str
    status_code: int
    duration_ms: float
    client: str


class OperationHistoryResponse(BaseModel):
    items: list[OperationRecord]
