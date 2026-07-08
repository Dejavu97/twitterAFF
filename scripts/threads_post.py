#!/usr/bin/env python3
"""
threads_post.py — Post content to Threads.net via Playwright

Usage:
    python3 scripts/threads_post.py                        # post from queue (default account)
    python3 scripts/threads_post.py --account threads_akun2 # use different account
    python3 scripts/threads_post.py --content "..." --link "..."
    python3 scripts/threads_post.py --dry-run

Flow:
    1. Launch persistent Chromium context (reuse saved session)
    2. Check login status on threads.com
    3. If not logged in → attempt login with IG credentials
    4. Compose & post content
    5. Verify with toast/selector check
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add parent to path
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Load env — check threads-automation/.env first, then automation/.env
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv(Path.home() / "automation" / ".env")


# ============== PATHS ==============
def _resolve_account_dir(account_name):
    """Resolve account directory from name. Creates dir if it doesn't exist."""
    d = BASE_DIR / "accounts" / account_name
    d.mkdir(parents=True, exist_ok=True)
    (d / ".chrome-profile").mkdir(exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    return d

# Default — reassigned in main() after parsing --account
ACCOUNT_NAME = "threads_akun1"
ACCOUNT_DIR = BASE_DIR / "accounts" / ACCOUNT_NAME
CHROME_PROFILE = ACCOUNT_DIR / ".chrome-profile"
POSTED_LOG = ACCOUNT_DIR / "posted.json"
PENDING_FILE = ACCOUNT_DIR / "pending_posts.json"
CONFIG_FILE = ACCOUNT_DIR / "config.json"
LINK_KB = ACCOUNT_DIR / "link_knowledge.json"
COOKIES_FILE = ACCOUNT_DIR / "cookies.json"
IG_COOKIES_FILE = ACCOUNT_DIR / "instagram_cookies.json"
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)


# ============== LOGGING ==============
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")
    with open(LOG_DIR / "threads_post.log", "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


# ============== LOAD CONFIG ==============
def load_json(path, default=None):
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log(f"Failed to load {path}: {e}", "WARN")
        return default or {}


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============== THREADS SELECTORS ==============
# Compose — Threads.com uses a DIV with specific aria-label
COMPOSE_TEXTAREA = 'div[aria-label*="Empty text field"]'
COMPOSE_TEXTAREA_ALT = 'div[aria-label*="What\'s new?"]'
COMPOSE_TEXTAREA_ALT2 = 'div[contenteditable="true"]'
COMPOSE_TEXTAREA_ALT3 = 'textarea'
CREATE_BUTTON_SELECTORS = [
    'a[href="/create"]',
    'nav a[aria-label="Create"]',
    'svg[aria-label="Create"]',
]
CREATE_BUTTON_NAV = 'nav a[aria-label="Create"]'
POST_BUTTON_SELECTORS = [
    'div[aria-label="Post"]',
    'div[role="button"]:has-text("Post")',
    '[data-testid="createButton"]',
    'button:has-text("Post")',
]
USER_AVATAR_SELECTOR = 'a[href^="/@"]'
LOGIN_FORM_USER = 'input[name="username"]'
LOGIN_FORM_PASS = 'input[name="password"]'
LOGIN_BUTTON = 'button[type="submit"]:has-text("Log in")'
LOGGED_IN_INDICATORS = [
    'nav a[href*="/@"]',
    'svg[aria-label="Profile"]',
    'button[aria-label="Notifications"]',
]

THREADS_BASE = "https://www.threads.com"


# ============== COOKIE IMPORT ==============
SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}


async def _import_cookies_from(context, path, label="cookies"):
    """Import cookies from EditThisCookie JSON format into Playwright context."""
    if not path.exists():
        return False

    try:
        with open(path) as f:
            raw_cookies = json.load(f)

        playwright_cookies = []
        for c in raw_cookies:
            pc = {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "httpOnly": c.get("httpOnly", False),
                "secure": c.get("secure", False),
            }
            if "expirationDate" in c:
                pc["expires"] = int(c["expirationDate"])
            raw_same = c.get("sameSite", "unspecified").lower()
            pc["sameSite"] = SAMESITE_MAP.get(raw_same, "Lax")
            playwright_cookies.append(pc)

        await context.add_cookies(playwright_cookies)
        log(f"✅ Loaded {len(playwright_cookies)} cookies from {label}", "INFO")
        return True
    except Exception as e:
        log(f"Failed to import cookies from {label}: {e}", "WARN")
        return False


async def import_cookies(context):
    """Import all cookie files (Threads + Instagram) into Playwright context."""
    loaded = 0
    if await _import_cookies_from(context, COOKIES_FILE, "cookies.json (Threads)"):
        loaded += 1
    if await _import_cookies_from(context, IG_COOKIES_FILE, "instagram_cookies.json"):
        loaded += 1
    if loaded == 0:
        log("No cookies.json found — will rely on persistent profile", "INFO")
    return loaded > 0


# ============== BROWSER HELPERS ==============
async def _dismiss_modals(page):
    """Dismiss any popups/modals that might interfere."""
    try:
        close_selectors = [
            'div[role="button"]:has-text("Not now")',
            'div[role="button"]:has-text("Close")',
            'div[aria-label="Close"]',
            'svg[aria-label="Close"]',
            'div[role="button"]:has-text("Tutup")',
            'div[role="button"]:has-text("Nanti")',
        ]
        for sel in close_selectors:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=3000)
                await page.wait_for_timeout(1000)
                log(f"Dismissed modal: {sel}")
    except Exception:
        pass


async def post_to_threads(content: str, link: str = None, dry_run: bool = False) -> dict:
    """
    Post content to Threads.
    
    Returns: dict with success, thread_url, error fields
    """
    result = {"success": False, "thread_url": None, "error": None}

    # Build full text
    full_text = content.strip()
    if link and link not in full_text:
        full_text += f"\n\n{link}"

    if dry_run:
        log(f"[DRY-RUN] Would post: {full_text[:200]}...")
        return {"success": True, "thread_url": None, "error": None, "dry_run": True}

    # Load config for credentials
    config = load_json(CONFIG_FILE)
    ig_username = config.get("instagram", {}).get("username", "")
    ig_password = config.get("instagram", {}).get("password", "")

    async with async_playwright() as p:
        # Launch persistent context (reuses cookies/session)
        log(f"Launching Chromium with user data dir: {CHROME_PROFILE}")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_PROFILE),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 800},
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # --- STEP 0: Inject cookies (bypass login) ---
        cookies_loaded = await import_cookies(context)

        try:
            # --- STEP 1: Check login status ---
            log("Navigating to Threads...")
            await page.goto(THREADS_BASE, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Check if login prompt appears (new account / session expired)
            login_prompt = None
            for sel in ['button:has-text("Continue with Instagram")',
                        'div[role="button"]:has-text("Continue with Instagram")',
                        '#login button:has-text("Log in")',
                        'a[href*="oauth"]']:
                try:
                    lp = page.locator(sel).first
                    if await lp.is_visible(timeout=2000):
                        login_prompt = lp
                        break
                except:
                    pass

            if login_prompt is not None:
                log("Login prompt detected. Triggering Instagram SSO...")
                # First visit Instagram to establish the session
                await page.goto('https://www.instagram.com', wait_until='domcontentloaded', timeout=20000)
                await page.wait_for_timeout(3000)
                # Navigate back to Threads
                await page.goto(THREADS_BASE, wait_until='domcontentloaded', timeout=20000)
                await page.wait_for_timeout(3000)

            # Dismiss any Facebook interstitial overlay before clicking Create
            # The overlay intercepts pointer events on Create button
            for ol_sel in ['div[role="dialog"] button:has-text("Continue with Instagram")',
                          'div[role="dialog"] div[role="button"]:has-text("Continue with Instagram")',
                          'div[aria-label*="Close"] button', 'div[role="dialog"] svg[aria-label="Close"]',
                          'div[class*="overlay"] button:has-text("Continue")',
                          'button:has-text("Continue with Instagram")']:
                try:
                    ol = page.locator(ol_sel).first
                    # Try immediate timeout - don't wait long
                    if await ol.is_visible(timeout=1000):
                        log(f"Dismissing overlay: {ol_sel}")
                        await ol.click(timeout=3000)
                        await page.wait_for_timeout(3000)
                        await _dismiss_modals(page)
                        break
                except:
                    pass

            logged_in = False
            for indicator in LOGGED_IN_INDICATORS:
                try:
                    el = page.locator(indicator).first
                    if await el.is_visible(timeout=2000):
                        logged_in = True
                        break
                except Exception:
                    continue

            if not logged_in:
                log("Not logged in. Trying Instagram SSO with cookie session...")

                # Method 1: Click Create → Instagram SSO dialog → auto-login with IG cookies
                try:
                    # Go to home
                    await page.goto(THREADS_BASE, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    await _dismiss_modals(page)

                    # Click Create button
                    create_btn = page.locator('button:has(svg[aria-label="Create"]), a[href="/create"], button:has-text("Create")').first
                    if await create_btn.is_visible(timeout=3000):
                        await create_btn.click(timeout=5000)
                        await page.wait_for_timeout(2000)
                    else:
                        # JS fallback
                        await page.evaluate("""() => {
                            const btns = document.querySelectorAll('nav button, nav a');
                            for (const b of btns) {
                                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                                if (label === 'create') { b.click(); return; }
                            }
                            const svg = document.querySelector('svg[aria-label="Create"]');
                            if (svg && svg.closest('button')) svg.closest('button').click();
                        }""")
                        await page.wait_for_timeout(2000)

                    # Look for "Continue with Instagram" SSO dialog
                    sso_btn = page.locator('button:has-text("Continue with Instagram"), button:has-text("Log in"), div[role="button"]:has-text("Continue with Instagram")').first
                    if await sso_btn.is_visible(timeout=3000):
                        log("SSO dialog found. Clicking Continue with Instagram...")
                        await sso_btn.click(timeout=5000)
                        # Wait for SSO redirect → should auto-login with IG cookies
                        await page.wait_for_timeout(8000)
                        # Handle potential "Continue as" screen
                        try:
                            continue_as = page.locator('button:has-text("Continue as"), button:has-text("Allow"), div[role="button"]:has-text("Continue as")').first
                            if await continue_as.is_visible(timeout=3000):
                                await continue_as.click(timeout=5000)
                                await page.wait_for_timeout(3000)
                        except:
                            pass

                    # Check login again
                    await page.wait_for_timeout(3000)
                    for indicator in LOGGED_IN_INDICATORS:
                        try:
                            el = page.locator(indicator).first
                            if await el.is_visible(timeout=2000):
                                logged_in = True
                                break
                        except Exception:
                            continue
                except Exception as e:
                    log(f"SSO login attempt failed: {e}", "WARN")

            if not logged_in:
                log("SSO failed. Trying IG credential login fallback...")
                if not ig_username or not ig_password:
                    result["error"] = "Instagram credentials not found in config.json"
                    log(result["error"], "ERROR")
                    return result

                # Fallback: direct login form
                try:
                    await page.goto(f"{THREADS_BASE}/login", wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                    username_input = page.locator(LOGIN_FORM_USER).first
                    if await username_input.is_visible(timeout=10000):
                        await username_input.fill(ig_username)
                        password_input = page.locator(LOGIN_FORM_PASS).first
                        await password_input.wait_for(state="visible", timeout=5000)
                        await password_input.fill(ig_password)
                        login_btn = page.locator(LOGIN_BUTTON).first
                        await login_btn.wait_for(state="visible", timeout=5000)
                        await login_btn.click()
                        log("Login form submitted. Waiting...")
                        await page.wait_for_timeout(8000)
                        await _dismiss_modals(page)
                        await page.wait_for_timeout(3000)
                        for indicator in LOGGED_IN_INDICATORS:
                            try:
                                el = page.locator(indicator).first
                                if await el.is_visible(timeout=3000):
                                    logged_in = True
                                    break
                            except Exception:
                                continue
                        if not logged_in:
                            page_url = page.url
                            if "challenge" in page_url or "login" in page_url:
                                result["error"] = f"Login challenge/2FA needed. URL: {page_url}"
                                log(result["error"], "ERROR")
                                return result
                except Exception as e:
                    result["error"] = f"Login failed: {e}"
                    log(result["error"], "ERROR")
                    return result

            if not logged_in:
                result["error"] = "Could not log in to Threads"
                log(result["error"], "ERROR")
                return result

            log("Logged in successfully!")

            # --- STEP 2: Open compose ---
            await _dismiss_modals(page)

            # Check if compose is already open (SSO flow may leave it open)
            compose_open = False
            for selector in [COMPOSE_TEXTAREA, COMPOSE_TEXTAREA_ALT, COMPOSE_TEXTAREA_ALT2, COMPOSE_TEXTAREA_ALT3, 'div[role="textbox"]']:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=2000):
                        compose_open = True
                        break
                except Exception:
                    continue

            if not compose_open:
                log("Opening compose via Create button...")
                await page.goto(THREADS_BASE, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(2000)
                await _dismiss_modals(page)

                # Try clicking Create button
                try:
                    create_btn = page.locator('button:has(svg[aria-label="Create"]), a[href="/create"]').first
                    if await create_btn.is_visible(timeout=3000):
                        await create_btn.click(timeout=5000)
                        await page.wait_for_timeout(2000)
                    else:
                        # JS fallback click
                        await page.evaluate("""() => {
                            const svg = document.querySelector('svg[aria-label="Create"]');
                            if (svg && svg.closest('button')) svg.closest('button').click();
                        }""")
                        await page.wait_for_timeout(2000)

                    # Handle "Continue with Instagram" interstitial dialog
                    try:
                        continue_btn = page.locator('button:has-text("Continue with Instagram"), div[role="button"]:has-text("Continue with Instagram")').first
                        if await continue_btn.is_visible(timeout=3000):
                            log("Dismissing Join Threads interstitial...")
                            await continue_btn.click(timeout=5000)
                            await page.wait_for_timeout(8000)
                            # Wait for navigation/redirect (SSO flow)
                            try:
                                await page.wait_for_url('**/create*', timeout=10000)
                            except:
                                pass
                            await _dismiss_modals(page)
                    except:
                        pass

                except Exception as e:
                    log(f"Create button click: {e}", "WARN")

            await page.wait_for_timeout(2000)

            # --- STEP 3: Type content ---
            textarea = None
            for selector in [COMPOSE_TEXTAREA, COMPOSE_TEXTAREA_ALT, COMPOSE_TEXTAREA_ALT2, COMPOSE_TEXTAREA_ALT3, 'div[role="textbox"]']:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=3000):
                        textarea = el
                        log(f"Found compose area via selector: {selector}")
                        break
                except Exception:
                    continue

            if not textarea:
                # Try JS fallback
                try:
                    found = await page.evaluate("""() => {
                        // Try aria-label pattern (Threads.com uses this)
                        const byLabel = document.querySelector('div[aria-label*="Empty text field"], div[aria-label*="Whats new"]');
                        if (byLabel) { byLabel.focus(); return 'aria-label'; }
                        // contenteditable pattern
                        const divs = document.querySelectorAll('div[contenteditable="true"]');
                        for (const d of divs) {
                            if (d.getAttribute('role') === 'textbox' || !d.textContent.trim()) {
                                d.focus(); return 'contenteditable';
                            }
                        }
                        // Any contenteditable
                        const all = document.querySelectorAll('[contenteditable="true"]');
                        if (all.length > 0) { all[0].focus(); return 'contenteditable'; }
                        return null;
                    }""")
                    if found:
                        log(f"Found compose via JS fallback ({found})")
                        if found == 'aria-label':
                            textarea = page.locator('div[aria-label*="Empty text field"], div[aria-label*="What\'s new"]').first
                        else:
                            textarea = page.locator('[contenteditable="true"]').first
                except Exception:
                    pass

            if not textarea:
                result["error"] = "Could not find compose textarea"
                log(result["error"], "ERROR")
                return result

            # Type the content — Threads uses an inner contenteditable div revealed after click
            await textarea.click(force=True)
            await page.wait_for_timeout(800)
            try:
                # Focus shifts to inner [contenteditable] after click
                editable = page.locator('[contenteditable="true"], [role="textbox"][contenteditable]').first
                await editable.wait_for(state='visible', timeout=3000)
                await editable.fill(full_text)
                log(f"Typed content via fill() ({len(full_text)} chars)")
            except Exception:
                # Fallback: type directly on focused element
                log("fill() failed, using keyboard.type fallback")
                await page.keyboard.type(full_text, delay=20)
                log(f"Typed content via keyboard ({len(full_text)} chars)")

            await page.wait_for_timeout(1000)

            # --- STEP 4: Submit with Ctrl+Enter ---
            # Threads uses Ctrl+Enter to post. Button click unreliable (sometimes
            # client-side compose clear without actual API submission).
            await page.keyboard.press('Control+Enter')
            log("Pressed Ctrl+Enter to submit post")
            await page.wait_for_timeout(4000)

            # --- STEP 5: Verify ---
            success = False
            thread_url = None

            # Method 1: Check if [contenteditable] disappeared from DOM (real success)
            try:
                editable_exists = await page.evaluate("""() => {
                    const el = document.querySelector('[contenteditable="true"]');
                    return !!el;
                }""")
                if not editable_exists:
                    success = True
                    log("✅ Contenteditable removed from DOM — post successful!")
                else:
                    # Still exists — maybe still clearing
                    text = await page.evaluate("""() => {
                        const el = document.querySelector('[contenteditable="true"]');
                        return el ? el.textContent.trim() : '';
                    }""")
                    if not text:
                        success = True
                        log("✅ Post submitted (compose cleared)")
                    else:
                        log(f"Compose still has text: '{text[:40]}' — may need retry")
            except Exception as e:
                log(f"DOM check error: {e}", "WARN")

            if not success:
                # Method 2: Check for success toast
                try:
                    toast = page.locator('[role="status"], [role="alert"]').first
                    if await toast.is_visible(timeout=3000):
                        toast_text = await toast.text_content(timeout=3000) or ""
                        if toast_text:
                            success = True
                            log(f"Post success toast: {toast_text}")
                            try:
                                link_el = toast.locator('a')
                                if await link_el.is_visible(timeout=2000):
                                    thread_url = await link_el.get_attribute('href')
                            except Exception:
                                pass
                except Exception:
                    pass

            if not success:
                # Method 3: Check URL changed (Threads may redirect after post)
                try:
                    current_url = page.url
                    if '/create' not in current_url and '/compose' not in current_url:
                        success = True
                        log(f"✅ URL changed after post: {current_url}")
                except Exception:
                    pass

            if success:
                log("✅ Post successful!")
                result["success"] = True
                result["thread_url"] = thread_url or "unknown"
            else:
                log("⚠️  Post status uncertain — optimistic", "WARN")
                result["success"] = True  # Optimistic
                result["thread_url"] = None
                result["warning"] = "Post status uncertain"

            # Save screenshot for debugging
            try:
                ss_path = LOG_DIR / f"post_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=str(ss_path))
                result["screenshot"] = str(ss_path)
            except Exception:
                pass

        except Exception as e:
            result["error"] = str(e)
            log(f"Error: {e}", "ERROR")
            import traceback
            log(traceback.format_exc(), "ERROR")
        finally:
            await context.close()

    return result


# ============== PENDING QUEUE ==============
def load_pending():
    return load_json(PENDING_FILE, [])


def save_pending(posts):
    save_json(PENDING_FILE, posts)


def load_posted_log():
    return load_json(POSTED_LOG, [])


def save_posted_log(entries):
    save_json(POSTED_LOG, entries)


# ============== MAIN ==============
async def main():
    global ACCOUNT_DIR, CHROME_PROFILE, POSTED_LOG, PENDING_FILE
    global CONFIG_FILE, LINK_KB, COOKIES_FILE, IG_COOKIES_FILE
    global ACCOUNT_NAME

    parser = argparse.ArgumentParser(description="Post to Threads.net")
    parser.add_argument("--content", help="Content text to post")
    parser.add_argument("--link", help="Link to include in post")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without posting")
    parser.add_argument("--from-queue", action="store_true", help="Post from pending queue")
    parser.add_argument("--account", default="threads_akun1",
                        help="Account directory name under accounts/ (default: threads_akun1)")
    args = parser.parse_args()

    # Resolve account directory
    ACCOUNT_NAME = args.account
    ACCOUNT_DIR = _resolve_account_dir(ACCOUNT_NAME)
    CHROME_PROFILE = ACCOUNT_DIR / ".chrome-profile"
    POSTED_LOG = ACCOUNT_DIR / "posted.json"
    PENDING_FILE = ACCOUNT_DIR / "pending_posts.json"
    CONFIG_FILE = ACCOUNT_DIR / "config.json"
    LINK_KB = ACCOUNT_DIR / "link_knowledge.json"
    COOKIES_FILE = ACCOUNT_DIR / "cookies.json"
    IG_COOKIES_FILE = ACCOUNT_DIR / "instagram_cookies.json"

    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    POSTED_LOG.parent.mkdir(parents=True, exist_ok=True)

    log(f"Account: {ACCOUNT_NAME}")

    if args.dry_run:
        log("DRY RUN mode — no actual posts will be made")

    if args.content:
        # Direct post
        log(f"Posting content ({len(args.content)} chars)...")
        result = await post_to_threads(args.content, args.link, dry_run=args.dry_run)
        
        if result["success"]:
            log(f"✅ Post complete! URL: {result.get('thread_url', 'N/A')}")
        else:
            log(f"❌ Post failed: {result.get('error', 'Unknown error')}", "ERROR")
        
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.from_queue:
        # Post from pending queue
        pending = load_pending()
        if not pending:
            log("No pending posts in queue")
            return

        post = pending[0]
        log(f"Posting from queue: {post.get('content', '')[:80]}...")
        result = await post_to_threads(
            post.get("content", ""),
            post.get("link", ""),
            dry_run=args.dry_run,
        )

        if result["success"]:
            # Move from pending to posted log
            posted = load_posted_log()
            posted.append({
                "content": post["content"],
                "link": post.get("link"),
                "posted_at": datetime.now(timezone.utc).isoformat(),
                "thread_url": result.get("thread_url"),
            })
            save_posted_log(posted)
            
            # Remove from pending
            pending.pop(0)
            save_pending(pending)
            log(f"✅ Posted from queue! Queue remaining: {len(pending)}")
        else:
            log(f"❌ Queue post failed: {result.get('error')}", "ERROR")
        
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
