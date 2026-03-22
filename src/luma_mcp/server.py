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
_web_client_cookie: Optional[str] = None
_event_store: Optional[EventStore] = None

_VALIDATION_MAX_AGE = timedelta(hours=24)
_NEVER_TIMESTAMP = "9999-12-31T23:59:59+00:00"


def _get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _get_event_store() -> EventStore:
    global _event_store
    if _event_store is None:
        cfg = _get_config()
        db_path = Path(cfg.event_store_path) if cfg.event_store_path else None
        _event_store = EventStore(db_path=db_path)
    return _event_store


def _get_web_client(session_cookie: Optional[str] = None) -> LumaWebClient:
    """Return a LumaWebClient, recreating it if the cookie changed."""
    global _web_client, _web_client_cookie
    if _web_client is None or session_cookie != _web_client_cookie:
        _web_client_cookie = session_cookie
        _web_client = LumaWebClient(session_cookie)
    return _web_client


def _get_stored_cookie(store: EventStore) -> Optional[str]:
    row = store.get_setting("luma_session")
    return row[0] if row else None


def _geocode_fn(address: str) -> tuple[Optional[float], Optional[float]]:
    cfg = _get_config()
    return geocode(address, provider=cfg.geocoding_provider, api_key=cfg.geocoding_api_key)


# --------------------------------------------------------------------------
# Session management helpers
# --------------------------------------------------------------------------

async def _resolve_session(
    store: EventStore,
    messages: list[str],
    *,
    no_login: bool,
    login: bool,
    skip_login_days: Optional[int],
) -> Optional[str]:
    """Determine the session cookie to use for subscribed calendars.

    Returns the cookie value if available, or None to skip subscribed calendars.
    May add user-facing prompts to ``messages``.
    """
    if no_login:
        return None

    # Handle explicit skip_login_days from a prior prompt answer
    if skip_login_days is not None:
        if skip_login_days < 0:
            store.set_setting("luma_login_declined_until", _NEVER_TIMESTAMP)
        elif skip_login_days == 0:
            store.delete_setting("luma_login_declined_until")
        else:
            until = datetime.now(tz=timezone.utc) + timedelta(days=skip_login_days)
            store.set_setting("luma_login_declined_until", until.isoformat())
        return None

    # Handle explicit login=true from a prior prompt answer
    if login:
        cookie = _do_browser_login(store, messages)
        return cookie

    # Check if user previously declined and window is still active
    declined_row = store.get_setting("luma_login_declined_until")
    if declined_row:
        declined_until = datetime.fromisoformat(declined_row[0])
        if datetime.now(tz=timezone.utc) < declined_until:
            return None
        # Window expired — clear it so we re-prompt
        store.delete_setting("luma_login_declined_until")

    # Try existing cookie
    cookie = _get_stored_cookie(store)
    if cookie:
        valid = await _validate_if_stale(store, cookie, messages)
        if valid:
            return cookie
        # Cookie expired — clear it
        store.delete_setting("luma_session")
        store.delete_setting("luma_session_validated")

    # No valid cookie — check if user ever logged in before
    had_cookie_row = store.get_setting("luma_login_had_cookie")
    if had_cookie_row and had_cookie_row[0] == "true":
        # Returning user with expired cookie — auto-relogin
        messages.append(
            "Your Luma session expired. Opening browser to re-authenticate..."
        )
        cookie = _do_browser_login(store, messages)
        return cookie

    # First time ever, or previously declined and window expired — prompt
    messages.append(
        "Would you like to log in to Luma to also see events from your "
        "subscribed calendars? (Y/n)\n\n"
        "Call search_events with login=true to log in, or "
        "skip_login_days=N to decline (0 = ask next time, -1 = never ask again)."
    )
    return None


async def _validate_if_stale(
    store: EventStore,
    cookie: str,
    messages: list[str],
) -> bool:
    """Validate stored cookie if last validation is older than 24h. Returns True if valid."""
    validated_row = store.get_setting("luma_session_validated")
    if validated_row:
        validated_at = validated_row[1]
        if datetime.now(tz=timezone.utc) - validated_at < _VALIDATION_MAX_AGE:
            return True

    from luma_mcp.auth import validate_session
    valid = await validate_session(cookie)
    if valid:
        store.set_setting("luma_session_validated", datetime.now(tz=timezone.utc).isoformat())
        return True
    return False


def _do_browser_login(store: EventStore, messages: list[str]) -> Optional[str]:
    """Launch browser login, persist cookie on success."""
    try:
        from luma_mcp.auth import browser_login
        cookie = browser_login()
        store.set_setting("luma_session", cookie)
        store.set_setting("luma_session_validated", datetime.now(tz=timezone.utc).isoformat())
        store.set_setting("luma_login_had_cookie", "true")
        global _web_client, _web_client_cookie
        _web_client = None
        _web_client_cookie = None
        return cookie
    except ImportError as e:
        messages.append(str(e))
        return None
    except TimeoutError as e:
        messages.append(str(e))
        return None


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
    login: bool = False,
    skip_login_days: Optional[int] = None,
    no_login: bool = False,
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
        login: Set to true to open a browser and log in to Luma for subscribed calendar access.
        skip_login_days: Decline Luma login for N days. 0 = ask next time, -1 = never ask again.
        no_login: If true, skip subscribed calendars for this call without changing stored preferences.
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

    store = _get_event_store()

    # Resolve session cookie for subscribed calendars
    session_cookie: Optional[str] = None
    want_subscribed = source in ("all", "subscribed")
    if want_subscribed:
        session_cookie = await _resolve_session(
            store, messages,
            no_login=no_login, login=login, skip_login_days=skip_login_days,
        )

    if source in ("all", "discover"):
        try:
            web = _get_web_client(session_cookie)
            discover = await web.discover_events(
                geo_region_slug=city,
                category=category,
                after=after_dt,
                before=before_dt,
            )
            event_lists.append(discover)
        except Exception as e:
            messages.append(f"Discover source error: {e}")

    if want_subscribed and session_cookie:
        try:
            web = _get_web_client(session_cookie)
            subscribed = await web.subscribed_calendar_events()
            event_lists.append(subscribed)
        except Exception as e:
            messages.append(f"Subscribed calendars error: {e}")

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
