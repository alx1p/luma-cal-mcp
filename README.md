# Luma Events MCP Server

A [FastMCP](https://gofastmcp.com) server that discovers events from [Luma](https://luma.com) — combining the Discover feed and subscribed calendars — with distance filtering, keyword search, and ICS export. No API key required for basic discovery.

## Tools

| Tool | What it does |
|------|-------------|
| `search_events` | Unified search across Discover and subscribed calendars. Filter by city/region, category, distance from a point (coordinates or address), keywords, and recency (`added_within_days`, `new_only`). Handles login flow for subscribed calendars automatically. |
| `get_event` | Fetch full details for a single event by API id or `lu.ma` URL. Includes RSVP link. |
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

On first use, `search_events` will prompt you to log in via a browser window. The session cookie is stored locally in SQLite and re-validated automatically. No manual cookie extraction needed.

### Configure

Copy `.env.example` to `.env` and optionally fill in defaults:

```bash
cp .env.example .env
```

**No env vars are required.** Discover and event lookup work out of the box, with no API key and no account.

**Optional env vars (enhance functionality):**
- `DEFAULT_CITY` — Discover region slug (e.g. `sf-bay-area`, `new-york`, `los-angeles`).
- `DEFAULT_CATEGORY` — category filter (e.g. `ai`, `tech`, `crypto`, `food-drink`).
- `DEFAULT_CENTER_LAT`, `DEFAULT_CENTER_LON` — default center point for distance filtering.
- `DEFAULT_CENTER_ADDRESS` — alternative to lat/lon; geocoded on first use.
- `DEFAULT_MAX_DISTANCE_MILES` — default radius.
- `DEFAULT_KEYWORDS` — comma-separated keyword list.
- `GEOCODING_PROVIDER` — `nominatim` (default, free), `google`, or `mapbox`.
- `GEOCODING_API_KEY` — required for Google or Mapbox geocoding.
- `EVENT_STORE_PATH` — path to the SQLite DB that tracks first-seen timestamps, login state, and saved preferences. Default: `~/.luma-mcp/events.db`.

#### Persistent preferences

You can also set defaults at runtime via `search_events` parameters. These are stored in the local SQLite DB and persist across restarts — no `.env` editing needed:

- `set_default_address="3180 18th St, San Francisco"` — your home location for distance filtering.
- `set_default_category="ai"` — event type to browse by default.
- `set_default_city="sf-bay-area"` — Discover region.

Precedence: explicit call parameter > stored DB preference > env var. On first run with no defaults configured anywhere, the server prompts you to set these.

### Run

```bash
# stdio transport (for Cursor, Claude Desktop, etc.)
fastmcp run src/luma_mcp/server.py

# or directly
python -m luma_mcp.server
```

## Authentication

Subscribed calendars require a Luma session cookie. The server handles this automatically via an inline login flow — no manual cookie extraction or environment variables needed.

**How it works:**

1. **First run** — `search_events` returns Discover results plus a message asking whether you'd like to log in for subscribed calendars. The agent relays this prompt in chat.
2. **Login** — call `search_events` with `login=true`. A Chromium browser opens to `lu.ma/signin`; log in normally. The session cookie is captured and stored in the local SQLite DB.
3. **Decline** — call `search_events` with `skip_login_days=N` to defer (0 = ask next time, -1 = never).
4. **Returning user, cookie expired** — the browser opens automatically for re-authentication (you opted in by logging in previously).
5. **Validation** — the stored cookie is validated against Luma's API every 24 hours. If it expires, the server re-opens the browser.

**Agent parameter:** `no_login=true` skips subscribed calendars for a single call without changing stored preferences.

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

Provide a center point as coordinates (`center_lat` + `center_lon`) or a street address (`center_address`), plus `max_distance_miles`. Events beyond the radius are excluded. Events without location data are included by default (with `distance_miles: null`), or excluded if `exclude_unknown_location` is set.

Geocoding uses [Nominatim](https://nominatim.org/) (free, OpenStreetMap) by default. For higher volume, set `GEOCODING_PROVIDER=google` or `mapbox` with the corresponding `GEOCODING_API_KEY`.

## Limitations

- **RSVP is browser-only.** `get_event` returns the RSVP URL; there's no headless registration path. Use `export_event_ics` to add events to your calendar.
- **Web endpoints are undocumented.** The Discover and subscribed-calendars feeds use Luma's internal API (`api.lu.ma`), which can change without notice. Breakage is isolated to `luma_web_client.py`.
