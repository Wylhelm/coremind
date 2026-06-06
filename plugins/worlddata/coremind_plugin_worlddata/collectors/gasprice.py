"""Gas price collector — scrapes CAA-Québec Info Essence (JSON-LD structured data)."""

from __future__ import annotations

import os
import re
from typing import Any

import requests
import structlog

log = structlog.get_logger(__name__)

_URL = "https://www.caaquebec.com/fr/mobilite/info-essence"
_REGION = "Capitale-Nationale"  # Guillaume's region
_TIMEOUT = 15
# Spread between regular and super/premium (91 octane) in CAD/L.
# CAA only publishes regular; super is estimated from market spread.
_SUPER_SPREAD = float(os.environ.get("WORLDDATA_GASPRICE_SUPER_SPREAD", "0.14"))


def _compute_salience(
    avg_price: float,
    realistic_price: float,
    code: str,
) -> float:
    """Map price difference and recommendation to salience."""
    diff = avg_price - realistic_price

    if code == "B":  # Below realistic price — good time to fill up
        return 0.2
    if code == "H":  # High above realistic price
        return 0.6

    # "M" = neutral
    if diff > 10:  # 10¢ above realistic
        return 0.5
    if diff > 5:  # 5¢ above
        return 0.3
    return 0.1


class GasPriceCollector:
    """Fetches Québec gas prices from CAA-Québec Info Essence."""

    def __init__(self) -> None:
        self._last_prices: dict[str, float] = {}
        log.info("gasprice.collector_init", region=_REGION)

    def fetch(self) -> list[dict[str, Any]]:
        """Return event dicts for gas price metrics in Capitale-Nationale."""
        try:
            r = requests.get(_URL, timeout=_TIMEOUT, headers={
                "User-Agent": "CoreMind-WorldData/0.1 (+https://github.com/galilai-group/coremind)",
            })
            r.raise_for_status()
            html = r.text
        except Exception:
            log.warning("gasprice.fetch_failed", exc_info=True)
            return []

        # Extract JSON-LD blob from the page
        # It's in: <script type="application/ld+json">{...}</script>
        match = re.search(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            log.warning("gasprice.no_jsonld")
            return []

        try:
            import json
            data = json.loads(match.group(1))
        except Exception:
            log.warning("gasprice.jsonld_parse_failed", exc_info=True)
            return []

        # Navigate to the Capitale-Nationale entry
        items = data.get("mainEntity", {}).get("itemListElement", [])
        events: list[dict[str, Any]] = []

        for item in items:
            region_data = item.get("item", {})
            region_name = region_data.get("name", "")
            if _REGION.lower() not in region_name.lower():
                continue

            props = region_data.get("additionalProperty", [])
            avg_price = 0.0
            realistic_price = 0.0
            code = "M"

            for prop in props:
                pname = prop.get("name", "")
                pval = prop.get("value", 0)
                if pname == "prixMoyenCourant":
                    avg_price = float(pval)
                elif pname == "prixRealiste":
                    realistic_price = float(pval)
                elif pname == "codeRecommandation":
                    code = str(pval)

            if not avg_price:
                log.warning("gasprice.no_data", region=region_name)
                continue

            salience = _compute_salience(avg_price, realistic_price, code)

            # Check if unchanged
            prev_avg = self._last_prices.get("avg")
            prev_real = self._last_prices.get("realistic")
            if prev_avg == avg_price and prev_real == realistic_price:
                continue
            self._last_prices["avg"] = avg_price
            self._last_prices["realistic"] = realistic_price

            # Price in $/L (API returns cents/L)
            # Super/premium is ESTIMATED: CAA only publishes regular gas.
            # Spread configurable via WORLDDATA_GASPRICE_SUPER_SPREAD env var.
            super_price = round(avg_price / 100 + _SUPER_SPREAD, 3)
            events = [
                {
                    "entity_type": "gas_price",
                    "entity_id": "quebec",
                    "attribute": "avg_price_per_liter",
                    "value": round(avg_price / 100, 3),
                    "unit": "CAD/L",
                    "confidence": salience,
                },
                {
                    "entity_type": "gas_price",
                    "entity_id": "quebec",
                    "attribute": "realistic_price_per_liter",
                    "value": round(realistic_price / 100, 3),
                    "unit": "CAD/L",
                    "confidence": salience,
                },
                {
                    "entity_type": "gas_price",
                    "entity_id": "quebec",
                    "attribute": "super_price_per_liter",
                    "value": super_price,
                    "unit": "CAD/L",
                    "confidence": salience * 0.8,
                    "metadata": {
                        "method": "estimated",
                        "spread": _SUPER_SPREAD,
                        "note": "CAA only publishes regular; super is estimated from market spread",
                    },
                },
                {
                    "entity_type": "gas_price",
                    "entity_id": "quebec",
                    "attribute": "recommendation",
                    "value": code,
                    "confidence": salience,
                },
            ]
            log.info(
                "gasprice.fetched",
                avg=round(avg_price / 100, 3),
                super=super_price,
                realistic=round(realistic_price / 100, 3),
                code=code,
            )
            break  # Found our region, stop looking

        return events
