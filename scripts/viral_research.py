#!/usr/bin/env python3
"""
viral_research.py — Scrape top X content di 3 niche untuk Storyteller context.

Niche: lifestyle / education / web3-gamer (cewek 20an urban ID)
Strategy: connect ke existing Chrome (akun2 port 9224) yg punya session valid,
          buka NEW context (isolated, no contamination), search X public,
          close context, leave main Chrome alone.

Output: data/viral_research/raw_<date>.json

Usage:
    python3 scripts/viral_research.py
    python3 scripts/viral_research.py --niche lifestyle
    python3 scripts/viral_research.py --count 10
    python3 scripts/viral_research.py --browser-port 9224
"""
import argparse
import asyncio
import json
import os
import re
import socket
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

AUTOMATION_DIR = Path(__file__).parent.parent
RESEARCH_DIR = AUTOMATION_DIR / "data" / "viral_research"

# Chrome ports to try (affiliate accounts have valid X sessions)
DEFAULT_BROWSER_PORTS = [9224, 9225, 9226, 9223, 9227, 9228, 9229, 9230]

# 3 niche × 5 keyword
KEYWORD_SETS = {
    "lifestyle": [
        "anak muda jakarta",
        "tips glow up",
        "duit 20an",
        "self care",
        "produktif cewek",
    ],
    "education": [
        "tips kuliah",
        "belajar efektif",
        "side hustle anak kos",
        "skill 20an",
        "produktif anak muda",
    ],
    "gamer": [
        "web3 game cewek",
        "play to earn",
        "gamefi indonesia",
        "NFT game",
        "gamer crypto",
    ],
}

# Engagement threshold
MIN_LIKES = 500
MIN_RETWEETS = 100

load_dotenv(AUTOMATION_DIR / ".env")


def _port_alive(port):
    """Check if a Chrome DevTools port is listening."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    ok = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    return ok


def find_browser_port(preferred=None):
    """Find first alive Chrome port, or use preferred one."""
    ports = [int(preferred)] + DEFAULT_BROWSER_PORTS if preferred else DEFAULT_BROWSER_PORTS
    ports = list(dict.fromkeys(ports))  # dedupe preserve order
    for p in ports:
        if _port_alive(p):
            return p
    return None


async def scrape_search(page, keyword, count=10):
    """Scrape top tweets from X search (works with valid session cookies)."""
    # [Fix] Limit range scan ke 30 hari ke belakang (avoid stale tweets)
    # Env: SCAN_DAYS_BACK (default 30)
    import os as _os
    from urllib.parse import quote as _quote
    days_back = int(_os.getenv("SCAN_DAYS_BACK", "30"))
    q_with_filter = f"{keyword} within_time:{days_back}d"
    encoded = _quote(q_with_filter)
    url = f"https://x.com/search?q={encoded}&src=typed_query&f=top"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(4000)
        for _ in range(2):
            await page.evaluate("window.scrollBy(0, 600)")
            await page.wait_for_timeout(1500)

        tweets = await page.evaluate(
            r"""() => {
                const results = [];
                const seen = new Set();
                document.querySelectorAll('article').forEach(a => {
                    try {
                        const textEl = a.querySelector('[data-testid="tweetText"]');
                        const text = textEl ? textEl.textContent.trim() : '';
                        if (!text) return;
                        const linkEl = a.querySelector('a[href*="/status/"]');
                        if (!linkEl) return;
                        const m = (linkEl.getAttribute('href') || '').match(/\/([^/]+)\/status\/(\d+)/);
                        if (!m) return;
                        const key = m[2];
                        if (seen.has(key)) return;
                        seen.add(key);

                        let likes = 0, retweets = 0, replies = 0;
                        a.querySelectorAll('[aria-label]').forEach(el => {
                            const label = (el.getAttribute('aria-label') || '').toLowerCase();
                            const m = label.match(/(\d+(?:[.,]\d+)?[kmb]?)\s+(like|retweet|repost|reply)/);
                            if (!m) return;
                            let n = parseFloat(m[1].replace(/,/g, ''));
                            if (/k$/.test(m[1])) n *= 1000;
                            else if (/m$/.test(m[1])) n *= 1_000_000;
                            if (/like/.test(m[2])) likes = Math.max(likes, n);
                            else if (/retweet|repost/.test(m[2])) retweets = Math.max(retweets, n);
                            else if (/reply/.test(m[2])) replies = Math.max(replies, n);
                        });

                        results.push({
                            username: m[1],
                            tweet_id: key,
                            text: text.substring(0, 500),
                            likes: Math.round(likes),
                            retweets: Math.round(retweets),
                            replies: Math.round(replies),
                        });
                    } catch (e) {}
                });
                return results;
            }"""
        )

        viral = [t for t in tweets if t["likes"] >= MIN_LIKES or t["retweets"] >= MIN_RETWEETS]
        return viral[:count]
    except Exception as e:
        print(f"   ⚠️ scrape error: {e}")
        return []


async def run_research(niches=None, count_per_keyword=5, browser_port=None):
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    niches = niches or list(KEYWORD_SETS.keys())

    # Find browser port
    port = browser_port or find_browser_port(
        os.getenv("RESEARCH_CHROME_PORT")
    )
    if not port:
        raise RuntimeError(
            f"No alive Chrome found on ports {DEFAULT_BROWSER_PORTS}. "
            f"Start Chrome on one of these ports first."
        )
    print(f"🔌 Connecting to Chrome on port {port}...")

    all_results = []
    creator_stats = {}

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
        except Exception as e:
            raise RuntimeError(f"Cannot connect to Chrome on port {port}: {e}")

        # Use MAIN context (has cookies from logged-in X session)
        # Research is read-only, doesn't interfere with affiliate posting
        # Posting cron runs 09:00 + 17:00 UTC, research runs 05:00 UTC, no overlap
        if not browser.contexts:
            raise RuntimeError("Chrome has no contexts — is it properly logged in?")
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        print("   ✅ New page opened in main context (uses logged-in session)")

        try:
            for niche in niches:
                print(f"\n📂 Niche: {niche}")
                for kw in KEYWORD_SETS[niche]:
                    print(f"   🔍 {kw}...", end="")
                    tweets = await scrape_search(page, kw, count=count_per_keyword)
                    print(f" {len(tweets)} viral")
                    for t in tweets:
                        t["niche"] = niche
                        t["keyword"] = kw
                        t["scraped_at"] = datetime.now(timezone.utc).isoformat()
                        all_results.append(t)
                        author = t["username"].lower()
                        if author not in creator_stats:
                            creator_stats[author] = {
                                "tweets": 0, "likes": 0, "retweets": 0
                            }
                        creator_stats[author]["tweets"] += 1
                        creator_stats[author]["likes"] += t["likes"]
                        creator_stats[author]["retweets"] += t["retweets"]
                    await page.wait_for_timeout(5000)  # 5s cooldown
        finally:
            # Always cleanup
            try:
                await page.close()
            except Exception:
                pass
            # Don't close ctx or browser — main account is in use

    # Dedupe by tweet_id
    seen = set()
    unique = []
    for t in all_results:
        if t["tweet_id"] in seen:
            continue
        seen.add(t["tweet_id"])
        unique.append(t)

    # Sort by engagement
    unique.sort(key=lambda t: t["likes"] + t["retweets"] * 3, reverse=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output = {
        "date": today,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "browser_port": port,
        "total_tweets": len(unique),
        "niches": niches,
        "creators_discovered": len(creator_stats),
        "top_creators": sorted(
            [{"author": k, **v} for k, v in creator_stats.items()],
            key=lambda x: x["likes"] + x["retweets"] * 3,
            reverse=True,
        )[:20],
        "tweets": unique[:50],
    }

    out_path = RESEARCH_DIR / f"raw_{today}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Saved {len(unique)} tweets to {out_path.name}")
    if output["top_creators"]:
        top5 = ", ".join(c["author"] for c in output["top_creators"][:5])
        print(f"   Top creators: {top5}")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--niche",
        choices=list(KEYWORD_SETS.keys()) + ["all"],
        default="all",
    )
    parser.add_argument(
        "--count", type=int, default=5, help="Tweets per keyword (max)"
    )
    parser.add_argument(
        "--browser-port",
        type=int,
        help="Specific Chrome DevTools port to use (default: auto-detect)",
    )
    args = parser.parse_args()

    niches = None if args.niche == "all" else [args.niche]
    asyncio.run(
        run_research(
            niches=niches,
            count_per_keyword=args.count,
            browser_port=args.browser_port,
        )
    )


if __name__ == "__main__":
    main()
