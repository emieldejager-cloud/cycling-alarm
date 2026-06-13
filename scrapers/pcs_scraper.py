"""
ProcyclingStats scraper — race schedule, stage profile, live ticker.
"""
import json
import logging
import re
from datetime import datetime, date
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from db.database import (
    EventType, FinishType, Race, StageProfile, TickerEvent, get_db
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.procyclingstats.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}

# Keywords for event type detection
EVENT_KEYWORDS = {
    EventType.attack: [
        "aanval", "attack", "solo", "break", "breakaway",
        "ontsnapping", "demarrage", "offensief",
    ],
    EventType.sprint: [
        "sprint", "spurt", "leadout", "treintje", "massasprint",
        "sprinttijd", "sprintend",
    ],
    EventType.crash: [
        "val", "crash", "chute", "gevallen", "gevallen",
        "neergevallen", "ten val", "accident",
    ],
    EventType.summit: [
        "boven", "top", "summit", "col franchi", "col franchie",
        "bergtop", "passage",
    ],
    EventType.gap: [
        "seconden", "seconds", "minuten", "minuut", "écart",
        "voorsprong", "achterstand", "gap", "lead",
    ],
    EventType.caught: [
        "bijgehaald", "caught", "neutralized", "geneutraliseerd",
        "ingerekend", "teruggepakt",
    ],
}

FINISH_TYPE_KEYWORDS = {
    FinishType.sprint: [
        "flat", "sprint", "vlak", "massasprint", "bunch sprint",
    ],
    FinishType.uphill_finish: [
        "uphill finish", "bergaankomst", "summit finish",
        "arrival uphill", "heuvelaankomst",
    ],
    FinishType.mountain: [
        "mountain", "berg", "cols", "haute montagne", "alpenetappe",
    ],
    FinishType.hill: [
        "hilly", "heuvel", "semi-mountain", "ondulé",
    ],
    FinishType.tt: [
        "time trial", "tijdrit", "contre-la-montre", "clm", "itt", "ttt",
    ],
}


def _detect_event_type(text: str) -> EventType:
    lower = text.lower()
    for etype, keywords in EVENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return etype
    return EventType.other


def _detect_finish_type(text: str) -> Optional[FinishType]:
    lower = text.lower()
    # Check uphill first (more specific than mountain)
    for ftype in [
        FinishType.uphill_finish, FinishType.tt, FinishType.sprint,
        FinishType.mountain, FinishType.hill,
    ]:
        if any(kw in lower for kw in FINISH_TYPE_KEYWORDS[ftype]):
            return ftype
    return None


def _parse_km(text: str) -> Optional[float]:
    """Extract km value from strings like '45 km' or '45.2km to go'."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*km", text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


async def scrape_race_schedule() -> list[Race]:
    """Scrape today's races from PCS races page."""
    today = date.today().strftime("%Y-%m-%d")
    url = f"{BASE_URL}/races.php"
    races_found = []

    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Failed to fetch PCS race schedule: %s", e)
            return []

    soup = BeautifulSoup(resp.text, "lxml")
    db = get_db()
    try:
        # PCS races.php table rows
        table = soup.find("table", class_=re.compile(r"basic"))
        if not table:
            # Fallback: look for any table with race links
            table = soup.find("table")
        if not table:
            logger.warning("No race table found on PCS races page")
            return []

        for row in table.find_all("tr")[1:]:  # skip header
            cols_td = row.find_all("td")
            if len(cols_td) < 3:
                continue

            # Try to find date cell
            date_text = cols_td[0].get_text(strip=True)
            if today not in date_text and date_text not in today:
                # PCS may show date in different formats; try matching month/day
                today_obj = date.today()
                pcs_date = re.search(r"(\d{2})[./](\d{2})", date_text)
                if pcs_date:
                    day, month = int(pcs_date.group(1)), int(pcs_date.group(2))
                    if not (day == today_obj.day and month == today_obj.month):
                        continue
                else:
                    continue

            # Race link
            link_tag = row.find("a", href=re.compile(r"/race/"))
            if not link_tag:
                continue
            race_name = link_tag.get_text(strip=True)
            race_path = link_tag["href"]
            race_url = BASE_URL + race_path if race_path.startswith("/") else race_path

            # Check for stage number in URL (e.g. /race/tour-de-france/2024/stage-1)
            stage_match = re.search(r"/stage-?(\d+)", race_path, re.IGNORECASE)
            stage_number = int(stage_match.group(1)) if stage_match else None

            # Check if ticker exists on race page
            ticker_url = await _find_ticker_url(client, race_url)

            # Upsert race
            existing = db.query(Race).filter(Race.name == race_name, Race.date == today).first()
            if existing:
                existing.ticker_url = ticker_url
                existing.stage_number = stage_number
                db.commit()
                races_found.append(existing)
            else:
                race = Race(
                    name=race_name,
                    date=today,
                    ticker_url=ticker_url,
                    stage_number=stage_number,
                )
                db.add(race)
                db.commit()
                db.refresh(race)
                races_found.append(race)
                logger.info("New race found: %s (stage %s)", race_name, stage_number)

    finally:
        db.close()

    logger.info("Found %d race(s) for today", len(races_found))
    return races_found


async def _find_ticker_url(client: httpx.AsyncClient, race_url: str) -> Optional[str]:
    """Check race page for live ticker link."""
    try:
        resp = await client.get(race_url, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    # Look for ticker/live link patterns
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        if "ticker" in href or "live" in text or "ticker" in text:
            if href.startswith("/"):
                return BASE_URL + href
            if href.startswith("http"):
                return href

    # Check for embedded ticker section
    ticker_section = soup.find(id=re.compile(r"ticker", re.IGNORECASE))
    if ticker_section:
        return race_url  # ticker is on same page

    return None


async def scrape_stage_profile(race_id: int) -> Optional[StageProfile]:
    """Scrape etappe profile for a race and store in DB."""
    db = get_db()
    try:
        race = db.query(Race).filter(Race.id == race_id).first()
        if not race:
            logger.error("Race %d not found", race_id)
            return None

        # Build stage URL: the ticker_url is the race page; derive stage profile URL
        # PCS stage profile is usually at /race/.../stage-N
        base_race_url = race.ticker_url or ""
        if not base_race_url:
            logger.warning("No URL for race %d, cannot scrape profile", race_id)
            return None

        # Remove ticker fragment if present
        profile_url = re.sub(r"/live/?$", "", base_race_url.rstrip("/"))

        async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
            try:
                resp = await client.get(profile_url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("Failed to fetch stage profile for race %d: %s", race_id, e)
                return None

        soup = BeautifulSoup(resp.text, "lxml")

        # --- finish_type ---
        finish_type = None
        # Check profile info sections
        profile_text = soup.get_text(" ", strip=True)
        finish_type = _detect_finish_type(profile_text)

        # --- difficulty_score ---
        difficulty_score = None
        pts_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:pts?|punten|points)", profile_text, re.IGNORECASE)
        if pts_match:
            difficulty_score = float(pts_match.group(1).replace(",", "."))

        # --- finish_km (total distance) ---
        finish_km = None
        dist_match = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*km", profile_text)
        if dist_match:
            finish_km = float(dist_match.group(1).replace(",", "."))

        # --- cols (mountain passes) ---
        cols = _parse_cols(soup, finish_km)

        # Update race finish_type and difficulty
        race.finish_type = finish_type
        race.difficulty_score = difficulty_score

        # Upsert stage profile
        profile = db.query(StageProfile).filter(StageProfile.race_id == race_id).first()
        if profile:
            profile.cols = cols
            profile.finish_km = finish_km
        else:
            profile = StageProfile(race_id=race_id, finish_km=finish_km)
            profile.cols = cols
            db.add(profile)

        db.commit()
        db.refresh(profile)
        logger.info(
            "Stage profile for race %d: finish_type=%s, %d cols, %.1f km",
            race_id, finish_type, len(cols), finish_km or 0,
        )
        return profile

    finally:
        db.close()


def _parse_cols(soup: BeautifulSoup, finish_km: Optional[float]) -> list[dict]:
    """Parse mountain pass info from PCS stage page."""
    cols = []

    # PCS typically has a climbs table or profile image alt texts
    # Look for table with climb data
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if any(h in headers for h in ["climb", "col", "berg", "category", "cat"]):
            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                if len(cells) < 2:
                    continue
                col_name = cells[0].get_text(strip=True)
                category = None
                km_position = None

                for cell in cells:
                    text = cell.get_text(strip=True)
                    # Category: HC, 1, 2, 3, 4
                    cat_match = re.search(r"\b(HC|[1-4])\b", text)
                    if cat_match:
                        category = cat_match.group(1)
                    # km position
                    km_match = re.search(r"(\d+(?:[.,]\d+)?)\s*km", text)
                    if km_match:
                        km_position = float(km_match.group(1).replace(",", "."))

                if col_name and (category or km_position):
                    cols.append({
                        "name": col_name,
                        "km_position": km_position,
                        "category": category,
                    })

    # Fallback: scan text for col mentions like "Col du Galibier (HC, km 120)"
    if not cols:
        text = soup.get_text(" ", strip=True)
        pattern = re.compile(
            r"(col\s+\w+(?:\s+\w+)?)\s*[\(\[]?\s*(HC|[1-4])\s*[\)\]]?.*?(\d+(?:[.,]\d+)?)\s*km",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            cols.append({
                "name": m.group(1).strip(),
                "km_position": float(m.group(3).replace(",", ".")),
                "category": m.group(2),
            })

    return cols


async def poll_ticker(race_id: int) -> list[TickerEvent]:
    """
    Poll live ticker for a race and store new events.

    Strategy (in order):
      1. PCS XHR/JSON feed endpoint  — fast, structured, no HTML parsing needed
      2. Embedded JS data object      — PCS sometimes inlines ticker data as JSON in a <script>
      3. HTML parser fallback         — scrape the rendered ticker <div>
    """
    db = get_db()
    try:
        race = db.query(Race).filter(Race.id == race_id).first()
        if not race or not race.ticker_url:
            return []

        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            raw_events = (
                await _fetch_ticker_xhr(client, race.ticker_url)
                or await _fetch_ticker_inline_json(client, race.ticker_url)
                or await _fetch_ticker_html(client, race.ticker_url)
            )

        if not raw_events:
            return []

        new_events = []
        for raw in raw_events:
            exists = (
                db.query(TickerEvent)
                .filter(
                    TickerEvent.race_id == race_id,
                    TickerEvent.raw_text == raw["raw_text"],
                )
                .first()
            )
            if exists:
                continue

            event = TickerEvent(
                race_id=race_id,
                timestamp=raw.get("timestamp"),
                event_type=_detect_event_type(raw["raw_text"]),
                description=raw.get("description"),
                km_remaining=raw.get("km_remaining"),
                raw_text=raw["raw_text"],
            )
            db.add(event)
            new_events.append(event)

        if new_events:
            db.commit()
            for e in new_events:
                db.refresh(e)
            logger.info("Race %d: %d new ticker event(s)", race_id, len(new_events))

        return new_events

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Ticker strategy 1 — XHR / JSON feed
# ---------------------------------------------------------------------------

# PCS serves ticker data via two known XHR patterns.
# Pattern A: /race/{slug}/{year}/stage-{n}/live  with ?page=...&offset=...
# Pattern B: legacy AJAX endpoint at /inc/php/ajax/race-ticker.php
#
# Both return either JSON or HTML fragments depending on request headers.
# We probe them in order and take the first successful response.

async def _fetch_ticker_xhr(client: httpx.AsyncClient, ticker_url: str) -> list[dict]:
    """Try PCS XHR endpoints for JSON ticker data."""
    endpoints = _build_xhr_endpoints(ticker_url)
    for url, params in endpoints:
        try:
            resp = await client.get(
                url,
                params=params,
                headers={
                    **HEADERS,
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Referer": ticker_url,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            continue

        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            events = _parse_pcs_json(resp.json())
            if events:
                logger.debug("Ticker XHR JSON: %d events from %s", len(events), url)
                return events
        elif "html" in ct or "javascript" in ct:
            # Some endpoints return an HTML fragment for innerHTML injection
            events = _parse_ticker_html_fragment(resp.text)
            if events:
                logger.debug("Ticker XHR HTML fragment: %d events from %s", len(events), url)
                return events

    return []


def _build_xhr_endpoints(ticker_url: str) -> list[tuple[str, dict]]:
    """
    Derive candidate XHR endpoint URLs from the race ticker URL.

    PCS URL shapes we handle:
      https://www.procyclingstats.com/race/tour-de-france/2025/stage-14/live
      https://www.procyclingstats.com/race/tour-de-france/2025/stage-14
    """
    # Normalise: strip trailing /live
    base = re.sub(r"/live/?$", "", ticker_url.rstrip("/"))

    endpoints = []

    # Pattern A — live page with XHR parameters (PCS 2023+ format)
    #   GET /race/.../live?id=<race_id>&offset=0&limit=50
    live_url = base + "/live"
    endpoints.append((live_url, {"offset": 0, "limit": 50, "type": "json"}))
    endpoints.append((live_url, {"offset": 0, "limit": 50}))

    # Pattern B — legacy AJAX PHP endpoint
    #   GET /inc/php/ajax/race-ticker.php?id=<slug>&offset=0
    slug_match = re.search(r"/race/([^/]+/[^/]+(?:/stage-\d+)?)", base)
    if slug_match:
        slug = slug_match.group(1)
        ajax_url = f"{BASE_URL}/inc/php/ajax/race-ticker.php"
        endpoints.append((ajax_url, {"id": slug, "offset": 0}))
        endpoints.append((ajax_url, {"race": slug, "offset": 0, "format": "json"}))

    # Pattern C — some races use a /feed or /data suffix
    endpoints.append((base + "/feed", {}))
    endpoints.append((base + "/data", {"format": "json"}))

    return endpoints


def _parse_pcs_json(data) -> list[dict]:
    """
    Parse PCS JSON ticker response.

    PCS JSON shapes observed:
      {"data": [{"time": "14:23", "km": 45, "text": "..."}, ...]}
      [{"timestamp": "...", "message": "...", "km": ...}, ...]
      {"events": [...]}
      {"ticker": [...]}
    """
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (
            data.get("data")
            or data.get("events")
            or data.get("ticker")
            or data.get("items")
            or []
        )
    else:
        return []

    events = []
    for item in items:
        if not isinstance(item, dict):
            continue

        # Text field — try multiple key names
        text = (
            item.get("text")
            or item.get("message")
            or item.get("description")
            or item.get("msg")
            or ""
        )
        if not text:
            continue

        raw_text = re.sub(r"\s+", " ", str(text)).strip()

        # km_remaining
        km = item.get("km") or item.get("km_remaining") or item.get("distance")
        km_remaining = None
        if km is not None:
            try:
                km_remaining = float(km)
            except (ValueError, TypeError):
                km_remaining = _parse_km(raw_text)

        # timestamp
        ts_raw = item.get("time") or item.get("timestamp") or item.get("date")
        timestamp = _parse_timestamp(str(ts_raw)) if ts_raw else None

        events.append({
            "raw_text": raw_text,
            "description": raw_text,
            "timestamp": timestamp,
            "km_remaining": km_remaining,
        })

    return events


# ---------------------------------------------------------------------------
# Ticker strategy 2 — inline JS data object in page <script>
# ---------------------------------------------------------------------------

async def _fetch_ticker_inline_json(client: httpx.AsyncClient, ticker_url: str) -> list[dict]:
    """
    PCS sometimes ships ticker data as a JS variable in a <script> tag:
      var tickerData = [{...}, ...];
      window.__INITIAL_STATE__ = {...};
    Extract and parse it.
    """
    try:
        resp = await client.get(ticker_url, timeout=20)
        resp.raise_for_status()
    except httpx.HTTPError:
        return []

    html = resp.text

    # Pattern: var tickerData = [...]; or similar
    patterns = [
        r"var\s+tickerData\s*=\s*(\[.*?\])\s*;",
        r"window\.__ticker__\s*=\s*(\[.*?\])\s*;",
        r'"ticker"\s*:\s*(\[.*?\])',
        r'"events"\s*:\s*(\[.*?\])',
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;",
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            events = _parse_pcs_json(data)
            if events:
                logger.debug(
                    "Ticker inline JS: %d events via pattern '%s'",
                    len(events), pattern[:40],
                )
                return events
        except (_json.JSONDecodeError, Exception):
            continue

    return []


# ---------------------------------------------------------------------------
# Ticker strategy 3 — HTML fallback
# ---------------------------------------------------------------------------

async def _fetch_ticker_html(client: httpx.AsyncClient, ticker_url: str) -> list[dict]:
    """Fetch ticker page and parse HTML — last resort."""
    try:
        resp = await client.get(ticker_url, timeout=20)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Ticker HTML fetch failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    events = _parse_ticker_html_soup(soup)
    if events:
        logger.debug("Ticker HTML fallback: %d events", len(events))
    return events


def _parse_ticker_html_fragment(html: str) -> list[dict]:
    """Parse an HTML fragment returned by an XHR call."""
    soup = BeautifulSoup(html, "lxml")
    return _parse_ticker_html_soup(soup)


def _parse_ticker_html_soup(soup: BeautifulSoup) -> list[dict]:
    """Extract ticker events from PCS page/fragment HTML."""
    events = []

    ticker_div = (
        soup.find("div", id=re.compile(r"ticker", re.IGNORECASE))
        or soup.find("div", class_=re.compile(r"ticker|live-feed|livefeed", re.IGNORECASE))
        or soup.find("ul", class_=re.compile(r"ticker|live", re.IGNORECASE))
        or soup.find("div", class_=re.compile(r"content|main", re.IGNORECASE))
    )

    container = ticker_div or soup

    for item in container.find_all(["li", "div", "p", "span"], recursive=True):
        # Skip deeply nested noise
        if len(item.find_all()) > 10:
            continue

        text = item.get_text(" ", strip=True)
        if len(text) < 8 or len(text) > 600:
            continue

        if any(skip in text.lower() for skip in ["cookie", "javascript", "menu", "navigation", "©"]):
            continue

        time_match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text)
        timestamp = None
        if time_match:
            now = datetime.now()
            try:
                timestamp = now.replace(
                    hour=int(time_match.group(1)),
                    minute=int(time_match.group(2)),
                    second=int(time_match.group(3) or 0),
                    microsecond=0,
                )
            except ValueError:
                pass

        km_remaining = _parse_km(text)
        description = re.sub(r"\s+", " ", text).strip()

        events.append({
            "raw_text": description,
            "description": description,
            "timestamp": timestamp,
            "km_remaining": km_remaining,
        })

    # Deduplicate by raw_text within this batch (same text in nested elements)
    seen: set[str] = set()
    unique = []
    for e in events:
        if e["raw_text"] not in seen:
            seen.add(e["raw_text"])
            unique.append(e)

    return unique[:50]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(ts: str) -> Optional[datetime]:
    """Try to parse a timestamp string into a datetime."""
    now = datetime.now()
    # HH:MM or HH:MM:SS
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", ts.strip())
    if m:
        try:
            return now.replace(
                hour=int(m.group(1)),
                minute=int(m.group(2)),
                second=int(m.group(3) or 0),
                microsecond=0,
            )
        except ValueError:
            pass
    # ISO 8601
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts[:19], fmt)
        except ValueError:
            pass
    return None
