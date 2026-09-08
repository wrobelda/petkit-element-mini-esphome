from __future__ import annotations

import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

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
                install.read_simple_secrets(path),
                {
                    "plain": "plain value",
                    "double": "double value",
                    "single": "single value",
                    "apostrophe": "owner's network",
                },
            )

    def test_secures_existing_secrets_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text("wifi_ssid: test\n", encoding="utf-8")
            path.chmod(0o644)

            install.load_or_create_secrets(path)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_generated_secrets_include_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            answers = iter(["TestWiFi", "wifi-password", "Europe/Warsaw", "ap-password", "admin", "web-password"])
            with mock.patch.object(install, "prompt", side_effect=answers):
                values = install.write_secrets(path)

            self.assertEqual(values["timezone"], "Europe/Warsaw")
            self.assertEqual(install.read_simple_secrets(path)["timezone"], "Europe/Warsaw")


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

if __name__ == "__main__":
    unittest.main()
