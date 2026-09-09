from __future__ import annotations

import contextlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

import install


class SecretsTest(unittest.TestCase):
    def test_reads_yaml_string_styles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text(
                "plain: plain value\n"
                'double: "double value"\n'
                "single: 'single value'\n"
                "apostrophe: 'owner''s network'\n",
                encoding="utf-8",
            )

            self.assertEqual(
                install.read_yaml_secrets(path, Path(sys.executable)),
                {
                    "plain": "plain value",
                    "double": "double value",
                    "single": "single value",
                    "apostrophe": "owner's network",
                },
            )

    def test_yaml_quotes_and_comments_match_yaml_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text(
                "wifi_ssid: 'My: WiFi' # saved network\n"
                'web_password: "hash # inside quotes" # comment\n',
                encoding="utf-8",
            )

            self.assertEqual(
                install.read_yaml_secrets(path, Path(sys.executable)),
                {
                    "wifi_ssid": "My: WiFi",
                    "web_password": "hash # inside quotes",
                },
            )

    def test_rejects_non_string_yaml_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text("wifi_ssid: true\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "must be a string"):
                install.read_yaml_secrets(path, Path(sys.executable))

    def test_secures_existing_secrets_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text("wifi_ssid: test\n", encoding="utf-8")
            path.chmod(0o644)

            install.load_or_create_secrets(path, Path(sys.executable))

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_generated_secrets_include_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            answers = iter(["TestWiFi", "wifi-password", "Europe/Warsaw", "ap-password", "admin", "web-password"])
            with mock.patch.object(install, "prompt", side_effect=answers):
                values = install.write_secrets(path)

            self.assertEqual(values["timezone"], "Europe/Warsaw")
            self.assertEqual(
                install.read_yaml_secrets(path, Path(sys.executable))["timezone"],
                "Europe/Warsaw",
            )


class CheckoutTest(unittest.TestCase):
    def make_checkout(self, parent: Path, name: str, origin: str) -> Path:
        path = parent / name
        path.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
        for marker in install.REPOSITORY_MARKERS[name]:
            marker_path = path / marker
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.touch()
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test checkout",
            ],
            cwd=path,
            check=True,
        )
        return path

    def test_accepts_expected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            expected = self.make_checkout(
                parent,
                "petkit-compat-server",
                "git@github.com:wrobelda/petkit-compat-server.git",
            )

            actual = install.ensure_checkout(
                parent,
                "petkit-compat-server",
                install.REPOSITORIES["petkit-compat-server"],
                "HEAD",
            )

            self.assertEqual(actual, expected)

    def test_accepts_expected_kickstart_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            expected = self.make_checkout(
                parent,
                "esphome-kickstart",
                "https://github.com/wrobelda/esphome-kickstart.git",
            )

            actual = install.ensure_checkout(
                parent,
                "esphome-kickstart",
                install.REPOSITORIES["esphome-kickstart"],
                "HEAD",
            )

            self.assertEqual(actual, expected)

    def test_rejects_unrelated_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            self.make_checkout(
                parent,
                "petkit-compat-server",
                "https://github.com/example/unrelated.git",
            )

            with self.assertRaisesRegex(RuntimeError, "unexpected origin"):
                install.ensure_checkout(
                    parent,
                    "petkit-compat-server",
                    install.REPOSITORIES["petkit-compat-server"],
                    "HEAD",
                )

    def test_rejects_non_checkout_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / "petkit-compat-server").mkdir()

            with self.assertRaisesRegex(RuntimeError, "not a Git checkout"):
                install.ensure_checkout(
                    parent,
                    "petkit-compat-server",
                    install.REPOSITORIES["petkit-compat-server"],
                    "HEAD",
                )

    def test_rejects_checkout_at_another_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            checkout = self.make_checkout(
                parent,
                "petkit-compat-server",
                "https://github.com/wrobelda/petkit-compat-server.git",
            )
            first = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (checkout / "later").touch()
            subprocess.run(["git", "add", "later"], cwd=checkout, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "later",
                ],
                cwd=checkout,
                check=True,
            )

            with self.assertRaisesRegex(RuntimeError, "requires"):
                install.ensure_checkout(
                    parent,
                    "petkit-compat-server",
                    install.REPOSITORIES["petkit-compat-server"],
                    first,
                )


class ProcessOutputTest(unittest.TestCase):
    def test_run_hides_successful_command_output_by_default(self) -> None:
        result = subprocess.CompletedProcess(
            ["tool"], 0, stdout="diagnostic output\n", stderr=""
        )
        with mock.patch.object(
            install.subprocess, "run", return_value=result
        ) as subprocess_run:
            install.run(["tool"], cwd=Path("."))

        self.assertTrue(subprocess_run.call_args.kwargs["capture_output"])
        self.assertTrue(subprocess_run.call_args.kwargs["text"])

    def test_run_prints_captured_output_when_command_fails(self) -> None:
        result = subprocess.CompletedProcess(
            ["tool"], 2, stdout="build context\n", stderr="build failed\n"
        )
        with (
            mock.patch.object(install.subprocess, "run", return_value=result),
            mock.patch.object(install.sys, "stderr") as stderr,
        ):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                install.run(["tool"], cwd=Path("."))

        self.assertEqual(raised.exception.stdout, "build context\n")
        self.assertEqual(raised.exception.stderr, "build failed\n")
        self.assertEqual(
            [call.args[0] for call in stderr.write.call_args_list],
            ["build context", "\n", "build failed", "\n"],
        )

    def test_run_shows_command_and_inherits_output_in_debug_mode(self) -> None:
        with mock.patch.object(install.subprocess, "run") as subprocess_run:
            install.run(["tool", "argument"], cwd=Path("."), debug=True)

        self.assertNotIn("capture_output", subprocess_run.call_args.kwargs)
        self.assertTrue(subprocess_run.call_args.kwargs["check"])


class RecoveryDownloadTest(unittest.TestCase):
    def test_creates_private_recovery_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.bin"

            def write_download(arguments: list[str], **_kwargs: object) -> None:
                Path(arguments[1]).write_bytes(b"recovery")

            with mock.patch.object(
                install, "run_authenticated_curl", side_effect=write_download
            ):
                install.download_recovery(
                    path,
                    url="http://192.0.2.1/hub/flash_read",
                    username="admin",
                    password="secret",
                    cwd=Path(directory),
                )

            self.assertEqual(path.read_bytes(), b"recovery")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_removes_partial_recovery_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.bin"
            with mock.patch.object(
                install,
                "run_authenticated_curl",
                side_effect=subprocess.CalledProcessError(22, ["curl"]),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    install.download_recovery(
                        path,
                        url="http://192.0.2.1/hub/flash_read",
                        username="admin",
                        password="secret",
                        cwd=Path(directory),
                    )

            self.assertFalse(path.exists())


class BuildArtifactTest(unittest.TestCase):
    def test_accepts_nonempty_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.bin"
            path.write_bytes(b"firmware")
            install.require_build_artifact(path, "test image")

    def test_rejects_missing_or_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.bin"
            with self.assertRaisesRegex(RuntimeError, "test image was not created"):
                install.require_build_artifact(path, "test image")
            path.touch()
            with self.assertRaisesRegex(RuntimeError, "test image was not created"):
                install.require_build_artifact(path, "test image")


class ServerRequestTest(unittest.TestCase):
    def test_recognizes_stock_device_request_but_not_readiness_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            path.write_text(
                '{"event":"ota_offer"}\n'
                "listening on 0.0.0.0:8080\n"
                '{"event":"readiness_check","path":"/"}\n'
                '{"event":"request","method":"POST",'
                '"path":"/6/feedermini/dev_ota_check"}\n',
                encoding="utf-8",
            )

            self.assertFalse(
                install.server_log_has_event(path, "request", {"method": "GET"})
            )
            self.assertTrue(
                install.server_log_has_event(
                    path,
                    "request",
                    {
                        "method": "POST",
                        "path": "/6/feedermini/dev_ota_check",
                    },
                )
            )

    def test_absent_request_is_not_prior_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            path.write_text('{"event":"ota_offer"}\n', encoding="utf-8")

            self.assertFalse(install.server_log_has_event(path, "request"))

    def test_matches_required_event_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.log"
            path.write_text(
                '{"event":"ota_transfer_complete","image_complete":true}\n',
                encoding="utf-8",
            )

            self.assertTrue(
                install.server_log_has_event(
                    path,
                    "ota_transfer_complete",
                    {"image_complete": True},
                )
            )


class DeviceIdentityTest(unittest.TestCase):
    def test_reads_authenticated_project_identity(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                '{"mac_address":"E8:68:E7:00:00:01",'
                '"project_name":"petkit.fresh-element-mini"}'
            ),
            stderr="",
        )
        with mock.patch.object(install.subprocess, "run", return_value=result) as run:
            identity = install.read_device_identity(
                Path("python"), Path("/project"), "192.0.2.10", "private-api-key"
            )

        assert identity is not None
        self.assertEqual(identity.project_name, "petkit.fresh-element-mini")
        command = run.call_args.args[0]
        self.assertNotIn("private-api-key", command)
        self.assertEqual(
            run.call_args.kwargs["env"]["ESPHOME_API_KEY"], "private-api-key"
        )

    def test_unreachable_device_returns_none(self) -> None:
        result = subprocess.CompletedProcess([], 4, stdout="", stderr="")
        with mock.patch.object(install.subprocess, "run", return_value=result):
            self.assertIsNone(
                install.read_device_identity(
                    Path("python"), Path("/project"), "missing.local", "key"
                )
            )

    def test_api_connection_failure_is_not_unreachable(self) -> None:
        result = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="ESPHome API connection failed"
        )
        with mock.patch.object(install.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                install.read_device_identity(
                    Path("python"), Path("/project"), "wrong.local", "key"
                )


class FirmwarePhaseTest(unittest.TestCase):
    def identity(
        self,
        project_name: str,
        mac: str = "E8:68:E7:00:00:01",
    ) -> install.DeviceIdentity:
        return install.DeviceIdentity(
            mac_address=mac,
            project_name=project_name,
        )

    def test_detects_final_project_by_authenticated_identity(self) -> None:
        final = self.identity(install.FINAL_PROJECT)
        with mock.patch.object(install, "read_device_identity", return_value=final):
            detected = install.detect_running_firmware(
                Path("python"), Path("/project"), ["192.0.2.10"], "key", None
            )

        assert detected is not None
        self.assertEqual(detected.identity.project_name, install.FINAL_PROJECT)

    def test_rejects_a_different_physical_device(self) -> None:
        final = self.identity(install.FINAL_PROJECT, "E8:68:E7:00:00:02")
        with mock.patch.object(install, "read_device_identity", return_value=final):
            with self.assertRaises(install.DeviceMismatchError):
                install.detect_running_firmware(
                    Path("python"),
                    Path("/project"),
                    ["192.0.2.10"],
                    "key",
                    "e868e7000001",
                )

    def test_waits_past_kickstart_for_final_project(self) -> None:
        kickstart = install.DetectedFirmware(
            "192.0.2.10", self.identity(install.KICKSTART_PROJECT)
        )
        final = install.DetectedFirmware(
            "192.0.2.10", self.identity(install.FINAL_PROJECT)
        )
        with (
            mock.patch.object(
                install, "detect_running_firmware", side_effect=[kickstart, final]
            ),
            mock.patch.object(install.time, "sleep"),
        ):
            detected = install.wait_for_firmware(
                Path("python"),
                Path("/project"),
                ["192.0.2.10"],
                "key",
                install.FINAL_PROJECT,
                "e868e7000001",
                timeout=5,
            )

        self.assertEqual(detected.identity.project_name, install.FINAL_PROJECT)

    def test_install_state_preserves_expected_mac_privately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "install-state.json"
            install.save_expected_mac(path, "E8:68:E7:00:00:01")

            self.assertEqual(install.load_expected_mac(path), "e868e7000001")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class WifiDetectionTest(unittest.TestCase):
    def test_lists_unique_networkmanager_ssids(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout="PETKIT_FEEDER_B\nHome\nPETKIT_FEEDER_A\nPETKIT_FEEDER_B\n",
            stderr="",
        )
        with (
            mock.patch.object(install.shutil, "which", return_value="/usr/bin/nmcli"),
            mock.patch.object(install.subprocess, "run", return_value=result),
        ):
            self.assertEqual(
                install.available_wifi_ssids(),
                ["Home", "PETKIT_FEEDER_A", "PETKIT_FEEDER_B"],
            )

    def test_reads_active_networkmanager_ssid(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout=" :Home\n*:PETKIT_FEEDER_A\n",
            stderr="",
        )
        with (
            mock.patch.object(install.shutil, "which", return_value="/usr/bin/nmcli"),
            mock.patch.object(install.subprocess, "run", return_value=result),
        ):
            self.assertEqual(install.current_wifi_ssid(), "PETKIT_FEEDER_A")

    def test_reads_active_macos_ssid(self) -> None:
        ports = subprocess.CompletedProcess(
            [],
            0,
            stdout="Hardware Port: Wi-Fi\nDevice: en0\nEthernet Address: test\n",
            stderr="",
        )
        current = subprocess.CompletedProcess(
            [], 0, stdout="Current Wi-Fi Network: PETKIT_FEEDER_A\n", stderr=""
        )
        with (
            mock.patch.object(
                install.shutil,
                "which",
                side_effect=lambda name: (
                    "/usr/sbin/networksetup" if name == "networksetup" else None
                ),
            ),
            mock.patch.object(
                install.subprocess, "run", side_effect=[ports, current]
            ),
        ):
            self.assertEqual(install.current_wifi_ssid(), "PETKIT_FEEDER_A")

    def test_lists_macos_ssids(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                '{"SPAirPortDataType":[{"spairport_airport_interfaces":[{'
                '"spairport_other_local_wireless_networks":['
                '{"_name":"Home"},{"_name":"PETKIT_FEEDER_A"}]}]}]}'
            ),
            stderr="",
        )
        with (
            mock.patch.object(
                install.shutil,
                "which",
                side_effect=lambda name: (
                    "/usr/sbin/system_profiler" if name == "system_profiler" else None
                ),
            ),
            mock.patch.object(install.subprocess, "run", return_value=result),
        ):
            self.assertEqual(
                install.available_wifi_ssids(), ["Home", "PETKIT_FEEDER_A"]
            )

    def test_failed_networkmanager_scan_is_unavailable(self) -> None:
        result = subprocess.CompletedProcess([], 10, stdout="", stderr="failed")
        with (
            mock.patch.object(install.shutil, "which", return_value="/usr/bin/nmcli"),
            mock.patch.object(install.subprocess, "run", return_value=result),
        ):
            with self.assertRaises(install.WifiDetectionUnavailable):
                install.available_wifi_ssids()

    def test_wifi_detection_requires_a_supported_platform_tool(self) -> None:
        with mock.patch.object(install.shutil, "which", return_value=None):
            with self.assertRaises(install.WifiDetectionUnavailable):
                install.available_wifi_ssids()

    def test_waits_until_a_petkit_network_appears(self) -> None:
        with (
            mock.patch.object(
                install,
                "available_wifi_ssids",
                side_effect=[["Home"], ["PETKIT_PURIFIER", "PETKIT_FEEDER_A"]],
            ),
            mock.patch.object(install.time, "sleep"),
        ):
            self.assertEqual(
                install.wait_for_available_wifi_networks("PETKIT", timeout=5),
                ["PETKIT_PURIFIER", "PETKIT_FEEDER_A"],
            )

    def test_waits_for_the_selected_network_not_any_petkit_network(self) -> None:
        with (
            mock.patch.object(
                install,
                "current_wifi_ssid",
                side_effect=["PETKIT_PURIFIER", "PETKIT_FEEDER_A"],
            ),
            mock.patch.object(install.time, "sleep"),
        ):
            self.assertEqual(
                install.wait_for_wifi_network("PETKIT_FEEDER_A", timeout=5),
                "PETKIT_FEEDER_A",
            )

    def test_chooses_one_of_multiple_petkit_networks(self) -> None:
        with mock.patch.object(install, "prompt", return_value="2"):
            self.assertEqual(
                install.choose_wifi_network(
                    ["PETKIT_PURIFIER", "PETKIT_FEEDER_A"]
                ),
                "PETKIT_FEEDER_A",
            )

    def test_falls_back_to_manual_confirmation_when_detection_is_unavailable(self) -> None:
        with (
            mock.patch.object(
                install,
                "wait_for_available_wifi_networks",
                side_effect=install.WifiDetectionUnavailable("unsupported"),
            ),
            mock.patch("builtins.input", return_value="") as user_input,
        ):
            install.connect_to_petkit_setup_network()

        user_input.assert_called_once()


class OrchestrationResultTest(unittest.TestCase):
    def test_provisioner_accepts_only_confirmed_or_indeterminate_commit(self) -> None:
        with mock.patch.object(
            install.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 3),
                subprocess.CompletedProcess([], 2),
            ],
        ):
            self.assertTrue(
                install.run_provisioner([], cwd=Path("."), env={})
            )
            self.assertFalse(
                install.run_provisioner([], cwd=Path("."), env={})
            )
            with self.assertRaises(subprocess.CalledProcessError):
                install.run_provisioner([], cwd=Path("."), env={})

    def test_only_transport_loss_makes_migration_result_ambiguous(self) -> None:
        self.assertTrue(
            install.migration_result_is_ambiguous(
                subprocess.CalledProcessError(56, ["curl"])
            )
        )
        self.assertFalse(
            install.migration_result_is_ambiguous(
                subprocess.CalledProcessError(22, ["curl"])
            )
        )


class EntrypointTest(unittest.TestCase):
    def test_expected_timeout_exits_without_reraising(self) -> None:
        with (
            mock.patch.object(
                install,
                "main",
                side_effect=install.InstallationTimeout("download took too long"),
            ),
            mock.patch("builtins.print") as output,
        ):
            self.assertEqual(install.cli(), 1)

        output.assert_called_once_with(
            "\n⏱️  Installation timed out: download took too long",
            file=sys.stderr,
        )


class ServerEventWaitTest(unittest.TestCase):
    def test_prints_delayed_guidance_once(self) -> None:
        server = mock.Mock()
        server.poll.return_value = None
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                install.time,
                "monotonic",
                side_effect=[0, 0, 0, 0, 31, 36],
            ),
            mock.patch.object(install.time, "sleep"),
            mock.patch("builtins.print") as output,
        ):
            self.assertFalse(
                install.wait_for_server_event(
                    Path(directory) / "server.log",
                    server,
                    "request",
                    timeout=35,
                    delayed_message="Open the status page in a browser.",
                )
            )

        output.assert_called_once_with("\nOpen the status page in a browser.")


class MainResumeTest(unittest.TestCase):
    SECRETS = {
        "wifi_ssid": "test-network",
        "wifi_password": "test-password",
        "timezone": "Europe/Warsaw",
        "fallback_ap_password": "test-fallback-password",
        "kickstart_web_username": "admin",
        "kickstart_web_password": "test-web-password",
        "api_key": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=",
    }

    @staticmethod
    def identity(project_name: str) -> install.DeviceIdentity:
        return install.DeviceIdentity(
            mac_address="E8:68:E7:00:00:01",
            project_name=project_name,
        )

    def common_patches(self, project: Path) -> tuple[mock._patch, ...]:
        compat = project.parent / "petkit-compat-server"
        kickstart = project.parent / "esphome-kickstart"
        compat.mkdir()
        kickstart.mkdir()

        def checkout(
            _parent: Path,
            name: str,
            _url: str,
            _revision: str,
            **_kwargs: object,
        ) -> Path:
            if name == "petkit-compat-server":
                return compat
            return kickstart

        return (
            mock.patch.object(install, "__file__", str(project / "install.py")),
            mock.patch.object(install, "ensure_checkout", side_effect=checkout),
            mock.patch.object(install.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(install, "run"),
            mock.patch.object(
                install,
                "load_or_create_secrets",
                return_value=self.SECRETS,
            ),
            mock.patch.object(install, "require_build_artifact"),
            mock.patch.object(sys, "argv", ["install.py"]),
        )

    def test_rerun_stops_when_final_firmware_is_authenticated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "petkit-element-mini-esphome"
            (project / "esphome").mkdir(parents=True)
            final = install.DetectedFirmware(
                "petkit-feeder.local", self.identity(install.FINAL_PROJECT)
            )
            patches = self.common_patches(project)
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(
                        install, "detect_running_firmware", return_value=final
                    )
                )
                popen = stack.enter_context(
                    mock.patch.object(install.subprocess, "Popen")
                )
                download = stack.enter_context(
                    mock.patch.object(install, "download_recovery")
                )
                upload = stack.enter_context(
                    mock.patch.object(install, "run_authenticated_curl")
                )
                install.main()

            popen.assert_not_called()
            download.assert_not_called()
            upload.assert_not_called()
            self.assertEqual(
                install.load_expected_mac(project / install.INSTALL_STATE),
                "e868e7000001",
            )

    def test_rerun_from_kickstart_skips_stock_provisioning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "petkit-element-mini-esphome"
            (project / "esphome").mkdir(parents=True)
            kickstart = install.DetectedFirmware(
                "petkit-kickstart.local", self.identity(install.KICKSTART_PROJECT)
            )
            final = install.DetectedFirmware(
                "petkit-feeder.local", self.identity(install.FINAL_PROJECT)
            )

            def download(path: Path, **_kwargs: object) -> None:
                path.write_bytes(b"R" * 0x200000)

            patches = self.common_patches(project)
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(
                        install, "detect_running_firmware", return_value=kickstart
                    )
                )
                wait = stack.enter_context(
                    mock.patch.object(
                        install, "wait_for_firmware", return_value=final
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install, "download_recovery", side_effect=download
                    )
                )
                upload = stack.enter_context(
                    mock.patch.object(install, "run_authenticated_curl")
                )
                popen = stack.enter_context(
                    mock.patch.object(install.subprocess, "Popen")
                )
                provision = stack.enter_context(
                    mock.patch.object(install, "run_provisioner")
                )
                install.main()

            popen.assert_not_called()
            provision.assert_not_called()
            upload.assert_called_once()
            self.assertEqual(wait.call_args.args[4], install.FINAL_PROJECT)

    def test_server_starts_before_softap_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "petkit-element-mini-esphome"
            (project / "esphome").mkdir(parents=True)
            kickstart = install.DetectedFirmware(
                "petkit-kickstart.local", self.identity(install.KICKSTART_PROJECT)
            )
            final = install.DetectedFirmware(
                "petkit-feeder.local", self.identity(install.FINAL_PROJECT)
            )
            events: list[str] = []
            server = mock.MagicMock()
            server.poll.return_value = None

            def input_answer(message: str) -> str:
                if "Put the feeder in setup mode" in message:
                    events.append("setup-confirmed")
                elif "Reconnect this computer" in message:
                    events.append("regular-wifi-reconnected")
                return ""

            def start_server(*_args: object, **_kwargs: object) -> mock.MagicMock:
                events.append("server-started")
                return server

            def provision(*_args: object, **_kwargs: object) -> bool:
                events.append("provisioned")
                return True

            def wait_for_firmware(
                _python: Path,
                _project: Path,
                _hosts: list[str],
                _api_key: str,
                expected_project: str,
                _expected_mac: str | None,
                **_kwargs: object,
            ) -> install.DetectedFirmware:
                if expected_project == install.KICKSTART_PROJECT:
                    return kickstart
                return final

            def download(path: Path, **_kwargs: object) -> None:
                path.write_bytes(b"R" * 0x200000)

            patches = self.common_patches(project)
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(
                        install, "detect_running_firmware", return_value=None
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_firmware",
                        side_effect=wait_for_firmware,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install, "download_recovery", side_effect=download
                    )
                )
                stack.enter_context(mock.patch.object(install, "run_authenticated_curl"))
                popen = stack.enter_context(
                    mock.patch.object(
                        install.subprocess, "Popen", side_effect=start_server
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install, "run_provisioner", side_effect=provision
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_available_wifi_networks",
                        return_value=["PETKIT_FEEDER_test"],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_wifi_network",
                        side_effect=lambda _prefix: events.append("softap-connected")
                        or "PETKIT_FEEDER_test",
                    )
                )
                stack.enter_context(
                    mock.patch.object(install, "prompt", return_value="10.0.0.100")
                )
                stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_server_event",
                        side_effect=[False, True, True],
                    )
                )
                stack.enter_context(mock.patch.object(install.time, "sleep"))
                output = stack.enter_context(mock.patch("builtins.print"))
                stack.enter_context(
                    mock.patch("builtins.input", side_effect=input_answer)
                )
                install.main()

            self.assertEqual(
                events,
                [
                    "server-started",
                    "setup-confirmed",
                    "softap-connected",
                    "provisioned",
                    "regular-wifi-reconnected",
                ],
            )
            popen_kwargs = popen.call_args.kwargs
            self.assertIn("stdout", popen_kwargs)
            self.assertEqual(popen_kwargs["stderr"], subprocess.STDOUT)
            self.assertIn("-u", popen.call_args.args[0])
            self.assertIn(
                "  ✓ Wi-Fi and local-server settings sent.",
                [call.args[0] for call in output.call_args_list],
            )

    def test_existing_stock_server_configuration_skips_softap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "petkit-element-mini-esphome"
            (project / "esphome").mkdir(parents=True)
            kickstart = install.DetectedFirmware(
                "petkit-kickstart.local", self.identity(install.KICKSTART_PROJECT)
            )
            final = install.DetectedFirmware(
                "petkit-feeder.local", self.identity(install.FINAL_PROJECT)
            )
            server = mock.MagicMock()
            server.poll.return_value = None

            def download(path: Path, **_kwargs: object) -> None:
                path.write_bytes(b"R" * 0x200000)

            patches = self.common_patches(project)
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(
                        install, "detect_running_firmware", return_value=None
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_firmware",
                        side_effect=[kickstart, final],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install, "download_recovery", side_effect=download
                    )
                )
                stack.enter_context(mock.patch.object(install, "run_authenticated_curl"))
                stack.enter_context(
                    mock.patch.object(install.subprocess, "Popen", return_value=server)
                )
                wait = stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_server_event",
                        side_effect=[True, True],
                    )
                )
                provision = stack.enter_context(
                    mock.patch.object(install, "run_provisioner")
                )
                connect = stack.enter_context(
                    mock.patch.object(install, "connect_to_petkit_setup_network")
                )
                user_input = stack.enter_context(mock.patch("builtins.input"))
                stack.enter_context(
                    mock.patch.object(install, "prompt", return_value="10.0.0.100")
                )
                stack.enter_context(mock.patch.object(install.time, "sleep"))
                install.main()

            provision.assert_not_called()
            connect.assert_not_called()
            user_input.assert_not_called()
            self.assertEqual(wait.call_count, 2)
            self.assertEqual(
                wait.call_args_list[0].kwargs["required_fields"],
                {
                    "method": "POST",
                    "path": "/6/feedermini/dev_ota_check",
                },
            )

    def test_debug_keeps_existing_provisioning_and_download_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "petkit-element-mini-esphome"
            (project / "esphome").mkdir(parents=True)
            kickstart = install.DetectedFirmware(
                "petkit-kickstart.local", self.identity(install.KICKSTART_PROJECT)
            )
            final = install.DetectedFirmware(
                "petkit-feeder.local", self.identity(install.FINAL_PROJECT)
            )
            server = mock.MagicMock()
            server.poll.return_value = None

            def download(path: Path, **_kwargs: object) -> None:
                path.write_bytes(b"R" * 0x200000)

            patches = self.common_patches(project)
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                stack.enter_context(
                    mock.patch.object(sys, "argv", ["install.py", "--debug"])
                )
                stack.enter_context(
                    mock.patch.object(
                        install, "detect_running_firmware", return_value=None
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_firmware",
                        side_effect=[kickstart, final],
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        install, "download_recovery", side_effect=download
                    )
                )
                stack.enter_context(mock.patch.object(install, "run_authenticated_curl"))
                popen = stack.enter_context(
                    mock.patch.object(
                        install.subprocess, "Popen", return_value=server
                    )
                )
                wait = stack.enter_context(
                    mock.patch.object(
                        install,
                        "wait_for_server_event",
                        side_effect=[True, True],
                    )
                )
                provision = stack.enter_context(
                    mock.patch.object(install, "run_provisioner")
                )
                connect = stack.enter_context(
                    mock.patch.object(install, "connect_to_petkit_setup_network")
                )
                user_input = stack.enter_context(mock.patch("builtins.input"))
                stack.enter_context(
                    mock.patch.object(install, "prompt", return_value="10.0.0.100")
                )
                stack.enter_context(mock.patch.object(install.time, "sleep"))
                install.main()

            provision.assert_not_called()
            connect.assert_not_called()
            user_input.assert_not_called()
            self.assertEqual(wait.call_count, 2)
            self.assertNotIn("stdout", popen.call_args.kwargs)
            command = popen.call_args.args[0]
            self.assertIn("--event-log", command)

if __name__ == "__main__":
    unittest.main()
