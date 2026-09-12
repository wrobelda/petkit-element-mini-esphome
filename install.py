#!/usr/bin/env python3
"""Build and wirelessly install ESPHome on a Petkit Fresh Element Mini."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Iterator
import contextlib
from dataclasses import dataclass
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import secrets
import select
import shlex
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
    "petkit-compat-server": "a4227124dbfa98a885544bc1e2ac34e149ced03d",
    "esphome-kickstart": "98c070e23fdfd9c05de8cce4376cdc31a220e80d",
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
LOCAL_CACHE = Path("local-cache")
INSTALL_STATE = LOCAL_CACHE / "install-state.json"
RELEASE_DIR = LOCAL_CACHE / "release"
COMPAT_SERVER_LOG = LOCAL_CACHE / "compat-server.log"
COMPAT_SERVER_EVENT_LOG = LOCAL_CACHE / "compat-server-events.log"
COMPAT_SERVER_PORT = 8080
ESPHOME_VERSION = "2026.9.0b1"
KICKSTART_IMAGE_NAME = "petkit-element-mini-kickstart-v2.bin"
KICKSTART_ELF = Path(
    "esphome/.esphome/build/petkit-feeder/.pioenvs/petkit-feeder/firmware.elf"
)
# Flash mapping of the Petkit non-OS V2 slots; keep in step with
# esphome/petkit-kickstart.yaml.
KICKSTART_IMAGE_ARGS = [
    "--irom-vma", "0x40201010",
    "--entry-symbol", "app_entry",
    "--max-size", "0x0fa000",
    "--flash-mode", "qio",
    "--flash-frequency", "40m",
    "--flash-layout", "2MB-c1",
]
STOCK_OTA_CHECK_REQUEST = {"method": "POST", "path": "/6/feedermini/dev_ota_check"}
STOCK_OTA_START_REQUEST = {"method": "POST", "path": "/6/feedermini/dev_ota_start"}
KICKSTART_PROJECT = "petkit.fresh-element-mini-kickstart"
FINAL_PROJECT = "petkit.fresh-element-mini"
PROVISION_COMMIT_OUTCOME_UNKNOWN_EXIT = 3
PETKIT_SOFTAP_PREFIX = "PETKIT"


class DeviceMismatchError(RuntimeError):
    pass


class WifiDetectionUnavailable(RuntimeError):
    """No usable Wi-Fi detector on this computer."""


class WifiDetectionFailed(WifiDetectionUnavailable):
    """The detector exists but one invocation failed; worth retrying."""


class InstallationTimeout(RuntimeError):
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


def check_process(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    """Report a failed command's captured output, then raise CalledProcessError."""
    if result.returncode != 0:
        report_process_failure(result)
        result.check_returncode()
    return result


def announce_command(command: list[str]) -> None:
    print(f"\n+ {shlex.join(command)}")


def create_private_file(path: Path, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    os.close(os.open(path, flags, 0o600))


def write_private_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def parse_json_object(text: str, *, source: str) -> dict[str, object]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"expected JSON from {source}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"expected a JSON object from {source}")
    return parsed


def poll(
    timeout: float, *, interval: float = 2, progress_label: str | None = None
) -> Iterator[float]:
    """Yield the current monotonic time until *timeout* elapses.

    Prints a progress line every 10 seconds when *progress_label* is set and
    sleeps *interval* seconds between iterations.
    """
    deadline = time.monotonic() + timeout
    next_progress = time.monotonic() + 10
    while True:
        now = time.monotonic()
        if now >= deadline:
            return
        yield now
        if progress_label is not None and now >= next_progress:
            print(f"  … Still waiting for {progress_label}...")
            next_progress = now + 10
        time.sleep(interval)


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    debug: bool = False,
) -> None:
    if debug:
        announce_command(command)
        subprocess.run(command, cwd=cwd, env=env, check=True)
        return
    check_process(
        subprocess.run(
            command, cwd=cwd, env=env, check=False, capture_output=True, text=True
        )
    )


def curl_config_line(username: str, password: str) -> str:
    def quote(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return f'user = "{quote(username)}:{quote(password)}"\n'


# curl exit codes for failures that clear up while the bridge reboots.
TRANSIENT_CURL_EXIT_CODES = frozenset({6, 7, 28, 35, 52, 56})


def run_authenticated_curl(
    arguments: list[str],
    *,
    username: str,
    password: str,
    debug: bool = False,
    capture: bool = False,
    quiet: bool = False,
) -> str:
    """Run curl with digest credentials passed on stdin; return captured stdout.

    With *quiet*, a failure raises without echoing curl's output, for polls
    whose misses are expected.
    """
    command = ["curl", "--config", "-", "--digest", "--fail-with-body", *arguments]
    if debug:
        announce_command(command)
    result = subprocess.run(
        command,
        input=curl_config_line(username, password),
        text=True,
        check=False,
        capture_output=capture or not debug,
    )
    if quiet:
        result.check_returncode()
    else:
        check_process(result)
    return result.stdout or ""


def download_recovery(
    path: Path,
    *,
    url: str,
    username: str,
    password: str,
    debug: bool = False,
) -> None:
    create_private_file(path, exclusive=True)
    try:
        run_authenticated_curl(
            ["--output", str(path), url],
            username=username,
            password=password,
            debug=debug,
        )
        path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def fetch_authenticated_json(
    url: str,
    *,
    username: str,
    password: str,
    debug: bool = False,
    quiet: bool = False,
) -> dict[str, object]:
    # Polled while the bridge reboots, so bound the connect and transfer time
    # and keep curl's progress meter out of the captured output.
    text = run_authenticated_curl(
        ["--silent", "--show-error", "--connect-timeout", "5", "--max-time", "30", url],
        username=username,
        password=password,
        debug=debug,
        capture=True,
        quiet=quiet,
    )
    return parse_json_object(text, source=url)


def post_authenticated(
    url: str,
    *,
    username: str,
    password: str,
    debug: bool = False,
    timeout: int = 30,
) -> None:
    """POST to the bridge, retrying resolution and connection failures.

    The bridge's mDNS name can take a few seconds to resolve again after a
    reboot; failures the bridge itself reports (an HTTP error) are not retried.
    """
    last_error: subprocess.CalledProcessError | None = None
    for _ in poll(timeout):
        try:
            run_authenticated_curl(
                ["--silent", "--show-error", "--connect-timeout", "5", "--request", "POST", url],
                username=username,
                password=password,
                debug=debug,
                quiet=True,
            )
            return
        except subprocess.CalledProcessError as error:
            if error.returncode not in TRANSIENT_CURL_EXIT_CODES:
                report_process_failure(error)
                raise
            last_error = error
    assert last_error is not None
    report_process_failure(last_error)
    raise last_error


def wait_for_slot(
    hosts: list[str],
    expected_slot: int,
    *,
    username: str,
    password: str,
    timeout: int = 300,
    debug: bool = False,
) -> str:
    """Wait until /hub/slot_status reports *expected_slot* is active.

    The slot routes report the active slot 1-based and 1 is the lower slot, so
    relocation from the lower slot completes when this reports 2. Relocation
    reboots the bridge, so try each host until one answers, and return the
    host that did.
    """
    for _ in poll(timeout, progress_label=f"the bridge to reach slot {expected_slot}"):
        for host in dict.fromkeys(hosts):
            try:
                status = fetch_authenticated_json(
                    f"http://{host}/hub/slot_status",
                    username=username,
                    password=password,
                    debug=debug,
                    quiet=True,
                )
            except (subprocess.CalledProcessError, RuntimeError):
                continue
            if status.get("current_slot") == expected_slot:
                return host
    raise InstallationTimeout(f"the bridge did not reach slot {expected_slot}")


def wait_for_conversion(
    hosts: list[str],
    *,
    username: str,
    password: str,
    timeout: int = 300,
    debug: bool = False,
) -> str:
    """Wait for the bridge to report the eboot layout; return the host that did."""
    for _ in poll(timeout, progress_label="the bridge to finish preparing"):
        for host in dict.fromkeys(hosts):
            try:
                status = fetch_authenticated_json(
                    f"http://{host}/hub/convert",
                    username=username,
                    password=password,
                    debug=debug,
                    quiet=True,
                )
            except (subprocess.CalledProcessError, RuntimeError):
                continue
            result = str(status.get("result", ""))
            # already_converted is the layout-is-eboot signal on every boot
            # after the first; success is the conversion boot itself.
            if result in ("success", "already_converted"):
                return host
            # Everything except a still-running conversion is terminal.
            if result not in ("idle", "in_progress", ""):
                raise RuntimeError(
                    f"the bridge reported a failed conversion: {result}. "
                    "The recovery image and POST /hub/boot_other are the way "
                    "back to the stock firmware."
                )
    raise InstallationTimeout("the bridge did not report a successful conversion")


def prompt(label: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        value = reader(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default


def confirm_final_install() -> bool:
    print(
        "\n✅ Kickstart is ready for the feeder firmware.\n\n"
        "To manage the feeder in ESPHome Device Builder, find it under "
        "Discovered, select Take Control, then Install.\n"
        "Or finish the installation from this terminal below.\n"
    )
    return prompt(
        "    Compile and install the feeder firmware from this installer instead",
        "no",
    ).lower() in ("y", "yes")


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
        detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise RuntimeError(f"could not parse {path} as YAML: {detail}")
    parsed = parse_json_object(result.stdout, source=f"the YAML loader for {path}")

    values: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError(f"every key and value in {path} must be a string")
        values[key] = value
    return values


def write_secrets(path: Path) -> dict[str, str]:
    values = {
        "wifi_ssid": prompt(
            "    Regular 2.4 GHz Wi-Fi network name", detect_wifi_ssid()
        ),
        "wifi_password": prompt("    Wi-Fi password", secret=True),
        "timezone": prompt("    IANA time zone", detect_timezone()),
        "fallback_ap_password": prompt(
            "    Fallback access-point password", secrets.token_urlsafe(12)
        ),
        "kickstart_web_username": prompt("    Kickstart web username", "admin"),
        "kickstart_web_password": prompt(
            "    Kickstart web password", secrets.token_urlsafe(16)
        ),
        "api_key": prompt(
            "    API encryption key",
            base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        ),
    }
    write_private_text(
        path,
        "# Generated by install.py. Do not commit this file.\n"
        + "".join(f"{key}: {json.dumps(value)}\n" for key, value in values.items()),
    )
    return values


def display_path(path: Path) -> str:
    """Show paths inside the project checkout relative to it."""
    try:
        return f"./{path.relative_to(Path(__file__).resolve().parent)}"
    except ValueError:
        return str(path)


def load_or_create_secrets(path: Path, python: Path) -> dict[str, str]:
    display = display_path(path)
    if path.exists():
        values = read_yaml_secrets(path, python)
        path.chmod(0o600)
        if values.get("wifi_ssid"):
            print(
                f"  ✓ {display} already contains credentials for the "
                f"{values['wifi_ssid']!r} network the feeder will connect to."
            )
        else:
            print(f"  ✓ Using existing {display}")
        return values
    return write_secrets(path)


def installed_esphome_version(python: Path) -> str | None:
    if not python.exists():
        return None
    result = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('esphome'))"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


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


def nmcli_wifi_list(nmcli: str, fields: str, *, rescan: bool, failure: str) -> list[str]:
    result = subprocess.run(
        [
            nmcli,
            "--terse",
            "--escape",
            "no",
            "--fields",
            fields,
            "device",
            "wifi",
            "list",
            "--rescan",
            "yes" if rescan else "no",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise WifiDetectionFailed(failure)
    return result.stdout.splitlines()


def current_wifi_ssid() -> str | None:
    nmcli = shutil.which("nmcli")
    if nmcli is not None:
        lines = nmcli_wifi_list(
            nmcli,
            "IN-USE,SSID",
            rescan=False,
            failure="NetworkManager could not read Wi-Fi state",
        )
        for line in lines:
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
            raise WifiDetectionFailed("macOS could not list network interfaces")
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
            raise WifiDetectionUnavailable("macOS did not report a Wi-Fi interface")
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

    raise WifiDetectionUnavailable(
        "automatic Wi-Fi detection requires nmcli on Linux or networksetup on macOS"
    )


def detect_wifi_ssid() -> str | None:
    try:
        return current_wifi_ssid()
    except WifiDetectionUnavailable:
        return None


def available_wifi_ssids() -> list[str]:
    nmcli = shutil.which("nmcli")
    if nmcli is not None:
        lines = nmcli_wifi_list(
            nmcli,
            "SSID",
            rescan=True,
            failure="NetworkManager could not scan for Wi-Fi networks",
        )
        return sorted({line for line in lines if line})

    system_profiler = shutil.which("system_profiler")
    if system_profiler is not None:
        result = subprocess.run(
            [system_profiler, "SPAirPortDataType", "-json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise WifiDetectionFailed("macOS could not scan for Wi-Fi networks")
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise WifiDetectionFailed("macOS returned an invalid Wi-Fi scan")
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
        if not networks:
            raise WifiDetectionUnavailable(
                "macOS did not expose Wi-Fi network names; Location Services "
                "permission may be required"
            )
        return sorted(networks)

    raise WifiDetectionUnavailable(
        "automatic Wi-Fi scanning requires nmcli on Linux or system_profiler "
        "on macOS"
    )


def wait_for_available_wifi_networks(prefix: str, *, timeout: int = 180) -> list[str]:
    last_failure: WifiDetectionFailed | None = None
    for _ in poll(timeout):
        try:
            ssids = available_wifi_ssids()
        except WifiDetectionFailed as error:
            # A scan can fail while the adapter is busy; keep trying.
            last_failure = error
            continue
        matches = [ssid for ssid in ssids if ssid.startswith(prefix)]
        if matches:
            return matches
    raise TimeoutError(
        f"no Wi-Fi network beginning with {prefix!r} appeared within {timeout} seconds"
        + (f" (last scan error: {last_failure})" if last_failure else "")
    )


def stdin_has_input() -> bool:
    """True when a line is waiting on stdin (a terminal Enter press)."""
    try:
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except (OSError, ValueError):
        return False


def wait_for_wifi_network(
    expected_ssid: str, *, timeout: int = 180, allow_enter: bool = False
) -> str | None:
    """Wait for *expected_ssid* to become the active network.

    With *allow_enter*, an Enter press ends the wait early and returns None,
    so the user can stay on another network that also reaches the feeder.
    """
    last_failure: WifiDetectionFailed | None = None
    for _ in poll(timeout, interval=1):
        if allow_enter and stdin_has_input():
            sys.stdin.readline()
            return None
        try:
            ssid = current_wifi_ssid()
        except WifiDetectionFailed as error:
            # Reading the active network can fail while it is being switched.
            last_failure = error
            continue
        if ssid == expected_ssid:
            return ssid
    raise TimeoutError(
        f"Wi-Fi network {expected_ssid!r} was not connected within {timeout} seconds"
        + (f" (last detector error: {last_failure})" if last_failure else "")
    )


def choose_wifi_network(ssids: list[str]) -> str:
    if len(ssids) == 1:
        return ssids[0]
    while True:
        choice = prompt("    Petkit setup network (name or number)")
        if choice.isdigit() and 1 <= int(choice) <= len(ssids):
            return ssids[int(choice) - 1]
        if choice in ssids:
            return choice
        print("  Choose one of the listed Petkit setup networks.")


def manual_wifi_fallback(error: Exception, instruction: str) -> None:
    # Always say why the automatic path gave up; a bare prompt after "the
    # installer will continue automatically" reads as a contradiction.
    print(f"  Automatic Wi-Fi detection did not confirm the switch: {error}")
    input(instruction)


def wait_for_wifi_network_or_manual(
    expected_ssid: str,
    *,
    manual_instruction: str,
    debug: bool = False,
    timeout: int = 180,
    allow_enter: bool = False,
) -> None:
    """Wait for *expected_ssid*, falling back to a manual Enter prompt.

    Detection is best-effort: an unavailable detector or a timeout asks for
    confirmation by Enter, so the manual path is never skipped. With
    *allow_enter*, Enter during the wait skips the switch altogether.
    """
    try:
        detected = wait_for_wifi_network(
            expected_ssid, timeout=timeout, allow_enter=allow_enter
        )
    except (WifiDetectionUnavailable, TimeoutError) as error:
        manual_wifi_fallback(error, manual_instruction)
        return
    if detected is None:
        print("  ✓ Continuing without waiting for the network switch.")
        return
    print(f"  ✓ Connected to {detected!r}.")


def connect_to_petkit_setup_network(*, debug: bool = False) -> None:
    manual_instruction = (
        "\n  → Connect this computer to the feeder's setup Wi-Fi network, "
        "normally named like PETKIT_FEEDER_xyz.\n"
        "    Press Enter when connected.\n"
    )
    try:
        softaps = wait_for_available_wifi_networks(PETKIT_SOFTAP_PREFIX)
        print("\n  Available Petkit setup networks:")
        for number, ssid in enumerate(softaps, start=1):
            print(f"    {number}. {ssid}")
        if len(softaps) > 1:
            print(
                "  The feeder network is normally named like PETKIT_FEEDER_xyz."
            )
        selected_softap = choose_wifi_network(softaps)
        print(
            f"  → Connect this computer to {selected_softap!r} (or press Enter "
            "to continue)."
        )
    except (WifiDetectionUnavailable, TimeoutError) as error:
        manual_wifi_fallback(error, manual_instruction)
        return
    wait_for_wifi_network_or_manual(
        selected_softap,
        manual_instruction=manual_instruction,
        debug=debug,
        allow_enter=True,
    )


def reconnect_to_regular_wifi_network(
    ssid: str, *, debug: bool = False, timeout: int = 60
) -> None:
    print(f"\n  → Reconnect this computer to the {ssid!r} network (or press Enter to continue).")
    wait_for_wifi_network_or_manual(
        ssid,
        manual_instruction="    Press Enter when connected.\n",
        debug=debug,
        timeout=timeout,
        allow_enter=True,
    )


def read_device_identity(
    python: Path,
    project: Path,
    host: str,
    api_key: str,
    *,
    timeout: float = 5.0,
) -> DetectedFirmware | None:
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
        data = parse_json_object(result.stdout, source=host)
        return DetectedFirmware(
            host=str(data["connected_address"]),
            identity=DeviceIdentity(
                mac_address=str(data["mac_address"]),
                project_name=str(data["project_name"]),
            ),
        )
    except (KeyError, RuntimeError) as error:
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
    write_private_text(
        temporary, json.dumps({"device_mac": normalize_mac(mac_address)}) + "\n"
    )
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
            detected = read_device_identity(python, project, host, api_key)
        except RuntimeError as error:
            errors.append(str(error))
            continue
        if detected is None:
            continue
        identity = detected.identity
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
        return detected
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
    progress_label: str | None = None,
) -> DetectedFirmware:
    last_project: str | None = None
    last_error: RuntimeError | None = None
    for _ in poll(timeout, progress_label=progress_label):
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
    if last_project is not None:
        raise InstallationTimeout(
            f"expected ESPHome project {expected_project!r}, but {last_project!r} "
            "remained reachable"
        )
    if last_error is not None:
        raise InstallationTimeout(
            f"could not authenticate the expected ESPHome firmware: {last_error}"
        ) from last_error
    raise InstallationTimeout(
        f"ESPHome project {expected_project!r} did not become reachable within "
        f"{timeout} seconds"
    )


def run_provisioner(
    command: list[str], *, cwd: Path, env: dict[str, str], debug: bool = False
) -> bool:
    if debug:
        announce_command(command)
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=not debug,
        text=not debug,
    )
    if result.returncode == PROVISION_COMMIT_OUTCOME_UNKNOWN_EXIT:
        return False
    check_process(result)
    return True


def require_build_artifact(path: Path, description: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"{description} was not created at {path}")


def server_log_has_event(
    path: Path, event_name: str, required_fields: dict[str, object] | None = None
) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != event_name:
            continue
        if required_fields is None or all(
            event.get(key) == value for key, value in required_fields.items()
        ):
            return True
    return False


def wait_for_server_event(
    path: Path,
    server: subprocess.Popen[bytes],
    event_name: str,
    *,
    required_fields: dict[str, object] | None = None,
    timeout: int = 10,
    progress_label: str | None = None,
    delayed_message: str | None = None,
    message_delay: int = 30,
) -> bool:
    message_at = time.monotonic() + message_delay
    for now in poll(timeout, interval=1, progress_label=progress_label):
        if server.poll() is not None:
            raise RuntimeError("the local Petkit API server stopped")
        if server_log_has_event(path, event_name, required_fields):
            return True
        if delayed_message is not None and now >= message_at:
            print(f"\n{delayed_message}")
            delayed_message = None
    return False


def require_server_event(
    path: Path,
    server: subprocess.Popen[bytes],
    *,
    required_fields: dict[str, object],
    timeout: int,
    progress_label: str,
    failure: str,
    delayed_message: str | None = None,
) -> None:
    """Wait for a stock-firmware request or raise a timeout naming *failure*."""
    if not wait_for_server_event(
        path,
        server,
        "request",
        required_fields=required_fields,
        timeout=timeout,
        progress_label=progress_label,
        delayed_message=delayed_message,
    ):
        raise InstallationTimeout(f"{failure} within {timeout} seconds")


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
    head, expected = subprocess.run(
        ["git", "rev-parse", "HEAD", f"{revision}^{{commit}}"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
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


@contextlib.contextmanager
def compat_server(
    python: Path,
    compat: Path,
    transition: Path,
    *,
    log_path: Path,
    event_log_path: Path,
    debug: bool = False,
) -> Iterator[subprocess.Popen[bytes]]:
    """Run the local Petkit API server for the duration of the block.

    Without --debug the server's output is kept in *log_path* and shown only
    when the block fails for a reason other than an expected timeout.
    """
    create_private_file(event_log_path)
    log = None
    popen_output: dict[str, object] = {}
    if not debug:
        create_private_file(log_path)
        log = open(log_path, "w", encoding="utf-8")
        popen_output = {"stdout": log, "stderr": subprocess.STDOUT}
    server: subprocess.Popen[bytes] | None = None
    try:
        print("  • Starting the local compatibility server...")
        server = subprocess.Popen(
            [
                str(python),
                "-u",
                "serve_petkit_api.py",
                "--host",
                "0.0.0.0",
                "--port",
                str(COMPAT_SERVER_PORT),
                "--profile",
                str(PROFILE),
                "--ota-image",
                str(transition),
                "--event-log",
                str(event_log_path),
            ],
            cwd=compat,
            **popen_output,
        )
        time.sleep(1)
        if server.poll() is not None:
            raise RuntimeError("the local Petkit API server did not start")
        yield server
    except BaseException as error:
        if not isinstance(error, InstallationTimeout) and log is not None:
            log.flush()
            diagnostics = log_path.read_text(encoding="utf-8").strip()
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
        if log is not None:
            log.close()


def build_kickstart_image(
    python: Path,
    esphome: Path,
    project: Path,
    kickstart: Path,
    build_env: dict[str, str],
    *,
    debug: bool = False,
) -> Path:
    """Compile the Kickstart bridge and package it as a stock-compatible V2 image."""
    print("  • Building the Kickstart transition firmware...")
    run(
        [str(esphome), "compile", "petkit-kickstart.yaml"],
        cwd=project / "esphome",
        env=build_env,
        debug=debug,
    )
    transition_elf = project / KICKSTART_ELF
    require_build_artifact(transition_elf, "Kickstart ELF")
    transition = project / RELEASE_DIR / KICKSTART_IMAGE_NAME
    print("  • Packaging the stock-compatible Kickstart image...")
    run(
        [
            str(python),
            str(kickstart / "tools/build_esp8266_nonos_v2.py"),
            *KICKSTART_IMAGE_ARGS,
            str(transition_elf),
            str(transition),
        ],
        cwd=project,
        debug=debug,
    )
    return transition


def install_kickstart(
    python: Path,
    project: Path,
    compat: Path,
    transition: Path,
    values: dict[str, str],
    hosts: list[str],
    expected_mac: str | None,
    *,
    debug: bool = False,
) -> DetectedFirmware:
    """Provision the stock feeder to fetch Kickstart and wait for it to boot."""
    print("\n🔹 Phase 1 of 3 — Install the temporary Kickstart bridge")
    print("  • Confirm how the feeder can reach this computer.")
    network = f"the {values['wifi_ssid']!r} network"
    computer_ip = prompt(f"    Computer IP address on {network}", detect_local_ip())
    server_url = f"http://{computer_ip}:{COMPAT_SERVER_PORT}"
    offset = datetime.now().astimezone().utcoffset()
    timezone_offset = str((offset.total_seconds() if offset else 0) / 3600)
    event_log_path = project / COMPAT_SERVER_EVENT_LOG
    with compat_server(
        python,
        compat,
        transition,
        log_path=project / COMPAT_SERVER_LOG,
        event_log_path=event_log_path,
        debug=debug,
    ) as server:
        print("  • Checking whether the stock feeder already contacts this computer...")
        already_provisioned = wait_for_server_event(
            event_log_path, server, "request", required_fields=STOCK_OTA_CHECK_REQUEST
        )
        if already_provisioned:
            print(
                "  ✓ The feeder was already set up to use this computer as a "
                "server; skipping Wi-Fi setup."
            )
        else:
            input(
                "\n  → Put the feeder in setup mode.\n"
                "    Press Enter after the confirmation beep.\n"
            )
            connect_to_petkit_setup_network(debug=debug)
            provision_env = os.environ.copy()
            provision_env["ESPHOME_WIFI_PASSWORD"] = values["wifi_password"]
            print("  • Sending Wi-Fi and local-server settings to the feeder...")
            acknowledged = run_provisioner(
                [
                    str(python),
                    "provision_petkit_device.py",
                    "--profile",
                    str(PROFILE),
                    "--ssid",
                    values["wifi_ssid"],
                    "--server",
                    f"{server_url}/6/",
                    "--timezone",
                    timezone_offset,
                    "--locale",
                    values["timezone"],
                    "--send",
                ],
                cwd=compat,
                env=provision_env,
                debug=debug,
            )
            print("  ✓ Wi-Fi and local-server settings sent.")
            if not acknowledged:
                print(
                    "    The SoftAP connection ended before acknowledgement. "
                    f"The installer will verify the result on {network}."
                )
            reconnect_to_regular_wifi_network(values["wifi_ssid"], debug=debug)
            print("  • Waiting for the feeder to contact this computer...")
            require_server_event(
                event_log_path,
                server,
                required_fields=STOCK_OTA_CHECK_REQUEST,
                timeout=180,
                progress_label="the feeder to contact this computer",
                failure="the feeder did not contact the local compatibility server",
                delayed_message=(
                    f"  ⚠️  No connection has arrived yet. TCP port "
                    f"{COMPAT_SERVER_PORT} on {computer_ip} must be reachable from "
                    f"{network}.\n     On another device connected to that "
                    "network, open this address in a web browser:\n\n"
                    f"       {server_url}/\n\n"
                    "     A response showing status 'ready' proves that the "
                    "server is reachable."
                ),
            )
        print("  ✓ The feeder contacted this computer.")
        print("  • Waiting for the stock firmware to accept Kickstart...")
        require_server_event(
            event_log_path,
            server,
            required_fields=STOCK_OTA_START_REQUEST,
            timeout=60,
            progress_label="the stock firmware to accept Kickstart",
            failure="the stock firmware did not accept Kickstart",
        )
        print("  ✓ The stock firmware accepted Kickstart.")
        print("  • Waiting for the feeder to download, validate, and boot Kickstart...")
        return wait_for_firmware(
            python,
            project,
            hosts,
            values["api_key"],
            KICKSTART_PROJECT,
            expected_mac,
            timeout=600,
            progress_label="the temporary Kickstart bridge",
        )


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
    print(
        "🚀 Petkit Fresh Element Mini ESPHome installer\n\n"
        "The installation has three phases:\n"
        "  1. Install Kickstart, a temporary bridge that can start from Petkit's "
        "stock firmware layout.\n"
        "  2. Prepare the feeder for the final ESPHome firmware.\n"
        "  3. Install the final ESPHome firmware here, or take control of the "
        "feeder in ESPHome Device Builder.\n"
    )
    print("📦 Preparing installation")
    print("  • Checking supporting projects...")
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
        raise SystemExit("curl is required to talk to the Kickstart bridge")

    venv = project / ".venv"
    if not venv.exists():
        print("  • Creating the Python build environment...")
        run(
            [sys.executable, "-m", "venv", str(venv)],
            cwd=project,
            debug=args.debug,
        )
    python = venv / "bin" / "python"
    esphome = venv / "bin" / "esphome"
    if installed_esphome_version(python) != ESPHOME_VERSION:
        print("  • Preparing ESPHome build dependencies...")
        run(
            [str(python), "-m", "pip", "install", f"esphome=={ESPHOME_VERSION}"],
            cwd=project,
            debug=args.debug,
        )

    print("  • Loading installation settings...")
    secrets_path = project / "esphome" / "secrets.yaml"
    secrets_display = display_path(secrets_path)
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
        raise SystemExit(f"{secrets_display} is missing: {', '.join(missing)}")
    placeholders = sorted(
        key for key, placeholder in PLACEHOLDER_SECRETS.items() if values[key] == placeholder
    )
    if placeholders:
        raise SystemExit(
            f"{secrets_display} still contains example values for: "
            f"{', '.join(placeholders)}"
        )

    build_env = os.environ.copy()
    build_env["KICKSTART_COMPONENTS_PATH"] = str((kickstart / "components").resolve())
    release = project / RELEASE_DIR
    release.mkdir(parents=True, exist_ok=True)

    state_path = project / INSTALL_STATE
    expected_mac = load_expected_mac(state_path)
    print("  • Checking for an existing Kickstart or final ESPHome installation...")
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
            f"  ✓ Final ESPHome firmware is already running at {detected.host} "
            f"on feeder {detected.identity.mac_address}."
        )
        return

    if detected is None:
        transition = build_kickstart_image(
            python, esphome, project, kickstart, build_env, debug=args.debug
        )
        detected = install_kickstart(
            python,
            project,
            compat,
            transition,
            values,
            [args.kickstart_host, args.final_host],
            expected_mac,
            debug=args.debug,
        )
    if detected.identity.project_name != KICKSTART_PROJECT:
        raise RuntimeError(
            f"expected Kickstart, but {detected.identity.project_name!r} is running"
        )
    expected_mac = normalize_mac(detected.identity.mac_address)
    save_expected_mac(state_path, detected.identity.mac_address)
    kickstart_host = detected.host
    print(
        f"  ✓ Kickstart is authenticated at {kickstart_host} on feeder "
        f"{detected.identity.mac_address}."
    )

    print("\n🔹 Phase 2 of 3 — Prepare the feeder for the final ESPHome firmware")
    hub = {
        "username": values["kickstart_web_username"],
        "password": values["kickstart_web_password"],
        "debug": args.debug,
    }
    recovery = release / f"petkit-post-kickstart-{int(time.time())}.bin"
    print("  • Saving the 2 MiB recovery image and slot status...")
    download_recovery(recovery, url=f"http://{kickstart_host}/hub/flash_read", **hub)
    if recovery.stat().st_size != 0x200000:
        raise RuntimeError("Kickstart recovery download is not 2 MiB")

    slot_status = fetch_authenticated_json(
        f"http://{kickstart_host}/hub/slot_status", **hub
    )
    status_path = recovery.with_name(f"{recovery.stem}-slot-status.json")
    write_private_text(status_path, json.dumps(slot_status, indent=2) + "\n")
    print(
        f"  ✓ Saved the recovery image and slot status "
        f"({recovery.name}, {status_path.name})."
    )

    if slot_status.get("current_slot") == 1:
        print("  • Relocating Kickstart to the upper slot...")
        post_authenticated(
            f"http://{kickstart_host}/hub/copy_lower_to_upper_slot"
            "?confirm=copy-lower-to-upper-slot",
            **hub,
        )
        kickstart_host = wait_for_slot([kickstart_host, args.kickstart_host], 2, **hub)

    print("  • Converting the feeder's flash layout from non-OS V2 to ESPHome's eboot V1...")
    post_authenticated(
        f"http://{kickstart_host}/hub/convert?confirm=convert-v2-to-eboot", **hub
    )
    kickstart_host = wait_for_conversion([kickstart_host, args.kickstart_host], **hub)

    print("\n🔹 Phase 3 of 3 — Install the final ESPHome firmware")
    if not confirm_final_install():
        print(
            "\nKickstart is ready. Install the feeder firmware through Device Builder.\n\n"
            "Until this device-builder issue is fixed, add the block below to the\n"
            "adopted YAML configuration in Device Builder first:\n"
            "https://github.com/esphome/device-builder/issues/2691\n\n"
            "api:\n"
            "  encryption:\n"
            f"    key: \"{values['api_key']}\"\n\n"
            "To install without Device Builder:\n"
            f"  cd {project}\n"
            f"  .venv/bin/esphome run esphome/petkit-feeder-local.yaml "
            f"--device {kickstart_host}\n"
        )
        return

    for step, command in (
        ("Building", ["compile", "petkit-feeder-local.yaml"]),
        ("Installing", ["upload", "petkit-feeder-local.yaml", "--device", kickstart_host]),
    ):
        print(f"  • {step} the final feeder firmware...")
        run(
            [str(esphome), *command],
            cwd=project / "esphome",
            env=build_env,
            debug=args.debug,
        )

    print("  • Waiting for the authenticated final ESPHome firmware...")
    final = wait_for_firmware(
        python,
        project,
        [args.final_host, kickstart_host],
        values["api_key"],
        FINAL_PROJECT,
        expected_mac,
        progress_label="the final ESPHome feeder firmware",
    )

    save_expected_mac(state_path, final.identity.mac_address)
    print(
        "\n✅ Installation complete.\n"
        f"Final ESPHome firmware {final.identity.project_name} is authenticated "
        f"at {final.host} on feeder {final.identity.mac_address}.\n\n"
        "⚠️  Keep the recovery image and slot status:\n"
        f"{display_path(recovery)}\n"
        f"{display_path(status_path)}\n"
    )


def cli() -> int:
    try:
        main()
    except InstallationTimeout as error:
        print(f"\n⏱️  Installation timed out: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
