"""Forecast helpers used by the Precog demo."""

from __future__ import annotations

import httpx

API = "https://api.open-meteo.com/v1/forecast"


def celsius_to_fahrenheit(c: float) -> float:
    return round(c * 9 / 5 + 32, 1)


def describe(temp_c: float | None) -> str:
    match temp_c:
        case None:
            return "unknown"
        case t if t < 0:
            return "freezing"
        case t if t < 18:
            return "cool"
        case t if t < 28:
            return "warm"
        case _:
            return "hot"


def client(timeout: float = 5.0) -> httpx.Client:
    return httpx.Client(base_url=API, timeout=timeout)
