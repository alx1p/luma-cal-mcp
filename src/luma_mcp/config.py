from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    luma_web_session: str | None = None

    default_city: str | None = None
    default_category: str | None = None
    default_center_lat: float | None = None
    default_center_lon: float | None = None
    default_center_address: str | None = None
    default_max_distance_miles: float | None = None
    default_keywords: list[str] = field(default_factory=list)

    geocoding_provider: str = "nominatim"
    geocoding_api_key: str | None = None

    @property
    def web_session_configured(self) -> bool:
        return bool(self.luma_web_session)


def load_config() -> Config:
    """Load configuration from environment variables (.env supported)."""
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    keywords_raw = os.getenv("DEFAULT_KEYWORDS", "")
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()] if keywords_raw else []

    lat = os.getenv("DEFAULT_CENTER_LAT")
    lon = os.getenv("DEFAULT_CENTER_LON")

    return Config(
        luma_web_session=os.getenv("LUMA_WEB_SESSION") or None,
        default_city=os.getenv("DEFAULT_CITY") or None,
        default_category=os.getenv("DEFAULT_CATEGORY") or None,
        default_center_lat=float(lat) if lat else None,
        default_center_lon=float(lon) if lon else None,
        default_center_address=os.getenv("DEFAULT_CENTER_ADDRESS") or None,
        default_max_distance_miles=_opt_float(os.getenv("DEFAULT_MAX_DISTANCE_MILES")),
        default_keywords=keywords,
        geocoding_provider=os.getenv("GEOCODING_PROVIDER", "nominatim").lower(),
        geocoding_api_key=os.getenv("GEOCODING_API_KEY") or None,
    )


def _opt_float(val: str | None) -> float | None:
    if val is None or val.strip() == "":
        return None
    return float(val)
