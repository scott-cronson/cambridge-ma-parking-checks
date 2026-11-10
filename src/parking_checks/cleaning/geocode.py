"""Reverse geocoding with OpenStreetMap Nominatim, used only when the Shortcut sends no street.

Usage policy: at most one request per second and an identifying User-Agent.
"""

from __future__ import annotations

import logging

from ..net import DataSourceError, PoliteSession

log = logging.getLogger(__name__)
URL = "https://nominatim.openstreetmap.org/reverse"


def reverse_geocode(http: PoliteSession, lat: float, lon: float) -> tuple[str | None, str | None]:
    """Returns (street address like '201 Franklin Street', city) or (None, None) on failure."""
    try:
        r = http.get(
            URL,
            params={
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "format": "jsonv2",
                "zoom": 18,
                "addressdetails": 1,
            },
        )
        if r.status_code != 200:
            log.warning("nominatim HTTP %s", r.status_code)
            return None, None
        addr = r.json().get("address", {})
    except (DataSourceError, ValueError) as e:
        log.warning("nominatim failed: %s", e)
        return None, None
    road = addr.get("road")
    if not road:
        return None, addr.get("city") or addr.get("town")
    num = addr.get("house_number", "").split(";")[0].strip()
    street = f"{num} {road}".strip()
    return street, addr.get("city") or addr.get("town") or addr.get("village")
