"""Air quality collector — Open-Meteo (free, no API key)."""

from __future__ import annotations

from typing import Any

import requests
import structlog

log = structlog.get_logger(__name__)

# Québec City coordinates
_API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
_LAT = 46.81
_LON = -71.21


def _compute_salience(eu_aqi: float, us_aqi: float, pm25: float) -> float:
    """Map AQI to base salience."""
    if us_aqi > 150 or eu_aqi > 60:  # Unhealthy
        return 0.8
    if us_aqi > 100 or eu_aqi > 40:  # Unhealthy for sensitive
        return 0.6
    if us_aqi > 50 or eu_aqi > 20:  # Moderate
        return 0.3
    if pm25 > 25:
        return 0.4
    return 0.1


class AirQualityCollector:
    """Fetches air quality and UV index for Québec City."""

    def __init__(self) -> None:
        log.info("airquality.collector_init", lat=_LAT, lon=_LON)

    def fetch(self) -> list[dict[str, Any]]:
        """Return event dicts for air quality metrics."""
        params = (
            f"?latitude={_LAT}&longitude={_LON}"
            "&current=european_aqi,us_aqi,pm2_5,pm10,ozone,uv_index"
        )
        try:
            r = requests.get(_API_URL + params, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception:
            log.warning("airquality.fetch_failed", exc_info=True)
            return []

        current = data.get("current", {})
        if not current:
            return []

        eu_aqi = float(current.get("european_aqi", 0))
        us_aqi = float(current.get("us_aqi", 0))
        pm25 = float(current.get("pm2_5", 0))
        pm10 = float(current.get("pm10", 0))
        ozone = float(current.get("ozone", 0))
        uv = float(current.get("uv_index", 0))

        salience = _compute_salience(eu_aqi, us_aqi, pm25)

        events: list[dict[str, Any]] = [
            {
                "entity_type": "air_quality",
                "entity_id": "quebec",
                "attribute": "european_aqi",
                "value": eu_aqi,
                "confidence": salience,
            },
            {
                "entity_type": "air_quality",
                "entity_id": "quebec",
                "attribute": "us_aqi",
                "value": us_aqi,
                "confidence": salience,
            },
            {
                "entity_type": "air_quality",
                "entity_id": "quebec",
                "attribute": "pm2_5",
                "value": pm25,
                "confidence": salience,
                "unit": "μg/m³",
            },
            {
                "entity_type": "air_quality",
                "entity_id": "quebec",
                "attribute": "pm10",
                "value": pm10,
                "confidence": salience,
                "unit": "μg/m³",
            },
            {
                "entity_type": "air_quality",
                "entity_id": "quebec",
                "attribute": "ozone",
                "value": ozone,
                "confidence": salience,
                "unit": "μg/m³",
            },
            {
                "entity_type": "air_quality",
                "entity_id": "quebec",
                "attribute": "uv_index",
                "value": uv,
                "confidence": 0.4 if uv > 7 else 0.2,
            },
        ]

        log.info("airquality.fetched", eu_aqi=eu_aqi, us_aqi=us_aqi, uv=uv)
        return events
