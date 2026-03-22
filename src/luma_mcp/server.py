"""Luma MCP server — tools for event discovery, details, preferences, and calendar export."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from luma_mcp.config import Config, load_config
from luma_mcp.event_store import EventStore
from luma_mcp.geo import filter_by_distance, filter_by_keywords, haversine_miles
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

LUMA_CITY_CENTERS: dict[str, tuple[float, float]] = {
    "sf-bay-area": (37.7749, -122.4194),
    "new-york": (40.7128, -74.0060),
    "los-angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "seattle": (47.6062, -122.3321),
    "austin": (30.2672, -97.7431),
    "boston": (42.3601, -71.0589),
    "miami": (25.7617, -80.1918),
    "denver": (39.7392, -104.9903),
    "london": (51.5074, -0.1278),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "tokyo": (35.6762, 139.6503),
    "singapore": (1.3521, 103.8198),
    "sydney": (-33.8688, 151.2093),
    "toronto": (43.6532, -79.3832),
    "amsterdam": (52.3676, 4.9041),
}


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


def _stored_default(store: EventStore, key: str) -> Optional[str]:
    row = store.get_setting(key)
    return row[0] if row else None


def _nearest_city(lat: float, lon: float) -> str:
    """Return the Luma city slug closest to the given coordinates."""
    best_slug = "sf-bay-area"
    best_dist = float("inf")
    for slug, (clat, clon) in LUMA_CITY_CENTERS.items():
        d = haversine_miles(lat, lon, clat, clon)
        if d < best_dist:
            best_dist = d
            best_slug = slug
    return best_slug


# --------------------------------------------------------------------------
# Preference resolution
# --------------------------------------------------------------------------


def _resolve_defaults(
    store: EventStore,
    messages: list[str],
    *,
    city: Optional[str],
    category: Optional[str],
    center_address: Optional[str],
    max_distance_miles: Optional[float],
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[float], bool]:
    """Resolve city/category/address/distance with precedence: param > stored DB default.

    If no city or address is available from any source, adds a prompt for the
    agent telling it to call set_preferences.  Returns ``needs_location=True``
    so the caller can short-circuit.

    Returns (city, category, address, max_distance_miles, needs_location).
    """
    stored_city = _stored_default(store, "default_city")
    stored_category = _stored_default(store, "default_category")
    stored_address = _stored_default(store, "default_center_address")
    stored_distance = _stored_default(store, "default_max_distance_miles")

    eff_city = city or stored_city
    eff_category = category or stored_category
    eff_address = center_address or stored_address
    eff_distance = max_distance_miles or (float(stored_distance) if stored_distance else None)

    has_location = eff_city or eff_address
    if not has_location:
        city_list = ", ".join(LUMA_CITY_CENTERS.keys())
        messages.append(
            "[agent] No location configured. Ask the user where they want to "
            "search, then call set_preferences.\n\n"
            "Option 1 — by city/region:\n"
            f"  set_preferences(city=\"<slug>\")  available: {city_list}\n\n"
            "Option 2 — near an exact address (enables distance filtering):\n"
            '  set_preferences(address="street address", max_distance_miles=15)\n\n'
            "The user can also combine both."
        )
        return eff_city, eff_category, eff_address, eff_distance, True

    # Offer to save explicitly-passed values that aren't stored yet
    if not _stored_default(store, "defaults_declined"):
        save_hints: list[str] = []
        if city and not stored_city:
            save_hints.append(f'city="{city}"')
        if center_address and not stored_address:
            save_hints.append(f'address="{center_address}"')
        if max_distance_miles and not stored_distance:
            save_hints.append(f"max_distance_miles={max_distance_miles}")
        if category and not stored_category:
            save_hints.append(f'category="{category}"')
        if save_hints:
            messages.append(
                "[agent] The user passed values that aren't saved as defaults yet. "
                "Ask them: \"Save these as your defaults for future searches? "
                "(yes / no / never ask again)\"\n\n"
                f"On yes: call set_preferences({', '.join(save_hints)})\n"
                "On no: do nothing (will ask again next time)\n"
                "On never: call set_preferences(skip=true)"
            )

    return eff_city, eff_category, eff_address, eff_distance, False


# --------------------------------------------------------------------------
# Session management helpers
# --------------------------------------------------------------------------

async def _resolve_session(
    store: EventStore,
    messages: list[str],
    *,
    login: bool,
    skip_login_days: Optional[int],
) -> tuple[Optional[str], bool]:
    """Determine the session cookie to use for subscribed calendars.

    Returns (cookie, prompted).  ``prompted`` is True when a login prompt was
    added to messages, so callers can defer other prompts.
    """
    # Handle explicit skip_login_days from a prior prompt answer
    if skip_login_days is not None:
        if skip_login_days < 0:
            store.set_setting("luma_login_declined_until", _NEVER_TIMESTAMP)
        elif skip_login_days == 0:
            store.delete_setting("luma_login_declined_until")
        else:
            until = datetime.now(tz=timezone.utc) + timedelta(days=skip_login_days)
            store.set_setting("luma_login_declined_until", until.isoformat())
        return None, False

    # Handle explicit login=true from a prior prompt answer
    if login:
        cookie = _do_browser_login(store, messages)
        return cookie, False

    # Check if user previously declined and window is still active
    declined_row = store.get_setting("luma_login_declined_until")
    if declined_row:
        declined_until = datetime.fromisoformat(declined_row[0])
        if datetime.now(tz=timezone.utc) < declined_until:
            return None, False
        store.delete_setting("luma_login_declined_until")

    # Try existing cookie
    cookie = _get_stored_cookie(store)
    if cookie:
        valid = await _validate_if_stale(store, cookie, messages)
        if valid:
            return cookie, False
        store.delete_setting("luma_session")
        store.delete_setting("luma_session_validated")

    # No valid cookie — check if user ever logged in before
    had_cookie_row = store.get_setting("luma_login_had_cookie")
    if had_cookie_row and had_cookie_row[0] == "true":
        messages.append(
            "Your Luma session expired. Opening browser to re-authenticate..."
        )
        cookie = _do_browser_login(store, messages)
        return cookie, False

    # First time ever, or previously declined and window expired — prompt
    messages.append(
        "[agent] Results above are from Luma Discover (public events). "
        "The user can also log in to see events from calendars they follow "
        "on Luma, which may surface more results. Ask if they want to connect "
        "their Luma account for additional events.\n\n"
        "On yes: call search_events with login=true\n"
        "On no: call search_events with skip_login_days=0\n"
        "On never: call search_events with skip_login_days=-1"
    )
    return None, True


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
# Tool: set_preferences
# --------------------------------------------------------------------------


@mcp.tool
async def set_preferences(
    city: Optional[str] = None,
    address: Optional[str] = None,
    max_distance_miles: Optional[float] = None,
    category: Optional[str] = None,
    skip: bool = False,
) -> dict:
    """Save default search preferences. These persist across restarts.

    When an address is provided without a city, the nearest Luma city is
    inferred automatically from the address coordinates.

    Args:
        city: Luma region slug (e.g. "sf-bay-area", "new-york").
        address: Street address for distance filtering center point.
        max_distance_miles: Default search radius in miles.
        category: Event category (e.g. "ai", "tech", "crypto").
        skip: Permanently decline the save-defaults prompt.
    """
    store = _get_event_store()

    if skip:
        store.set_setting("defaults_declined", "true")
        return {"status": "ok", "message": "Defaults prompt permanently dismissed."}

    saved: dict[str, str] = {}

    if address is not None:
        store.set_setting("default_center_address", address)
        saved["address"] = address
        # Auto-infer city from address when not explicitly provided
        if city is None and not _stored_default(store, "default_city"):
            lat, lon = _geocode_fn(address)
            if lat is not None and lon is not None:
                inferred = _nearest_city(lat, lon)
                store.set_setting("default_city", inferred)
                saved["city"] = f"{inferred} (inferred from address)"

    if city is not None:
        store.set_setting("default_city", city)
        saved["city"] = city

    if max_distance_miles is not None:
        store.set_setting("default_max_distance_miles", str(max_distance_miles))
        saved["max_distance_miles"] = str(max_distance_miles)

    if category is not None:
        store.set_setting("default_category", category)
        saved["category"] = category

    current = {
        "city": _stored_default(store, "default_city"),
        "address": _stored_default(store, "default_center_address"),
        "max_distance_miles": _stored_default(store, "default_max_distance_miles"),
        "category": _stored_default(store, "default_category"),
    }

    return {"saved": saved, "current_preferences": current}


# --------------------------------------------------------------------------
# Tool: search_events
# --------------------------------------------------------------------------


@mcp.tool
async def search_events(
    city: Optional[str] = None,
    category: Optional[str] = None,
    center_address: Optional[str] = None,
    max_distance_miles: Optional[float] = None,
    keywords: Optional[list[str]] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    exclude_unknown_location: bool = False,
    latin_only: bool = True,
    added_within_days: Optional[float] = None,
    new_only: bool = False,
    login: bool = False,
    skip_login_days: Optional[int] = None,
) -> dict:
    """Search for Luma events from Discover and subscribed calendars.

    Filters by distance from a center address, keywords, date range, and more.
    Uses saved preferences from set_preferences as defaults for city, address,
    distance, and category. Pass explicit values to override for one search.

    Args:
        city: Discover region slug (e.g. "sf-bay-area", "new-york"). Overrides saved default.
        category: Discover category filter (e.g. "ai", "tech", "crypto"). Overrides saved default.
        center_address: Street address for distance filtering center. Overrides saved default.
        max_distance_miles: Maximum distance in miles from center. Overrides saved default.
        keywords: Filter by keywords (matches title/description).
        after: ISO 8601 datetime — only events starting after this time.
        before: ISO 8601 datetime — only events starting before this time.
        exclude_unknown_location: If true, drop events without coordinates.
        latin_only: Filter out non-Latin-script events (default true).
        added_within_days: Only return events first seen within this many days.
        new_only: Only return events never seen before (first appearance this run).
        login: Set to true to open browser and log in to Luma.
        skip_login_days: Decline login for N days (0 = ask next time, -1 = never).
    """
    cfg = _get_config()
    event_lists: list[list[LumaEvent]] = []
    messages: list[str] = []
    store = _get_event_store()

    # Location first — no search without it
    city, category, center_address, max_dist, needs_location = _resolve_defaults(
        store, messages,
        city=city, category=category, center_address=center_address,
        max_distance_miles=max_distance_miles,
    )

    if needs_location:
        return {"events": [], "count": 0, "messages": messages}

    # Login second — defer prompt if location was just set up this call
    session_cookie, _login_prompted = await _resolve_session(
        store, messages,
        login=login, skip_login_days=skip_login_days,
    )

    after_dt = _parse_dt(after)
    before_dt = _parse_dt(before)

    # Geocode address for distance filtering and city inference
    resolved_lat: Optional[float] = None
    resolved_lon: Optional[float] = None
    if center_address:
        resolved_lat, resolved_lon = _geocode_fn(center_address)

    # Infer city from address coordinates when not explicitly set
    if not city and resolved_lat is not None and resolved_lon is not None:
        city = _nearest_city(resolved_lat, resolved_lon)

    # Discover
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

    # Subscribed calendars (only if logged in)
    if session_cookie:
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

    if keywords:
        events = filter_by_keywords(events, keywords)

    if latin_only:
        events = [e for e in events if _is_latin_event(e)]

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


def _local_dt(dt: datetime, tz_name: Optional[str]) -> str:
    """Format a UTC datetime in the event's local timezone as a human-friendly ISO string."""
    if tz_name:
        try:
            local = dt.astimezone(ZoneInfo(tz_name))
            return local.isoformat()
        except (KeyError, ValueError):
            pass
    return dt.isoformat()


_STATE_ZIP_RE = re.compile(r"^[A-Z]{2}\s+\d{4,5}")


def _esc(text: Optional[str]) -> Optional[str]:
    """Escape pipe characters so values don't break markdown tables."""
    if text is None:
        return None
    return text.replace("|", "\\|")


def _extract_city(full_address: Optional[str]) -> Optional[str]:
    """Pull the city name from a full address string.

    Handles formats like:
      '123 Main St, Palo Alto, CA 94301, USA' → 'Palo Alto'
      'San Francisco, CA 94102' → 'San Francisco'
      'San Francisco, CA' → 'San Francisco'
      'Online' → None
    """
    if not full_address:
        return None
    parts = [p.strip() for p in full_address.split(",")]
    # Find the part just before a state+zip pattern
    for i in range(1, len(parts)):
        if _STATE_ZIP_RE.match(parts[i]):
            return parts[i - 1]
    # "City, ST" pattern — first part is the city if second looks like a state
    if len(parts) == 2 and len(parts[1].strip()) == 2 and parts[1].strip().isalpha():
        return parts[0]
    # 3+ parts without state+zip: second-to-last is likely the city
    if len(parts) >= 3:
        return parts[-2]
    return None


def _venue_name(label: Optional[str]) -> Optional[str]:
    """Return the venue name, or None if it looks like a bare street address."""
    if not label:
        return None
    stripped = label.strip()
    if stripped and stripped[0].isdigit():
        return None
    return stripped


def _event_summary(event: LumaEvent) -> dict:
    venue = _venue_name(event.location_label)
    city = event.city or _extract_city(event.full_address) or _extract_city(event.location_label)
    addr_lower = (event.full_address or "").lower()
    label_lower = (event.location_label or "").lower()
    if "online" in addr_lower or "online" in label_lower:
        location = "Online"
    elif venue and city:
        location = f"{venue}, {city}"
    else:
        location = venue or city

    d: dict = {
        "id": event.id,
        "title": _esc(event.title),
        "start_at": _local_dt(event.start_at, event.timezone),
        "end_at": _local_dt(event.end_at, event.timezone) if event.end_at else None,
        "timezone": event.timezone,
        "location": _esc(location) if location else None,
        "url": event.canonical_url,
    }
    if event.distance_miles is not None:
        d["distance_miles"] = event.distance_miles
    return d


def _event_detail(event: LumaEvent) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "start_at": _local_dt(event.start_at, event.timezone),
        "end_at": _local_dt(event.end_at, event.timezone) if event.end_at else None,
        "timezone": event.timezone,
        "city": event.city or _extract_city(event.full_address) or _extract_city(event.location_label),
        "lat": event.lat,
        "lon": event.lon,
        "location_label": event.location_label,
        "full_address": event.full_address,
        "cover_url": event.cover_url,
        "url": event.canonical_url,
        "rsvp_url": event.canonical_url,
        "source": event.source.value,
    }


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string, defaulting to UTC if no timezone is given."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _latin_ratio(text: str) -> float:
    """Return the fraction of alphabetic characters that are Latin-script."""
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return 1.0
    return sum(1 for c in alpha if c < "\u0250") / len(alpha)


def _has_cjk(text: str) -> bool:
    """Return True if text contains any CJK Unified Ideograph characters."""
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def _is_latin_event(event: LumaEvent) -> bool:
    """Return True if an event appears to be in a Latin-script language.

    Checks title at a strict threshold (brand names like 'OpenAI' inflate
    Latin counts in otherwise non-Latin titles). If a description exists,
    it's checked separately — a non-Latin description filters the event
    even when the title looks Latin. Titles containing CJK characters
    require a higher ratio to pass.
    """
    title_ratio = _latin_ratio(event.title)
    title_threshold = 0.9 if _has_cjk(event.title) else 0.8
    if title_ratio < title_threshold:
        return False
    if event.description and _latin_ratio(event.description) < 0.5:
        return False
    return True


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
