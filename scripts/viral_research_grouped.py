#!/usr/bin/env python3
"""
viral_research_grouped.py — Niche-group-aware viral research orchestrator.

Replaces the per-account viral_research step with 1 research run per niche_group.
Each group's results are written to:
    data/viral_research/{niche_group}/{YYYY-MM-DD}.json
    data/viral_research/{niche_group}/latest.json  (copy for auto_reply consumption)

Each account in the group then runs auto_reply.py with --from-cache to consume
the shared cache, and apply_interleave() splits candidates so the 2 accounts
in the same group never post to the same tweet.

Usage:
    python3 scripts/viral_research_grouped.py
    python3 scripts/viral_research_grouped.py --only wellness_affiliate_v1
    python3 scripts/viral_research_grouped.py --count 5
    python3 scripts/viral_research_grouped.py --dry-run     # print plan, don't scrape

[Why grouped?]
- 4 accounts × 28 keywords × 4 cycles = 448 searches/day (current)
- 2 groups × 28 keywords × 4 cycles = 224 searches/day (after) = -50%
- 1 research Chrome per group (instead of 4 = each account's Chrome)
- rate limit / bot detection risk reduced since each account's Chrome is
  used for posting only, not for repeated research queries.
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

AUTOMATION_DIR = Path(__file__).parent.parent
ACCOUNTS_DIR = AUTOMATION_DIR / "accounts"
RESEARCH_ROOT = AUTOMATION_DIR / "data" / "viral_research"
ENV_PATH = AUTOMATION_DIR / ".env"

# Engagement threshold (mirror viral_research.py)
MIN_LIKES = 500
MIN_RETWEETS = 100
DAYS_BACK = int(os.getenv("SCAN_DAYS_BACK", "30"))

# Chrome ports to try (in order)
DEFAULT_BROWSER_PORTS = [9224, 9225, 9226, 9223, 9227, 9228, 9229, 9230]

load_dotenv(ENV_PATH)


# ============== ACCOUNT DISCOVERY ==============
def discover_groups():
    """Discover all accounts, group by niche_group.

    Returns dict: {niche_group: [account_info, ...]}
    where account_info = {name, dir, persona, status, handle, chrome_port, keywords}

    Skips accounts with status not ACTIVE.
    Skips accounts with no niche_group field (treated as solo / no grouping).
    """
    if not ACCOUNTS_DIR.exists():
        return {}
    groups = {}
    solo = []  # accounts without niche_group

    for acc_dir in sorted(ACCOUNTS_DIR.iterdir()):
        if not acc_dir.is_dir():
            continue
        persona_path = acc_dir / "persona.json"
        if not persona_path.exists():
            continue
        try:
            with open(persona_path) as f:
                persona = json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to load {acc_dir.name}/persona.json: {e}")
            continue

        status = persona.get("account", {}).get("status", "UNKNOWN")
        if status != "ACTIVE":
            continue

        niche_group = persona.get("niche_group")
        if not niche_group:
            solo.append({
                "name": acc_dir.name,
                "dir": acc_dir,
                "persona": persona,
                "handle": persona.get("account", {}).get("handle", "?"),
                "chrome_port": persona.get("fingerprint", {}).get("chrome_port", "?"),
            })
            continue

        keywords = persona.get("keywords_priority", [])
        keywords = [k for k in keywords if k and k != "TBD"]
        if not keywords:
            print(f"⚠️ {acc_dir.name} has niche_group but no keywords_priority, skip")
            continue

        acc_info = {
            "name": acc_dir.name,
            "dir": acc_dir,
            "persona": persona,
            "handle": persona.get("account", {}).get("handle", "?"),
            "chrome_port": persona.get("fingerprint", {}).get("chrome_port", "?"),
            "keywords": keywords,
        }
        groups.setdefault(niche_group, []).append(acc_info)

    return groups, solo


# ============== SCRAPING (reuse logic from viral_research.py) ==============
async def scrape_search(page, keyword, count=5):
    """Scrape top tweets from X search."""
    from urllib.parse import quote as _quote
    q_with_filter = f"{keyword} within_time:{DAYS_BACK}d"
    encoded = _quote(q_with_filter)
    url = f"https://x.com/search?q={encoded}&src=typed_query&f=top"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(3000)
        for _ in range(2):
            await page.evaluate("window.scrollBy(0, 500)")
            await page.wait_for_timeout(1000)
        tweets = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            document.querySelectorAll('article').forEach(a => {
                try {
                    const textEl = a.querySelector('[data-testid="tweetText"]');
                    const text = textEl ? textEl.textContent.trim() : '';
                    if (!text) return;
                    const linkEl = a.querySelector('a[href*="/status/"]');
                    if (!linkEl) return;
                    const m = (linkEl.getAttribute('href') || '').match(/\\/([^/]+)\\/status\\/(\\d+)/);
                    if (!m) return;
                    const key = m[2];
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({
                        username: m[1],
                        tweet_id: key,
                        text: text.substring(0, 300),
                    });
                } catch (e) {}
            });
            return results;
        }""")
        viral = [t for t in tweets if t.get("likes", 0) >= MIN_LIKES or t.get("retweets", 0) >= MIN_RETWEETS]
        return viral[:count]
    except Exception as e:
        print(f"   ⚠️ scrape error: {e}")
        return []


# ============== RESEARCH ONE GROUP ==============
async def research_group(group_name, accounts, count_per_keyword=5, dry_run=False):
    """Run research for one niche_group using first account's Chrome + keywords.

    Returns: dict with cache data, or None on failure.
    """
    if not accounts:
        return None
    master = accounts[0]  # first ACTIVE account = search master
    print(f"\n{'='*60}")
    print(f"📂 Group: {group_name}")
    print(f"   Master: @{master['handle']} (port {master['chrome_port']}, {master['name']})")
    print(f"   Members: {len(accounts)}")
    for a in accounts:
        print(f"     - @{a['handle']} ({a['name']})")
    print(f"   Keywords: {len(master['keywords'])}")

    if dry_run:
        print(f"   [DRY RUN] would scrape {len(master['keywords'])} keywords × {count_per_keyword} tweets")
        return None

    port = int(master["chrome_port"])
    candidates = []
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
        except Exception as e:
            print(f"   ❌ Cannot connect to Chrome on port {port}: {e}")
            print(f"      Start Chrome: google-chrome --remote-debugging-port={port} --user-data-dir={master['dir']}/.chrome-profile")
            return None

        if not browser.contexts:
            print(f"   ❌ Chrome has no contexts — is {master['name']} logged in?")
            return None

        ctx = browser.contexts[0]
        page = await ctx.new_page()
        print(f"   ✅ New page opened in main context (uses @{master['handle']}'s session)")

        try:
            for kw in master["keywords"]:
                print(f"   🔍 {kw}...", end="", flush=True)
                tweets = await scrape_search(page, kw, count=count_per_keyword)
                print(f" {len(tweets)} viral")
                for t in tweets:
                    t["keyword"] = kw
                    t["niche_group"] = group_name
                    t["scraped_at"] = datetime.now(timezone.utc).isoformat()
                    candidates.append(t)
                await page.wait_for_timeout(5000)  # 5s between keywords
        finally:
            try:
                await page.close()
            except Exception:
                pass
            # Don't close ctx or browser — main account may still use it

    # Dedupe by tweet_id
    seen = set()
    unique = []
    for t in candidates:
        if t["tweet_id"] in seen:
            continue
        seen.add(t["tweet_id"])
        unique.append(t)

    # Sort by engagement
    unique.sort(key=lambda t: t.get("likes", 0) + t.get("retweets", 0) * 3, reverse=True)

    cache_data = {
        "niche_group": group_name,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "master_account": master["name"],
        "master_handle": master["handle"],
        "browser_port": port,
        "group_members": [a["handle"] for a in accounts],
        "keyword_count": len(master["keywords"]),
        "total_candidates": len(unique),
        "candidates": unique,
    }

    # Save to group directory
    group_dir = RESEARCH_ROOT / group_name
    group_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated_path = group_dir / f"{today}.json"
    latest_path = group_dir / "latest.json"

    with open(dated_path, "w") as f:
        json.dump(cache_data, f, indent=2, ensure_ascii=False)
    # Copy to latest.json (auto_reply reads this)
    shutil.copy2(dated_path, latest_path)

    print(f"   ✅ Saved {len(unique)} unique candidates")
    print(f"      Dated: {dated_path.relative_to(AUTOMATION_DIR)}")
    print(f"      Latest: {latest_path.relative_to(AUTOMATION_DIR)}")
    return cache_data


# ============== MAIN ==============
async def main_async(args):
    groups, solo = discover_groups()

    print(f"\n{'='*60}")
    print(f"🔬 Viral Research (Grouped)")
    print(f"{'='*60}")
    print(f"   Groups: {len(groups)}")
    for g, accs in groups.items():
        print(f"     - {g}: {len(accs)} accounts ({', '.join(a['handle'] for a in accs)})")
    if solo:
        print(f"   Solo (no niche_group, will use own research): {len(solo)}")
        for s in solo:
            print(f"     - {s['handle']} ({s['name']})")

    if not groups and not solo:
        print("❌ No groups or solo accounts found")
        return False

    # Filter by --only if specified
    if args.only:
        if args.only not in groups:
            print(f"❌ Group '{args.only}' not found")
            return False
        groups = {args.only: groups[args.only]}

    # Research each group
    results = {}
    for group_name, accounts in groups.items():
        cache = await research_group(
            group_name, accounts,
            count_per_keyword=args.count,
            dry_run=args.dry_run,
        )
        if cache:
            results[group_name] = cache

    # Summary
    print(f"\n{'='*60}")
    print(f"📊 RESEARCH SUMMARY")
    print(f"{'='*60}")
    total_candidates = sum(r["total_candidates"] for r in results.values())
    for g, r in results.items():
        print(f"   {g}: {r['total_candidates']} candidates (master: @{r['master_handle']})")
    print(f"   Total: {total_candidates} unique candidates across {len(results)} groups")

    if args.dry_run:
        return True  # dry-run success even with no results
    return len(results) > 0


def main():
    parser = argparse.ArgumentParser(description="Niche-group viral research orchestrator")
    parser.add_argument("--only", help="Only research this niche_group")
    parser.add_argument("--count", type=int, default=5, help="Tweets per keyword (default 5)")
    parser.add_argument("--dry-run", action="store_true", help="Show plan, don't scrape")
    args = parser.parse_args()
    success = asyncio.run(main_async(args))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
