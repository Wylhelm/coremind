"""Crypto collector — fetches prices from CoinGecko (free API)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
import structlog

log = structlog.get_logger(__name__)

_API_URL = "https://api.coingecko.com/api/v3/simple/price"
_DEFAULT_COINS = ["bitcoin", "ethereum"]
_DEFAULT_VS = ["cad", "usd"]


def _compute_salience(change_24h_pct: float | None) -> float:
    """Map 24h change to a base salience score."""
    if change_24h_pct is None:
        return 0.1
    delta = abs(change_24h_pct)
    if delta > 10:
        return 0.7
    if delta > 5:
        return 0.5
    if delta < 1:
        return 0.1
    return 0.2


class CryptoCollector:
    """Fetches crypto prices and emits typed events."""

    def __init__(self, coins: list[str] | None = None) -> None:
        self._coins = coins or _DEFAULT_COINS
        self._last_values: dict[str, dict[str, float]] = {}
        log.info("crypto.collector_init", coins=self._coins)

    def fetch(self) -> list[dict[str, Any]]:
        """Return a list of event dicts (one per coin x currency).

        Each dict has keys: entity_type, entity_id, attribute, value, confidence, unit.
        """
        url = f"{_API_URL}?ids={','.join(self._coins)}&vs_currencies={','.join(_DEFAULT_VS)}&include_24hr_change=true"
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception:
            log.warning("crypto.fetch_failed", exc_info=True)
            return []

        events: list[dict[str, Any]] = []
        for coin in self._coins:
            coin_data = data.get(coin, {})
            if not coin_data:
                continue

            change_pct = coin_data.get("cad_24h_change") or coin_data.get("usd_24h_change")
            salience = _compute_salience(change_pct)

            # Price events
            for vs in _DEFAULT_VS:
                price = coin_data.get(vs)
                if price is None:
                    continue
                key = f"{coin}_{vs}"
                prev = self._last_values.get(key, {})
                if prev.get("price") == price and prev.get("change") == change_pct:
                    continue  # unchanged — skip
                prev["price"] = price
                prev["change"] = change_pct
                self._last_values[key] = prev

                events.append(
                    {
                        "entity_type": "crypto",
                        "entity_id": coin,
                        "attribute": f"price_{vs}",
                        "value": price,
                        "unit": vs.upper(),
                        "confidence": max(0.5, salience),
                    }
                )

            # 24h change event (one per coin)
            if change_pct is not None:
                events.append(
                    {
                        "entity_type": "crypto",
                        "entity_id": coin,
                        "attribute": "change_24h_pct",
                        "value": change_pct,
                        "unit": "%",
                        "confidence": max(0.5, salience),
                    }
                )

        if events:
            log.info("crypto.fetched", events=len(events))
        return events
