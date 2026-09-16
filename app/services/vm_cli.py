from __future__ import annotations

from contextlib import contextmanager, suppress
import fcntl
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class VmCommandOutput:
    command: list[str]
    return_code: int
    stdout: str
    stderr: str


class VmCliError(RuntimeError):
    pass


class VmCliService:
    def __init__(
        self,
        command: str = "vm",
        sysrc_command: str = "sysrc",
        vm_root_dir: str = "/vm",
        boot_media_dir: str | None = None,
        timeout_seconds: int = 30,
        max_upload_bytes: int | None = None,
    ) -> None:
        self.command = command
        self.sysrc_command = sysrc_command
        self.vm_root_dir = vm_root_dir
        self.boot_media_dir = boot_media_dir or os.environ.get("POSEIDON_BOOT_MEDIA_DIR", os.path.join(vm_root_dir, "media"))
        self.timeout_seconds = timeout_seconds
        try:
            self.max_upload_bytes = (
                max_upload_bytes
                if max_upload_bytes is not None
                else int(os.environ.get("POSEIDON_MAX_UPLOAD_BYTES", str(32 * 1024**3)))
            )
        except ValueError as exc:
            raise VmCliError("POSEIDON_MAX_UPLOAD_BYTES must be an integer.") from exc
        if self.max_upload_bytes <= 0:
            raise VmCliError("POSEIDON_MAX_UPLOAD_BYTES must be greater than zero.")

    def _run_external(self, executable: str, args: list[str]) -> VmCommandOutput:
        command = [executable, *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise VmCliError(f"Command not found: {executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VmCliError(
                f"Command timed out after {self.timeout_seconds}s: {' '.join(command)}"
            ) from exc

        return VmCommandOutput(
            command=command,
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _run(self, args: list[str]) -> VmCommandOutput:
        return self._run_external(self.command, args)

    @staticmethod
    def _sanitize_media_filename(file_name: str) -> str:
        original = (file_name or "").strip()
        clean = os.path.basename(original)
        if clean != original:
            raise VmCliError("Invalid media filename.")
        if not clean or clean in {".", ".."}:
            raise VmCliError("Invalid media filename.")
        return clean

    @staticmethod
    def _validate_media_extension(file_name: str) -> None:
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in {".iso", ".img"}:
            raise VmCliError("Only .iso and .img files are supported.")

    def _ensure_boot_media_dir(self) -> str:
        try:
            os.makedirs(self.boot_media_dir, exist_ok=True)
        except OSError as exc:
            raise VmCliError(f"Unable to create boot media directory: {self.boot_media_dir}: {exc}") from exc
        return self.boot_media_dir

    @contextmanager
    def _boot_media_lock(self):
        media_dir = self._ensure_boot_media_dir()
        lock_path = os.path.join(media_dir, ".poseidon-media.lock")
        try:
            with open(lock_path, "a", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise VmCliError(f"Unable to lock boot media directory: {media_dir}: {exc}") from exc

    def resolve_boot_media_path(self, file_name: str) -> str:
        clean_name = self._sanitize_media_filename(file_name)
        self._validate_media_extension(clean_name)
        candidate = os.path.join(self._ensure_boot_media_dir(), clean_name)
        if not os.path.isfile(candidate):
            raise VmCliError(f"Boot media not found: {clean_name}")
        return candidate

    @staticmethod
    def _file_sha256(file_path: str) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def list_boot_media(self, include_checksum: bool = False) -> tuple[str, list[dict[str, object]]]:
        media_dir = self._ensure_boot_media_dir()
        items: list[dict[str, object]] = []
        try:
            for entry in os.scandir(media_dir):
                if not entry.is_file():
                    continue
                try:
                    self._validate_media_extension(entry.name)
                except VmCliError:
                    continue

                stat = entry.stat()
                checksum = self._file_sha256(entry.path) if include_checksum else None
                items.append(
                    {
                        "file_name": entry.name,
                        "file_path": entry.path,
                        "size_bytes": int(stat.st_size),
                        "modified_epoch": int(stat.st_mtime),
                        "checksum_sha256": checksum,
                    }
                )
        except OSError as exc:
            raise VmCliError(f"Unable to list boot media directory: {media_dir}: {exc}") from exc

        items.sort(key=lambda item: str(item["file_name"]).lower())
        return media_dir, items

    def upload_boot_media(self, file_name: str, source_file, overwrite: bool = False) -> tuple[str, dict[str, object]]:
        with self._boot_media_lock():
            return self._upload_boot_media_unlocked(file_name, source_file, overwrite)

    def _upload_boot_media_unlocked(
        self,
        file_name: str,
        source_file,
        overwrite: bool,
    ) -> tuple[str, dict[str, object]]:
        clean_name = self._sanitize_media_filename(file_name)
        self._validate_media_extension(clean_name)

        media_dir = self._ensure_boot_media_dir()
        target_path = os.path.join(media_dir, clean_name)
        if os.path.exists(target_path) and not overwrite:
            raise VmCliError(f"Boot media already exists: {clean_name}")
        references = self._boot_media_references(target_path, clean_name) if os.path.exists(target_path) else []
        if references:
            raise VmCliError(f"Boot media is in use by VM: {', '.join(references)}")

        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{clean_name}.", suffix=".uploading", dir=media_dir)
        try:
            with os.fdopen(tmp_fd, "wb") as out_file:
                uploaded_bytes = 0
                while chunk := source_file.read(1024 * 1024):
                    uploaded_bytes += len(chunk)
                    if uploaded_bytes > self.max_upload_bytes:
                        raise VmCliError(
                            f"Boot media exceeds maximum upload size of {self.max_upload_bytes} bytes."
                        )
                    out_file.write(chunk)
                if uploaded_bytes == 0:
                    raise VmCliError("Boot media upload is empty.")
            if overwrite:
                os.replace(tmp_path, target_path)
            else:
                try:
                    os.link(tmp_path, target_path)
                except FileExistsError as exc:
                    raise VmCliError(f"Boot media already exists: {clean_name}") from exc
                os.remove(tmp_path)
            stat = os.stat(target_path)
            checksum = self._file_sha256(target_path)
        except VmCliError:
            with suppress(OSError):
                os.remove(tmp_path)
            raise
        except OSError as exc:
            with suppress(OSError):
                os.remove(tmp_path)
            raise VmCliError(f"Unable to store boot media: {clean_name}: {exc}") from exc

        item = {
            "file_name": clean_name,
            "file_path": target_path,
            "size_bytes": int(stat.st_size),
            "modified_epoch": int(stat.st_mtime),
            "checksum_sha256": checksum,
        }
        return media_dir, item

    def delete_boot_media(self, file_name: str) -> tuple[str, str, str]:
        with self._boot_media_lock():
            return self._delete_boot_media_unlocked(file_name)

    def _delete_boot_media_unlocked(self, file_name: str) -> tuple[str, str, str]:
        clean_name = self._sanitize_media_filename(file_name)
        self._validate_media_extension(clean_name)

        media_dir = self._ensure_boot_media_dir()
        target_path = os.path.join(media_dir, clean_name)
        if not os.path.isfile(target_path):
            raise VmCliError(f"Boot media not found: {clean_name}")

        references = self._boot_media_references(target_path, clean_name)
        if references:
            raise VmCliError(f"Boot media is in use by VM: {', '.join(references)}")

        try:
            os.remove(target_path)
        except OSError as exc:
            raise VmCliError(f"Unable to delete boot media: {clean_name}: {exc}") from exc

        return media_dir, clean_name, target_path

    def _boot_media_references(self, target_path: str, media_name: str) -> list[str]:
        references: list[str] = []
        target_realpath = os.path.realpath(target_path)
        try:
            entries = list(os.scandir(self.vm_root_dir))
        except OSError as exc:
            raise VmCliError(f"Unable to inspect VM directory: {self.vm_root_dir}: {exc}") from exc

        for entry in entries:
            if not entry.is_dir() or os.path.realpath(entry.path) == os.path.realpath(self.boot_media_dir):
                continue

            linked_media = os.path.join(entry.path, media_name)
            if os.path.islink(linked_media) and os.path.realpath(linked_media) == target_realpath:
                references.append(entry.name)
                continue

            config_path = os.path.join(entry.path, f"{entry.name}.conf")
            try:
                with open(config_path, "r", encoding="utf-8") as config_file:
                    if target_path in config_file.read():
                        references.append(entry.name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise VmCliError(f"Unable to inspect VM config: {config_path}: {exc}") from exc

        return sorted(set(references))

    def _run_safe_external(self, executable: str, args: list[str]) -> VmCommandOutput:
        try:
            return self._run_external(executable, args)
        except VmCliError as exc:
            return VmCommandOutput(
                command=[executable, *args],
                return_code=127,
                stdout="",
                stderr=str(exc),
            )

    @staticmethod
    def _parse_sysctl_assignments(stdout: str) -> dict[str, str]:
        details: dict[str, str] = {}
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue

            key, value = line.split(":", 1)
            details[key.strip()] = value.strip()
        return details

    @staticmethod
    def _parse_human_size_to_bytes(value: str) -> int | None:
        text = value.strip().upper()
        if not text or text == "-":
            return None

        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMGTP]?)(I?B)?$", text)
        if not match:
            return None

        number = float(match.group(1))
        unit = match.group(2)
        multipliers = {
            "": 1,
            "K": 1024,
            "M": 1024**2,
            "G": 1024**3,
            "T": 1024**4,
            "P": 1024**5,
        }
        return int(number * multipliers[unit])

    def parse_human_size_to_bytes(self, value: str) -> int | None:
        return self._parse_human_size_to_bytes(value)

    def storage_available_bytes(self, storage_path: str) -> int | None:
        try:
            if os.stat(storage_path).st_dev != os.stat(self.vm_root_dir).st_dev:
                return None
            return shutil.disk_usage(storage_path).free
        except OSError:
            return None

    def _parse_df_h(self, stdout: str) -> dict[str, object]:
        filesystems: list[dict[str, object]] = []
        total_size = 0
        total_used = 0
        total_avail = 0

        lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            return {"filesystems": filesystems, "totals": {"filesystem_count": 0}}

        for line in lines[1:]:
            parts = re.split(r"\s+", line.strip())
            if len(parts) < 6:
                continue

            filesystem, size, used, avail, capacity = parts[:5]
            mounted_on = " ".join(parts[5:])
            size_bytes = self._parse_human_size_to_bytes(size)
            used_bytes = self._parse_human_size_to_bytes(used)
            avail_bytes = self._parse_human_size_to_bytes(avail)

            if size_bytes is not None:
                total_size += size_bytes
            if used_bytes is not None:
                total_used += used_bytes
            if avail_bytes is not None:
                total_avail += avail_bytes

            filesystems.append(
                {
                    "filesystem": filesystem,
                    "size": size,
                    "used": used,
                    "avail": avail,
                    "capacity": capacity,
                    "mounted_on": mounted_on,
                    "size_bytes": size_bytes,
                    "used_bytes": used_bytes,
                    "avail_bytes": avail_bytes,
                }
            )

        return {
            "filesystems": filesystems,
            "totals": {
                "filesystem_count": len(filesystems),
                "size_bytes": total_size,
                "used_bytes": total_used,
                "avail_bytes": total_avail,
            },
        }

    def _parse_zpool_list(self, stdout: str) -> dict[str, object]:
        rows = self._parse_table(stdout)
        healthy_count = sum(1 for row in rows if row.get("HEALTH", "").upper() == "ONLINE")
        return {
            "pools": rows,
            "summary": {
                "pool_count": len(rows),
                "online_pool_count": healthy_count,
            },
        }

    def _parse_ifconfig(self, stdout: str) -> dict[str, object]:
        interfaces: list[dict[str, object]] = []
        current: dict[str, object] | None = None

        def commit_current() -> None:
            nonlocal current
            if current is not None:
                interfaces.append(current)
                current = None

        for raw_line in stdout.splitlines():
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue

            if line and not line[0].isspace() and ":" in line:
                commit_current()
                name, rest = line.split(":", 1)
                flags_match = re.search(r"flags=([^\s]+)", rest)
                mtu_match = re.search(r"mtu\s+([0-9]+)", rest)
                current = {
                    "name": name.strip(),
                    "flags": flags_match.group(1) if flags_match else "",
                    "mtu": int(mtu_match.group(1)) if mtu_match else None,
                    "mac": "",
                    "status": "",
                    "media": "",
                    "description": "",
                    "addresses": [],
                }
                continue

            if current is None:
                continue

            stripped = line.strip()
            if stripped.startswith("inet "):
                parts = stripped.split()
                current["addresses"].append({"family": "inet", "address": parts[1] if len(parts) > 1 else ""})
            elif stripped.startswith("inet6 "):
                parts = stripped.split()
                current["addresses"].append({"family": "inet6", "address": parts[1] if len(parts) > 1 else ""})
            elif stripped.startswith("ether "):
                parts = stripped.split()
                if len(parts) > 1:
                    current["mac"] = parts[1]
            elif stripped.startswith("status:"):
                current["status"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("media:"):
                current["media"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("description:"):
                current["description"] = stripped.split(":", 1)[1].strip()

        commit_current()

        up_count = 0
        for interface in interfaces:
            flags = str(interface.get("flags", ""))
            status = str(interface.get("status", "")).lower()
            if "UP" in flags or status == "active":
                up_count += 1

        return {
            "interfaces": interfaces,
            "summary": {
                "interface_count": len(interfaces),
                "up_interface_count": up_count,
            },
        }

    def _parse_netstat_routes(self, stdout: str) -> dict[str, object]:
        lines = [line.rstrip() for line in stdout.splitlines()]
        families: dict[str, list[dict[str, str]]] = {}
        default_routes: list[dict[str, str]] = []
        current_family = "unknown"
        headers: list[str] = []

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            if line.endswith(":") and " " not in line:
                current_family = line[:-1].lower()
                headers = []
                families.setdefault(current_family, [])
                continue

            if line.lower().startswith("destination"):
                headers = re.split(r"\s+", line)
                families.setdefault(current_family, [])
                continue

            if not headers:
                continue

            parts = re.split(r"\s+", line)
            if len(parts) < 2:
                continue

            row: dict[str, str] = {}
            for idx, header in enumerate(headers):
                row[header] = parts[idx] if idx < len(parts) else ""
            families[current_family].append(row)

            destination = row.get("Destination", "")
            if destination == "default":
                default_routes.append(
                    {
                        "family": current_family,
                        "gateway": row.get("Gateway", ""),
                        "netif": row.get("Netif", ""),
                    }
                )

        total_routes = sum(len(routes) for routes in families.values())
        return {
            "families": families,
            "default_routes": default_routes,
            "summary": {
                "route_count": total_routes,
            },
        }

    @staticmethod
    def _parse_table(stdout: str) -> list[dict[str, str]]:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            return []

        aligned = bool(re.search(r"\s{2,}", lines[0]))
        headers = re.split(r"\s{2,}" if aligned else r"\s+", lines[0])
        items: list[dict[str, str]] = []

        for line in lines[1:]:
            delimiter = r"\s{2,}" if aligned else r"\s+"
            parts = re.split(delimiter, line, maxsplit=max(0, len(headers) - 1))
            if not parts:
                continue

            row: dict[str, str] = {}
            for idx, header in enumerate(headers):
                row[header] = parts[idx] if idx < len(parts) else ""
            items.append(row)

        return items

    def vm_state(self, vm_name: str) -> str | None:
        _output, rows = self.list_vms()
        for row in rows:
            if row.get("NAME", "") == vm_name:
                state = row.get("STATE", "").strip()
                return state or None
        return None

    @staticmethod
    def is_active_vm_state(state: str | None) -> bool:
        if not state:
            return False

        normalized = state.strip().lower()
        return normalized.startswith("running") or normalized.startswith("bootloader")

    def list_vms(self) -> tuple[VmCommandOutput, list[dict[str, str]]]:
        output = self._run(["list"])
        if output.return_code != 0:
            raise VmCliError(output.stderr.strip() or output.stdout.strip() or "Failed to list VMs")
        parsed = self._parse_table(output.stdout)
        return output, parsed

    @staticmethod
    def _parse_netstat_interface_counters(stdout: str) -> dict[str, dict[str, int]]:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        header: list[str] = []
        counters: dict[str, dict[str, int]] = {}
        for line in lines:
            columns = re.split(r"\s+", line)
            if columns and columns[0] == "Name" and "Ibytes" in columns and "Obytes" in columns:
                header = columns
                continue
            if not header or len(columns) < len(header):
                continue
            row = dict(zip(header, columns))
            name = row.get("Name", "").rstrip("*")
            if not name:
                continue
            values: dict[str, int] = {}
            for source, target in (
                ("Ibytes", "rx_bytes"),
                ("Obytes", "tx_bytes"),
                ("Ipkts", "rx_packets"),
                ("Opkts", "tx_packets"),
            ):
                try:
                    values[target] = int(row.get(source, "0"))
                except ValueError:
                    values[target] = 0
            existing = counters.setdefault(
                name,
                {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0},
            )
            for key, value in values.items():
                existing[key] = max(existing[key], value)
        return counters

    @staticmethod
    def _optional_float(value: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def list_vm_metrics(self) -> tuple[VmCommandOutput, list[dict[str, object]]]:
        output = self._run(["list", "-v"])
        if output.return_code != 0:
            raise VmCliError(output.stderr.strip() or output.stdout.strip() or "Failed to list VM metrics")

        ifconfig_output = self._run_external("ifconfig", ["-a"])
        netstat_output = self._run_external("netstat", ["-ibn"])
        if ifconfig_output.return_code != 0:
            raise VmCliError(ifconfig_output.stderr.strip() or "Failed to inspect VM interfaces")
        if netstat_output.return_code != 0:
            raise VmCliError(netstat_output.stderr.strip() or "Failed to inspect VM network counters")

        network_by_vm: dict[str, list[dict[str, object]]] = {}
        counters = self._parse_netstat_interface_counters(netstat_output.stdout)
        interfaces = self._parse_ifconfig(ifconfig_output.stdout).get("interfaces", [])
        for interface in interfaces:
            description = str(interface.get("description", ""))
            match = re.fullmatch(r"vmnet/([^/]+)/(\d+)/([^/]+)", description)
            if not match:
                continue
            vm_name, _index, switch = match.groups()
            name = str(interface.get("name", ""))
            network_by_vm.setdefault(vm_name, []).append(
                {
                    "interface": name,
                    "switch": switch,
                    **counters.get(name, {}),
                }
            )

        items: list[dict[str, object]] = []
        for row in self._parse_table(output.stdout):
            cpu_text = row.get("CPU", "")
            try:
                configured_cpu = int(cpu_text)
            except ValueError:
                configured_cpu = None
            items.append(
                {
                    "vm_name": row.get("NAME", ""),
                    "state": row.get("STATE", ""),
                    "configured_cpu": configured_cpu,
                    "configured_memory": row.get("MEMORY") or None,
                    "cpu_percent": self._optional_float(row.get("%CPU", "")),
                    "resident_memory_bytes": self._parse_human_size_to_bytes(row.get("RSZ", "")),
                    "uptime": row.get("UPTIME") or None,
                    "network": network_by_vm.get(row.get("NAME", ""), []),
                    "scope_note": "Host-visible bhyve process and tap-interface metrics; guest-internal utilization requires a guest agent.",
                }
            )
        return output, items

    def vm_metrics(self, vm_name: str) -> dict[str, object]:
        _output, items = self.list_vm_metrics()
        for item in items:
            if item.get("vm_name") == vm_name:
                return item
        raise VmCliError(f"VM not found: {vm_name}")

    def network_inventory(self) -> dict[str, object]:
        switch_output = self._run(["switch", "list"])
        switch_info = self._run(["switch", "info"])
        ifconfig_output = self._run_external("ifconfig", ["-a"])
        for output, fallback in (
            (switch_output, "Failed to list VM switches"),
            (switch_info, "Failed to inspect VM switches"),
            (ifconfig_output, "Failed to inspect host interfaces"),
        ):
            if output.return_code != 0:
                raise VmCliError(output.stderr.strip() or output.stdout.strip() or fallback)
        return {
            "switches": self._parse_table(switch_output.stdout),
            "interfaces": self._parse_ifconfig(ifconfig_output.stdout).get("interfaces", []),
            "command_results": {
                "switch_list": switch_output,
                "switch_info": switch_info,
                "ifconfig": ifconfig_output,
            },
        }

    def readiness_checks(self) -> list[dict[str, object]]:
        checks: list[dict[str, object]] = []
        for name, args in (
            ("vm_bhyve", ["version"]),
            ("vm_list", ["list"]),
            ("switch_list", ["switch", "list"]),
        ):
            try:
                output = self._run(args)
                detail = output.stdout.strip() or output.stderr.strip()
                checks.append({"name": name, "ready": output.return_code == 0, "detail": detail})
            except VmCliError as exc:
                checks.append({"name": name, "ready": False, "detail": str(exc)})
        checks.append(
            {
                "name": "vm_root",
                "ready": os.path.isdir(self.vm_root_dir),
                "detail": self.vm_root_dir,
            }
        )
        return checks

    def list_switches(self) -> tuple[VmCommandOutput, list[str]]:
        output = self._run(["switch", "list"])
        if output.return_code != 0:
            raise VmCliError(output.stderr.strip() or output.stdout.strip() or "Failed to list VM switches")
        parsed = self._parse_table(output.stdout)
        names: list[str] = []
        for row in parsed:
            values = list(row.values())
            if values and values[0]:
                names.append(values[0])
        return output, names

    def vm_exists(self, vm_name: str) -> bool:
        _output, rows = self.list_vms()
        return any(row.get("NAME", "") == vm_name for row in rows)

    def start_vm(self, vm_name: str) -> VmCommandOutput:
        return self._run(["start", vm_name])

    def stop_vm(self, vm_name: str, force: bool = False) -> VmCommandOutput:
        args = ["stop"]
        if force:
            args.append("-f")
        args.append(vm_name)
        return self._run(args)

    def info_vm(self, vm_name: str) -> VmCommandOutput:
        return self._run(["info", vm_name])

    def console_vm(self, vm_name: str) -> VmCommandOutput:
        return self._run(["console", vm_name])

    def restart_vm(self, vm_name: str) -> VmCommandOutput:
        return self._run(["restart", vm_name])

    def snapshot_vm(
        self,
        vm_name: str,
        snapshot_name: str | None = None,
        force: bool = False,
    ) -> VmCommandOutput:
        target = f"{vm_name}@{snapshot_name}" if snapshot_name else vm_name
        args = ["snapshot"]
        if force:
            args.append("-f")
        args.append(target)
        return self._run(args)

    def list_vm_snapshots(self, vm_name: str) -> list[str]:
        output = self.info_vm(vm_name)
        if output.return_code != 0:
            raise VmCliError(output.stderr.strip() or output.stdout.strip() or "Failed to list snapshots")
        prefix = re.compile(rf"\b[^\s@]+/{re.escape(vm_name)}@([^\s]+)")
        return sorted({match.group(1) for match in prefix.finditer(output.stdout)})

    def delete_vm_snapshot(self, vm_name: str, snapshot_name: str) -> VmCommandOutput:
        return self._run(["destroy", f"{vm_name}@{snapshot_name}"])

    def rollback_vm_snapshot(
        self,
        vm_name: str,
        snapshot_name: str,
        destroy_newer: bool = False,
    ) -> VmCommandOutput:
        args = ["rollback"]
        if destroy_newer:
            args.append("-r")
        args.append(f"{vm_name}@{snapshot_name}")
        return self._run(args)

    def clone_vm(
        self,
        vm_name: str,
        new_vm_name: str,
        snapshot_name: str | None = None,
    ) -> VmCommandOutput:
        source = f"{vm_name}@{snapshot_name}" if snapshot_name else vm_name
        return self._run(["clone", source, new_vm_name])

    def add_vm_disk(self, vm_name: str, device_type: str, size: str) -> VmCommandOutput:
        return self._run(["add", "-d", "disk", "-t", device_type, "-s", size, vm_name])

    def vm_disk_inventory(self, vm_name: str) -> list[dict[str, object]]:
        _path, config = self.read_vm_config(vm_name)
        indices = sorted(
            {
                int(match.group(1))
                for key in config
                if (match := re.fullmatch(r"disk(\d+)_name", key))
            }
        )
        return [
            {
                "index": index,
                "name": config[f"disk{index}_name"],
                "device_type": config.get(f"disk{index}_dev", "file"),
                "emulation": config.get(f"disk{index}_type"),
                "options": config.get(f"disk{index}_opts"),
            }
            for index in indices
        ]

    def detach_vm_disk(self, vm_name: str, disk_index: int) -> list[str]:
        if disk_index <= 0:
            raise VmCliError("The root disk (disk0) cannot be detached.")
        state = self.vm_state(vm_name)
        if self.is_active_vm_state(state):
            raise VmCliError("The VM must be stopped before detaching a disk.")
        _path, config = self.read_vm_config(vm_name)
        prefix = f"disk{disk_index}_"
        keys = sorted(key for key in config if key.startswith(prefix))
        if not keys:
            raise VmCliError(f"Disk not found: disk{disk_index}")
        output = self._run_external(self.sysrc_command, ["-f", self._vm_config_path(vm_name), "-x", *keys])
        if output.return_code != 0:
            raise VmCliError(output.stderr.strip() or output.stdout.strip() or "Failed to detach disk")
        return keys

    def create_vm(self, vm_name: str, source_type: str, source: str) -> VmCommandOutput:
        args = ["create"]
        if source_type.strip().lower() == "template" and source.strip():
            args.extend(["-t", source])
        args.append(vm_name)
        return self._run(args)

    def destroy_vm(
        self,
        vm_name: str,
        force: bool = False,
        destroy_disks: bool = False,
    ) -> tuple[VmCommandOutput, bool]:
        args = ["destroy"]
        if force:
            args.append("-f")
        if destroy_disks:
            args.append("-d")
        args.append(vm_name)
        output = self._run(args)
        if destroy_disks and output.return_code != 0:
            stderr = (output.stderr or "").lower()
            if "unknown option" in stderr or "illegal option" in stderr:
                fallback_args = ["destroy"]
                if force:
                    fallback_args.append("-f")
                fallback_args.append(vm_name)
                return self._run(fallback_args), False
        return output, destroy_disks and output.return_code == 0

    @staticmethod
    def _normalize_setting(setting: str) -> str:
        if "=" not in setting:
            return setting

        key, value = setting.split("=", 1)
        key_normalized = key.strip().lower()
        alias_map = {
            "ram": "memory",
            "cpus": "cpu",
        }
        vm_key = alias_map.get(key_normalized, key.strip())
        return f"{vm_key}={value}"

    def _vm_config_path(self, vm_name: str) -> str:
        return os.path.join(self.vm_root_dir, vm_name, f"{vm_name}.conf")

    def link_boot_media_into_vm(self, vm_name: str, media_path: str) -> str:
        with self._boot_media_lock():
            return self._link_boot_media_into_vm_unlocked(vm_name, media_path)

    def _link_boot_media_into_vm_unlocked(self, vm_name: str, media_path: str) -> str:
        if not media_path or not os.path.isfile(media_path):
            raise VmCliError(f"Boot media not found: {media_path}")

        vm_dir = os.path.join(self.vm_root_dir, vm_name)
        if not os.path.isdir(vm_dir):
            raise VmCliError(f"VM directory not found: {vm_dir}")

        media_name = os.path.basename(media_path)
        link_path = os.path.join(vm_dir, media_name)

        try:
            if os.path.lexists(link_path):
                if os.path.islink(link_path):
                    existing_target = os.readlink(link_path)
                    existing_abs = os.path.abspath(os.path.join(vm_dir, existing_target))
                    if existing_abs == os.path.abspath(media_path):
                        return media_name
                raise VmCliError(f"VM media path already exists: {link_path}")

            os.symlink(media_path, link_path)
        except VmCliError:
            raise
        except OSError as exc:
            raise VmCliError(
                f"Unable to link boot media into VM directory: {media_path} -> {link_path}: {exc}"
            ) from exc

        return media_name

    def _read_rcconf_vm_list(self) -> list[str]:
        output = self._run_external(self.sysrc_command, ["-n", "vm_list"])
        if output.return_code != 0:
            error_text = (output.stderr or output.stdout).strip()
            if "unknown variable" in error_text.lower() or "not found" in error_text.lower():
                return []
            raise VmCliError(error_text or "Failed to read vm_list")

        entries = [item.strip() for item in output.stdout.split() if item.strip()]
        return entries

    @contextmanager
    def _autostart_lock(self):
        lock_path = "/var/run/poseidon-vm-list.lock"
        try:
            with open(lock_path, "a", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise VmCliError(f"Unable to lock autostart configuration: {lock_path}: {exc}") from exc

    def ensure_vm_autostart_on_boot(self, vm_name: str) -> None:
        with self._autostart_lock():
            enable_output = self._run_external(self.sysrc_command, ["vm_enable=YES"])
            if enable_output.return_code != 0:
                raise VmCliError(enable_output.stderr.strip() or enable_output.stdout.strip() or "Failed to set vm_enable=YES")

            existing = self._read_rcconf_vm_list()
            if vm_name in existing:
                return

            updated = [*existing, vm_name]
            list_output = self._run_external(self.sysrc_command, [f"vm_list={' '.join(updated)}"])
            if list_output.return_code != 0:
                raise VmCliError(list_output.stderr.strip() or list_output.stdout.strip() or "Failed to update vm_list")

    def remove_vm_autostart_on_boot(self, vm_name: str) -> None:
        with self._autostart_lock():
            existing = self._read_rcconf_vm_list()
            if vm_name not in existing:
                return

            updated = [item for item in existing if item != vm_name]
            list_output = self._run_external(self.sysrc_command, [f"vm_list={' '.join(updated)}"])
            if list_output.return_code != 0:
                raise VmCliError(list_output.stderr.strip() or list_output.stdout.strip() or "Failed to update vm_list")

    @staticmethod
    def _strip_simple_quoted_values_from_config(raw_text: str) -> str:
        normalized_lines: list[str] = []
        for line in raw_text.splitlines(keepends=True):
            newline = ""
            body = line
            if line.endswith("\r\n"):
                newline = "\r\n"
                body = line[:-2]
            elif line.endswith("\n"):
                newline = "\n"
                body = line[:-1]

            stripped = body.strip()
            if not stripped or stripped.startswith("#") or "=" not in body:
                normalized_lines.append(line)
                continue

            key, value = body.split("=", 1)
            trimmed_value = value.strip()
            if len(trimmed_value) >= 2 and trimmed_value[0] == '"' and trimmed_value[-1] == '"':
                inner = trimmed_value[1:-1]
                # Keep quoted values that would become ambiguous in shell-style config files.
                if inner and not any(ch.isspace() for ch in inner) and '"' not in inner and "'" not in inner:
                    body = f"{key}={inner}"

            normalized_lines.append(body + newline)

        return "".join(normalized_lines)

    def _normalize_vm_config_file(self, conf_path: str) -> None:
        try:
            with open(conf_path, "r", encoding="utf-8") as file_obj:
                original = file_obj.read()
        except FileNotFoundError as exc:
            raise VmCliError(f"VM config not found: {conf_path}") from exc
        except OSError as exc:
            raise VmCliError(f"Unable to read VM config: {conf_path}: {exc}") from exc

        normalized = self._strip_simple_quoted_values_from_config(original)
        if normalized == original:
            return

        try:
            with open(conf_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(normalized)
        except OSError as exc:
            raise VmCliError(f"Unable to write VM config: {conf_path}: {exc}") from exc

    @staticmethod
    def _parse_vm_config(text: str) -> dict[str, str]:
        config: dict[str, str] = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            lexer = shlex.shlex(line, posix=True)
            lexer.commenters = "#"
            lexer.whitespace_split = True
            try:
                line = " ".join(lexer)
            except ValueError:
                continue

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]

            if key:
                config[key] = value

        return config

    def read_vm_config(self, vm_name: str) -> tuple[str, dict[str, str]]:
        conf_path = self._vm_config_path(vm_name)
        try:
            with open(conf_path, "r", encoding="utf-8") as file_obj:
                raw = file_obj.read()
        except FileNotFoundError as exc:
            raise VmCliError(f"VM config not found: {conf_path}") from exc
        except OSError as exc:
            raise VmCliError(f"Unable to read VM config: {conf_path}: {exc}") from exc

        return conf_path, self._parse_vm_config(raw)

    def detach_installer_media(self, vm_name: str) -> dict[str, object]:
        conf_path = self._vm_config_path(vm_name)
        try:
            with open(conf_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError as exc:
            raise VmCliError(f"VM config not found: {conf_path}") from exc
        except OSError as exc:
            raise VmCliError(f"Unable to read VM config: {conf_path}: {exc}") from exc

        config = self._parse_vm_config(raw)
        removed_keys: list[str] = []
        removed_media: list[str] = []
        media_links: list[str] = []

        # Find all disk* keys where dev=file (installer media disks)
        # and remove them plus any linked media file inside the VM dir.
        disk_indices: set[str] = set()
        for key in config:
            m = re.match(r"^(disk(\d+))_", key)
            if m:
                disk_indices.add(m.group(2))

        for idx in sorted(disk_indices):
            dev_key = f"disk{idx}_dev"
            name_key = f"disk{idx}_name"
            if idx == "0" or config.get(dev_key, "") != "file":
                continue

            media_name = config.get(name_key, "")
            vm_dir = os.path.join(self.vm_root_dir, vm_name)
            link_path = os.path.join(vm_dir, os.path.basename(media_name))
            if (
                not media_name
                or not os.path.islink(link_path)
                or os.path.commonpath(
                    [os.path.realpath(link_path), os.path.realpath(self.boot_media_dir)]
                )
                != os.path.realpath(self.boot_media_dir)
            ):
                continue

            keys_to_remove = [k for k in config if re.match(rf"^disk{idx}_", k)]
            for key in keys_to_remove:
                removed_keys.append(key)

            removed_media.append(media_name)
            media_links.append(link_path)

        if not removed_keys:
            return {"removed_keys": [], "removed_media": [], "config_updated": False}

        args = ["-f", conf_path, "-x", *removed_keys]
        output = self._run_external(self.sysrc_command, args)
        if output.return_code != 0:
            raise VmCliError(output.stderr.strip() or output.stdout.strip() or "Failed to remove installer media from config")

        for link_path in media_links:
            with suppress(OSError):
                os.remove(link_path)

        return {"removed_keys": removed_keys, "removed_media": removed_media, "config_updated": True}

    def configure_vm(self, vm_name: str, settings: list[str] | None = None) -> VmCommandOutput:
        if not settings:
            raise VmCliError("No VM settings provided. Use one or more key=value entries.")

        normalized_settings = [self._normalize_setting(setting) for setting in settings]
        invalid = [setting for setting in normalized_settings if "=" not in setting]
        if invalid:
            raise VmCliError(
                "Each VM setting must be key=value. Invalid entries: " + ", ".join(invalid)
            )

        conf_path = self._vm_config_path(vm_name)
        args = ["-f", conf_path, *normalized_settings]
        output = self._run_external(self.sysrc_command, args)
        if output.return_code == 0:
            self._normalize_vm_config_file(conf_path)
        return output

    def host_summary(self) -> dict[str, dict[str, object]]:
        host_uname = self._run_safe_external("uname", ["-a"])
        host_sysctl = self._run_safe_external(
            "sysctl",
            ["kern.hostname", "kern.osrelease", "hw.model", "hw.machine", "hw.ncpu"],
        )
        memory_sysctl = self._run_safe_external(
            "sysctl",
            ["hw.physmem", "hw.realmem", "vm.stats.vm.v_free_count", "hw.pagesize"],
        )
        disk_df = self._run_safe_external("df", ["-h"])
        disk_zpool = self._run_safe_external("zpool", ["list"])
        network_ifconfig = self._run_safe_external("ifconfig", ["-a"])
        network_routes = self._run_safe_external("netstat", ["-rn"])

        memory_details = self._parse_sysctl_assignments(memory_sysctl.stdout)
        try:
            free_pages = int(memory_details.get("vm.stats.vm.v_free_count", "0"))
            page_size = int(memory_details.get("hw.pagesize", "0"))
            if free_pages > 0 and page_size > 0:
                memory_details["computed.free_bytes"] = str(free_pages * page_size)
        except ValueError:
            pass

        disk_df_summary = self._parse_df_h(disk_df.stdout)
        disk_zpool_summary = self._parse_zpool_list(disk_zpool.stdout)
        network_ifconfig_summary = self._parse_ifconfig(network_ifconfig.stdout)
        network_route_summary = self._parse_netstat_routes(network_routes.stdout)

        disk_details: dict[str, object] = {
            "filesystems": disk_df_summary.get("filesystems", []),
            "totals": disk_df_summary.get("totals", {}),
            "zpools": disk_zpool_summary.get("pools", []),
            "zpool_summary": disk_zpool_summary.get("summary", {}),
        }
        network_details: dict[str, object] = {
            "interfaces": network_ifconfig_summary.get("interfaces", []),
            "interface_summary": network_ifconfig_summary.get("summary", {}),
            "routes": network_route_summary.get("families", {}),
            "default_routes": network_route_summary.get("default_routes", []),
            "route_summary": network_route_summary.get("summary", {}),
        }

        return {
            "host": {
                "details": self._parse_sysctl_assignments(host_sysctl.stdout),
                "command_results": {
                    "uname": host_uname,
                    "sysctl": host_sysctl,
                },
            },
            "memory": {
                "details": memory_details,
                "command_results": {
                    "sysctl": memory_sysctl,
                },
            },
            "disk": {
                "details": disk_details,
                "command_results": {
                    "df": disk_df,
                    "zpool": disk_zpool,
                },
            },
            "network": {
                "details": network_details,
                "command_results": {
                    "ifconfig": network_ifconfig,
                    "netstat": network_routes,
                },
            },
        }
