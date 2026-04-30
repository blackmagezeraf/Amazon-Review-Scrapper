#!/usr/bin/env python3
"""
Amazon Review Scraper (Browser-based with Playwright)
Respects robots.txt by default. Use --ignore-robots to override.
Bundled Chromium is found automatically when built as an executable.
"""

import argparse
import logging
import os
import random
import re
import sys
import time
from textwrap import dedent
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# If running from a PyInstaller bundle, set the browser path
# ----------------------------------------------------------------------
if getattr(sys, "frozen", False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(sys._MEIPASS, "browsers")

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
BASE_URL_TEMPLATE = "https://www.amazon.com/product-reviews/{asin}/"
ROBOTS_URL = "https://www.amazon.com/robots.txt"


# ----------------------------------------------------------------------
# Robots.txt check
# ----------------------------------------------------------------------
def check_robots(asin, user_agent, ignore, logger):
    rp = RobotFileParser()
    rp.set_url(ROBOTS_URL)
    try:
        rp.read()
    except Exception as e:
        logger.warning(f"Could not fetch robots.txt ({e}). Proceeding cautiously.")
        return True

    path = f"/product-reviews/{asin}/"
    if rp.can_fetch(user_agent, path):
        logger.info("robots.txt allows scraping. ✅")
        return True
    if ignore:
        logger.warning(
            "robots.txt DISALLOWS this path, but --ignore-robots flag is set. ⚠️"
        )
        return True
    else:
        logger.error(
            f"robots.txt disallows {path}.\n"
            "Use --ignore-robots to bypass this check (at your own risk)."
        )
        sys.exit(1)


# ----------------------------------------------------------------------
# Review extraction (polling every 1 second)
# ----------------------------------------------------------------------
def extract_reviews_from_page(page, logger):
    """
    Wait for reviews to appear, handling page navigations gracefully.
    Returns a list of review dicts, or empty list if none found.
    """
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            # Wait for at least one review card to appear (no timeout)
            logger.info(f"Waiting for reviews to load... (attempt {attempt + 1})")
            page.wait_for_selector("[data-hook='review']", timeout=0)
            break  # success
        except PlaywrightTimeout:
            # timeout not used, but just in case
            continue
        except Exception as e:
            # Could be 'Execution context was destroyed' − page navigated
            logger.warning(f"Attempt {attempt + 1} failed: {e}")
            if "Execution context was destroyed" in str(e) or "Navigation" in str(
                type(e).__name__
            ):
                try:
                    # Wait for the page to fully reload
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    time.sleep(2)
                except Exception:
                    pass
                continue
            else:
                return []

    # Now collect all cards (page is stable)
    try:
        cards = page.query_selector_all("[data-hook='review']")
    except Exception:
        return []

    if not cards:
        return []

    reviews = []
    for card in cards:
        body = card.query_selector("[data-hook='review-body']")
        text = body.inner_text().strip() if body else ""

        stars = ""
        star_icon = card.query_selector("[data-hook='review-star-rating']")
        if star_icon:
            classes = star_icon.get_attribute("class") or ""
            match = re.search(r"a-icon-star-(\d)", classes)
            if match:
                stars = match.group(1)
            else:
                txt = star_icon.inner_text()
                match = re.search(r"(\d+\.?\d*)", txt)
                stars = match.group(1) if match else ""

        profile = ""
        a_tag = card.query_selector("a.a-profile")
        if a_tag:
            href = a_tag.get_attribute("href")
            if href:
                profile = urljoin("https://www.amazon.com/", href)
        if not profile:
            name_span = card.query_selector("span.a-profile-name")
            if name_span:
                parent = name_span.evaluate("el => el.closest('a')")
                if parent:
                    href = parent.get_attribute("href")
                    if href:
                        profile = urljoin("https://www.amazon.com/", href)

        reviews.append(
            {
                "review": text,
                "review_stars": stars,
                "user_profile_link": profile,
            }
        )
    return reviews


def get_next_page_url(page, current_url):
    next_li = page.query_selector("li.a-last")
    if next_li:
        a = next_li.query_selector("a")
        if a and "disabled" not in (next_li.get_attribute("class") or ""):
            href = a.get_attribute("href")
            if href:
                return urljoin(current_url, href)
    links = page.query_selector_all("a")
    for link in links:
        if "Next page" in (link.inner_text() or ""):
            href = link.get_attribute("href")
            if href:
                return urljoin(current_url, href)
    return None


# ----------------------------------------------------------------------
# Helper: locate Chrome executable in bundled folder
# ----------------------------------------------------------------------
def get_bundled_chrome_path():
    """
    Returns path to chrome.exe inside the PyInstaller bundle,
    or None if not frozen.
    """
    if not getattr(sys, "frozen", False):
        return None
    # The browsers folder contains subfolders like chromium-1217/chrome-win64/chrome.exe
    browsers_dir = os.path.join(sys._MEIPASS, "browsers")
    # Find the chromium directory
    for entry in os.listdir(browsers_dir):
        if entry.startswith("chromium-") and not entry.startswith("chromium_headless"):
            chrome_path = os.path.join(
                browsers_dir, entry, "chrome-win64", "chrome.exe"
            )
            if os.path.exists(chrome_path):
                return chrome_path
    raise FileNotFoundError("Could not find chrome.exe in bundled browsers")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description=dedent("""\
        🖥️  Amazon Review Scraper (Browser Engine)
        Respects robots.txt by default. Use --ignore-robots to override.
        Shows a visible Chromium browser (change with --headless).
        No time limits – waits forever for pages and reviews.

        Collects:
          • Review text
          • Star rating
          • User profile link

        Output CSV columns: review, review_stars, user_profile_link
        """),
        epilog=dedent("""\
        ───────────────────────────
        📋 Usage examples:
          %(prog)s B08N5WRWNW
              Scrape all reviews (visible browser, robots.txt respected)

          %(prog)s B08N5WRWNW --ignore-robots
              Bypass robots.txt check

          %(prog)s B08N5WRWNW -o reviews.csv --max-pages 5
              Limit to 5 pages, custom filename

          %(prog)s B08N5WRWNW --headless 1 --delay-min 0.5 --delay-max 2 -v
              Run hidden, custom jitter, verbose output
        ───────────────────────────
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "asin", metavar="ASIN", help="Amazon product ASIN (e.g., B08N5WRWNW)."
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output CSV file (default: amazon_reviews_<ASIN>.csv).",
    )
    parser.add_argument("--max-pages", type=int, default=0, help="Max pages (0 = all).")
    parser.add_argument(
        "--delay-min",
        type=float,
        default=0.4,
        help="Min jitter delay between pages (seconds, default: 0.4).",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=1.5,
        help="Max jitter delay between pages (seconds, default: 1.5).",
    )
    parser.add_argument(
        "--headless",
        type=int,
        choices=[0, 1],
        default=0,
        help="Browser visibility (0=visible, 1=headless). Default: 0.",
    )
    parser.add_argument("--user-agent", default=None, help="Custom User-Agent.")
    parser.add_argument(
        "--ignore-robots", action="store_true", help="Bypass robots.txt check."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return parser.parse_args()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger(__name__)

    asin = args.asin.strip()
    if not asin:
        logger.error("ASIN cannot be empty.")
        sys.exit(1)

    ua_for_robots = args.user_agent or "Playwright"
    check_robots(asin, ua_for_robots, args.ignore_robots, logger)

    output_file = args.output or f"amazon_reviews_{asin}.csv"
    headless = bool(args.headless)

    logger.info(
        f"ASIN: {asin} | Headless: {headless} | Jitter: {args.delay_min}–{args.delay_max} s"
    )
    if args.max_pages:
        logger.info(f"Page limit: {args.max_pages}")

    all_reviews = []
    page_num = 0

    with sync_playwright() as p:
        # Determine if we need to use the bundled browser
        executable_path = get_bundled_chrome_path()  # None if not frozen

        browser = p.chromium.launch(
            headless=headless,
            executable_path=executable_path,
        )
        context_options = {}
        if args.user_agent:
            context_options["user_agent"] = args.user_agent
        context = browser.new_context(**context_options)
        page = context.new_page()

        url = BASE_URL_TEMPLATE.format(asin=asin)

        while url:
            page_num += 1
            logger.info(f"Loading page {page_num}: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=0)
                time.sleep(3)  # allow for any redirects
                time.sleep(random.uniform(args.delay_min, args.delay_max))
            except Exception as e:
                logger.error(f"Failed to load page: {e}")
                break

            content = page.content()
            if "Enter the characters you see below" in content:
                logger.warning("CAPTCHA or robot check encountered. Stopping.")
                break

            reviews = extract_reviews_from_page(page, logger)
            if not reviews:
                logger.info("No reviews found. Ending.")
                break

            all_reviews.extend(reviews)
            logger.info(
                f"  → extracted {len(reviews)} reviews (total: {len(all_reviews)})"
            )

            if args.max_pages and page_num >= args.max_pages:
                logger.info("Reached max pages limit.")
                break

            url = get_next_page_url(page, url)

        browser.close()

    if not all_reviews:
        logger.error("No reviews collected.")
        sys.exit(1)

    df = pd.DataFrame(all_reviews)
    df.to_csv(output_file, index=False, encoding="utf-8")
    logger.info(f"Saved {len(df)} reviews to {output_file}")
    if args.verbose:
        print(df.head())


if __name__ == "__main__":
    main()
