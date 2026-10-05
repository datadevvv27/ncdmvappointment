#!/usr/bin/env python3
"""
ncdmv_watch.py - poll NCDMV skiptheline for open appointments and push alerts.

Unofficial. Drives the public site with Playwright the way a person would
(no login, no booking). Selectors target the site's current Qmatic-based UI
(.QflowObjectItem tiles + jQuery UI datepicker); if NCDMV changes the page,
run with --headed --debug once and adjust the selectors marked SELECTOR.

Usage:
    python ncdmv_watch.py --once --headed --debug     # first run: watch it work
    NTFY_TOPIC=phani-dmv-xyz python ncdmv_watch.py    # poll every 10 min, push to phone
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

BASE_URL = "https://skiptheline.ncdot.gov"
CHARLOTTE_GEO = {"latitude": 35.2271, "longitude": -80.8431}
DEFAULT_OFFICES = "Charlotte,Huntersville,Matthews,Mooresville,Concord,Gastonia,Monroe"
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_slots.json"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))

# SELECTOR: adjust here if the site's markup changes
OFFICE_TILE = ".QflowObjectItem"
OPEN_DAY = "td[data-handler='selectDay']"          # jQuery UI: only selectable days carry this
NEXT_MONTH = ".ui-datepicker-next:not(.ui-state-disabled)"
TIME_CANDIDATES = ["select option", "[data-time]", ".time-slot, .TimeSlot, .timeslot"]

log = logging.getLogger("ncdmv")


@dataclass
class OfficeResult:
    office: str
    open_dates: list[date] = field(default_factory=list)
    target_times: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- page helpers
def service_regex(name: str) -> re.Pattern[str]:
    """Match 'Driver License - First Time' regardless of -, – or — and spacing."""
    words = [re.escape(w) for w in re.split(r"[\s\-–—]+", name.strip()) if w]
    return re.compile(r"[\s\-–—]*".join(words), re.I)


async def settle(page: Page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(800)


async def dump(page: Page, tag: str) -> None:
    """Save screenshot + HTML so selector problems are easy to diagnose."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"\W+", "_", tag)[:40]
        await page.screenshot(path=str(DEBUG_DIR / f"{slug}.png"), full_page=True)
        (DEBUG_DIR / f"{slug}.html").write_text(await page.content(), encoding="utf-8")
        log.info("debug artifacts saved: %s/%s.*", DEBUG_DIR, slug)
    except Exception as exc:  # never let diagnostics break the run
        log.debug("dump failed: %s", exc)


def attach_json_logger(page: Page) -> None:
    """--debug: record the site's JSON/XHR traffic, in case you later want a pure-API version."""
    DEBUG_DIR.mkdir(exist_ok=True)

    async def on_response(resp) -> None:
        if "json" not in (resp.headers.get("content-type") or ""):
            return
        try:
            body = await resp.text()
        except Exception:
            return
        out = DEBUG_DIR / f"xhr_{datetime.now():%H%M%S%f}.json"
        out.write_text(json.dumps({"url": resp.url, "status": resp.status, "body": body[:200_000]}, indent=2))

    page.on("response", on_response)


async def open_service(page: Page, service: re.Pattern[str]) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    await settle(page)
    start = page.get_by_text(re.compile(r"make an appointment", re.I))
    if await start.count():
        await start.first.click()
        await settle(page)
    svc = page.get_by_text(service)
    if not await svc.count():
        raise RuntimeError("service tile not found - check --service text")
    await svc.first.click()
    await settle(page)


async def list_offices(page: Page) -> list[tuple[str, str, bool]]:
    """(display name, full tile text, enabled) for every office tile."""
    tiles = page.locator(OFFICE_TILE)
    offices = []
    for i in range(await tiles.count()):
        tile = tiles.nth(i)
        text = (await tile.inner_text()).strip()
        cls = (await tile.get_attribute("class")) or ""
        name = text.splitlines()[0].strip() if text else f"office-{i}"
        offices.append((name, " ".join(text.split()), "disabled" not in cls.lower()))
    return offices


async def read_times(page: Page) -> list[str]:
    await settle(page)
    for sel in TIME_CANDIDATES:
        loc = page.locator(sel)
        times = []
        for i in range(await loc.count()):
            txt = " ".join(((await loc.nth(i).text_content()) or "").split())
            if re.search(r"\d{1,2}:\d{2}", txt):
                times.append(txt)
        if times:
            return times
    return []


async def check_office(page: Page, service: re.Pattern[str], office: str, target: date) -> OfficeResult:
    await open_service(page, service)  # fresh navigation per office is slower but far more robust than Back
    await page.locator(OFFICE_TILE).filter(has_text=office).first.click()
    await settle(page)

    result = OfficeResult(office)
    for _ in range(2):  # current month + next month covers "tomorrow" across month ends
        days = page.locator(OPEN_DAY)
        month_dates: list[date] = []
        for i in range(await days.count()):
            cell = days.nth(i)
            year = int(await cell.get_attribute("data-year"))
            month = int(await cell.get_attribute("data-month")) + 1  # jQuery UI months are 0-based
            month_dates.append(date(year, month, int((await cell.inner_text()).strip())))
        result.open_dates.extend(month_dates)

        if target in month_dates:
            await days.nth(month_dates.index(target)).click()
            result.target_times = await read_times(page) or ["(date open - times not parsed, check site)"]
            break

        nxt = page.locator(NEXT_MONTH)
        if not await nxt.count():
            break
        await nxt.first.click()
        await page.wait_for_timeout(600)

    result.open_dates.sort()
    return result


# ---------------------------------------------------------------- scan + alert
async def scan(args: argparse.Namespace, target: date) -> list[OfficeResult]:
    keywords = [k.strip().lower() for k in args.offices.split(",") if k.strip()]
    service = service_regex(args.service)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(
            geolocation=CHARLOTTE_GEO,  # site sorts offices by distance when location is allowed
            permissions=["geolocation"],
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await ctx.new_page()
        if args.debug:
            attach_json_logger(page)
        try:
            await open_service(page, service)
            offices = await list_offices(page)
            if not offices:
                await dump(page, "no_offices")
                raise RuntimeError("no office tiles found - selector may need updating")
            wanted = [n for n, full, ok in offices if ok and any(k in full.lower() for k in keywords)]
            log.info("%d offices listed, %d match filter with availability", len(offices), len(wanted))

            results = []
            for name in wanted:
                try:
                    results.append(await check_office(page, service, name, target))
                except Exception as exc:
                    log.warning("%s: %s", name, exc)
                    await dump(page, name)
            return results
        except Exception:
            await dump(page, "scan_failure")
            raise
        finally:
            await browser.close()


def notify(message: str) -> None:
    print(message, flush=True)
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": "NCDMV appointment open", "Priority": "high", "Click": BASE_URL},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning("ntfy push failed: %s", exc)


def report(results: list[OfficeResult], target: date) -> None:
    seen: set[str] = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
    fresh: list[str] = []

    for r in results:
        earliest = r.open_dates[0].isoformat() if r.open_dates else "none"
        log.info("%-30s earliest=%s  %s: %s", r.office, earliest, target, ", ".join(r.target_times) or "-")
        for t in r.target_times:
            key = f"{r.office}|{target}|{t}"
            if key not in seen:
                seen.add(key)
                fresh.append(f"{r.office}: {t}")

    if fresh:
        notify(f"Open on {target:%a %b %d}:\n" + "\n".join(fresh) + "\nBook now and confirm within 15 min.")
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description="Watch NCDMV skiptheline for open appointments.")
    p.add_argument("--service", default=os.getenv("SERVICE", "Driver License - First Time"))
    p.add_argument("--offices", default=os.getenv("OFFICES", DEFAULT_OFFICES),
                   help="comma-separated keywords matched against office tile text")
    p.add_argument("--days-ahead", type=int, default=int(os.getenv("DAYS_AHEAD", "1")),
                   help="1 = tomorrow")
    p.add_argument("--interval", type=int, default=int(os.getenv("INTERVAL_MIN", "10")),
                   help="minutes between scans (keep >= 5 to stay polite)")
    p.add_argument("--once", action="store_true", help="single scan, then exit")
    p.add_argument("--headed", action="store_true", help="show the browser")
    p.add_argument("--debug", action="store_true", help="save XHR JSON + screenshots to ./debug")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    while True:
        target = date.today() + timedelta(days=args.days_ahead)
        try:
            report(asyncio.run(scan(args, target)), target)
        except Exception as exc:
            log.error("scan failed: %s", exc)
        if args.once:
            break
        time.sleep(max(args.interval, 5) * 60 + random.randint(0, 60))


if __name__ == "__main__":
    main()
