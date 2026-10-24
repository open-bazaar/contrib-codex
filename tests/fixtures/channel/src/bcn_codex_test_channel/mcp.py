"""A weather MCP tool backed by the live National Weather Service API."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx2
from mcp.server import MCPServer

root = Path(sys.argv[1]).resolve()
server = MCPServer("weather")


@server.tool()
async def get_forecast(latitude: float, longitude: float) -> dict[str, Any]:
    """Get the weather forecast for a US location by latitude and longitude."""
    points_url = f"https://api.weather.gov/points/{latitude},{longitude}"
    async with httpx2.AsyncClient(
        headers={
            "User-Agent": "bcn-codex-acceptance/0.3 (https://github.com/open-bazaar/contrib-codex)",
            "Accept": "application/geo+json",
        },
        timeout=30.0,
    ) as client:
        points_response = await client.get(points_url)
        points_response.raise_for_status()
        points = points_response.json()
        forecast_url = points["properties"]["forecast"]
        forecast_response = await client.get(forecast_url)
        forecast_response.raise_for_status()
        forecast = forecast_response.json()
    result = {
        "latitude": latitude,
        "longitude": longitude,
        "forecast_url": forecast_url,
        "updated_at": forecast["properties"]["updateTime"],
        "periods": forecast["properties"]["periods"][:3],
    }
    record = {
        "arguments": {"latitude": latitude, "longitude": longitude},
        "points_url": points_url,
        "points_status": points_response.status_code,
        "points_response": points,
        "forecast_status": forecast_response.status_code,
        "forecast_response": forecast,
        "result": result,
    }

    def retain_response():
        with (root / "weather-requests.jsonl").open("a") as log:
            log.write(json.dumps(record) + "\n")

    await asyncio.to_thread(retain_response)
    return result


if __name__ == "__main__":
    server.run()
