"""Geographic helpers."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

__all__ = ["haversine_distance_m"]

_EARTH_RADIUS_M = 6_371_008.8


def haversine_distance_m(
    lat_a: float,
    lon_a: float,
    lat_b: float,
    lon_b: float,
) -> float:
    """Return the great-circle distance between two WGS84 points."""
    lat_a_rad = radians(lat_a)
    lat_b_rad = radians(lat_b)
    delta_lat = radians(lat_b - lat_a)
    delta_lon = radians(lon_b - lon_a)

    haversine = (
        sin(delta_lat / 2) ** 2
        + cos(lat_a_rad) * cos(lat_b_rad) * sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * asin(sqrt(haversine))
