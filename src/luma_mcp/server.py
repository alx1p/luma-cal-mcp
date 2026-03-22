"""Luma MCP server — 3 tools for event discovery, details, and calendar export."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from luma_mcp.config import Config, load_config
from luma_mcp.event_store import EventStore
from luma_mcp.geo import filter_by_distance, filter_by_keywords, resolve_center
from luma_mcp.geocode import geocode
from luma_mcp.ics import build_ics
from luma_mcp.luma_web_client import LumaWebClient
from luma_mcp.models import LumaEvent, merge_events

mcp = FastMCP(name="Luma Events")

_config: Optional[Config] = None
_web_client: Optional[LumaWebClient] = None
_event_store: Optional[EventStore] = None


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _get_web_client() -> LumaWebClient:
    global _web_client
    if _web_client is None:
        _web_client = LumaWebClient(_get_config().luma_web_session)
    return _web_client


def _get_event_store() -> EventStore:
    global _event_store
    if _event_store is None:
        cfg = _get_config()
        db_path = Path(cfg.event_store_path) if cfg.event_store_path else None
        _event_store = EventStore(db_path=db_path)
    return _event_store


def _geocode_fn(address: str) -> tuple[Optional[float], Optional[float]]:
    cfg = _get_config()
    return geocode(address, provider=cfg.geocoding_provider, api_key=cfg.geocoding_api_key)


# --------------------------------------------------------------------------
# Tool: search_events
# --------------------------------------------------------------------------


@mcp.tool
async def search_events(
    city: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    center_lat: Optional[float] = None,
    center_lon: Optional[float] = None,
    center_address: Optional[str] = None,
    max_distance_miles: Optional[float] = None,
    keywords: Optional[list[str]] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    exclude_unknown_location: bool = False,
    added_within_days: Optional[float] = None,
    new_only: bool = False,
) -> dict:
    """Search for Luma events from Discover and/or subscribed calendars.

    Merges events from configured sources, filters by distance from a center
    point (coordinates or geocoded address) and optional keywords.

    Args:
        city: Discover region slug (e.g. "sf-bay-area", "new-york"). Falls back to DEFAULT_CITY env.
        category: Discover category filter (e.g. "ai", "tech", "crypto"). Falls back to DEFAULT_CATEGORY env.
        source: Which sources to query: "discover", "subscribed", or "all" (default).
        center_lat: Latitude of the center point for distance filtering.
        center_lon: Longitude of the center point for distance filtering.
        center_address: Street address to geocode as the center point (used if lat/lon not provided).
        max_distance_miles: Maximum distance in miles from the center. Events beyond this are excluded.
        keywords: List of keywords to filter by (matches title/description). Empty list means no keyword filter.
        after: ISO 8601 datetime — only events starting after this time.
        before: ISO 8601 datetime — only events starting before this time.
        exclude_unknown_location: If true, drop events that have no coordinates.
        added_within_days: Only return events first seen within this many days (e.g. 5). Requires prior runs to populate the store.
        new_only: If true, only return events that have never been seen before (first appearance this run).
    """
    cfg = _get_config()
    city = city or cfg.default_city
    category = category or cfg.default_category
    source = source or "all"
    kw = keywords if keywords is not None else cfg.default_keywords

    after_dt = datetime.fromisoformat(after) if after else None
    before_dt = datetime.fromisoformat(before) if before else None

    resolved_lat, resolved_lon = resolve_center(
        center_lat or cfg.default_center_lat,
        center_lon or cfg.default_center_lon,
        center_address or cfg.default_center_address,
        geocode_fn=_geocode_fn,
    )
    max_dist = max_distance_miles or cfg.default_max_distance_miles

    event_lists: list[list[LumaEvent]] = []
    messages: list[str] = []

    if source in ("all", "discover"):
        try:
            web = _get_web_client()
            discover = await web.discover_events(
                geo_region_slug=city,
                category=category,
                after=after_dt,
                before=before_dt,
            )
            event_lists.append(discover)
        except Exception as e:
            messages.append(f"Discover source error: {e}")

    if source in ("all", "subscribed"):
        if cfg.web_session_configured:
            try:
                web = _get_web_client()
                subscribed = await web.subscribed_calendar_events()
                event_lists.append(subscribed)
            except Exception as e:
                messages.append(f"Subscribed calendars error: {e}")
        else:
            messages.append(
                "Subscribed calendars unavailable — set LUMA_WEB_SESSION in env to enable."
            )

    events = merge_events(event_lists)

    if after_dt:
        events = [e for e in events if e.start_at >= after_dt]
    if before_dt:
        events = [e for e in events if e.start_at <= before_dt]

    if resolved_lat is not None and resolved_lon is not None and max_dist is not None:
        events = filter_by_distance(
            events,
            resolved_lat,
            resolved_lon,
            max_dist,
            exclude_unknown_location=exclude_unknown_location,
        )

    if kw:
        events = filter_by_keywords(events, kw)

    events.sort(key=lambda e: e.start_at)

    summaries = [_event_summary(e) for e in events]

    store = _get_event_store()
    new_urls = set(store.record(summaries))
    seen_times = store.first_seen_batch([s["url"] for s in summaries])

    for s in summaries:
        fs = seen_times.get(s["url"])
        s["first_seen_at"] = fs.isoformat() if fs else None
        s["is_new"] = s["url"] in new_urls

    if new_only:
        summaries = [s for s in summaries if s["is_new"]]

    if added_within_days is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=added_within_days)
        summaries = [
            s for s in summaries
            if s["first_seen_at"] and datetime.fromisoformat(s["first_seen_at"]) >= cutoff
        ]

    return {
        "events": summaries,
        "count": len(summaries),
        "messages": messages or None,
    }


# --------------------------------------------------------------------------
# Tool: get_event
# --------------------------------------------------------------------------


@mcp.tool
async def get_event(event_id: Optional[str] = None, url: Optional[str] = None) -> dict:
    """Get full details for a single Luma event.

    Args:
        event_id: Luma event API id (e.g. "evt-abc123").
        url: lu.ma event URL or slug (e.g. "https://lu.ma/myevent" or "myevent").
    """
    if not event_id and not url:
        return {"error": "Provide either event_id or url."}

    resolved_id = event_id
    if not resolved_id and url:
        resolved_id = _extract_event_id_from_url(url)

    web = _get_web_client()
    event = await web.get_event(resolved_id)  # type: ignore[arg-type]

    if event is None:
        return {"error": f"Event not found: {event_id or url}"}

    return _event_detail(event)


# --------------------------------------------------------------------------
# Tool: export_event_ics
# --------------------------------------------------------------------------


@mcp.tool
async def export_event_ics(event_id: Optional[str] = None, url: Optional[str] = None) -> dict:
    """Generate an ICS calendar string for a Luma event (Add to Calendar).

    Args:
        event_id: Luma event API id (e.g. "evt-abc123").
        url: lu.ma event URL or slug.
    """
    detail = await get_event(event_id=event_id, url=url)
    if "error" in detail:
        return detail

    event = LumaEvent(
        id=detail["id"],
        url=detail["url"],
        source=detail["source"],
        title=detail["title"],
        description=detail.get("description", ""),
        start_at=detail["start_at"],
        end_at=detail.get("end_at"),
        timezone=detail.get("timezone"),
        lat=detail.get("lat"),
        lon=detail.get("lon"),
        location_label=detail.get("location_label"),
        full_address=detail.get("full_address"),
        cover_url=detail.get("cover_url"),
    )

    ics_string = build_ics(event)
    return {
        "ics": ics_string,
        "event_title": event.title,
        "event_url": event.canonical_url,
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _event_summary(event: LumaEvent) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat() if event.end_at else None,
        "timezone": event.timezone,
        "location": event.location_label,
        "full_address": event.full_address,
        "distance_miles": event.distance_miles,
        "url": event.canonical_url,
        "source": event.source.value,
    }


def _event_detail(event: LumaEvent) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "start_at": event.start_at.isoformat(),
        "end_at": event.end_at.isoformat() if event.end_at else None,
        "timezone": event.timezone,
        "lat": event.lat,
        "lon": event.lon,
        "location_label": event.location_label,
        "full_address": event.full_address,
        "cover_url": event.cover_url,
        "url": event.canonical_url,
        "rsvp_url": event.canonical_url,
        "source": event.source.value,
    }


def _extract_event_id_from_url(url: str) -> str:
    """Extract event api_id or slug from a lu.ma URL or bare slug."""
    url = url.strip()
    if url.startswith("http"):
        parts = url.rstrip("/").split("/")
        return parts[-1]
    return url


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
