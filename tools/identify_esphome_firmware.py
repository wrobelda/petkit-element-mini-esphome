#!/usr/bin/env python3
"""Return authenticated ESPHome device identity as JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from aioesphomeapi import APIClient
from aioesphomeapi.core import (
    APIConnectionError,
    ResolveAPIError,
    SocketAPIError,
    TimeoutAPIError,
)


UNREACHABLE_EXIT = 4


async def read_identity(host: str, timeout: float) -> dict[str, str]:
    api_key = os.environ.get("ESPHOME_API_KEY")
    if not api_key:
        raise RuntimeError("ESPHOME_API_KEY is required")

    client = APIClient(
        host,
        6053,
        "",
        client_info="Petkit guided installer",
        noise_psk=api_key,
        provide_time=False,
    )
    try:
        await asyncio.wait_for(client.connect(login=True), timeout=timeout)
        info = await asyncio.wait_for(client.device_info(), timeout=timeout)
        return {
            "name": info.name,
            "friendly_name": info.friendly_name,
            "mac_address": info.mac_address,
            "project_name": info.project_name,
            "project_version": info.project_version,
        }
    finally:
        await client.disconnect(force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    try:
        identity = asyncio.run(read_identity(args.host, args.timeout))
    except (ResolveAPIError, SocketAPIError, TimeoutAPIError, TimeoutError, OSError):
        raise SystemExit(UNREACHABLE_EXIT) from None
    except APIConnectionError as error:
        raise SystemExit(f"ESPHome API authentication failed: {error}") from error
    print(json.dumps(identity, separators=(",", ":")))


if __name__ == "__main__":
    main()
