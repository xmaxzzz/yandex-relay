"""HTTP client for the Yandex Relay Control4 driver (protocol: docs/DESIGN.md §4)."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=10)


class RelayError(Exception):
    """The driver could not be reached or answered with an error."""


class RelayAuthError(RelayError):
    """The pairing code was rejected (the driver was re-added or re-coded)."""


class RelayClient:
    """Talks to the driver's HTTP bridge on the controller."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, code: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._code = code

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        try:
            async with self._session.request(
                method, self._base + path, json=body,
                headers={"X-Relay-Key": self._code}, timeout=TIMEOUT,
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = {}
                if resp.status == 401:
                    raise RelayAuthError("pairing code rejected")
                if resp.status != 200:
                    raise RelayError(f"HTTP {resp.status}: {data.get('error', '')}")
                return data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise RelayError(f"cannot reach driver at {self._base}: {err}") from err

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/info")

    async def rooms(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/rooms")).get("rooms", [])

    async def pair(self, webhook_url: str, rooms: list[int] | None = None,
                   volume_cfg: dict | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"webhook_url": webhook_url, "rooms": rooms or []}
        if volume_cfg:
            body["volume_cfg"] = volume_cfg
        return await self._request("POST", "/pair", body)

    async def play(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/play", payload)

    async def pause(self, room_id: int) -> dict[str, Any]:
        return await self._request("POST", "/pause", {"room_id": room_id})

    async def resume(self, room_id: int) -> dict[str, Any]:
        return await self._request("POST", "/resume", {"room_id": room_id})

    async def stop(self, room_id: int) -> dict[str, Any]:
        return await self._request("POST", "/stop", {"room_id": room_id})

    async def state(self, room_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/state?room_id={room_id}")

    async def station_volume(self, room_id: int, level: float, initial: bool) -> dict[str, Any]:
        return await self._request("POST", "/station_volume",
                                   {"room_id": room_id, "level": level, "initial": initial})

    async def volume_step(self, room_id: int, steps: int) -> dict[str, Any]:
        return await self._request("POST", "/volume_step", {"room_id": room_id, "steps": steps})

    async def volume_level(self, room_id: int, level: float) -> dict[str, Any]:
        return await self._request("POST", "/volume_level", {"room_id": room_id, "level": level})

    async def heartbeat(self, webhook_url: str, summary: str) -> dict[str, Any]:
        return await self._request("POST", "/heartbeat", {"webhook_url": webhook_url, "summary": summary})

    async def alarms(self, room_id: int, alarms: list[dict]) -> dict[str, Any]:
        return await self._request("POST", "/alarms", {"room_id": room_id, "alarms": alarms})

    async def duck(self, room_id: int, active: bool) -> dict[str, Any]:
        return await self._request("POST", "/duck", {"room_id": room_id, "active": active})
