"""
HBO Max live sport checker via Playwright headless browser.

Flow:
  1. Open play.max.com with a persistent browser session (cookies saved to disk).
  2. If not logged in, perform email+password login automatically.
  3. Navigate to the Live / Sport section and look for cycling broadcasts today.
  4. Return availability, start time, and title.

Session is reused across runs so login only happens once (or after cookie expiry).
"""

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)

logger = logging.getLogger(__name__)

HBO_MAX_URL = "https://play.max.com"
HBO_MAX_LOGIN_URL = "https://play.max.com/login"

# Selectors — HBO Max updates their DOM occasionally; these target stable attributes
_SEL_EMAIL = 'input[type="email"], input[name="email"], input[id*="email"]'
_SEL_CONTINUE = 'button[type="submit"], button:has-text("Continue"), button:has-text("Doorgaan")'
_SEL_PASSWORD = 'input[type="password"]'
_SEL_SUBMIT = 'button[type="submit"]'

# Keywords that indicate a cycling broadcast
CYCLING_KEYWORDS = [
    "wielrennen", "cyclisme", "cycling", "koers", "tour de france",
    "giro", "vuelta", "klassement", "etappe", "stage", "peloton",
]


def _get_session_path() -> Path:
    p = os.getenv("HBO_MAX_SESSION_FILE", "hbomax_session.json")
    return Path(p)


async def check_hbo_availability(race_name: str) -> dict:
    """
    Check whether today's race is available live on HBO Max.

    Returns:
        {
            "available": bool,
            "start_time": "HH:MM" | None,
            "title": str | None,
            "channel": "HBO Max",
        }
    """
    email = os.getenv("HBO_MAX_EMAIL")
    password = os.getenv("HBO_MAX_PASSWORD")

    if not email or not password:
        logger.warning("HBO_MAX_EMAIL / HBO_MAX_PASSWORD not set — skipping HBO check")
        return {"available": False, "start_time": None, "title": None, "channel": None}

    async with async_playwright() as pw:
        context = await _build_context(pw)
        page = await context.new_page()

        try:
            logged_in = await _ensure_logged_in(page, email, password)
            if not logged_in:
                logger.error("HBO Max login failed — cannot check availability")
                await context.close()
                return {"available": False, "start_time": None, "title": None, "channel": None}

            result = await _find_cycling_broadcast(page, race_name)

        except Exception as e:
            logger.exception("HBO Max check error: %s", e)
            result = {"available": False, "start_time": None, "title": None, "channel": None}

        finally:
            # Persist session cookies for next run
            await _save_session(context)
            await context.close()

    return result


# ---------------------------------------------------------------------------
# Browser context
# ---------------------------------------------------------------------------

async def _build_context(pw: Playwright) -> BrowserContext:
    """Launch a persistent Chromium context (headless)."""
    browser: Browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    session_path = _get_session_path()
    storage_state = str(session_path) if session_path.exists() else None

    context: BrowserContext = await browser.new_context(
        storage_state=storage_state,
        locale="nl-NL",
        timezone_id="Europe/Amsterdam",
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )

    # Mask headless tells
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """)

    return context


async def _save_session(context: BrowserContext) -> None:
    try:
        state = await context.storage_state()
        session_path = _get_session_path()
        session_path.write_text(json.dumps(state), encoding="utf-8")
        logger.debug("HBO Max session saved to %s", session_path)
    except Exception as e:
        logger.warning("Could not save HBO Max session: %s", e)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def _ensure_logged_in(page: Page, email: str, password: str) -> bool:
    """Navigate to HBO Max and login if necessary. Returns True on success."""
    logger.info("Navigating to HBO Max")
    try:
        await page.goto(HBO_MAX_URL, wait_until="domcontentloaded", timeout=30_000)
    except PWTimeout:
        logger.error("Timeout loading HBO Max homepage")
        return False

    # Check if already authenticated (profile icon or avatar present)
    if await _is_logged_in(page):
        logger.info("HBO Max: already logged in via saved session")
        return True

    logger.info("HBO Max: session expired or not found — logging in")
    return await _do_login(page, email, password)


async def _is_logged_in(page: Page) -> bool:
    """Heuristic: look for a user profile element on the page."""
    try:
        # HBO Max shows a profile/avatar icon when authenticated
        await page.wait_for_selector(
            '[data-testid="profile-icon"], [aria-label*="profiel"], [aria-label*="account"], '
            '.profile-avatar, [class*="ProfileIcon"], [class*="userAvatar"]',
            timeout=5_000,
        )
        return True
    except PWTimeout:
        return False


async def _do_login(page: Page, email: str, password: str) -> bool:
    """Perform the HBO Max login flow."""
    try:
        await page.goto(HBO_MAX_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

        # Step 1: email
        await page.wait_for_selector(_SEL_EMAIL, timeout=15_000)
        await page.fill(_SEL_EMAIL, email)
        await page.click(_SEL_CONTINUE)

        # Step 2: password (may appear on same or next page)
        await page.wait_for_selector(_SEL_PASSWORD, timeout=15_000)
        await page.fill(_SEL_PASSWORD, password)
        await page.click(_SEL_SUBMIT)

        # Wait for redirect to authenticated state
        await page.wait_for_url(re.compile(r"play\.max\.com(?!/login)"), timeout=20_000)

        if await _is_logged_in(page):
            logger.info("HBO Max login successful")
            return True

        # Sometimes there's a profile-selection step
        await _handle_profile_selection(page)
        return await _is_logged_in(page)

    except PWTimeout as e:
        logger.error("HBO Max login timed out: %s", e)
        # Take a screenshot for debugging
        try:
            await page.screenshot(path="hbomax_login_error.png")
            logger.info("Login error screenshot saved to hbomax_login_error.png")
        except Exception:
            pass
        return False
    except Exception as e:
        logger.exception("HBO Max login error: %s", e)
        return False


async def _handle_profile_selection(page: Page) -> None:
    """If HBO Max shows a profile picker, select the first profile."""
    try:
        profile_btn = await page.wait_for_selector(
            '[data-testid*="profile"], [class*="ProfileTile"], [class*="profile-tile"]',
            timeout=5_000,
        )
        if profile_btn:
            await profile_btn.click()
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            logger.info("HBO Max: selected first profile")
    except PWTimeout:
        pass  # No profile selection needed


# ---------------------------------------------------------------------------
# Content discovery
# ---------------------------------------------------------------------------

async def _find_cycling_broadcast(page: Page, race_name: str) -> dict:
    """
    Search for a cycling broadcast on HBO Max live/sport pages.
    Returns dict with available, start_time, title, channel.
    """
    race_keywords = _race_keywords(race_name)
    all_keywords = CYCLING_KEYWORDS + race_keywords

    # Try the Live tab first, then Sport, then search
    for strategy in [_check_live_tab, _check_sport_section, _check_search]:
        result = await strategy(page, all_keywords)
        if result["available"]:
            logger.info("HBO Max: found cycling via %s: %s", strategy.__name__, result)
            return {**result, "channel": "HBO Max"}

    logger.info("HBO Max: no cycling broadcast found for '%s'", race_name)
    return {"available": False, "start_time": None, "title": None, "channel": "HBO Max"}


async def _check_live_tab(page: Page, keywords: list[str]) -> dict:
    """Navigate to the Live tab and scan for cycling."""
    live_selectors = [
        'a[href*="/live"]',
        'a:has-text("Live")',
        '[data-testid*="live"]',
        'nav a:has-text("Live")',
    ]

    for sel in live_selectors:
        try:
            link = await page.wait_for_selector(sel, timeout=4_000)
            if link:
                await link.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                result = await _scan_page_for_cycling(page, keywords)
                if result["available"]:
                    return result
                break
        except PWTimeout:
            continue

    return {"available": False, "start_time": None, "title": None}


async def _check_sport_section(page: Page, keywords: list[str]) -> dict:
    """Navigate to the Sport section."""
    sport_selectors = [
        'a[href*="sport"]',
        'a:has-text("Sport")',
        'a:has-text("Sports")',
        '[data-testid*="sport"]',
    ]

    for sel in sport_selectors:
        try:
            link = await page.wait_for_selector(sel, timeout=4_000)
            if link:
                await link.click()
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                result = await _scan_page_for_cycling(page, keywords)
                if result["available"]:
                    return result
                break
        except PWTimeout:
            continue

    return {"available": False, "start_time": None, "title": None}


async def _check_search(page: Page, keywords: list[str]) -> dict:
    """Use HBO Max search for key cycling terms."""
    search_terms = ["wielrennen", "cycling", "koers"]

    search_btn_sels = [
        '[aria-label*="zoek"]', '[aria-label*="search"]',
        '[data-testid*="search"]', 'a[href*="search"]',
    ]

    search_input_sels = [
        'input[type="search"]', 'input[placeholder*="zoek"]',
        'input[placeholder*="Search"]', '[data-testid*="search-input"]',
    ]

    # Open search
    for sel in search_btn_sels:
        try:
            btn = await page.wait_for_selector(sel, timeout=3_000)
            if btn:
                await btn.click()
                break
        except PWTimeout:
            continue

    for term in search_terms:
        try:
            inp = None
            for sel in search_input_sels:
                try:
                    inp = await page.wait_for_selector(sel, timeout=3_000)
                    break
                except PWTimeout:
                    continue

            if not inp:
                break

            await inp.fill("")
            await inp.type(term, delay=50)
            await page.wait_for_timeout(1500)  # wait for results

            result = await _scan_page_for_cycling(page, keywords)
            if result["available"]:
                return result

        except Exception as e:
            logger.debug("Search attempt failed for '%s': %s", term, e)

    return {"available": False, "start_time": None, "title": None}


async def _scan_page_for_cycling(page: Page, keywords: list[str]) -> dict:
    """
    Scan the current page content for cycling keywords.
    Returns the first match with time if found.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except PWTimeout:
        pass

    content = (await page.content()).lower()

    matched_keyword = next((kw for kw in keywords if kw in content), None)
    if not matched_keyword:
        return {"available": False, "start_time": None, "title": None}

    # Try to extract a title and start time near the keyword
    title, start_time = await _extract_title_and_time(page, matched_keyword)

    return {
        "available": True,
        "start_time": start_time,
        "title": title,
    }


async def _extract_title_and_time(page: Page, keyword: str) -> tuple[Optional[str], Optional[str]]:
    """
    Use JS to find the element containing the keyword and extract nearby text for
    title and time information.
    """
    result = await page.evaluate(
        """(keyword) => {
            const walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                null
            );
            let node;
            while ((node = walker.nextNode())) {
                if (node.textContent.toLowerCase().includes(keyword)) {
                    // Walk up to find a card/tile container
                    let el = node.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!el) break;
                        const text = el.innerText || '';
                        // Look for HH:MM pattern nearby
                        const timeMatch = text.match(/\\b(\\d{1,2}):(\\d{2})\\b/);
                        if (timeMatch || text.length < 500) {
                            return {
                                title: text.trim().substring(0, 200),
                                time: timeMatch ? timeMatch[0] : null
                            };
                        }
                        el = el.parentElement;
                    }
                    return { title: node.textContent.trim().substring(0, 200), time: null };
                }
            }
            return null;
        }""",
        keyword,
    )

    if not result:
        return None, None

    raw_title = result.get("title", "")
    raw_time = result.get("time")

    # Clean up title — take first line
    title = raw_title.split("\n")[0].strip()[:120] if raw_title else None

    # Normalise time to HH:MM
    start_time = None
    if raw_time:
        parts = raw_time.split(":")
        try:
            start_time = f"{int(parts[0]):02d}:{parts[1]}"
        except (IndexError, ValueError):
            pass

    return title, start_time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _race_keywords(race_name: str) -> list[str]:
    """Extract search-friendly keywords from a race name."""
    # Remove stage/etappe suffix
    clean = re.sub(r"\s*(stage|etappe|rit)\s*\d+.*", "", race_name, flags=re.IGNORECASE)
    words = clean.lower().split()
    keywords = []
    # Add full name (lower)
    keywords.append(clean.lower().strip())
    # Add first meaningful word if longer than 4 chars
    for w in words:
        if len(w) > 4 and w not in ("stage", "etappe"):
            keywords.append(w)
    return list(dict.fromkeys(keywords))  # deduplicate, preserve order
