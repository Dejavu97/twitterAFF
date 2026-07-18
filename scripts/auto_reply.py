#!/usr/bin/env python3
"""
auto_reply.py — Generic X auto-reply (multi-account aware)

Usage:
    python3 scripts/auto_reply.py --account akun1_nunani
    python3 scripts/auto_reply.py --account akun2_

Each account folder has:
    - persona.json (voice, niche, limits)
    - products.json (affiliate catalog with trigger keywords)
    - .chrome-profile/ (browser cookies for that account)
    - replied.json, daily_count.json, pending_replies.json (per-account state)
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent to path
AUTOMATION_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(AUTOMATION_DIR))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(AUTOMATION_DIR / ".env")


# ============== PATHS ==============
def get_account_paths(account_name):
    """Resolve all paths for an account."""
    account_dir = AUTOMATION_DIR / "accounts" / account_name
    if not account_dir.exists():
        raise FileNotFoundError(f"Account folder not found: {account_dir}")
    return {
        "account_dir": account_dir,
        "persona": account_dir / "persona.json",
        "products": account_dir / "products.json",
        "drafts": account_dir / "drafts.json",
        "chrome_profile": account_dir / ".chrome-profile",
        "replied": account_dir / "replied.json",
        "daily_count": account_dir / "daily_count.json",
        "pending": account_dir / "pending_replies.json",
        "errors": account_dir / "errors.json",
    }


# ============== LOAD CONFIG ==============
def load_persona(paths):
    with open(paths["persona"]) as f:
        return json.load(f)


def load_products(paths):
    """Load products.json (legacy, sekarang kosong). Return {} kalau file missing.
    Real product data ada di link_knowledge.json."""
    products_path = paths.get("products")
    if not products_path or not products_path.exists():
        return {}
    try:
        with open(products_path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_drafts(paths):
    """Load per-persona draft templates. Returns empty dict if file missing."""
    drafts_path = paths.get("drafts")
    if not drafts_path or not drafts_path.exists():
        return {"templates": {}, "no_link": {}, "matching": {}}
    try:
        with open(drafts_path) as f:
            return json.load(f)
    except Exception:
        return {"templates": {}, "no_link": {}, "matching": {}}


def _get_llm_client():
    """Initialize OpenAI-compatible client (LLM_API_KEY from .env).

    Returns None if key missing — callers should fallback to template.
    Uses LLM_API_KEY + LLM_BASE_URL (same vars as auto_approve_pending.py).
    """
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key or api_key.startswith("***"):
        return None
    try:
        from openai import OpenAI
        return OpenAI(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        )
    except Exception as e:
        print(f"   ⚠️ LLM client init error: {e}", "WARN")
        return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============== MULTI-ACCOUNT GROUPING (interleave) ==============
def load_sibling_accounts(my_account_dir):
    """Load all sibling persona.json files (exclude self).
    Returns list of persona dicts for accounts in same parent dir.
    """
    accounts_root = my_account_dir.parent
    siblings = []
    if not accounts_root.exists():
        return siblings
    for acc_dir in accounts_root.iterdir():
        if not acc_dir.is_dir() or acc_dir == my_account_dir:
            continue
        persona_path = acc_dir / "persona.json"
        if not persona_path.exists():
            continue
        try:
            with open(persona_path) as f:
                siblings.append(json.load(f))
        except Exception as e:
            print(f"   ⚠️ Failed to load sibling {acc_dir.name}: {e}")
    return siblings


def apply_interleave(candidates, persona, sibling_accounts, cycle_date_str=None):
    """[Multi-account grouping] Round-robin split candidates across group members.

    When 2+ accounts share a `niche_group`, each takes a unique subset
    of candidates so they never post to the same tweet (zero correlation).

    Algorithm:
      1. Filter siblings to those in same niche_group + ACTIVE status
      2. Sort group members by handle (stable order, both accounts see same)
      3. Find my index in group
      4. Per-cycle start_offset rotation (for odd-count balance: 2-cycle fairness)
      5. Sort candidates deterministically (tweet_id or likes_desc)
      6. Apply modulo: keep only candidates where i % group_size == my_effective_offset

    Returns filtered list. If interleave disabled or <2 group members, returns input unchanged.
    """
    import hashlib

    interleave_cfg = persona.get("interleave", {}) or {}
    if not interleave_cfg.get("enabled"):
        return candidates

    my_niche_group = persona.get("niche_group")
    if not my_niche_group:
        return candidates

    my_handle = persona.get("account", {}).get("handle", "")

    # Build group: me + siblings in same niche_group, ACTIVE only
    group_members = [{"handle": my_handle, "persona": persona}]
    for sib in sibling_accounts:
        sib_group = sib.get("niche_group")
        sib_status = sib.get("account", {}).get("status", "")
        if sib_group == my_niche_group and sib_status == "ACTIVE":
            sib_handle = sib.get("account", {}).get("handle", "")
            if sib_handle and sib_handle != my_handle:
                group_members.append({"handle": sib_handle, "persona": sib})

    group_size = len(group_members)
    if group_size < 2:
        # Interleave only kicks in for 2+ group members
        return candidates

    # Sort group members by handle (stable, both accounts see same order)
    group_members.sort(key=lambda m: m["handle"])
    my_index = next(i for i, m in enumerate(group_members) if m["handle"] == my_handle)

    # Per-cycle start_offset rotation (hash of YYYY-MM-DD)
    # Ensures odd candidate counts alternate who gets the extra candidate
    if cycle_date_str is None:
        cycle_date_str = datetime.now().strftime("%Y-%m-%d")
    start_offset = int(hashlib.md5(cycle_date_str.encode()).hexdigest(), 16) % group_size
    my_effective_offset = (my_index - start_offset) % group_size

    # Deterministic candidate sort
    sort_key = interleave_cfg.get("sort_key", "tweet_id")
    if sort_key == "likes_desc":
        candidates_sorted = sorted(
            candidates,
            key=lambda c: (-int(c.get("likes", 0) or 0), c.get("tweet_id", ""))
        )
    else:  # default: tweet_id
        candidates_sorted = sorted(candidates, key=lambda c: c.get("tweet_id", ""))

    # Apply modulo split
    filtered = [
        c for i, c in enumerate(candidates_sorted)
        if i % group_size == my_effective_offset
    ]

    print(
        f"   🔀 Interleave: group={my_niche_group} size={group_size} "
        f"my_index={my_index} my_offset={my_effective_offset} "
        f"start_offset={start_offset} sort={sort_key} "
        f"→ {len(filtered)}/{len(candidates)} candidates (date={cycle_date_str})"
    )
    return filtered


# ============== KEYWORDS FROM PRODUCTS ==============
def get_search_keywords(persona, products_data):
    """Build search queries from persona + products."""
    queries = []

    # From persona's keywords_priority
    for kw in persona.get("keywords_priority", []):
        if kw and kw != "TBD":
            queries.append(kw)

    # From products' trigger_keywords
    for product in products_data.get("products", []):
        if product.get("status") != "active":
            continue
        for kw in product.get("trigger_keywords", []):
            if kw not in queries:
                queries.append(kw)
        # Also use product name as a query
        if product.get("search_aliases"):
            for alias in product["search_aliases"]:
                if alias not in queries:
                    queries.append(alias)

    return queries


# ============== MAIN BOT LOGIC ==============
async def run_bot(account_name, dry_run=False, from_cache=None):
    paths = get_account_paths(account_name)
    persona = load_persona(paths)
    products_data = load_products(paths)

    # Account status check
    account_status = persona.get("account", {}).get("status", "ACTIVE")
    if account_status in ("PAUSED", "DRAFT"):
        print(f"⏸️ Account '{account_name}' is {account_status} — abort.")
        print(f"   Reason: {persona.get('account', {}).get('paused_reason', persona.get('account', {}).get('draft_reason', 'unknown'))}")
        return False

    # Auto-post check
    # Note: auto_post_enabled only gates the actual post action (auto_post.py).
    # The SCAN can run regardless — finds candidates + saves to pending.
    # User manually approves via Telegram, then triggers post.
    # So this check is REMOVED — scan is always allowed.

    # Build config
    keywords = get_search_keywords(persona, products_data)
    limits = persona.get("limits", {})
    daily_limit = limits.get("daily_reply_max", 1)
    cooldown = limits.get("cooldown_min_seconds", 7200)

    # Chrome port: from persona fingerprint first, then env, then default
    fingerprint = persona.get("fingerprint", {})
    chrome_profile = paths["chrome_profile"]
    chrome_port = int(fingerprint.get("chrome_port")
                      or os.getenv("CHROME_REMOTE_PORT", "9223"))

    # Telegram notif target
    telegram_target = os.getenv("TELEGRAM_TARGET", "telegram")
    telegram_notify = os.getenv("TELEGRAM_NOTIFY", "true").lower() == "true"

    print(f"🤖 Account: {persona['account']['handle']} ({account_name})")
    print(f"   Niche: {persona['niche']['primary']}")
    print(f"   Status: {persona['account'].get('status', '?')}")
    print(f"   Daily limit: {daily_limit}")
    print(f"   Cooldown: {cooldown}s ({cooldown/60:.0f} min)")
    print(f"   Keywords: {len(keywords)} queries")
    print(f"   Chrome profile: {chrome_profile}")
    print(f"   Chrome port: {chrome_port}")
    print(f"   Dry-run: {dry_run}")
    print()

    if not chrome_profile.exists():
        print(f"❌ Chrome profile not found: {chrome_profile}")
        print(f"   Login X di Camofox browser, simpan ke path ini dulu.")
        return False

    if not keywords:
        print(f"⚠️ No keywords found (persona.keywords_priority + products.trigger_keywords)")
        print(f"   Edit persona.json / products.json, atau pakai defaults.")
        return False

    # ====== Load candidates: from cache OR live scan ======
    candidates = []
    cache_mode = bool(from_cache)
    if cache_mode:
        # Resolve cache path (relative to AUTOMATION_DIR or absolute)
        cache_path = Path(from_cache)
        if not cache_path.is_absolute():
            cache_path = AUTOMATION_DIR / from_cache
        if not cache_path.exists():
            print(f"❌ Cache file not found: {cache_path}")
            return False
        try:
            cache_data = json.loads(cache_path.read_text())
        except Exception as e:
            print(f"❌ Cache read error: {e}")
            return False
        cache_candidates = cache_data.get("candidates", [])
        cache_group = cache_data.get("niche_group", "?")
        cache_scraped = cache_data.get("scraped_at", "?")
        my_group = persona.get("niche_group", "?")
        if cache_group != my_group:
            print(
                f"⚠️ Cache group mismatch: cache={cache_group} vs account={my_group}. "
                f"Proceeding anyway (filtering will happen via interleave)."
            )
        print(
            f"📂 Loaded {len(cache_candidates)} candidates from cache: {cache_path.name} "
            f"(group={cache_group}, scraped_at={cache_scraped})"
        )
        candidates = cache_candidates
    else:
        # ====== Connect to Chrome via CDP + live scan ======
        # chrome_port was already resolved from persona.fingerprint / env at line 164-165
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(f"http://localhost:{chrome_port}")
            except Exception as e:
                print(f"❌ Cannot connect to Chrome on port {chrome_port}: {e}")
                print(f"   Start Chrome: google-chrome --remote-debugging-port={chrome_port} --user-data-dir={chrome_profile}")
                return False

            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()

            # ====== Scan keywords (with batching) ======
            batch_size = int(os.getenv("SEARCH_BATCH_SIZE", "14"))
            batch_cooldown = int(os.getenv("SEARCH_BATCH_COOLDOWN_SECONDS", "120"))
            # [Fix] Limit range scan ke 30 hari ke belakang (avoid stale tweets)
            # Env: SCAN_DAYS_BACK (default 30)
            scan_days_back = int(os.getenv("SCAN_DAYS_BACK", "30"))
            total_batches = (len(keywords) + batch_size - 1) // batch_size
            print(f"🔍 Scanning {len(keywords)} keywords in {total_batches} batches (range: {scan_days_back}d back)")

            for batch_idx, batch_start in enumerate(range(0, len(keywords), batch_size), 1):
                batch = keywords[batch_start:batch_start + batch_size]
                print(f"   📦 Batch {batch_idx}/{total_batches}")
                for q in batch:
                    print(f"      🔍 {q}...")
                    # [Fix] Tambah within_time:{N}d untuk limit range (avoid stale tweets)
                    # Env: SCAN_DAYS_BACK (default 30)
                    q_with_filter = f"{q} within_time:{scan_days_back}d"
                    url = f"https://x.com/search?q={q_with_filter}&src=typed_query&f=top"
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                        await page.wait_for_timeout(3000)
                        for _ in range(4):
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
                                    
                                    // Extract engagement metrics from buttons
                                    function parseCount(label) {
                                        if (!label) return 0;
                                        const num = label.replace(/[^0-9.]/g, '').replace('.', '');
                                        return parseInt(num) || 0;
                                    }
                                    
                                    const likeBtn = a.querySelector('[data-testid="like"]');
                                    const replyBtn = a.querySelector('[data-testid="reply"]');
                                    const retweetBtn = a.querySelector('[data-testid="retweet"]');
                                    
                                    const likeLabel = likeBtn ? (likeBtn.getAttribute('aria-label') || '') : '';
                                    const replyLabel = replyBtn ? (replyBtn.getAttribute('aria-label') || '') : '';
                                    const retweetLabel = retweetBtn ? (retweetBtn.getAttribute('aria-label') || '') : '';
                                    
                                    results.push({
                                        username: m[1],
                                        tweet_id: key,
                                        text: text.substring(0, 300),
                                        like_count: parseCount(likeLabel),
                                        reply_count: parseCount(replyLabel),
                                        retweet_count: parseCount(retweetLabel),
                                    });
                                } catch (e) {}
                            });
                            return results;
                        }""")
                        candidates.extend(tweets)
                        print(f"         Found: {len(tweets)}")
                    except Exception as e:
                        print(f"         ⚠️ Error: {e}", "WARN")
                if batch_idx < total_batches:
                    print(f"   ⏸️ Cooldown {batch_cooldown}s...")
                    await page.wait_for_timeout(batch_cooldown * 1000)

            await page.close()
            await browser.close()

    # ====== Filter candidates (existing logic) ======
    replied = load_json(paths["replied"], {})
    pending = load_json(paths["pending"], {"overrides": {}})
    already_handled = set(replied.keys()) | set(pending["overrides"].keys())

    own_handle = persona["account"]["handle"].lower()
    unique_candidates = []
    for c in candidates:
        cid = c["tweet_id"]
        if cid in already_handled:
            continue
        if c["username"].lower() == own_handle:
            continue
        unique_candidates.append(c)

    print(f"\n📊 Total unique new candidates: {len(unique_candidates)}")

    # ====== Engagement filter: only reply to tweets with good engagement ======
    min_likes = int(os.getenv("MIN_LIKES", "3"))
    min_replies = int(os.getenv("MIN_REPLIES", "1"))
    min_retweets = int(os.getenv("MIN_RETWEETS", "1"))
    engaged = []
    skipped_low_engage = []
    for c in unique_candidates:
        lk = c.get("like_count", 0) or 0
        rp = c.get("reply_count", 0) or 0
        rt = c.get("retweet_count", 0) or 0
        if lk >= min_likes or rp >= min_replies or rt >= min_retweets:
            engaged.append(c)
        else:
            skipped_low_engage.append(c)
    if skipped_low_engage:
        print(f"   ⏭️ Filtered {len(skipped_low_engage)} low-engagement tweets (likes<{min_likes} & replies<{min_replies} & retweets<{min_retweets})")
        for c in skipped_low_engage[:3]:
            print(f"      ❌ @{c['username']}: {c.get('like_count',0)}❤️ {c.get('reply_count',0)}💬 {c.get('retweet_count',0)}🔁 | {c['text'][:60]}...")
    unique_candidates = engaged
    print(f"📊 After engagement filter: {len(unique_candidates)} candidates")

    # ====== INTERLEAVE: round-robin split across niche_group members ======
    # [Multi-account grouping] When 2+ accounts share niche_group, split candidates
    # so they never post to the same tweet (zero correlation). Each account in
    # the group takes a unique subset based on (sorted_index % group_size).
    sibling_accounts = load_sibling_accounts(paths["account_dir"])
    unique_candidates = apply_interleave(
        unique_candidates, persona, sibling_accounts
    )
    print(f"📊 After interleave: {len(unique_candidates)} candidates for this account")

    # ====== AUTO-MATCH: kalau kategori dikenal → auto-post ======
    knowledge = load_knowledge(paths["account_dir"])
    auto_posted = []
    auto_skipped = []

    if knowledge and knowledge.get("global_settings", {}).get("auto_mode_enabled", False):
        # Count today's auto-posts
        from datetime import datetime as _dt
        today_key = _dt.now().strftime("%Y-%m-%d")
        daily_total = replied.get("_daily_count_meta", {}).get(today_key, 0) if isinstance(replied, dict) else 0

        max_per_day = knowledge["global_settings"].get("max_auto_posts_per_day", 3)
        max_per_cycle = knowledge["global_settings"].get("max_auto_posts_per_cycle", 1)
        cycle_count = 0
        for c in unique_candidates:
            if daily_total >= max_per_day:
                auto_skipped.append((c["tweet_id"], "daily_limit_global"))
                continue
            if cycle_count >= max_per_cycle:
                auto_skipped.append((c["tweet_id"], "max_per_cycle"))
                continue

            match = match_category(c, knowledge)
            if not match:
                continue  # not for this candidate

            category_id, link, style_seed = match
            cat_cfg = knowledge["categories"][category_id]
            cat_daily = cat_cfg.get("daily_limit_per_category", 2)

            # Per-category daily count
            # Source of truth = real entries replied.json. JANGAN trust `_category_daily` counter
            # (bisa ngaco, di-increment manual tanpa verify real post). Recompute on-the-fly.
            cat_count_today = 0
            if isinstance(replied, dict):
                for k, v in replied.items():
                    if k.startswith("_") or not isinstance(v, dict):
                        continue
                    if v.get("category") == category_id:
                        ts = v.get("time") or v.get("replied_at") or v.get("timestamp") or ""
                        if ts.startswith(today_key):
                            cat_count_today += 1
            if cat_count_today >= cat_daily:
                auto_skipped.append((c["tweet_id"], f"daily_limit_{category_id}"))
                continue

            # Build reply text — LLM first, fallback to template
            llm_client = _get_llm_client()
            used_llm = False
            if llm_client:
                try:
                    candidate_data = {"text": c.get("text", ""), "username": c.get("username", "?")}
                    llm_text = _llm_generate_reply(candidate_data, persona, products_data, llm_client)
                    if llm_text and len(llm_text) > 8:
                        # LLM reply doesn't include link, append separately
                        reply_text = f"{llm_text}\n\n{link}" if link else llm_text
                        used_llm = True
                        print(f"      Generated via LLM")
                    else:
                        raise ValueError("LLM returned empty/too short")
                except Exception as e:
                    print(f"      ⚠️ LLM error: {str(e)[:80]}, fallback to template")
            if not used_llm:
                reply_text = fill_template(style_seed["template"], style_seed["fillers"], link=link)
                print(f"      Style: {style_seed['id']} (used {style_seed['used_count']}x)")
            if len(reply_text) > 280:
                reply_text = reply_text[:277] + "..."

            print(f"   ⚡ AUTO-MATCH: {c['username']} → {category_id}")
            if used_llm:
                print(f"      Generated via LLM")
            else:
                print(f"      Style: {style_seed['id']} (used {style_seed['used_count']}x)")
            print(f"      Reply: {reply_text[:100]}...")

            # Update style seed usage (only for template)
            if not used_llm:
                style_seed["used_count"] = style_seed.get("used_count", 0) + 1
                style_seed["last_used"] = datetime.now(timezone.utc).isoformat()
            # Update link performance counter (track link rotation)
            for _l in cat_cfg.get("links", []):
                if _l.get("url") == link:
                    _perf = _l.setdefault("performance", {"posted": 0, "engagement": 0})
                    _perf["posted"] = _perf.get("posted", 0) + 1
                    _l["last_used"] = datetime.now(timezone.utc).isoformat()
                    break

            # Auto-post via subprocess (reuse auto_post.py logic)
            success = call_auto_post_subprocess(
                account_name, c["tweet_id"],
                f"https://x.com/{c['username']}/status/{c['tweet_id']}",
                reply_text, link, category_id
            )

            if success:
                auto_posted.append({
                    "tweet_id": c["tweet_id"],
                    "username": c["username"],
                    "category": category_id,
                    "link": link,
                    "reply": reply_text,
                    "tweet_url": f"https://x.com/{c['username']}/status/{c['tweet_id']}",
                    "tweet_text": c["text"],
                })
                daily_total += 1
                cycle_count += 1
            else:
                auto_skipped.append((c["tweet_id"], "post_failed"))
            # Intra-cycle cooldown: 30s spacing antar auto-post
            if success and cycle_count > 0:
                import time as _t
                _t.sleep(30)

        # Save updated knowledge (with style seed counters)
        if auto_posted or auto_skipped:
            save_knowledge(paths["account_dir"], knowledge)

    if auto_posted:
        print(f"\n⚡ Auto-posted: {len(auto_posted)}")
        for ap in auto_posted:
            print(f"   ✅ @{ap['username']} via {ap['category']}")
            if telegram_notify:
                send_auto_post_notification(persona, ap)

    if auto_skipped:
        print(f"\n⏭️ Auto-skipped: {len(auto_skipped)}")
        for tid, reason in auto_skipped:
            print(f"   • {tid}: {reason}")

    # ====== Save remaining (no match) to pending for manual approve ======
    auto_posted_ids = {ap["tweet_id"] for ap in auto_posted}
    new_pending = []
    llm_client = _get_llm_client()
    drafts_data = load_drafts(paths)
    for c in unique_candidates[:5]:  # max 5 per cycle
        if c["tweet_id"] in auto_posted_ids:
            continue  # already auto-posted
        draft = generate_draft_reply(c, persona, products_data, drafts_data=drafts_data, llm_client=llm_client)
        pending["overrides"][c["tweet_id"]] = {
            "tweet_id": c["tweet_id"],
            "tweet_url": f"https://x.com/{c['username']}/status/{c['tweet_id']}",
            "tweet_text": c["text"],
            "tweet_author": c["username"],
            "draft_reply": draft,
            "status": "awaiting",
            "notified_at": datetime.now(timezone.utc).isoformat(),
            "engagement": {
                "likes": c.get("like_count", 0),
                "replies": c.get("reply_count", 0),
                "retweets": c.get("retweet_count", 0),
            },
        }
        new_pending.append(c["tweet_id"])

    if new_pending:
        save_json(paths["pending"], pending)
        print(f"✅ Saved {len(new_pending)} to pending (manual approve)")
        if telegram_notify:
            send_telegram_summary(persona, new_pending, pending["overrides"])

    return True


def load_knowledge(account_dir):
    """Load link_knowledge.json. Returns None if not found."""
    import random
    global _RANDOM
    if '_RANDOM' not in globals():
        _RANDOM = random.Random()
    p = Path(account_dir) / "link_knowledge.json"
    if not p.exists():
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to load link_knowledge.json: {e}")
        return None


def save_knowledge(account_dir, knowledge):
    """Save link_knowledge.json with updated counters."""
    p = Path(account_dir) / "link_knowledge.json"
    knowledge["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(p, "w") as f:
        json.dump(knowledge, f, indent=2, ensure_ascii=False)


def match_category(candidate, knowledge):
    """Match a tweet candidate to a known category.
    Returns (category_id, link, style_seed) or None.

    Uses word-boundary matching for triggers to avoid substring false-positives
    (e.g. "bra" in "vibrator").
    """
    import re as _re_word
    text = candidate.get("text", "").lower()
    categories = knowledge.get("categories", {})

    best_match = None
    best_score = 0

    for cat_id, cat in categories.items():
        if not cat.get("auto_mode", False):
            continue
        if not cat.get("style_seeds"):
            continue
        if not cat.get("links"):
            continue

        # Check exclude triggers (word-boundary)
        exclude = [t.lower() for t in cat.get("exclude_triggers", [])]
        excluded = False
        for ex in exclude:
            if _re_word.search(r'\b' + _re_word.escape(ex) + r'\b', text):
                excluded = True
                break
        if excluded:
            continue

        # Check triggers (word-boundary to avoid "bra" in "vibrator")
        triggers = [t.lower() for t in cat.get("triggers", [])]
        trigger_hits = 0
        for t in triggers:
            if _re_word.search(r'\b' + _re_word.escape(t) + r'\b', text):
                trigger_hits += 1

        # Check context signals (substring OK — they're adjectives)
        signals = [s.lower() for s in cat.get("context_signals", [])]
        signal_hits = sum(1 for s in signals if s in text)

        # Score: trigger * 2 + signal
        score = trigger_hits * 2 + signal_hits
        if trigger_hits == 0:
            continue  # must match at least 1 trigger

        # Priority: tie-break — higher priority wins (specific > generic)
        priority = cat.get("priority", 1)
        if score > best_score:
            best_score = score
            best_match = (cat_id, cat, score, priority)
        elif score == best_score and best_match and priority > best_match[3]:
            best_match = (cat_id, cat, score, priority)

    if not best_match:
        return None

    # Build blocked URLs set (supports both str and dict entries)
    blocked_raw = knowledge.get("blocked_urls", []) or []
    blocked_set = set()
    for b in blocked_raw:
        if isinstance(b, dict):
            blocked_set.add(b.get("url", ""))
        else:
            blocked_set.add(b)

    cat_id, cat, _, _ = best_match
    # Pick link — round-robin by least-used (performance.posted) with random
    # tie-break. Kalau cuma 1 link, langsung pake (no choice).
    import random as _rnd
    blocked_raw = knowledge.get("blocked_urls", []) or []
    blocked_set = set()
    for b in blocked_raw:
        if isinstance(b, dict):
            blocked_set.add(b.get("url", ""))
        else:
            blocked_set.add(b)
    # Filter non-blocked + valid
    candidates = [l for l in cat["links"] if l.get("url") and l["url"] not in blocked_set]
    if not candidates:
        return None  # all links in this category blocked — skip
    if len(candidates) == 1:
        link_url = candidates[0]["url"]
    else:
        # Sort by (posted ASC, last_used ASC) — least-used & oldest wins.
        # Random tie-break di antara yang posted-nya sama, biar gak predictable.
        def sort_key(l):
            perf = l.get("performance") or {}
            return (
                perf.get("posted", 0),
                l.get("last_used") or l.get("added_at") or "",
            )
        candidates_sorted = sorted(candidates, key=sort_key)
        min_posted = sort_key(candidates_sorted[0])[0]
        # All yang punya posted == min → random pick di antara mereka
        top_tier = [l for l in candidates_sorted if sort_key(l)[0] == min_posted]
        link_url = _rnd.choice(top_tier)["url"]

    # Pick style seed (round-robin by least-used)
    seeds = cat["style_seeds"]
    seed = min(seeds, key=lambda s: (s.get("used_count", 0), s.get("last_used") or ""))

    return (cat_id, link_url, seed)


def fill_template(template, fillers, link):
    """Fill template placeholders with random fillers. {link} is replaced explicitly."""
    import random
    result = template
    # Replace {link} first
    result = result.replace("{link}", link)
    # Replace other [PLACEHOLDER] tokens
    import re as _re
    placeholders = _re.findall(r'\[([A-Z_0-9]+)\]', result)
    for ph in placeholders:
        if ph in fillers:
            result = result.replace(f"[{ph}]", random.choice(fillers[ph]))
    return result


def call_auto_post_subprocess(account_name, tweet_id, tweet_url, reply_text, link, category_id):
    """Call auto_post.py --auto via subprocess for auto-mode posting."""
    import subprocess as _sp
    cmd = [
        str(Path(AUTOMATION_DIR) / "venv" / "bin" / "python3"),
        str(AUTOMATION_DIR / "scripts" / "auto_post.py"),
        "--account", account_name,
        "--auto",  # new flag for direct post
        "--tweet-id", tweet_id,
        "--tweet-url", tweet_url,
        "--text", reply_text,
        "--link", link,
        "--category", category_id,
    ]
    try:
        result = _sp.run(cmd, capture_output=True, text=True, timeout=60)
        # Success: either toast says "sent" or thread verification passed
        success = "✅ Success toast" in result.stdout or "Verified at attempt" in result.stdout
        if not success:
            # Pull last useful line from stdout for diagnostic
            lines = [l for l in result.stdout.splitlines() if l.strip()]
            last_useful = lines[-1] if lines else "(empty stdout)"
            print(f"   ⚠️ Auto-post failed: {last_useful[:200]}")
        return success
    except _sp.TimeoutExpired:
        print(f"   ⚠️ Auto-post timed out (60s)")
        return False
    except Exception as e:
        print(f"   ⚠️ Subprocess error: {e}")
        return False


def send_auto_post_notification(persona, ap):
    """Send Telegram notification for auto-posted reply.

    Cron env doesn't have `hermes_tools` module — use `send_to_role.py` subprocess
    (same pattern as multi_account_scan.py). Routes to 'setting' topic (id=56)
    which is where technical/automation reports live.
    """
    send_to_role = Path.home() / ".hermes/skills/autonomous-ai-agents/affiliate-agent-ecosystem/scripts/send_to_role.py"
    if not send_to_role.exists():
        print(f"   ⚠️ send_to_role.py not found at {send_to_role}")
        return

    msg = (
        f"⚡ *Auto-posted*\n\n"
        f"👤 @{ap['username']}\n"
        f"📂 Category: `{ap['category']}`\n"
        f"🔗 {ap['tweet_url']}\n\n"
        f"💬 _{ap['reply'][:200]}_"
    )
    try:
        proc = subprocess.run(
            ["/home/anggar221/automation/venv/bin/python3", str(send_to_role), "setting", msg],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            print(f"   📤 Telegram notified: @{ap['username']} (via setting topic)")
        else:
            print(f"   ⚠️ send_to_role failed: {proc.stderr.strip()[:200]}")
    except Exception as e:
        print(f"   ⚠️ Telegram notify failed: {e}")


def generate_draft_reply(candidate, persona, products_data, drafts_data=None, llm_client=None):
    """Generate a draft reply.

    Priority:
      1. LLM-powered (if OPENROUTER_API_KEY set) — uses persona voice
      2. Template-based (from drafts.json) — matches category by keyword
      3. Generic fallback (old behavior)

    The reply is generated WITHOUT the link embedded. The link is stored
    separately in `pending.overrides[tweet_id].link` and appended by
    `auto_post.py` (or kept in draft text if manually edited).
    """
    text = (candidate.get("text") or "").lower()
    drafts_data = drafts_data or {"templates": {}, "no_link": {}, "matching": {}}
    matching = drafts_data.get("matching", {})
    templates = drafts_data.get("templates", {})
    no_link = drafts_data.get("no_link", {})

    # 1) LLM path
    if llm_client:
        try:
            return _llm_generate_reply(candidate, persona, products_data, llm_client)
        except Exception as e:
            print(f"   ⚠️ LLM draft error, falling back to template: {e}", "WARN")

    # 2) Template path
    category = _match_category(text, matching)
    pool = templates.get(category) or templates.get("default") or []
    if pool:
        import random
        return random.choice(pool)

    # 3) Generic fallback
    return f"setuju banget, relate. nice share 👀"


def _match_category(text, matching):
    """Match tweet text to a template category via WORD-BOUNDARY matching.

    Naive substring matching causes false positives (e.g. "vibrator" contains
    "bra" as substring). Use word boundaries to avoid that.
    """
    import re
    for category, keywords in matching.items():
        for kw in keywords:
            # \b requires non-word chars at edges — won't match "bra" inside "vibrator"
            pattern = r"(?:^|\b)" + re.escape(kw.lower()) + r"(?:$|\b)"
            if re.search(pattern, text):
                return category
    return "default"


def _llm_generate_reply(candidate, persona, products_data, llm_client):
    """Call OPENROUTER (or compatible) to generate a persona-voice reply."""
    persona_dict = persona if isinstance(persona, dict) else {}
    voice = persona_dict.get("persona", {})
    niche = persona_dict.get("niche", {})

    system_prompt = (
        f"BAHASA INDONESIA WAJIB. Bales pake bahasa Indonesia, JANGAN Inggris. "
        f"JANGAN template generic kayak 'yoi relate'/'nice share'/'setuju banget'. "
        f"Kalo generic, delete dan tulis ulang. "
        f"Persona: {voice.get('lifestyle', 'anak muda urban')}, "
        f"{voice.get('age', '20-an')}, {voice.get('gender', 'female')}. "
        f"Tone: {voice.get('tone', 'casual')}. "
        f"Gaya: {', '.join(voice.get('voice_rules', ['NO copywriter']))}. "
        f"Output HANYA teks reply (1-3 kalimat), tanpa hashtag, tanpa emoji berlebihan, "
        f"dalam bahasa Indonesia casual. "
        f"Reply harus relate sama tweet original, no generic. "
    )

    # Add product context if available (used by AFFILIATE accounts)
    products_list = []
    if isinstance(products_data, list):
        for p in products_data:
            if isinstance(p, dict):
                name = p.get("name") or p.get("brand") or p.get("product_name") or ""
                if name and name not in [x.split(":")[0].strip() for x in products_list]:
                    products_list.append(f"{name}: {p.get('description', '')[:80]}")
    elif isinstance(products_data, dict):
        for key, val in products_data.items():
            if isinstance(val, dict):
                name = val.get("name") or val.get("brand") or key
                products_list.append(f"{name}: {val.get('description', '')[:80]}")
    if products_list:
        system_prompt += (
            f"\n\nProduk yang relevan untuk diselipin kalo natural:\n"
            + "\n".join(f"- {p}" for p in products_list[:5])
        )
        system_prompt += (
            "\n\nJANGAN paksa masukin produk. PENTING: produk cuma diselipin KALO "
            "tweet original cocok. Kalo gak cocok, reply natural aja tanpa produk. "
            "Link gak perlu disebut explicit — cukup hint natural kayak 'ada rekomendasi yang worth dicoba'."
        )

    user_prompt = (
        f"Tweet original (from @{candidate.get('username', '?')}):\n"
        f"\"{candidate.get('text', '')}\"\n\n"
        f"Tulis reply yang natural, max 200 karakter."
    )

    model = os.getenv("LLM_MODEL", os.getenv("MODEL", "deepseek/deepseek-chat"))
    try:
        response = llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,  # reasoning model needs extra tokens for CoT
            temperature=0.85,
        )
        text = (response.choices[0].message.content or "").strip()
        # Reasoning model (deepseek-v4-flash) leaks CoT in content or uses
        # reasoning_content — strip <think> blocks and fallback to content
        import re as _re
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        # Remove surrounding quotes if LLM added them
        text = text.strip('"').strip("'")
        return text
    except Exception as e:
        print(f"   ⚠️ LLM generate error: {e}", "WARN")
        return ""


def send_telegram_summary(persona, new_pending_ids, overrides):
    """Send Telegram notification about new pending items."""
    handle = persona["account"]["handle"]
    lines = [f"📥 *{len(new_pending_ids)} kandidat baru* — @{handle}"]
    for tid in new_pending_ids:
        entry = overrides[tid]
        lines.append(f"\n👤 *@{entry['tweet_author']}*")
        lines.append(f"💬 _{entry['tweet_text'][:150]}..._")
        lines.append(f"🔗 {entry['tweet_url']}")
        lines.append(f"\n💭 _{entry['draft_reply'][:150]}..._")
    lines.append("\nReply chat:")
    lines.append("  • `ok` → post as-is")
    lines.append("  • `https://...` → inject link")
    lines.append("  • `skip` → skip tweet ini")

    try:
        subprocess.run(
            ["hermes", "send", "--to", "telegram", "\n".join(lines), "--quiet"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        print(f"⚠️ Telegram notif error: {e}")


# ============== CLI ==============
def main():
    parser = argparse.ArgumentParser(description="Generic X auto-reply (multi-account)")
    parser.add_argument("--account", required=True, help="Account folder name (e.g. akun1_nunani)")
    parser.add_argument("--dry-run", action="store_true", help="Generate draft only, don't post")
    parser.add_argument(
        "--from-cache",
        metavar="PATH",
        help="Load candidates from viral_research cache file instead of live X.com scan. "
             "Path relative to AUTOMATION_DIR or absolute. "
             "Used by grouped research orchestrator (viral_research_grouped.py).",
    )
    args = parser.parse_args()

    success = asyncio.run(run_bot(
        args.account,
        dry_run=args.dry_run,
        from_cache=args.from_cache,
    ))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
