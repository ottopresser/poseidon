import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import BytesIO, StringIO
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Response
from pydantic import ValidationError

import app.main as main
from app.audit import OperationAuditStore
from app.models import VmCreateRequest
from app.security import validate_console_token_ttl_seconds
from app.services.vm_cli import VmCliError, VmCliService, VmCommandOutput
from scripts import console_ws_client, deploy, install_freebsd_service as installer
from scripts import uninstall_freebsd_service as uninstaller
from scripts import upgrade


def create_payload(vm_name: str = "vm-01", storage_target: str = "/vm") -> dict[str, object]:
    return {
        "vm_name": vm_name,
        "resources": {"cpu": 1, "memory": "1G"},
        "disk": {"size": "10G", "storage_target": storage_target},
        "network": {"switch": "public"},
        "boot": {"source_type": "template", "source": "freebsd"},
    }


class ModelValidationTests(unittest.TestCase):
    def test_vm_name_policy(self) -> None:
        VmCreateRequest(**create_payload("vm-01_test.example"))

        for invalid_name in ("../escape", "-force", "a" * 65):
            with self.subTest(invalid_name=invalid_name), self.assertRaises(ValidationError):
                VmCreateRequest(**create_payload(invalid_name))

    def test_storage_target_must_be_absolute(self) -> None:
        with self.assertRaises(ValidationError):
            VmCreateRequest(**create_payload(storage_target="zroot/vm"))

    def test_invalid_create_resources_are_rejected(self) -> None:
        invalid_values = (
            ("resources", "memory", "0G"),
            ("resources", "memory", "not-a-size"),
            ("disk", "size", "0G"),
            ("network", "interface_type", "rtl8139"),
            ("boot", "source", ""),
        )
        for section, field, value in invalid_values:
            payload = create_payload()
            payload[section][field] = value
            with self.subTest(section=section, field=field), self.assertRaises(ValidationError):
                VmCreateRequest(**payload)


class VmCliServiceTests(unittest.TestCase):
    def test_storage_available_bytes_uses_requested_path(self) -> None:
        with TemporaryDirectory() as storage_path:
            service = VmCliService(vm_root_dir=storage_path)
            self.assertIsNotNone(service.storage_available_bytes(storage_path))
            self.assertIsNone(service.storage_available_bytes("/missing/poseidon/storage"))

    def test_normal_destroy_returns_success_outcome(self) -> None:
        service = VmCliService()
        with patch.object(
            service,
            "_run",
            return_value=VmCommandOutput([], 0, "", ""),
        ):
            output, disks_destroyed = service.destroy_vm("vm-01", destroy_disks=True)
        self.assertEqual(output.return_code, 0)
        self.assertTrue(disks_destroyed)

    def test_destroy_fallback_reports_disks_not_destroyed(self) -> None:
        service = VmCliService()
        outputs = [
            VmCommandOutput([], 1, "", "unknown option -- d"),
            VmCommandOutput([], 0, "", ""),
        ]
        with patch.object(service, "_run", side_effect=outputs) as run:
            output, disks_destroyed = service.destroy_vm("vm-01", destroy_disks=True)

        self.assertEqual(output.return_code, 0)
        self.assertFalse(disks_destroyed)
        self.assertEqual(run.call_count, 2)

    def test_list_failure_is_not_returned_as_empty_success(self) -> None:
        service = VmCliService()
        with patch.object(
            service,
            "_run",
            return_value=VmCommandOutput([], 1, "", "list failed"),
        ), self.assertRaisesRegex(VmCliError, "list failed"):
            service.list_vms()

    def test_concurrent_upload_has_one_winner(self) -> None:
        with TemporaryDirectory() as vm_root:
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=f"{vm_root}/media")

            def upload() -> bool:
                try:
                    service.upload_boot_media("shared.img", BytesIO(b"image"))
                    return True
                except VmCliError as exc:
                    self.assertEqual(str(exc), "Boot media already exists: shared.img")
                    return False

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _index: upload(), range(4)))

        self.assertEqual(results.count(True), 1)

    def test_referenced_shared_media_cannot_be_deleted(self) -> None:
        with TemporaryDirectory() as vm_root:
            media_dir = Path(vm_root) / "media"
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=str(media_dir))
            service.upload_boot_media("shared.img", BytesIO(b"image"))
            vm_dir = Path(vm_root) / "vm-01"
            vm_dir.mkdir()
            (vm_dir / "shared.img").symlink_to(media_dir / "shared.img")

            with self.assertRaisesRegex(VmCliError, "in use by VM: vm-01"):
                service.delete_boot_media("shared.img")

    def test_referenced_shared_media_cannot_be_overwritten(self) -> None:
        with TemporaryDirectory() as vm_root:
            media_dir = Path(vm_root) / "media"
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=str(media_dir))
            service.upload_boot_media("shared.img", BytesIO(b"original"))
            vm_dir = Path(vm_root) / "vm-01"
            vm_dir.mkdir()
            (vm_dir / "shared.img").symlink_to(media_dir / "shared.img")

            with self.assertRaisesRegex(VmCliError, "in use by VM: vm-01"):
                service.upload_boot_media("shared.img", BytesIO(b"replacement"), overwrite=True)
            self.assertEqual((media_dir / "shared.img").read_bytes(), b"original")

    def test_upload_limit_removes_partial_file(self) -> None:
        with TemporaryDirectory() as vm_root:
            service = VmCliService(
                vm_root_dir=vm_root,
                boot_media_dir=f"{vm_root}/media",
                max_upload_bytes=4,
            )
            with self.assertRaisesRegex(VmCliError, "exceeds maximum upload size"):
                service.upload_boot_media("large.img", BytesIO(b"12345"))
            self.assertFalse((Path(vm_root) / "media" / "large.img").exists())

    def test_empty_upload_is_rejected(self) -> None:
        with TemporaryDirectory() as vm_root:
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=f"{vm_root}/media")
            with self.assertRaisesRegex(VmCliError, "upload is empty"):
                service.upload_boot_media("empty.img", BytesIO(b""))

    def test_detach_preserves_file_backed_root_disk(self) -> None:
        with TemporaryDirectory() as vm_root:
            media_dir = Path(vm_root) / "media"
            media_dir.mkdir()
            media_path = media_dir / "shared.img"
            media_path.write_bytes(b"image")
            vm_dir = Path(vm_root) / "vm-01"
            vm_dir.mkdir()
            link_path = vm_dir / "shared.img"
            link_path.symlink_to(media_path)
            (vm_dir / "vm-01.conf").write_text(
                'disk0_dev="file"\ndisk0_name="shared.img"\n',
                encoding="utf-8",
            )
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=str(media_dir))

            result = service.detach_installer_media("vm-01")

            self.assertFalse(result["config_updated"])
            self.assertTrue(link_path.is_symlink())

    def test_failed_detach_preserves_installer_link(self) -> None:
        with TemporaryDirectory() as vm_root:
            media_dir = Path(vm_root) / "media"
            media_dir.mkdir()
            media_path = media_dir / "installer.img"
            media_path.write_bytes(b"image")
            vm_dir = Path(vm_root) / "vm-01"
            vm_dir.mkdir()
            link_path = vm_dir / "installer.img"
            link_path.symlink_to(media_path)
            (vm_dir / "vm-01.conf").write_text(
                'disk1_dev="file"\ndisk1_name="installer.img"\n',
                encoding="utf-8",
            )
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=str(media_dir))
            with patch.object(
                service,
                "_run_external",
                return_value=VmCommandOutput([], 1, "", "sysrc failed"),
            ), self.assertRaisesRegex(VmCliError, "sysrc failed"):
                service.detach_installer_media("vm-01")
            self.assertTrue(link_path.is_symlink())

    def test_media_link_does_not_replace_existing_vm_file(self) -> None:
        with TemporaryDirectory() as vm_root:
            media_dir = Path(vm_root) / "media"
            media_dir.mkdir()
            media_path = media_dir / "disk.img"
            media_path.write_bytes(b"media")
            vm_dir = Path(vm_root) / "vm-01"
            vm_dir.mkdir()
            vm_path = vm_dir / "disk.img"
            vm_path.write_bytes(b"vm-data")
            service = VmCliService(vm_root_dir=vm_root, boot_media_dir=str(media_dir))
            with self.assertRaisesRegex(VmCliError, "already exists"):
                service.link_boot_media_into_vm("vm-01", str(media_path))
            self.assertEqual(vm_path.read_bytes(), b"vm-data")

    def test_config_parser_preserves_hash_inside_quotes(self) -> None:
        config = VmCliService._parse_vm_config('vnc_password="abc#123" # comment\n')
        self.assertEqual(config["vnc_password"], "abc#123")

    def test_switch_list_failure_is_not_missing_switch(self) -> None:
        service = VmCliService()
        with patch.object(
            service,
            "_run",
            return_value=VmCommandOutput([], 1, "", "switch failed"),
        ), self.assertRaisesRegex(VmCliError, "switch failed"):
            service.list_switches()

    def test_metrics_parse_verbose_list_and_tap_counters(self) -> None:
        service = VmCliService()
        verbose = VmCommandOutput(
            [],
            0,
            "NAME DATASTORE LOADER CPU MEMORY VNC AUTO %CPU RSZ UPTIME STATE\n"
            "vm-01 default uefi 2 4G - No 12.5 512M 01:00 Running\n",
            "",
        )
        ifconfig = VmCommandOutput(
            [],
            0,
            "tap0: flags=8843<UP> mtu 1500\n"
            "\tdescription: vmnet/vm-01/0/public\n",
            "",
        )
        netstat = VmCommandOutput(
            [],
            0,
            "Name Mtu Network Address Ipkts Ierrs Idrop Ibytes Opkts Oerrs Obytes Coll\n"
            "tap0 1500 <Link#1> aa 10 0 0 1000 20 0 2000 0\n",
            "",
        )
        with patch.object(service, "_run", return_value=verbose), patch.object(
            service,
            "_run_external",
            side_effect=[ifconfig, netstat],
        ):
            _output, metrics = service.list_vm_metrics()
        self.assertEqual(metrics[0]["cpu_percent"], 12.5)
        self.assertEqual(metrics[0]["resident_memory_bytes"], 512 * 1024**2)
        self.assertEqual(metrics[0]["network"][0]["rx_bytes"], 1000)

    def test_aligned_vm_list_preserves_autostart_and_state_fields(self) -> None:
        rows = VmCliService._parse_table(
            "NAME   DATASTORE  AUTO     %CPU  STATE\n"
            "vm-01  default    Yes [1]  12.5  Running (123)\n"
        )
        self.assertEqual(rows[0]["AUTO"], "Yes [1]")
        self.assertEqual(rows[0]["STATE"], "Running (123)")

    def test_management_commands_match_vm_bhyve_contract(self) -> None:
        service = VmCliService()
        with patch.object(
            service,
            "_run",
            return_value=VmCommandOutput([], 0, "", ""),
        ) as run:
            service.snapshot_vm("vm-01", "clean", force=True)
            service.rollback_vm_snapshot("vm-01", "clean", destroy_newer=True)
            service.clone_vm("vm-01", "vm-02", "clean")
            service.add_vm_disk("vm-01", "file", "10G")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["snapshot", "-f", "vm-01@clean"],
                ["rollback", "-r", "vm-01@clean"],
                ["clone", "vm-01@clean", "vm-02"],
                ["add", "-d", "disk", "-t", "file", "-s", "10G", "vm-01"],
            ],
        )

    def test_disk_inventory_and_detach_preserve_data(self) -> None:
        with TemporaryDirectory() as vm_root:
            vm_dir = Path(vm_root) / "vm-01"
            vm_dir.mkdir()
            disk_path = vm_dir / "disk1.img"
            disk_path.write_bytes(b"disk-data")
            (vm_dir / "vm-01.conf").write_text(
                'disk0_name="disk0.img"\n'
                'disk0_dev="file"\n'
                'disk1_name="disk1.img"\n'
                'disk1_dev="file"\n',
                encoding="utf-8",
            )
            service = VmCliService(vm_root_dir=vm_root)
            inventory = service.vm_disk_inventory("vm-01")
            self.assertEqual([item["index"] for item in inventory], [0, 1])
            with patch.object(service, "vm_state", return_value="Stopped"), patch.object(
                service,
                "_run_external",
                return_value=VmCommandOutput([], 0, "", ""),
            ):
                removed = service.detach_vm_disk("vm-01", 1)
            self.assertIn("disk1_name", removed)
            self.assertEqual(disk_path.read_bytes(), b"disk-data")

    def test_readiness_reports_command_errors(self) -> None:
        service = VmCliService()
        with patch.object(service, "_run", side_effect=VmCliError("missing vm")):
            checks = service.readiness_checks()
        self.assertFalse(any(check["ready"] for check in checks))


class StubVmService:
    def __init__(self, state: str | None = "Stopped", cleanup_fails: bool = False) -> None:
        self.state = state
        self.cleanup_fails = cleanup_fails
        self.calls: list[tuple[object, ...]] = []
        self.exists_checks = 0

    def info_vm(self, vm_name: str) -> VmCommandOutput:
        return VmCommandOutput([], 0, "", "")

    def vm_exists(self, vm_name: str) -> bool:
        self.exists_checks += 1
        return self.exists_checks == 1

    def stop_vm(self, vm_name: str, force: bool = False) -> VmCommandOutput:
        self.calls.append(("stop", vm_name, force))
        return VmCommandOutput([], 1, "", "already stopped")

    def vm_state(self, vm_name: str) -> str | None:
        return self.state

    def is_active_vm_state(self, state: str | None) -> bool:
        return bool(state and state.lower().startswith("running"))

    def destroy_vm(
        self,
        vm_name: str,
        force: bool = False,
        destroy_disks: bool = False,
    ) -> tuple[VmCommandOutput, bool]:
        self.calls.append(("destroy", vm_name, force, destroy_disks))
        return VmCommandOutput([], 0, "", ""), destroy_disks

    def remove_vm_autostart_on_boot(self, vm_name: str) -> None:
        self.calls.append(("autostart", vm_name))
        if self.cleanup_fails:
            raise VmCliError("rc.conf locked")


class MainOperationTests(unittest.TestCase):
    def test_create_failure_rolls_back(self) -> None:
        service = StubVmService()
        with patch.object(main, "vm_service", service), self.assertRaises(HTTPException) as raised:
            main._raise_create_failure("vm-01", 400, "configure failed")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("was rolled back", raised.exception.detail)
        self.assertIn(("destroy", "vm-01", True, True), service.calls)

    def test_delete_continues_when_vm_is_already_stopped(self) -> None:
        service = StubVmService(cleanup_fails=True)
        with patch.object(main, "vm_service", service):
            response = main.vm_delete("vm-01", destroy_disks=True)

        self.assertTrue(response.result.stopped)
        self.assertTrue(response.result.disks_destroyed)
        self.assertEqual(response.warnings[0].code, "AUTOSTART_CLEANUP_FAILED")

    def test_delete_does_not_destroy_active_vm_after_stop_failure(self) -> None:
        service = StubVmService(state="Running")
        with patch.object(main, "vm_service", service), self.assertRaises(HTTPException) as raised:
            main.vm_delete("vm-01", force=True)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(any(call[0] == "destroy" for call in service.calls))

    def test_validation_only_reports_failed_hard_checks(self) -> None:
        payload = VmCreateRequest(**{**create_payload(), "options": {"validate_only": True}})
        checks = main.VmCreateChecks(
            cpu={"gate": "soft", "result": "pass"},
            memory={"gate": "soft", "result": "pass"},
            disk={"gate": "hard", "result": "fail"},
            network={"gate": "hard", "result": "pass"},
        )
        response = Response()
        with patch.object(main.vm_service, "vm_exists", return_value=False), patch.object(
            main,
            "_build_create_checks",
            return_value=(checks, [], True),
        ):
            result = main.vm_create(payload, response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result.status, "validation_failed")

    def test_new_endpoint_families_are_in_openapi(self) -> None:
        expected = {
            "/ready",
            "/v1/networks",
            "/v1/operations",
            "/v1/vms/metrics",
            "/v1/vms/{vm_name}/metrics",
            "/v1/vms/{vm_name}/snapshots",
            "/v1/vms/{vm_name}/snapshots/rollback",
            "/v1/vms/{vm_name}/clone",
            "/v1/vms/{vm_name}/disks",
            "/v1/vms/{vm_name}/disks/{disk_index}",
        }
        self.assertLessEqual(expected, main.app.openapi()["paths"].keys())

    def test_stopped_guard_rejects_running_vm(self) -> None:
        service = MagicMock()
        service.vm_exists.return_value = True
        service.vm_state.return_value = "Running (123)"
        with patch.object(main, "vm_service", service), self.assertRaises(HTTPException) as raised:
            main._require_vm_stopped("vm-01")
        self.assertEqual(raised.exception.status_code, 409)


class AuditTests(unittest.TestCase):
    def test_audit_records_are_bounded_and_reload(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            store = OperationAuditStore(max_records=2, log_path=str(path))
            for index in range(3):
                store.record(
                    method="GET",
                    path=f"/v1/{index}",
                    status_code=200,
                    duration_ms=1.0,
                    client="local",
                )
            self.assertEqual(len(store.list_records(10)), 2)
            self.assertEqual(len(OperationAuditStore(max_records=2, log_path=str(path)).list_records(10)), 2)


class FakeWebSocket:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        self.headers = {"x-api-key": "test-key"} if headers is None else headers
        self.query_params = {} if query_params is None else query_params
        self.close_code: int | None = None

    async def accept(self) -> None:
        pass

    async def receive(self) -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    async def send_text(self, text: str) -> None:
        pass

    async def close(self, code: int, reason: str | None = None) -> None:
        self.close_code = code


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.pid = 1
        self.stdin = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return 0


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_stdin_read_can_be_cancelled(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            task = asyncio.create_task(
                console_ws_client._read_stdin(asyncio.get_running_loop(), read_fd)
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_websocket_closes_when_console_process_exits(self) -> None:
        async def create_process(*args, **kwargs) -> FakeProcess:
            return FakeProcess()

        websocket = FakeWebSocket()
        with patch.dict(os.environ, {"POSEIDON_API_KEY": "test-key"}), patch.object(
            main.vm_service, "vm_exists", return_value=True
        ), patch.object(main.asyncio, "create_subprocess_exec", new=create_process):
            await asyncio.wait_for(main.vm_console_ws(websocket, "vm-01"), timeout=1)

        self.assertEqual(websocket.close_code, 1000)

    async def test_websocket_rejects_api_key_query_parameter(self) -> None:
        websocket = FakeWebSocket(headers={}, query_params={"api_key": "test-key"})
        with patch.dict(os.environ, {"POSEIDON_API_KEY": "test-key"}):
            await main.vm_console_ws(websocket, "vm-01")
        self.assertEqual(websocket.close_code, 1008)

    def test_console_client_keeps_api_key_out_of_url(self) -> None:
        ws_url = console_ws_client._build_ws_url(
            "localhost",
            8000,
            "vm-01",
            "root-secret",
            None,
            False,
            False,
        )
        self.assertEqual(ws_url, "ws://localhost:8000/v1/vms/vm-01/console/ws")


class DeploymentTests(unittest.TestCase):
    def test_rendered_service_keeps_secrets_out_of_command_args(self) -> None:
        rendered = deploy.RCD_SCRIPT_TEMPLATE.format(
            service_name="poseidon",
            remote_dir="/usr/local/poseidon",
            host="127.0.0.1",
            port=8000,
            run_user="root",
        )
        command_line = next(line for line in rendered.splitlines() if line.startswith("command_args="))
        self.assertNotIn("POSEIDON_API_KEY=", command_line)
        self.assertIn("export POSEIDON_API_KEY=", rendered)

    def test_local_installer_redacts_secrets(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            installer.set_rc_conf_value(Path("/etc/rc.conf"), "poseidon_api_key", "secret", True)
        self.assertNotIn("secret", output.getvalue())
        self.assertIn("<redacted>", output.getvalue())

    def test_deploy_defaults_to_preview_mode(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["deploy.py", "--skip-freebsd-service", "--non-interactive"],
        ), patch.object(
            deploy,
            "deploy_to_server",
        ) as deploy_to_server, patch.dict(os.environ, {}, clear=True), redirect_stdout(StringIO()):
            deploy.main()
        self.assertTrue(deploy_to_server.called)
        self.assertTrue(all(call.args[-1] is True for call in deploy_to_server.call_args_list))

    def test_uninstall_uses_sysrc_to_remove_keys(self) -> None:
        with patch.object(uninstaller, "run_command") as run:
            uninstaller.remove_rc_conf_keys(Path("/etc/rc.conf"), ["poseidon_api_key"], True)
        run.assert_called_once_with(
            ["sysrc", "-f", "/etc/rc.conf", "-x", "poseidon_api_key"],
            dry_run=True,
            check=False,
        )

    def test_console_token_ttl_bounds(self) -> None:
        self.assertEqual(validate_console_token_ttl_seconds(60), 60)
        for ttl in (4, 3601):
            with self.subTest(ttl=ttl), self.assertRaises(HTTPException):
                validate_console_token_ttl_seconds(ttl)

    def test_deploy_selects_user_and_reuses_generated_credentials(self) -> None:
        self.assertEqual(
            deploy.apply_ssh_user(["host1", "root@host2"], "admin"),
            ["admin@host1", "admin@host2"],
        )
        with TemporaryDirectory() as temp:
            path = Path(temp) / "deploy.env"
            with patch.object(
                deploy.secrets,
                "token_urlsafe",
                side_effect=["api", "admin", "console"],
            ):
                first = deploy.resolve_credentials(None, None, None, path)
            second = deploy.resolve_credentials(None, None, None, path)
            self.assertEqual(first, ("api", "admin", "console", True))
            self.assertEqual(second, ("api", "admin", "console", False))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class UpgradeTests(unittest.TestCase):
    def test_upgrade_preview_never_executes_commands(self) -> None:
        with patch.object(sys, "argv", ["upgrade.py", "--server", "root@vmhost1"]), patch.object(
            upgrade.subprocess,
            "run",
        ) as run, redirect_stdout(StringIO()):
            upgrade.main()
        run.assert_not_called()

    def test_preflight_only_runs_one_read_only_remote_check(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["upgrade.py", "--server", "root@vmhost1", "--preflight-only"],
        ), patch.object(upgrade.subprocess, "run") as run, redirect_stdout(StringIO()):
            upgrade.main()

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["ssh", "root@vmhost1", "sh", "-s"])
        script = run.call_args.kwargs["input"]
        self.assertNotIn("mkdir -p", script)
        self.assertNotIn("pip install", script)

    def test_multi_server_apply_requires_explicit_confirmation(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "upgrade.py",
                "--server",
                "root@vmhost1",
                "--server",
                "root@vmhost2",
                "--apply",
            ],
        ), self.assertRaises(SystemExit), redirect_stdout(StringIO()):
            upgrade.main()

    def test_preflight_requires_credentials_and_local_bind(self) -> None:
        script = upgrade.preflight_script(
            "poseidon",
            "/usr/local/poseidon-releases",
            "/usr/local/poseidon-releases/release-1",
            False,
        )
        self.assertIn("Missing required rc.conf key", script)
        self.assertIn("Refusing non-local service bind", script)
        self.assertIn("python3 -m ensurepip --version", script)

    def test_promotion_rolls_back_service_directory_on_failure(self) -> None:
        script = upgrade.promotion_script(
            "poseidon",
            "/usr/local/poseidon-releases/release-1",
        )
        self.assertIn('old_dir=$(sysrc -n poseidon_dir)', script)
        self.assertIn('sysrc poseidon_dir="$old_dir"', script)
        self.assertIn('/health', script)
        self.assertIn('while [ "$attempts" -lt 30 ]', script)
        self.assertIn("sleep 1", script)

    def test_upgrade_command_failure_has_no_python_traceback(self) -> None:
        with patch.object(
            upgrade.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(1, ["ssh", "root@vmhost1"]),
        ), self.assertRaisesRegex(SystemExit, "exit status 1"):
            upgrade.run_command(["ssh", "root@vmhost1"], False)

    def test_bootstrap_validates_required_openapi_paths(self) -> None:
        script = upgrade.bootstrap_script(
            "poseidon",
            "/usr/local/poseidon-releases/release-1",
        )
        self.assertIn('required_paths = {"/health", "/v1/vms", "/v1/vms/create"}', script)
        self.assertNotIn('schema["info"]["title"]', script)


if __name__ == "__main__":
    unittest.main()