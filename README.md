# Luma Events MCP Server

A [FastMCP](https://gofastmcp.com) server that discovers events from [Luma](https://luma.com) — combining the Discover feed and subscribed calendars — with distance filtering, keyword search, and ICS export. No API key required for basic discovery.

## Tools

| Tool | What it does |
|------|-------------|
| `search_events` | Search Discover and subscribed calendars. Filter by city, category, distance from address, keywords, date range, and recency. Event times in local timezone. |
| `set_preferences` | Save default city, address, distance, and category. Persists in SQLite across restarts. |
| `get_event` | Fetch full details for a single event by API id or `lu.ma` URL. |
| `export_event_ics` | Generate an ICS string for any event — paste into Apple Calendar, Google Calendar, Outlook, etc. |

## Setup

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Install

```bash
git clone <this-repo>
cd "Luma Cal MCP"
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e .
```

### Subscribed calendars (optional)

To access events from calendars you follow on Luma, install the optional auth dependencies:

```bash
uv pip install -e ".[auth]"
playwright install chromium
```

### First run

On first use, the server walks you through setup one step at a time:

1. **Login** — asks whether to log in for subscribed calendars.
2. **Location** — asks where to search (city or address). No events are returned until a location is configured via `set_preferences`.

Each prompt appears once per call, so the agent handles them sequentially.

### Configure

Use `set_preferences` to save defaults that persist across restarts:

```
set_preferences(address="3180 18th St, San Francisco", max_distance_miles=15)
set_preferences(category="ai")
```

When you provide an address without a city, the nearest Luma region is inferred automatically. You can also set `city` explicitly (e.g. `city="sf"`).

### Run

```bash
# stdio transport (for Cursor, Claude Desktop, etc.)
fastmcp run src/luma_mcp/server.py

# or directly
python -m luma_mcp.server
```

## Authentication

Subscribed calendars require a Luma session cookie. The server handles this automatically via an inline login flow.

**How it works:**

1. **First call** — the server prompts for login. The agent asks you in chat.
2. **Login** — the agent calls `search_events` with `login=true`. A Chromium browser opens to `lu.ma/signin`; log in normally. The session cookie is stored in the local SQLite DB.
3. **Decline** — the agent calls `search_events` with `skip_login_days=N` to defer (0 = ask next time, -1 = never).
4. **Returning user, cookie expired** — the browser opens automatically for re-authentication.
5. **Validation** — the stored cookie is validated against Luma's API every 24 hours.

## New Event Tracking

The server maintains a local SQLite database (`~/.luma-mcp/events.db` by default) that records the first time each event is seen. This enables two filters on `search_events`:

- **`added_within_days`** — only return events first seen within the last N days (e.g. `added_within_days=5` for events discovered in the past week).
- **`new_only`** — only return events that have never been seen before (first appearance this run).

Every result also includes `first_seen_at` (ISO timestamp) and `is_new` (boolean). The store builds up over repeated runs, so after regular use you can reliably ask "what's new since last time."

## Cursor MCP Configuration

Add to your Cursor MCP settings (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "luma-events": {
      "command": "uv",
      "args": [
        "run",
        "--directory", "/path/to/Luma Cal MCP",
        "fastmcp", "run", "src/luma_mcp/server.py"
      ],
      "env": {
        "PYTHONPATH": "/path/to/Luma Cal MCP/src"
      }
    }
  }
}
```

## Data Sources

The server pulls events from up to two sources and merges them:

| Source | Auth | Coverage |
|--------|------|----------|
| **Discover** (`api.lu.ma`) | None required | Public events by city and category — same feed as [luma.com/discover](https://luma.com/discover) |
| **Subscribed calendars** (`api.lu.ma`) | Browser login (auto-managed) | Events from calendars you follow on Luma |

Without logging in, the server still works — Discover is fully available with no authentication.

## Distance Filtering

Provide a street address via `set_preferences(address="...")` or as `center_address` on `search_events`, plus `max_distance_miles`. Events beyond the radius are excluded. Events without location data are included by default (with `distance_miles: null`), or excluded if `exclude_unknown_location` is set.

Geocoding uses [Nominatim](https://nominatim.org/) (free, OpenStreetMap) by default. For higher volume, set `GEOCODING_PROVIDER=google` or `mapbox` with the corresponding `GEOCODING_API_KEY` in your environment.

## Event Times

Event times (`start_at`, `end_at`) are returned in each event's local timezone as reported by Luma (e.g. `America/Los_Angeles`), not UTC. The `timezone` field is included in every result for reference.

## Limitations

- **RSVP is browser-only.** `get_event` returns the RSVP URL; there's no headless registration path. Use `export_event_ics` to add events to your calendar.
- **Web endpoints are undocumented.** The Discover and subscribed-calendars feeds use Luma's internal API (`api.lu.ma`), which can change without notice. Breakage is isolated to `luma_web_client.py`.
