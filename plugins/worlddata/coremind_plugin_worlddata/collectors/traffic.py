"""Traffic collector — fetches Québec 511 camera/road conditions (open data)."""

from __future__ import annotations

from typing import Any

import requests
import structlog

log = structlog.get_logger(__name__)

# Québec 511 open data — WFS endpoints (GeoJSON)
# ms:infos_cameras = liste des caméras (images statiques)
_CAMERAS_URL = (
    "https://ws.mapserver.transports.gouv.qc.ca/swtq"
    "?service=wfs&version=2.0.0&request=getfeature"
    "&typename=ms:infos_cameras&outputformat=geojson"
)
# ms:evenements = entraves et événements de circulation (conditions, fermetures, travaux)
_CONDITIONS_URL = (
    "https://ws.mapserver.transports.gouv.qc.ca/swtq"
    "?service=wfs&version=2.0.0&request=getfeature"
    "&typename=ms:evenements&outputformat=geojson"
)

_REGION_FILTER = "Capitale-Nationale"


def _extract_camera_events(features: list[Any]) -> list[dict[str, Any]]:
    """Build events from camera WFS features."""
    events: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties", {})
        region = str(props.get("NomRegionDiffusion", ""))
        if _REGION_FILTER.lower() not in region.lower():
            continue
        cam_id = str(props.get("NumeroCamera", props.get("IDEcamera", "")))
        if not cam_id:
            continue
        snapshot_url = str(props.get("URL_FLUX_DONNEE", ""))
        status = "active" if snapshot_url else "inactive"

        events.append({
            "entity_type": "traffic_camera",
            "entity_id": f"camera_{cam_id}",
            "attribute": "status",
            "value": status,
            "confidence": 0.2,
        })
        if snapshot_url:
            events.append({
                "entity_type": "traffic_camera",
                "entity_id": f"camera_{cam_id}",
                "attribute": "snapshot_url",
                "value": snapshot_url,
                "confidence": 0.2,
            })
    return events


def _extract_event_features(features: list[Any]) -> list[dict[str, Any]]:
    """Build events from road-event WFS features (entraves)."""
    events: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties", {})
        regions = str(props.get("regions", ""))
        if _REGION_FILTER.lower() not in regions.lower():
            continue

        ev_id = str(props.get("identifiant", "unknown"))
        entrave = str(props.get("entrave", ""))
        cause = str(props.get("cause", ""))
        localisation = str(props.get("localisation", ""))
        route = str(props.get("numeroRoute", ""))

        salience = 0.3
        if any(kw in entrave.lower() for kw in ("fermée", "ferm", "bloquée", "déviation")):
            salience = 0.8
        elif any(kw in entrave.lower() for kw in ("alternance", "circulation réduite", "voie unique")):
            salience = 0.5
        elif any(kw in cause.lower() for kw in ("accident", "collision", "véhicule", "camion")):
            salience = 0.7

        events.append({
            "entity_type": "road_event",
            "entity_id": f"event_{ev_id}",
            "attribute": "entrave",
            "value": entrave,
            "confidence": salience,
        })
        if localisation:
            events.append({
                "entity_type": "road_event",
                "entity_id": f"event_{ev_id}",
                "attribute": "localisation",
                "value": localisation,
                "confidence": salience,
            })
        if route:
            events.append({
                "entity_type": "road_event",
                "entity_id": f"event_{ev_id}",
                "attribute": "route",
                "value": route,
                "confidence": salience,
            })
    return events[:10]


class TrafficCollector:
    """Fetches Québec 511 cameras and road events for Capitale-Nationale."""

    def __init__(self) -> None:
        log.info("traffic.collector_init", region=_REGION_FILTER)

    def fetch(self) -> list[dict[str, Any]]:
        """Return event dicts for cameras and road events."""
        events: list[dict[str, Any]] = []

        # Fetch camera statuses (WFS GeoJSON)
        try:
            r = requests.get(_CAMERAS_URL, timeout=15)
            r.raise_for_status()
            raw = r.json()
            cameras_features = raw.get("features", []) if isinstance(raw, dict) else []
        except Exception:
            log.warning("traffic.cameras_fetch_failed", exc_info=True)
            cameras_features = []

        # Fetch road events/conditions (WFS GeoJSON)
        try:
            r = requests.get(_CONDITIONS_URL, timeout=15)
            r.raise_for_status()
            raw = r.json()
            events_features = raw.get("features", []) if isinstance(raw, dict) else []
        except Exception:
            log.warning("traffic.events_fetch_failed", exc_info=True)
            events_features = []

        events.extend(_extract_camera_events(cameras_features))
        events.extend(_extract_event_features(events_features))

        if events:
            log.info("traffic.fetched", events=len(events))
        return events
