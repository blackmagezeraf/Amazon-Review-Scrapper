#!/usr/bin/env python3
"""
Amazon Review Scraper (Browser-based with Playwright)
Supports manual Amazon login via --login (no time limit – waits forever).

Run `python amazon_reviews_browser.py --help` for full documentation.
"""

import argparse
import datetime
import logging
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
# Constants
# ----------------------------------------------------------------------
BASE_URL_TEMPLATE = "https://www.amazon.com/product-reviews/{asin}/"
ROBOTS_URL = "https://www.amazon.com/robots.txt"


# ----------------------------------------------------------------------
# Robots.txt check (unchanged)
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
# Login waiter – INDEFINITE with datetime
# ----------------------------------------------------------------------
def wait_for_manual_login(page, browser, logger):
    """
    Navigate to Amazon and wait INDEFINITELY for user to log in.
    Exits only when login is detected or the browser window is closed.
    """
    try:
        page.goto(
            "https://www.amazon.com", wait_until="domcontentloaded", timeout=15000
        )
    except Exception as e:
        logger.error(f"Could not load Amazon: {e}")
        sys.exit(1)

    logger.info("🔐  Please log in to your Amazon account in the browser.")
    logger.info(
        "⏳  Waiting indefinitely (no time limit). Closing the browser will cancel."
    )

    start_time = datetime.datetime.now()
    while True:
        # 1. If browser or page is closed, exit immediately
        if page.is_closed() or not browser.is_connected():
            elapsed = datetime.datetime.now() - start_time
            logger.error(
                f"❌  Browser was closed after {str(elapsed).split('.')[0]}. Exiting."
            )
            sys.exit(1)

        # 2. Check for login indicators
        try:
            greeting_elem = page.query_selector("#nav-link-accountList-nav-line-1")
            if greeting_elem:
                greeting = greeting_elem.inner_text()
                if "Sign in" not in greeting:
                    logger.info("✅  Login detected (greeting).")
                    return

            signout = page.query_selector("a#nav-item-signout")
            if signout:
                logger.info("✅  Login detected (signout link).")
                return
        except Exception:
            # Page might be navigating, ignore and retry
            pass

        # 3. Show waiting status every 30 seconds
        elapsed_seconds = (datetime.datetime.now() - start_time).seconds
        if elapsed_seconds > 0 and elapsed_seconds % 30 == 0:
            minutes = elapsed_seconds // 60
            secs = elapsed_seconds % 60
            logger.info(f"⏳  Still waiting for login... (elapsed: {minutes}m {secs}s)")

        time.sleep(1)


# ----------------------------------------------------------------------
# Review extraction (unchanged)
# ----------------------------------------------------------------------
def extract_reviews_from_page(page):
    reviews = []
    try:
        page.wait_for_selector("[data-hook='review']", timeout=10000)
    except PlaywrightTimeout:
        pass

    cards = page.query_selector_all("[data-hook='review']")
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
# CLI
# ----------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description=dedent("""\
        🖥️  Amazon Review Scraper (Browser Engine)
        Uses a real Chromium browser via Playwright to scrape reviews.
        Supports manual login with --login (no time limit – waits forever).

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
              Scrape all reviews (headless, no login)

          %(prog)s B08N5WRWNW --login
              Open browser, wait indefinitely for login, then scrape

          %(prog)s B08N5WRWNW --login --headless 0 -v
              Visible browser, verbose logging, login first

          %(prog)s B08N5WRWNW -o reviews.csv --max-pages 5 --delay-min 0.5 --delay-max 2
              Custom output, page limit, and jitter
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
        help="Min delay between requests, seconds (default: 0.4).",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=1.5,
        help="Max delay between requests, seconds (default: 1.5).",
    )
    parser.add_argument(
        "--headless",
        type=int,
        default=1,
        help="Headless mode (1 = yes, 0 = show browser).",
    )
    parser.add_argument("--user-agent", default=None, help="Custom User-Agent.")
    parser.add_argument(
        "--ignore-robots", action="store_true", help="Bypass robots.txt check."
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Wait for manual Amazon login (no time limit).",
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
        browser = p.chromium.launch(headless=headless)
        context_options = {}
        if args.user_agent:
            context_options["user_agent"] = args.user_agent
        context = browser.new_context(**context_options)
        page = context.new_page()

        # ── Login step (if requested) – NO TIME LIMIT ──
        if args.login:
            wait_for_manual_login(page, browser, logger)

        url = BASE_URL_TEMPLATE.format(asin=asin)

        while url:
            page_num += 1
            logger.info(f"Page {page_num}: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(args.delay_min, args.delay_max))
            except Exception as e:
                logger.error(f"Failed to load page: {e}")
                break

            content = page.content()
            if "Enter the characters you see below" in content:
                logger.warning("CAPTCHA or robot check encountered. Stopping.")
                break

            reviews = extract_reviews_from_page(page)
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
