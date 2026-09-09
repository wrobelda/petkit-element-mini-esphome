#!/usr/bin/env python3
"""Build and wirelessly install ESPHome on a Petkit Fresh Element Mini."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse


REPOSITORIES = {
    "petkit-compat-server": "https://github.com/wrobelda/petkit-compat-server.git",
    "esphome-kickstart": "https://github.com/wrobelda/esphome-kickstart.git",
}
REPOSITORY_REVISIONS = {
    "petkit-compat-server": "cdc504c2824cd7eeaccdaf67df13776d0a1ab4a3",
    "esphome-kickstart": "451a67abf2488aef7f0c6d26b9a637d5be328d5e",
}
ALLOWED_REPOSITORY_ORIGINS = {
    "petkit-compat-server": {"wrobelda/petkit-compat-server"},
    "esphome-kickstart": {"wrobelda/esphome-kickstart"},
}
REPOSITORY_MARKERS = {
    "petkit-compat-server": ("serve_petkit_api.py", "provision_petkit_device.py"),
    "esphome-kickstart": (
        "components/esp8266_nonos_v2_to_eboot_v1/__init__.py",
        "tools/build_esp8266_nonos_v2.py",
    ),
}
PROFILE = Path("devices/esp8266/nonos_v2/fresh-element-mini/profile.json")
PLACEHOLDER_SECRETS = {
    "wifi_ssid": "YourWiFi",
    "wifi_password": "YourWiFiPassword",
    "timezone": "Your/Timezone",
    "fallback_ap_password": "ChangeThisAPPassword",
    "kickstart_web_password": "ChangeThisWebPassword",
    "api_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}
IDENTITY_HELPER = Path("tools/identify_esphome_firmware.py")
INSTALL_STATE = Path("local-cache/install-state.json")
KICKSTART_PROJECT = "petkit.fresh-element-mini-kickstart"
FINAL_PROJECT = "petkit.fresh-element-mini"
PROVISION_COMMIT_OUTCOME_UNKNOWN_EXIT = 3
AMBIGUOUS_MIGRATION_CURL_EXIT_CODES = {18, 28, 52, 55, 56}
PETKIT_SOFTAP_PREFIX = "PETKIT"


class DeviceMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceIdentity:
    mac_address: str
    project_name: str


@dataclass(frozen=True)
class DetectedFirmware:
    host: str
    identity: DeviceIdentity


def report_process_failure(result: subprocess.CompletedProcess[str]) -> None:
    for output in (result.stdout, result.stderr):
        if output:
            print(output.rstrip(), file=sys.stderr)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    debug: bool = False,
) -> None:
    if debug:
        print(f"\n+ {' '.join(command)}")
        subprocess.run(command, cwd=cwd, env=env, check=True)
        return
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        report_process_failure(result)
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )


def run_authenticated_curl(
    arguments: list[str],
    *,
    username: str,
    password: str,
    cwd: Path,
    debug: bool = False,
) -> None:
    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    command = ["curl", "--config", "-", "--digest", "--fail-with-body", *arguments]
    if debug:
        print(f"\n+ curl --config - {' '.join(command[3:])}")
    result = subprocess.run(
        command,
        cwd=cwd,
        input=f'user = "{quote(username)}:{quote(password)}"\n',
        text=True,
        check=False,
        capture_output=not debug,
    )
    if result.returncode != 0:
        report_process_failure(result)
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )


def download_recovery(
    path: Path,
    *,
    url: str,
    username: str,
    password: str,
    cwd: Path,
    debug: bool = False,
) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        run_authenticated_curl(
            ["--output", str(path), url],
            username=username,
            password=password,
            cwd=cwd,
            debug=debug,
        )
        path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        value = reader(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default


def yaml_string(value: str) -> str:
    return json.dumps(value)


def read_yaml_secrets(path: Path, python: Path) -> dict[str, str]:
    loader = (
        "import json, pathlib, sys, yaml; "
        "data = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); "
        "print(json.dumps(data))"
    )
    result = subprocess.run(
        [str(python), "-c", loader, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise RuntimeError(f"could not parse {path} as YAML: {detail}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"YAML loader returned invalid data for {path}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")

    values: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError(f"every key and value in {path} must be a string")
        values[key] = value
    return values


def write_secrets(path: Path) -> dict[str, str]:
    values = {
        "wifi_ssid": prompt("Regular 2.4 GHz Wi-Fi network name"),
        "wifi_password": prompt("Wi-Fi password", secret=True),
        "timezone": prompt("IANA time zone", detect_timezone()),
        "fallback_ap_password": prompt(
            "Fallback access-point password", secrets.token_urlsafe(12)
        ),
        "kickstart_web_username": prompt("Kickstart web username", "admin"),
        "kickstart_web_password": prompt(
            "Kickstart web password", secrets.token_urlsafe(16)
        ),
        "api_key": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    }
    path.write_text(
        "# Generated by install.py. Do not commit this file.\n"
        + "".join(f"{key}: {yaml_string(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return values


def load_or_create_secrets(path: Path, python: Path) -> dict[str, str]:
    if path.exists():
        values = read_yaml_secrets(path, python)
        path.chmod(0o600)
        if values.get("wifi_ssid"):
            print(
                f"Using existing {path} for regular 2.4 GHz Wi-Fi network "
                f"{values['wifi_ssid']!r}"
            )
        else:
            print(f"Using existing {path}")
        return values
    return write_secrets(path)


def detect_timezone() -> str:
    localtime = Path("/etc/localtime")
    try:
        target = localtime.resolve()
        marker = "/zoneinfo/"
        if marker in str(target):
            return str(target).split(marker, 1)[1]
    except OSError:
        pass
    return os.environ.get("TZ", "UTC")


def detect_local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))
        return str(sock.getsockname()[0])


def current_wifi_ssid() -> str | None:
    nmcli = shutil.which("nmcli")
    if nmcli is not None:
        result = subprocess.run(
            [
                nmcli,
                "--terse",
                "--escape",
                "no",
                "--fields",
                "IN-USE,SSID",
                "device",
                "wifi",
                "list",
                "--rescan",
                "no",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if line.startswith("*:"):
                return line[2:]
        return None

    networksetup = shutil.which("networksetup")
    if networksetup is not None:
        ports = subprocess.run(
            [networksetup, "-listallhardwareports"],
            check=False,
            capture_output=True,
            text=True,
        )
        if ports.returncode != 0:
            return None
        interface: str | None = None
        lines = ports.stdout.splitlines()
        for index, line in enumerate(lines):
            if line in {"Hardware Port: Wi-Fi", "Hardware Port: AirPort"}:
                for detail in lines[index + 1 : index + 4]:
                    if detail.startswith("Device: "):
                        interface = detail.removeprefix("Device: ")
                        break
                break
        if interface is None:
            return None
        current = subprocess.run(
            [networksetup, "-getairportnetwork", interface],
            check=False,
            capture_output=True,
            text=True,
        )
        prefix = "Current Wi-Fi Network: "
        if current.returncode == 0 and current.stdout.startswith(prefix):
            return current.stdout.removeprefix(prefix).strip()
        return None

    raise RuntimeError(
        "automatic Wi-Fi detection requires nmcli on Linux or networksetup on macOS"
    )


def available_wifi_ssids() -> list[str]:
    nmcli = shutil.which("nmcli")
    if nmcli is not None:
        result = subprocess.run(
            [
                nmcli,
                "--terse",
                "--escape",
                "no",
                "--fields",
                "SSID",
                "device",
                "wifi",
                "list",
                "--rescan",
                "yes",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return sorted({line for line in result.stdout.splitlines() if line})

    system_profiler = shutil.which("system_profiler")
    if system_profiler is not None:
        result = subprocess.run(
            [system_profiler, "SPAirPortDataType", "-json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        networks: set[str] = set()

        def collect(value: object, *, network_list: bool = False) -> None:
            if isinstance(value, dict):
                name = value.get("_name")
                if network_list and isinstance(name, str):
                    networks.add(name)
                for key, child in value.items():
                    collect(
                        child,
                        network_list=network_list
                        or key
                        in {
                            "spairport_current_network_information",
                            "spairport_other_local_wireless_networks",
                        },
                    )
            elif isinstance(value, list):
                for child in value:
                    collect(child, network_list=network_list)

        collect(report)
        return sorted(networks)

    raise RuntimeError(
        "automatic Wi-Fi scanning requires nmcli on Linux or system_profiler "
        "on macOS"
    )


def wait_for_available_wifi_networks(prefix: str, *, timeout: int = 180) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [ssid for ssid in available_wifi_ssids() if ssid.startswith(prefix)]
        if matches:
            return matches
        time.sleep(2)
    raise RuntimeError(
        f"no Wi-Fi network beginning with {prefix!r} appeared within {timeout} seconds"
    )


def wait_for_wifi_network(prefix: str, *, timeout: int = 180) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ssid = current_wifi_ssid()
        if ssid is not None and ssid.startswith(prefix):
            return ssid
        time.sleep(1)
    raise RuntimeError(
        f"a Wi-Fi network beginning with {prefix!r} was not connected within "
        f"{timeout} seconds"
    )


def read_device_identity(
    python: Path,
    project: Path,
    host: str,
    api_key: str,
    *,
    timeout: float = 5.0,
) -> DeviceIdentity | None:
    env = os.environ.copy()
    env["ESPHOME_API_KEY"] = api_key
    result = subprocess.run(
        [
            str(python),
            str(project / IDENTITY_HELPER),
            host,
            "--timeout",
            str(timeout),
        ],
        cwd=project,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 4:
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown ESPHome API error"
        raise RuntimeError(f"could not identify ESPHome firmware at {host}: {detail}")
    try:
        data = json.loads(result.stdout)
        return DeviceIdentity(
            mac_address=data["mac_address"],
            project_name=data["project_name"],
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid ESPHome identity returned for {host}") from error


def normalize_mac(value: str) -> str:
    normalized = value.lower().replace(":", "").replace("-", "")
    if len(normalized) != 12 or any(character not in "0123456789abcdef" for character in normalized):
        raise RuntimeError(f"invalid ESPHome MAC address: {value!r}")
    return normalized


def load_expected_mac(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return normalize_mac(data["device_mac"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid installation state in {path}") from error


def save_expected_mac(path: Path, mac_address: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"device_mac": normalize_mac(mac_address)}) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def detect_running_firmware(
    python: Path,
    project: Path,
    hosts: list[str],
    api_key: str,
    expected_mac: str | None,
) -> DetectedFirmware | None:
    errors: list[str] = []
    for host in dict.fromkeys(hosts):
        try:
            identity = read_device_identity(python, project, host, api_key)
        except RuntimeError as error:
            errors.append(str(error))
            continue
        if identity is None:
            continue
        if identity.project_name not in {KICKSTART_PROJECT, FINAL_PROJECT}:
            errors.append(
                f"{host} runs unexpected ESPHome project {identity.project_name!r}"
            )
            continue
        actual_mac = normalize_mac(identity.mac_address)
        if expected_mac is not None and actual_mac != expected_mac:
            raise DeviceMismatchError(
                f"{host} is {identity.mac_address}, not the expected feeder MAC"
            )
        return DetectedFirmware(host=host, identity=identity)
    if errors:
        raise RuntimeError("; ".join(errors))
    return None


def wait_for_firmware(
    python: Path,
    project: Path,
    hosts: list[str],
    api_key: str,
    expected_project: str,
    expected_mac: str | None,
    *,
    timeout: int = 180,
) -> DetectedFirmware:
    deadline = time.monotonic() + timeout
    last_project: str | None = None
    last_error: RuntimeError | None = None
    while time.monotonic() < deadline:
        try:
            detected = detect_running_firmware(
                python, project, hosts, api_key, expected_mac
            )
        except DeviceMismatchError:
            raise
        except RuntimeError as error:
            last_error = error
            detected = None
        if detected is not None:
            last_project = detected.identity.project_name
            if last_project == expected_project:
                return detected
        time.sleep(2)
    if last_project is not None:
        raise RuntimeError(
            f"expected ESPHome project {expected_project!r}, but {last_project!r} "
            "remained reachable"
        )
    if last_error is not None:
        raise RuntimeError(
            f"could not authenticate the expected ESPHome firmware: {last_error}"
        ) from last_error
    raise RuntimeError(
        f"ESPHome project {expected_project!r} did not become reachable within "
        f"{timeout} seconds"
    )


def run_provisioner(
    command: list[str], *, cwd: Path, env: dict[str, str], debug: bool = False
) -> bool:
    if debug:
        print(f"\n+ {' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=not debug,
        text=not debug,
    )
    if result.returncode == 0:
        return True
    if result.returncode == PROVISION_COMMIT_OUTCOME_UNKNOWN_EXIT:
        return False
    report_process_failure(result)
    raise subprocess.CalledProcessError(
        result.returncode,
        command,
        output=result.stdout,
        stderr=result.stderr,
    )


def migration_result_is_ambiguous(error: subprocess.CalledProcessError) -> bool:
    return error.returncode in AMBIGUOUS_MIGRATION_CURL_EXIT_CODES


def require_build_artifact(path: Path, description: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"{description} was not created at {path}")


def github_repository(url: str) -> str | None:
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    return path.removesuffix(".git").rstrip("/")


def validate_checkout(path: Path, name: str, revision: str) -> None:
    if not (path / ".git").exists():
        raise RuntimeError(f"{path} exists but is not a Git checkout")
    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{path} has no readable origin remote") from error
    repository = github_repository(origin)
    if repository not in ALLOWED_REPOSITORY_ORIGINS[name]:
        expected = ", ".join(sorted(ALLOWED_REPOSITORY_ORIGINS[name]))
        raise RuntimeError(
            f"{path} has unexpected origin {origin!r}; expected {expected}"
        )
    missing = [marker for marker in REPOSITORY_MARKERS[name] if not (path / marker).is_file()]
    if missing:
        raise RuntimeError(f"{path} is missing required files: {', '.join(missing)}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    expected = subprocess.run(
        ["git", "rev-parse", f"{revision}^{{commit}}"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != expected:
        raise RuntimeError(
            f"{path} is at {head[:12]}, but this installer requires {revision}; "
            "move the checkout aside or check out the required revision"
        )


def ensure_checkout(
    parent: Path,
    name: str,
    url: str,
    revision: str,
    *,
    debug: bool = False,
) -> Path:
    path = parent / name
    if path.is_dir():
        validate_checkout(path, name, revision)
        return path
    run(["git", "clone", url, name], cwd=parent, debug=debug)
    run(["git", "checkout", "--detach", revision], cwd=path, debug=debug)
    validate_checkout(path, name, revision)
    return path


def configure_firewall() -> bool:
    if shutil.which("firewall-cmd") is None:
        return False
    query = subprocess.run(
        ["firewall-cmd", "--query-port=8080/tcp"], capture_output=True, check=False
    )
    if query.returncode == 0:
        return False
    answer = input("Temporarily allow TCP port 8080 through firewalld? [Y/n]: ").strip()
    if answer.lower() not in {"", "y", "yes"}:
        return False
    subprocess.run(
        ["sudo", "firewall-cmd", "--add-port=8080/tcp"], check=True
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kickstart-host",
        default="petkit-kickstart.local",
        help="Kickstart hostname or IP address",
    )
    parser.add_argument(
        "--final-host",
        default="petkit-feeder.local",
        help="final ESPHome hostname or IP address",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show commands and diagnostic output",
    )
    args = parser.parse_args()

    project = Path(__file__).resolve().parent
    parent = project.parent
    print("Preparing supporting projects...")
    compat = ensure_checkout(
        parent,
        "petkit-compat-server",
        REPOSITORIES["petkit-compat-server"],
        REPOSITORY_REVISIONS["petkit-compat-server"],
        debug=args.debug,
    )
    kickstart = ensure_checkout(
        parent,
        "esphome-kickstart",
        REPOSITORIES["esphome-kickstart"],
        REPOSITORY_REVISIONS["esphome-kickstart"],
        debug=args.debug,
    )
    if shutil.which("curl") is None:
        raise SystemExit("curl is required for the authenticated migration upload")

    venv = project / ".venv"
    if not venv.exists():
        print("Creating the Python build environment...")
        run(
            [sys.executable, "-m", "venv", str(venv)],
            cwd=project,
            debug=args.debug,
        )
    python = venv / "bin" / "python"
    esphome = venv / "bin" / "esphome"
    print("Preparing ESPHome build dependencies...")
    run(
        [str(python), "-m", "pip", "install", "esphome==2026.9.0b1"],
        cwd=project,
        debug=args.debug,
    )

    secrets_path = project / "esphome" / "secrets.yaml"
    values = load_or_create_secrets(secrets_path, python)
    required = {
        "wifi_ssid",
        "wifi_password",
        "timezone",
        "fallback_ap_password",
        "kickstart_web_username",
        "kickstart_web_password",
        "api_key",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise SystemExit(f"{secrets_path} is missing: {', '.join(missing)}")
    placeholders = sorted(
        key for key, placeholder in PLACEHOLDER_SECRETS.items() if values[key] == placeholder
    )
    if placeholders:
        raise SystemExit(
            f"{secrets_path} still contains example values for: "
            f"{', '.join(placeholders)}"
        )

    build_env = os.environ.copy()
    build_env["KICKSTART_COMPONENTS_PATH"] = str((kickstart / "components").resolve())
    print("Building the Kickstart transition firmware...")
    run(
        [str(esphome), "compile", "petkit-kickstart.yaml"],
        cwd=project / "esphome",
        env=build_env,
        debug=args.debug,
    )
    print("Building the final feeder firmware...")
    run(
        [str(esphome), "compile", "petkit-feeder.yaml"],
        cwd=project / "esphome",
        env=build_env,
        debug=args.debug,
    )

    release = project / "local-cache" / "release"
    release.mkdir(parents=True, exist_ok=True)
    transition = release / "petkit-element-mini-kickstart-v2.bin"
    transition_elf = (
        project
        / "esphome/.esphome/build/petkit-kickstart/.pioenvs/petkit-kickstart/firmware.elf"
    )
    factory = (
        project
        / "esphome/.esphome/build/petkit-feeder/.pioenvs/petkit-feeder/firmware.factory.bin"
    )
    require_build_artifact(transition_elf, "Kickstart ELF")
    require_build_artifact(factory, "final ESPHome factory image")
    print("Packaging the stock-compatible Kickstart image...")
    run(
        [
            str(python),
            str(kickstart / "tools/build_esp8266_nonos_v2.py"),
            "--irom-vma",
            "0x40201010",
            "--entry-symbol",
            "app_entry",
            "--max-size",
            "0x0fa000",
            "--flash-mode",
            "qio",
            "--flash-frequency",
            "40m",
            "--flash-layout",
            "2MB-c1",
            str(transition_elf),
            str(transition),
        ],
        cwd=project,
        debug=args.debug,
    )

    state_path = project / INSTALL_STATE
    expected_mac = load_expected_mac(state_path)
    print("Checking for an existing Kickstart or final ESPHome installation...")
    detected = detect_running_firmware(
        python,
        project,
        [args.final_host, args.kickstart_host],
        values["api_key"],
        expected_mac,
    )
    if detected is not None and detected.identity.project_name == FINAL_PROJECT:
        save_expected_mac(state_path, detected.identity.mac_address)
        print(
            f"Final ESPHome firmware is already running at {detected.host} "
            f"on feeder {detected.identity.mac_address}."
        )
        return

    if detected is None:
        computer_ip = prompt(
            "This computer's IP address on the regular 2.4 GHz Wi-Fi network",
            detect_local_ip(),
        )
        timezone_name = values["timezone"]
        offset = datetime.now().astimezone().utcoffset()
        timezone_offset = str((offset.total_seconds() if offset else 0) / 3600)
        firewall_added = configure_firewall()
        server: subprocess.Popen[bytes] | None = None
        server_log = None
        server_log_path = project / "local-cache" / "compat-server.log"
        try:
            input(
                "\nPut the feeder in setup mode. After the confirmation beep, "
                "press Enter.\n"
            )
            softaps = wait_for_available_wifi_networks(PETKIT_SOFTAP_PREFIX)
            print("\nAvailable Petkit setup networks:")
            for ssid in softaps:
                print(f"- {ssid}")
            if len(softaps) > 1:
                print(
                    "The feeder network is normally named like "
                    "PETKIT_FEEDER_xyz."
                )
            print(
                "Connect this computer to the correct PETKIT_FEEDER_xyz Wi-Fi "
                "network. The installer will continue automatically."
            )
            softap_ssid = wait_for_wifi_network(PETKIT_SOFTAP_PREFIX)
            print(f"Connected to {softap_ssid!r}.")
            popen_output: dict[str, object] = {}
            if not args.debug:
                descriptor = os.open(
                    server_log_path,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                server_log = os.fdopen(descriptor, "w", encoding="utf-8")
                popen_output = {
                    "stdout": server_log,
                    "stderr": subprocess.STDOUT,
                }
            print("Starting the local compatibility server...")
            server = subprocess.Popen(
                [
                    str(python),
                    "-u",
                    "serve_petkit_api.py",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8080",
                    "--profile",
                    str(PROFILE),
                    "--ota-image",
                    str(transition),
                ],
                cwd=compat,
                **popen_output,
            )
            time.sleep(1)
            if server.poll() is not None:
                raise RuntimeError("the local Petkit API server did not start")
            provision_env = os.environ.copy()
            provision_env["ESPHOME_WIFI_PASSWORD"] = values["wifi_password"]
            print("Sending the Wi-Fi and local-server settings to the feeder...")
            acknowledged = run_provisioner(
                [
                    str(python),
                    "provision_petkit_device.py",
                    "--profile",
                    str(PROFILE),
                    "--ssid",
                    values["wifi_ssid"],
                    "--server",
                    f"http://{computer_ip}:8080/6/",
                    "--timezone",
                    timezone_offset,
                    "--locale",
                    timezone_name,
                    "--send",
                ],
                cwd=compat,
                env=provision_env,
                debug=args.debug,
            )
            if not acknowledged:
                print(
                    "The commit was sent, but the SoftAP connection ended before "
                    "acknowledgement. The installer will verify the result on the "
                    "regular 2.4 GHz Wi-Fi network."
                )
            input(
                "\nReconnect this computer to the regular 2.4 GHz Wi-Fi network, "
                "then press Enter. Keep this installer running while the feeder "
                "downloads and boots Kickstart.\n"
            )
            detected = wait_for_firmware(
                python,
                project,
                [args.kickstart_host, args.final_host],
                values["api_key"],
                KICKSTART_PROJECT,
                expected_mac,
            )
        except BaseException:
            if server is not None and server_log is not None:
                server_log.flush()
                diagnostics = server_log_path.read_text(encoding="utf-8").strip()
                if diagnostics:
                    print(diagnostics, file=sys.stderr)
            raise
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
            if server_log is not None:
                server_log.close()
            if firewall_added:
                subprocess.run(
                    ["sudo", "firewall-cmd", "--remove-port=8080/tcp"],
                    check=False,
                )

    assert detected is not None
    if detected.identity.project_name != KICKSTART_PROJECT:
        raise RuntimeError(
            f"expected Kickstart, but {detected.identity.project_name!r} is running"
        )
    expected_mac = normalize_mac(detected.identity.mac_address)
    save_expected_mac(state_path, detected.identity.mac_address)
    kickstart_host = detected.host
    print(
        f"Kickstart is authenticated at {kickstart_host} on feeder "
        f"{detected.identity.mac_address}."
    )

    recovery = release / f"petkit-post-kickstart-{int(time.time())}.bin"
    print("Saving the 2 MiB recovery image...")
    download_recovery(
        recovery,
        url=f"http://{kickstart_host}/hub/flash_read",
        username=values["kickstart_web_username"],
        password=values["kickstart_web_password"],
        cwd=project,
        debug=args.debug,
    )
    if recovery.stat().st_size != 0x200000:
        raise RuntimeError("Kickstart recovery download is not 2 MiB")

    migration_error: subprocess.CalledProcessError | None = None
    print("Installing the final feeder firmware...")
    try:
        run_authenticated_curl(
            [
                "--form",
                f"firmware=@{factory}",
                f"http://{kickstart_host}/hub/migrate?confirm=replace-vendor-bootloader",
            ],
            username=values["kickstart_web_username"],
            password=values["kickstart_web_password"],
            cwd=project,
            debug=args.debug,
        )
    except subprocess.CalledProcessError as error:
        if not migration_result_is_ambiguous(error):
            raise
        migration_error = error
        print(
            "The migration response was lost or rejected. The installer will "
            "identify the firmware now running before deciding the outcome."
        )

    print("\nWaiting for the authenticated final ESPHome firmware.")
    try:
        final = wait_for_firmware(
            python,
            project,
            [args.final_host, kickstart_host],
            values["api_key"],
            FINAL_PROJECT,
            expected_mac,
        )
    except RuntimeError as error:
        if migration_error is not None:
            raise RuntimeError(
                "the migration response was indeterminate and the final firmware "
                "could not be verified; rerun the installer to reconcile the "
                "current firmware before another upload"
            ) from error
        raise

    save_expected_mac(state_path, final.identity.mac_address)
    print(
        f"Final ESPHome firmware {final.identity.project_name} is authenticated "
        f"at {final.host} on feeder {final.identity.mac_address}. Keep the "
        f"recovery image at {recovery}."
    )


if __name__ == "__main__":
    main()
