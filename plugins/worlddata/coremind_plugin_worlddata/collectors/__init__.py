"""World Data plugin — collectors package."""

from __future__ import annotations

from coremind_plugin_worlddata.collectors.airquality import AirQualityCollector
from coremind_plugin_worlddata.collectors.crypto import CryptoCollector
from coremind_plugin_worlddata.collectors.gasprice import GasPriceCollector
from coremind_plugin_worlddata.collectors.traffic import TrafficCollector

__all__ = [
    "AirQualityCollector",
    "CryptoCollector",
    "GasPriceCollector",
    "TrafficCollector",
]
