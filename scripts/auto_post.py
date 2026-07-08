#!/usr/bin/env python3
"""
auto_post.py — Post approved pending reply (multi-account aware)

Usage:
    python3 scripts/auto_post.py --account akun1_nunani --tweet-id 1234567890

This script:
1. Loads the approved entry from pending_replies.json
2. Connects to Chrome via CDP using the account's chrome profile
3. Posts the reply with verification (checks for errors, verifies reply in thread)
4. Updates state in pending_replies.json + replied.json

Approval flow:
    1. auto_reply.py generates draft, saves to pending (status: awaiting)
    2. User replies with 'ok' / 'https://link' / 'skip' in Telegram
    3. Use approve_pending.py to mark status: approved
    4. Then call this auto_post.py to actually post
"""
import argparse
import asyncio
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

AUTOMATION_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(AUTOMATION_DIR))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(AUTOMATION_DIR / ".env")

# Local utils (timezone-aware date helpers)
sys.path.insert(0, str(Path(__file__).parent))
from utils import now_wib, today_wib, iso_now_utc


def get_account_paths(account_name):
    account_dir = AUTOMATION_DIR / "accounts" / account_name
    return {
        "account_dir": account_dir,
        "persona": account_dir / "persona.json",
        "pending": account_dir / "pending_replies.json",
        "replied": account_dir / "replied.json",
        "daily_count": account_dir / "daily_count.json",
        "errors": account_dir / "errors.json",
    }


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def post_one(account_name, tweet_id, dry_run=False):
    paths = get_account_paths(account_name)
    persona = load_json(paths["persona"], {})

    # Check account status
    status = persona.get("account", {}).get("status", "ACTIVE")
    if status in ("PAUSED", "DRAFT"):
        print(f"⏸️ Account '{account_name}' is {status} — abort.")
        return False

    # Load pending entry
    pending = load_json(paths["pending"], {"overrides": {}})
    entry = pending["overrides"].get(tweet_id)
    if not entry:
        print(f"❌ Tweet {tweet_id} not in pending")
        return False
    if entry.get("status") not in ("approved",):
        print(f"⚠️ Status is '{entry.get('status')}', expected 'approved'")
        return False

    # Build final reply
    draft = entry["draft_reply"]
    link = entry.get("link")
    if link:
        # Use .format() agar {link} placeholder di draft ke-substitute
        if "{link}" in draft:
            final_reply = draft.format(link=link)
        else:
            # Bridge natural antara draft + link: pake random bridge biar gak monoton
            CTA_BRIDGES = [
                "ada di sini ya",
                "linknya",
                "",
            ]
            bridge = random.choice(CTA_BRIDGES)
            if bridge:
                final_reply = f"{draft} {bridge} {link}"
            else:
                final_reply = f"{draft} {link}"
    else:
        final_reply = draft
    if len(final_reply) > 280:
        final_reply = final_reply[:277] + "..."

    print(f"📋 Account: {persona.get('account', {}).get('handle', account_name)}")
    print(f"📋 Target: {entry['tweet_url']}")
    print(f"📝 Reply ({len(final_reply)} chars): {final_reply[:100]}...")

    # Connect to Chrome
    fingerprint = persona.get("fingerprint", {})
    chrome_port = int(fingerprint.get("chrome_port")
                      or __import__('os').getenv("CHROME_REMOTE_PORT", "9223"))
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{chrome_port}")
        except Exception as e:
            print(f"❌ Chrome CDP error: {e}")
            return False

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()

        try:
            # Navigate
            await page.goto(entry["tweet_url"], wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)

            # Find reply box
            reply_box = None
            for sel in ['[data-testid="tweetTextarea_0"]', '[data-testid="tweetTextarea_1"]',
                        'div[role="textbox"]']:
                reply_box = await page.query_selector(sel)
                if reply_box:
                    break
            if not reply_box:
                print("❌ Reply box not found")
                return False
            await reply_box.click()
            await page.wait_for_timeout(800)
            await reply_box.fill(final_reply)
            await page.wait_for_timeout(800)

            # Find send button
            send_btn = None
            for sel in ['[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]',
                        'button[aria-label*="Post"]', 'button[aria-label*="Reply"]']:
                send_btn = await page.query_selector(sel)
                if send_btn:
                    break
            if not send_btn:
                print("❌ Send button not found")
                return False

            if dry_run:
                print("🧪 DRY-RUN: would click send")
                return True

            # Click + verify
            await send_btn.click()
            await page.wait_for_timeout(2000)

            # Check 1: toast (X pakai [data-testid="toast"] untuk SUCCESS & ERROR)
            # SUCCESS toast: "Your post was sent" / "Your reply was sent" / "View"
            # ERROR toast: "Whoops" / "limit reached" / "already said" / "rate"
            toast = await page.query_selector('[data-testid="toast"]')
            if toast:
                toast_txt = (await toast.text_content() or "").lower()
                # Error keywords
                if any(p in toast_txt for p in ["whoops", "already", "limit reached", "limit on", "failed", "try again", "rate limit"]):
                    print(f"❌ X error toast: {toast_txt.strip()[:200]}")
                    return False
                # Success keywords (definitely sent)
                elif "sent" in toast_txt or "view" in toast_txt:
                    print(f"   ✅ Success toast: '{toast_txt.strip()[:80]}'")
                # Unknown — log but continue to thread verification
                else:
                    print(f"⚠️ Unknown toast: '{toast_txt.strip()[:200]}' — will verify via thread")

            # Check 2: explicit error dialog (separate from toast)
            error_modal = await page.query_selector('[data-testid="error"]')
            if error_modal:
                err_txt = (await error_modal.text_content() or "").strip()[:200]
                err_lower = err_txt.lower()
                # Skip if it's actually a success message (X weirdly sometimes uses this)
                if "your post was sent" not in err_lower and "your reply was sent" not in err_lower:
                    print(f"❌ X error dialog: {err_txt}")
                    return False
                else:
                    print(f"   ✅ Error dialog is actually success: '{err_txt[:80]}'")

            # Check 2+3: poll for reply in thread (12s)
            print("🔍 Verifying reply in thread (12s)...")
            short_marker = final_reply[:40]
            verified = False
            for attempt in range(6):
                await page.wait_for_timeout(2000)
                body = (await page.evaluate("() => document.body.innerText") or "").lower()
                if short_marker.lower() in body:
                    verified = True
                    print(f"   ✅ Verified at attempt {attempt+1}")
                    break
            if not verified:
                print(f"❌ Reply not visible in thread after 12s")
                return False

            # Update state
            entry["status"] = "posted"
            entry["posted_at"] = iso_now_utc()
            save_json(paths["pending"], pending)

            replied = load_json(paths["replied"], {})
            replied[tweet_id] = {
                "username": entry["tweet_author"],
                "original_text": entry.get("tweet_text", "")[:200],
                "reply": final_reply,
                "time": iso_now_utc(),
                "mode": "POSTED_VERIFIED",
            }
            save_json(paths["replied"], replied)

            # Increment daily count (WIB date key)
            daily = load_json(paths["daily_count"], {})
            today = today_wib()
            daily[today] = daily.get(today, 0) + 1
            save_json(paths["daily_count"], daily)

            print(f"✅ Posted successfully. Daily count for {today}: {daily[today]}")
            return True

        finally:
            await page.close()
            try:
                await browser.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--tweet-id", required=True)
    parser.add_argument("--dry-run", action="store_true")

    # Auto-mode (no pending entry, called from auto_reply.py for matched categories)
    parser.add_argument("--auto", action="store_true",
                        help="Auto-mode: post directly without pending entry")
    parser.add_argument("--tweet-url", help="Tweet URL (auto mode)")
    parser.add_argument("--text", help="Reply text (auto mode)")
    parser.add_argument("--link", help="Affiliate link (auto mode, for logging)")
    parser.add_argument("--category", help="Category ID (auto mode, for tracking)")
    parser.add_argument("--original-text", help="[Fix 3] Original tweet text (auto mode, for audit)")
    parser.add_argument("--generated-by", choices=["llm", "template", "manual"],
                        help="[Fix 2] How the reply was generated (for quality audit)")

    args = parser.parse_args()

    if args.auto:
        if not all([args.tweet_url, args.text]):
            print("❌ --auto requires --tweet-url and --text")
            sys.exit(1)
        success = asyncio.run(post_auto(
            args.account, args.tweet_id, args.tweet_url,
            args.text, args.link, args.category,
            original_text=args.original_text,
            generated_by=args.generated_by
        ))
    else:
        success = asyncio.run(post_one(args.account, args.tweet_id, dry_run=args.dry_run))
    sys.exit(0 if success else 1)


async def post_auto(account_name, tweet_id, tweet_url, reply_text, link=None, category=None,
                   original_text=None, generated_by=None):
    """Post a reply directly (no pending entry). Used by auto_reply.py auto-mode.

    [Fix 2/3/4] Args added:
        original_text: tweet text aslinya (dari pending entry atau X scrape)
        generated_by: "llm" atau "template" — buat audit kualitas
    """
    paths = get_account_paths(account_name)
    persona = load_json(paths["persona"], {})

    # Check account status
    status = persona.get("account", {}).get("status", "ACTIVE")
    if status in ("PAUSED", "DRAFT"):
        print(f"⏸️ Account '{account_name}' is {status} — abort.")
        return False

    # Check auto_post_enabled flag (conservative mode / soft-activate).
    # Kalau false, save draft ke pending buat manual review, jangan post.
    if not persona.get("limits", {}).get("auto_post_enabled", True):
        print(f"🔒 auto_post_enabled=false for '{account_name}' — save to pending, skip post")
        try:
            pending = load_json(paths["pending"], {"overrides": {}})
            overrides = pending.setdefault("overrides", {})
            entry = overrides.get(tweet_id, {})
            entry.update({
                "tweet_id": tweet_id,
                "tweet_url": tweet_url,
                "draft_reply": reply_text,
                "link": link or entry.get("link", ""),
                "category": category or entry.get("category", ""),
                "status": "awaiting",  # keep awaiting for manual approve
                "auto_post_blocked": True,
                "auto_post_blocked_at": datetime.now(timezone.utc).isoformat(),
                "auto_post_blocked_reason": "auto_post_enabled=false in persona.limits",
            })
            overrides[tweet_id] = entry
            save_json(paths["pending"], pending)
            print(f"   ✅ Saved to pending (status=awaiting, manual review needed)")
        except Exception as e:
            print(f"   ⚠️ Failed to save to pending: {e}")
        return False

    # Username from URL
    username = "unknown"
    try:
        username = tweet_url.split("/status/")[0].split("/")[-1]
    except Exception:
        pass

    # Check if already replied
    replied = load_json(paths["replied"], {})
    if tweet_id in replied:
        # Mark pending status=posted juga, biar cron gak re-process terus
        try:
            pending = load_json(paths["pending"], {"overrides": {}})
            overrides = pending.get("overrides", {})
            if tweet_id in overrides:
                overrides[tweet_id]["status"] = "posted"
                overrides[tweet_id]["posted_at"] = datetime.now(timezone.utc).isoformat()
                overrides[tweet_id]["verified_via"] = "already_in_replied"
                save_json(paths["pending"], pending)
                print(f"⏭️ Tweet {tweet_id} already in replied.json — synced pending → posted")
            else:
                print(f"⏭️ Tweet {tweet_id} already in replied.json — skip")
        except Exception as e:
            print(f"⏭️ Tweet {tweet_id} already in replied.json — skip (pending sync failed: {e})")
        return True  # idempotent: consider it success

    print(f"⚡ [AUTO-MODE] Account: {persona.get('account', {}).get('handle', account_name)}")
    print(f"⚡ [AUTO-MODE] Category: {category}")
    print(f"⚡ [AUTO-MODE] Target: {tweet_url}")
    print(f"⚡ [AUTO-MODE] Reply ({len(reply_text)} chars): {reply_text[:100]}...")

    # === Append link with natural bridge (same logic as pending mode) ===
    if link:
        if "{link}" in reply_text:
            final_reply = reply_text.format(link=link)
        else:
            CTA_BRIDGES = [
                "ada di sini ya",
                "linknya",
                "",
            ]
            bridge = random.choice(CTA_BRIDGES)
            if bridge:
                final_reply = f"{reply_text} {bridge} {link}"
            else:
                final_reply = f"{reply_text} {link}"
    else:
        final_reply = reply_text
    if len(final_reply) > 280:
        final_reply = final_reply[:277] + "..."

    # Connect to Chrome
    fingerprint = persona.get("fingerprint", {})
    chrome_port = int(fingerprint.get("chrome_port")
                      or __import__('os').getenv("CHROME_REMOTE_PORT", "9223"))
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(f"http://localhost:{chrome_port}")
        except Exception as e:
            print(f"❌ Chrome CDP error: {e}")
            return False

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()

        try:
            # Navigate to tweet
            await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Find and click reply button
            try:
                reply_btn = await page.query_selector('[data-testid="reply"]')
                if not reply_btn:
                    print("❌ Reply button not found")
                    return False
                await reply_btn.click()
                await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"❌ Reply click error: {e}")
                return False

            # Type the reply
            try:
                editor = await page.wait_for_selector(
                    '[data-testid="tweetTextarea_0"], [contenteditable="true"]',
                    timeout=10000
                )
                await editor.click()
                await page.wait_for_timeout(500)
                await page.keyboard.type(final_reply, delay=10)
                await page.wait_for_timeout(1000)
            except Exception as e:
                print(f"❌ Type error: {e}")
                return False

            # Click Post / Reply submit button
            try:
                send_btn = await page.query_selector('[data-testid="tweetTextarea_0"]')
                # Actually the post button is usually [data-testid="tweetButtonInline"] or similar
                post_btn = await page.query_selector('[data-testid="tweetButton"]') \
                    or await page.query_selector('[data-testid="tweetButtonInline"]') \
                    or await page.query_selector('button[aria-label*="Reply" i]')
                if not post_btn:
                    # Fallback: look for any button with "Reply" text near the modal
                    buttons = await page.query_selector_all('button')
                    for b in buttons:
                        txt = (await b.text_content() or "").strip().lower()
                        if "reply" in txt and "post" not in txt:
                            post_btn = b
                            break
                if not post_btn:
                    print("❌ Post button not found")
                    return False
                await post_btn.click()
                await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"❌ Post click error: {e}")
                return False

            # === Verify (same logic as post_one) ===
            # Check 1: toast
            toast = await page.query_selector('[data-testid="toast"]')
            toast_success = False
            if toast:
                toast_txt = (await toast.text_content() or "").lower()
                if any(p in toast_txt for p in ["whoops", "already", "limit reached", "limit on", "failed", "try again", "rate limit"]):
                    print(f"❌ X error toast: {toast_txt.strip()[:200]}")
                    return False
                elif "sent" in toast_txt or "view" in toast_txt:
                    print(f"   ✅ Success toast: '{toast_txt.strip()[:80]}'")
                    toast_success = True
                else:
                    print(f"⚠️ Unknown toast: '{toast_txt.strip()[:200]}'")

            # Check 2: poll thread for reply text
            print("🔍 Verifying reply in thread (12s)...")
            short_marker = final_reply[:40]
            verified = False
            for attempt in range(6):
                await page.wait_for_timeout(2000)
                body = (await page.evaluate("() => document.body.innerText") or "").lower()
                if short_marker.lower() in body:
                    verified = True
                    print(f"   ✅ Verified at attempt {attempt+1}")
                    break

            if not verified and not toast_success:
                print(f"❌ Reply not verified in thread after 12s")
                return False

            # === Update state ===
            # [Fix 2/3/4] Save original_text + generated_by buat audit
            replied[tweet_id] = {
                "username": username,
                "original_text": (original_text or "")[:500],  # truncate safety
                "reply": final_reply,
                "time": iso_now_utc(),
                "mode": "AUTO_AUTO_MATCH",
                "link": link,
                "category": category,
                "generated_by": generated_by or "unknown",  # "llm" / "template" / "unknown"
            }
            save_json(paths["replied"], replied)

            # Increment daily count
            daily = load_json(paths["daily_count"], {})
            today = today_wib()
            daily[today] = daily.get(today, 0) + 1
            save_json(paths["daily_count"], daily)

            # Update knowledge base category daily counter
            if category and (paths["account_dir"] / "link_knowledge.json").exists():
                try:
                    kp = paths["account_dir"] / "link_knowledge.json"
                    kd = json.loads(kp.read_text())
                    if category in kd.get("categories", {}):
                        cat = kd["categories"][category]
                        perf_list = cat.get("links", [])
                        if perf_list:
                            perf_list[0].setdefault("performance", {"posted": 0, "engagement": 0})
                            perf_list[0]["performance"]["posted"] += 1
                        kp.write_text(json.dumps(kd, indent=2, ensure_ascii=False))
                except Exception as e:
                    print(f"⚠️ Knowledge base update failed: {e}")

            print(f"✅ Auto-posted. Daily count for {today}: {daily[today]}")
            return True

        finally:
            await page.close()
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
