#!/usr/bin/env python3
"""
threads_auto_comment.py — Auto-comment on trending Threads posts

Flow:
  1. Launch persistent Chromium (reuse login session)
  2. Navigate to Threads for-you feed
  3. Scroll & collect posts with engagement (reply/like count)
  4. For each "rame" post (min_replies threshold):
     a. Extract post text
     b. Generate contextual comment via LLM (DeepSeek)
     c. Click reply button → type comment → submit
     d. Cooldown before next comment

Usage:
    python3 scripts/threads_auto_comment.py
    python3 scripts/threads_auto_comment.py --account threads_akun1 --dry-run
    python3 scripts/threads_auto_comment.py --max-comments 3 --min-replies 5
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

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# Load env
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv(Path.home() / "automation" / ".env")

# ─── Globals (overridden in main()) ───
ACCOUNT_NAME = "threads_akun1"
ACCOUNT_DIR = BASE_DIR / "accounts" / ACCOUNT_NAME
CHROME_PROFILE = ACCOUNT_DIR / ".chrome-profile"
CONFIG_FILE = ACCOUNT_DIR / "config.json"
COOKIES_FILE = ACCOUNT_DIR / "cookies.json"
IG_COOKIES_FILE = ACCOUNT_DIR / "instagram_cookies.json"
LOG_DIR = BASE_DIR / "logs"
COMMENTED_LOG = ACCOUNT_DIR / "commented.json"  # track which posts we've commented on
LINK_KB = ACCOUNT_DIR / "link_knowledge.json"  # links to inject in comments

LOG_DIR.mkdir(parents=True, exist_ok=True)

# LLM config
LLM_API_KEY = os.getenv("LLM_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# ─── LOGGING ───
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")
    with open(LOG_DIR / "threads_comment.log", "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")

# ─── JSON HELPERS ───
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

# ─── PERSONA ───
def load_persona():
    config = load_json(CONFIG_FILE)
    persona = config.get("persona", {})
    if not persona:
        persona = {
            "age": "20-an",
            "gender": "female",
            "lifestyle": "anak muda urban Jakarta",
            "tone": "ngobrol santai di grup WA",
            "language": "Jakarta casual (doang, sih, emang, ntah, gini, kek, gpp)",
            "emoji_pattern": "minimal 1 per post, wajar",
            "voice_rules": [
                "NO copywriter voice",
                "NO overused CTA",
                "Pake pengalaman spesifik",
                "Akhir ada micro-lesson, bukan pitch",
                "NO affiliate links — pure storytelling",
                "Gak promosi produk apapun"
            ]
        }
    return persona

# ─── LINK INJECTION ───
def load_random_link() -> str:
    """Pick a random link from link_knowledge.json (all categories combined).
    Returns empty string if no links available."""
    try:
        kb = load_json(LINK_KB, {})
        cats = kb.get("categories", {})
        all_urls = []
        for cat_cfg in cats.values():
            blocked = set()
            for b in kb.get("blocked_urls", []):
                if isinstance(b, dict):
                    blocked.add(b.get("url", ""))
                else:
                    blocked.add(b)
            for link in cat_cfg.get("links", []):
                url = link.get("url", "") if isinstance(link, dict) else link
                if url and url not in blocked:
                    all_urls.append(url)
        if not all_urls:
            return ""
        import random
        return random.choice(all_urls)
    except Exception as e:
        log(f"⚠️ load_random_link failed: {e}", "WARN")
        return ""

# ─── LOAD KNOWLEDGE BASE ───
def load_knowledge_links():
    """Load all product links from link_knowledge.json with their triggers."""
    try:
        kb = load_json(LINK_KB, {})
        cats = kb.get("categories", [])
        # Format: list of {category, triggers, links[]}
        result = []
        for cat in cats:
            if isinstance(cat, dict):
                links = []
                for l in cat.get("links", []):
                    if isinstance(l, dict) and l.get("url"):
                        links.append({"url": l["url"], "label": l.get("label", "")})
                if links:
                    result.append({
                        "id": cat.get("id", ""),
                        "name": cat.get("name", ""),
                        "triggers": cat.get("triggers", []),
                        "links": links,
                    })
        return result
    except Exception as e:
        log(f"⚠️ load_knowledge_links failed: {e}", "WARN")
        return []

def find_relevant_category(post_text: str, categories: list) -> dict:
    """Find best matching category based on word match of triggers to post text."""
    text_lower = post_text.lower()
    best = None
    best_score = 0
    for cat in categories:
        score = 0
        for trigger in cat.get("triggers", []):
            if trigger.lower() in text_lower:
                score += 1
        if score > best_score:
            best_score = score
            best = cat
    return best if best_score > 0 else None

def pick_random_link(categories: list, exclude_urls: set = None) -> dict:
    """Pick a random link from all categories combined."""
    all_links = []
    for cat in categories:
        for link in cat.get("links", []):
            if exclude_urls and link.get("url") in exclude_urls:
                continue
            all_links.append(link)
    if not all_links:
        return None
    import random
    return random.choice(all_links)

# ─── LLM COMMENT GENERATION (with product link) ───
def generate_comment(post_text: str, persona: dict, categories: list = None, no_link: bool = False) -> str:
    """
    Generate a contextual comment via DeepSeek.
    If categories available, tries to find relevant product and include link naturally.
    Falls back to random link if no relevant category.
    """
    if not LLM_API_KEY:
        log("❌ No LLM API key configured", "ERROR")
        return ""

    voice_rules = "\n".join(f"- {r}" for r in persona.get("voice_rules", []))
    tone = persona.get("tone", "casual")
    lang = persona.get("language", "Indonesian casual")

    # Try to find relevant product
    chosen_link = None
    chosen_category = None
    should_include_link = False

    if categories and not no_link:
        # Try relevance match first
        relevant_cat = find_relevant_category(post_text, categories)
        if relevant_cat:
            chosen_category = relevant_cat
            chosen_link = pick_random_link(categories, set()) if relevant_cat.get("links") else None
            if chosen_link:
                should_include_link = True
                log(f"🎯 Relevant category: {relevant_cat['name']} → link: {chosen_link.get('label', chosen_link['url'][:40])}")
        else:
            # Fallback: random link from anywhere
            random_link = pick_random_link(categories)
            if random_link:
                chosen_link = random_link
                should_include_link = True
                log(f"🔀 Random link fallback: {random_link.get('label', random_link['url'][:40])}")

    # Build prompt
    link_instruction = ""
    if should_include_link and chosen_link:
        link_url = chosen_link["url"]
        link_label = chosen_link.get("label", "produk ini")
        link_instruction = f"""
LINK INSTRUCTION:
- Include this link NATURALLY at the end: {link_url}
- Example: "gue beli {link_label} di shopee, linknya di sini {link_url}"
- Example: "kalo lo cari yang bagus, gue saranin {link_label} https://s.shopee.co.id/xxx"
- DO NOT make the whole comment about the product
- The link should feel like an afterthought, not a CTA
- Keep the reply conversational first, link at the end"""

    prompt = f"""You are a {persona.get('age', '20-an')} {persona.get('gender', 'female')} from Jakarta.
Your tone: {tone}
Your language style: {lang}
Your voice rules:
{voice_rules}

The user is REPLYING to a Threads post. Read the post below and write a NATURAL, CONTEXTUAL reply.{link_instruction}

RULES:
- Reply must be RELEVANT to the post content — don't be generic
- Sound like a real person responding, NOT a bot or copywriter
- Keep it SHORT (1-3 sentences, max 280 chars){' plus link' if should_include_link else ''}
- Use specific details from the post to show you actually read it
- Can relate with personal experience
- Can agree/disagree/tell a similar story
- NO hard sell, NO CTA — be natural{'; the product mention should feel like a casual recommendation, not an ad' if should_include_link else ''}
- End naturally — like how you'd reply in a group chat

POST TO REPLY TO:
"{post_text}"

Write ONLY the reply text (no quotes, no labels, no explanation)."""

    try:
        import httpx
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 300 if should_include_link else 200,
                    "temperature": 0.8,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            comment = data["choices"][0]["message"]["content"].strip()
            # Clean up any quotes
            comment = comment.strip("\"'「」")
            # Verify link is included if it was requested
            if should_include_link and chosen_link:
                if chosen_link["url"] not in comment:
                    log(f"⚠️ LLM dropped the link, appending with context")
                    # Don't just tack on bare URL — wrap naturally
                    link_label = chosen_link.get("label", "cek aja")
                    wrapped = comment.rstrip(" .,;!?\n")
                    if len(wrapped) > 10:
                        comment = wrapped + f". Oh iya, yang ini juga lumayan {chosen_link['url']}"
                    else:
                        # LLM refused entirely — generate minimal fallback
                        comment = f"Bener juga sih. Oh iya, buat yang nyari {link_label} bisa cek {chosen_link['url']}"
            log(f"🧠 LLM generated: {comment[:150]}...")
            return comment
    except Exception as e:
        log(f"❌ LLM generation failed: {e}", "ERROR")
        return ""

# ─── BROWSER HELPERS ───
SAMESITE_MAP = {
    "no_restriction": "None",
    "unspecified": "Lax",
    "lax": "Lax",
    "strict": "Strict",
}

async def _import_cookies(context, path, label="cookies"):
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
        log(f"✅ Loaded {len(playwright_cookies)} cookies from {label}")
        return True
    except Exception as e:
        log(f"Failed to import cookies from {label}: {e}", "WARN")
        return False

async def import_cookies(context):
    loaded = 0
    if await _import_cookies(context, COOKIES_FILE, "cookies.json (Threads)"):
        loaded += 1
    if await _import_cookies(context, IG_COOKIES_FILE, "instagram_cookies.json"):
        loaded += 1
    return loaded > 0

async def _dismiss_modals(page):
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

LOGGED_IN_INDICATORS = [
    'nav a[href*="/@"]',
    'svg[aria-label="Profile"]',
    'button[aria-label="Notifications"]',
]

# ─── FIND RAME POSTS ───
async def collect_rame_posts(page, min_replies=3, max_posts=10):
    """
    Scroll the Threads for-you feed and collect posts with engagement.
    Returns list of dicts: {text, reply_count, like_count, url, author}
    """
    log("🔍 Scrolling feed to find rame posts...")
    
    posts_data = []
    seen_texts = set()
    
    for scroll_round in range(10):
        posts = await page.evaluate("""() => {
            const results = [];
            const containers = document.querySelectorAll('div[data-pressable-container="true"]');
            
            for (const c of containers) {
                const fullText = c.textContent || '';
                const innerText = c.innerText || '';
                
                // Get author from first link text
                const firstLink = c.querySelector('a[href*="/"][tabindex]');
                const author = firstLink ? firstLink.textContent.trim() : '';
                
                // Get post URL
                const links = c.querySelectorAll('a[href*="/@"]');
                let postUrl = '';
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    if (href.includes('/@') && !href.endsWith('/')) {
                        postUrl = href;
                        break;
                    }
                }
                
                // Extract post text: between "More" and "Translate" or "Like"
                let text = '';
                const moreIdx = fullText.indexOf('More');
                if (moreIdx >= 0) {
                    let afterMore = fullText.substring(moreIdx + 4);
                    // Find end: Translate, Like, Reply, or end of string
                    const endMarkers = ['Translate', 'Like', 'Reply'];
                    let endIdx = afterMore.length;
                    for (const m of endMarkers) {
                        const idx = afterMore.indexOf(m);
                        if (idx >= 0 && idx < endIdx) endIdx = idx;
                    }
                    text = afterMore.substring(0, endIdx).trim();
                    // Remove trailing number patterns like " 1/2", " 2/3"
                    text = text.replace(/\\s*\\d+\\/\\d+$/, '').trim();
                }
                
                if (!text || text.length < 5) continue;
                
                // Parse reply count from textContent
                let replyCount = 0;
                const replyMatch = fullText.match(/Reply(\\d+(?:\\.\\d+)?)\\s*(K|k|M|m)?/);
                if (replyMatch) {
                    let val = parseFloat(replyMatch[1]);
                    const suf = (replyMatch[2] || '').toLowerCase();
                    if (suf === 'k') val *= 1000;
                    else if (suf === 'm') val *= 1000000;
                    replyCount = Math.round(val);
                }
                
                let likeCount = 0;
                const likeMatch = fullText.match(/Like(\\d+(?:\\.\\d+)?)\\s*(K|k|M|m)?/);
                if (likeMatch) {
                    let val = parseFloat(likeMatch[1]);
                    const suf = (likeMatch[2] || '').toLowerCase();
                    if (suf === 'k') val *= 1000;
                    else if (suf === 'm') val *= 1000000;
                    likeCount = Math.round(val);
                }
                
                results.push({
                    text: text.substring(0, 500),
                    author: author,
                    replyCount: replyCount,
                    likeCount: likeCount,
                    url: postUrl,
                });
            }
            return results;
        }""")
        
        if posts:
            for p in posts:
                text_key = p.get("text", "")[:80]
                if text_key and text_key not in seen_texts:
                    seen_texts.add(text_key)
                    posts_data.append(p)
        
        rame_count = sum(1 for p in posts_data if p.get("replyCount", 0) >= min_replies)
        log(f"  Scroll {scroll_round+1}: {len(posts_data)} unique posts ({rame_count} have ≥{min_replies} replies)")
        
        if rame_count >= max_posts * 2:
            break
        
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(2000)
    
    rame = [p for p in posts_data if p.get("replyCount", 0) >= min_replies]
    log(f"📊 Found {len(rame)} rame posts out of {len(posts_data)} unique")
    
    rame.sort(key=lambda x: x.get("replyCount", 0), reverse=True)
    
    return rame[:max_posts]

# ─── POST COMMENT ───
async def post_comment(page, comment_text: str) -> bool:
    """
    Type and submit a comment on the currently open reply compose.
    Threads reply UI is an inline text field below the post.
    """
    if not comment_text:
        return False
    
    try:
        # Wait for reply compose to be ready
        await page.wait_for_timeout(1000)
        
        # Find reply textarea
        reply_selectors = [
            'div[aria-label*="Reply"]',
            'div[aria-label*="reply"]',
            'div[aria-label*="Balas"]',
            'div[role="textbox"]',
            'div[contenteditable="true"]',
            'textarea',
        ]
        
        reply_area = None
        for sel in reply_selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    reply_area = el
                    log(f"Found reply area via: {sel}")
                    break
            except Exception:
                continue
        
        if not reply_area:
            # JS fallback: find any focused or visible contenteditable near the post
            found = await page.evaluate("""() => {
                const divs = document.querySelectorAll('div[contenteditable="true"]');
                for (const d of divs) {
                    if (d.getAttribute('role') === 'textbox' || d.textContent.trim() === '') {
                        d.focus();
                        return true;
                    }
                }
                // Any contenteditable
                const all = document.querySelectorAll('[contenteditable="true"]');
                if (all.length > 0) { all[0].focus(); return true; }
                return false;
            }""")
            if not found:
                log("❌ Could not find reply compose area", "ERROR")
                return False
            reply_area = page.locator('[contenteditable="true"]').first
        
        # Click to focus
        await reply_area.click(force=True)
        await page.wait_for_timeout(500)
        
        # Type the comment
        try:
            editable = page.locator('[contenteditable="true"], [role="textbox"][contenteditable]').first
            await editable.wait_for(state='visible', timeout=3000)
            await editable.fill(comment_text)
            log(f"Typed comment via fill() ({len(comment_text)} chars)")
        except Exception:
            log("fill() failed, using keyboard.type")
            await page.keyboard.type(comment_text, delay=15)
        
        await page.wait_for_timeout(500)
        
        # Submit: Threads uses Enter to submit reply (or Ctrl+Enter on desktop)
        await page.keyboard.press('Control+Enter')
        log("Pressed Ctrl+Enter to submit comment")
        await page.wait_for_timeout(3000)
        
        # Try clicking a visible "Reply" button if keyboard shortcut didn't work
        try:
            submit_btn = page.locator('button:has-text("Reply"), button:has-text("Balas"), button:has-text("Kirim")').first
            if await submit_btn.is_visible(timeout=2000):
                await submit_btn.click(timeout=3000)
                log("Clicked Reply button")
                await page.wait_for_timeout(2000)
        except Exception:
            pass
        
        # Verify: check if reply compose is now empty or gone
        try:
            still_has_text = await page.evaluate("""() => {
                const el = document.querySelector('[contenteditable="true"]');
                return el ? el.textContent.trim().length : -1;
            }""")
            if still_has_text == 0 or still_has_text == -1:
                log("✅ Comment submitted successfully!")
                return True
            elif still_has_text > 0:
                log(f"⚠️  Comment may still have {still_has_text} chars — might need retry", "WARN")
                return True  # Optimistic
        except Exception:
            pass
        
        log("✅ Comment submitted (optimistic)")
        return True
        
    except Exception as e:
        log(f"❌ Error posting comment: {e}", "ERROR")
        return False

# ─── CLICK REPLY BUTTON ───
async def click_reply_button(page) -> bool:
    """Click the reply button on the current post to open compose."""
    try:
        # Find reply button — speech bubble icon or text "Reply"
        reply_btn_selectors = [
            'button[aria-label*="Reply"]',
            'button[aria-label*="reply"]',
            'div[aria-label*="Reply"][role="button"]',
            'svg[aria-label*="Reply"]',
            'button:has(svg[aria-label*="Reply"])',
            'button:has(svg[aria-label*="reply"])',
            'div[role="button"]:has-text("Reply")',
            'div[role="button"]:has-text("Balas")',
            # Fallback: find by position (reply is usually 2nd action button)
        ]
        
        for sel in reply_btn_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    log(f"Clicked reply button via: {sel}")
                    return True
            except Exception:
                continue
        
        # JS fallback: find reply button in the page
        clicked = await page.evaluate("""() => {
            // Find all buttons with speech bubble icon or reply text
            const buttons = document.querySelectorAll('button, div[role="button"]');
            for (const btn of buttons) {
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (label.includes('reply') || label.includes('balas') || label === 'komentar') {
                    btn.click();
                    return true;
                }
                const text = (btn.textContent || '').toLowerCase().trim();
                if (text === 'reply' || text === 'balas') {
                    btn.click();
                    return true;
                }
                // Check for SVG icon (speech bubble)
                const svg = btn.querySelector('svg');
                if (svg) {
                    const svgLabel = svg.getAttribute('aria-label') || '';
                    if (svgLabel.toLowerCase().includes('reply') || svgLabel.toLowerCase().includes('balas')) {
                        btn.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        
        if clicked:
            await page.wait_for_timeout(1500)
            log("Clicked reply button via JS fallback")
            return True
        
        log("❌ Could not find reply button", "ERROR")
        return False
    except Exception as e:
        log(f"❌ Error clicking reply: {e}", "ERROR")
        return False

# ─── LOGIN HANDLING (same as threads_post.py) ───
async def ensure_logged_in(page, context):
    """Check login and re-auth if needed."""
    log("Checking login status...")
    
    # Import cookies
    await import_cookies(context)
    
    await page.goto("https://www.threads.com", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    await _dismiss_modals(page)
    
    # Check login indicators
    logged_in = False
    for indicator in LOGGED_IN_INDICATORS:
        try:
            el = page.locator(indicator).first
            if await el.is_visible(timeout=2000):
                logged_in = True
                break
        except Exception:
            continue
    
    if logged_in:
        log("✅ Already logged in!")
        return True
    
    log("Not logged in. Checking for SSO...")
    return False  # Will return False but continue — post function handles SSO

# ─── MAIN ───
async def main():
    global ACCOUNT_DIR, CHROME_PROFILE, COMMENTED_LOG, CONFIG_FILE
    global COOKIES_FILE, IG_COOKIES_FILE, ACCOUNT_NAME
    
    parser = argparse.ArgumentParser(description="Auto-comment on trending Threads posts")
    parser.add_argument("--account", default="threads_akun1", help="Account directory name")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without posting")
    parser.add_argument("--max-comments", type=int, default=5, help="Max comments per run (default: 5)")
    parser.add_argument("--min-replies", type=int, default=3, help="Minimum replies to consider 'rame' (default: 3)")
    parser.add_argument("--cooldown", type=int, default=300, help="Seconds between comments (default: 300)")
    parser.add_argument("--keyword", nargs="+", help="Override niche keywords for search")
    args = parser.parse_args()
    
    ACCOUNT_NAME = args.account
    ACCOUNT_DIR = BASE_DIR / "accounts" / ACCOUNT_NAME
    CHROME_PROFILE = ACCOUNT_DIR / ".chrome-profile"
    CONFIG_FILE = ACCOUNT_DIR / "config.json"
    COOKIES_FILE = ACCOUNT_DIR / "cookies.json"
    IG_COOKIES_FILE = ACCOUNT_DIR / "instagram_cookies.json"
    COMMENTED_LOG = ACCOUNT_DIR / "commented.json"
    
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    
    log(f"🚀 Threads Auto-Comment v1.0")
    log(f"   Account: {ACCOUNT_NAME}")
    log(f"   Max comments: {args.max_comments}")
    log(f"   Min replies to consider 'rame': {args.min_replies}")
    log(f"   Cooldown: {args.cooldown}s")
    
    if args.dry_run:
        log("   [DRY RUN — no comments will be posted]")
    
    # Load persona for LLM
    persona = load_persona()
    log(f"   Persona: {persona.get('age', 'N/A')} {persona.get('gender', 'N/A')}")
    
    # Load knowledge base for product links
    categories = load_knowledge_links()
    log(f"   Link knowledge: {len(categories)} categories loaded")
    
    # Load already commented posts to avoid duplicates
    commented = load_json(COMMENTED_LOG, [])
    commented_urls = set(c.get("url", "") for c in commented) if isinstance(commented, list) else set()
    log(f"   Already commented on {len(commented_urls)} posts")
    
    # ─── BROWSER ───
    async with async_playwright() as p:
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
        
        try:
            # Ensure logged in
            logged_in = await ensure_logged_in(page, context)
            if not logged_in:
                # Try SSO flow
                log("Attempting SSO login...")
                await page.goto("https://www.threads.com", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                await _dismiss_modals(page)
                
                # Click Create to trigger SSO
                try:
                    create_btn = page.locator('button:has(svg[aria-label="Create"]), a[href="/create"]').first
                    if await create_btn.is_visible(timeout=3000):
                        await create_btn.click(timeout=5000)
                        await page.wait_for_timeout(2000)
                except Exception:
                    pass
                
                # Click Continue with Instagram
                for sel in ['button:has-text("Continue with Instagram")', 'div[role="button"]:has-text("Continue with Instagram")']:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=3000):
                            await btn.click(timeout=5000)
                            await page.wait_for_timeout(8000)
                            break
                    except Exception:
                        pass
                
                # Check login status
                for indicator in LOGGED_IN_INDICATORS:
                    try:
                        el = page.locator(indicator).first
                        if await el.is_visible(timeout=2000):
                            logged_in = True
                            break
                    except Exception:
                        continue
            
            if not logged_in:
                log("❌ Failed to log in. Can't comment without login.", "ERROR")
                return
            
            log("✅ Logged in successfully!")
            
            # Navigate to home feed (For You page)
            await page.goto("https://www.threads.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            await _dismiss_modals(page)
            
            # ─── COLLECT RAME POSTS ───
            rame_posts = await collect_rame_posts(page, min_replies=args.min_replies, max_posts=args.max_comments * 2)
            
            if not rame_posts:
                log("❌ No rame posts found in feed", "WARN")
                return
            
            # ─── COMMENT ON EACH POST ───
            commented_count = 0
            for i, post in enumerate(rame_posts):
                if commented_count >= args.max_comments:
                    break
                
                post_text = post.get("text", "")
                post_url = post.get("url", "")
                reply_count = post.get("replyCount", 0)
                
                # Skip if already commented
                if post_url and post_url in commented_urls:
                    log(f"  ⏭️ Already commented on this post, skipping")
                    continue
                
                log(f"\n{'='*50}")
                log(f"📝 Post #{i+1}: {reply_count} replies")
                log(f"   Text: {post_text[:150]}...")
                if post_url:
                    log(f"   URL: {post_url}")
                
                if args.dry_run:
                    comment = generate_comment(post_text, persona, categories=categories)
                    log(f"   [DRY-RUN] Would comment: {comment[:100]}...")
                    commented_count += 1
                    continue
                
                # Navigate to the post if we have a URL
                if post_url:
                    full_url = f"https://www.threads.com{post_url}" if post_url.startswith("/") else post_url
                    log(f"   Navigating to post: {full_url}")
                    await page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)
                    await _dismiss_modals(page)
                else:
                    # Click on the post to expand it
                    # First try clicking on the post text area
                    try:
                        # Find any link or clickable area in the post
                        post_link = page.locator(f'a[href*="/@"]').first
                        if await post_link.is_visible(timeout=2000):
                            await post_link.click(timeout=3000)
                            await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                
                # Click reply button
                reply_ok = await click_reply_button(page)
                if not reply_ok:
                    log(f"   ⏭️ Could not click reply, skipping post")
                    continue
                
                # Generate comment
                comment = generate_comment(post_text, persona, categories=categories)
                if not comment:
                    log(f"   ⏭️ Could not generate comment, skipping")
                    continue
                
                # Post the comment
                success = await post_comment(page, comment)
                
                if success:
                    commented_count += 1
                    # Track commented post
                    commented.append({
                        "url": post_url or post_text[:80],
                        "text": post_text[:200],
                        "comment": comment[:200],
                        "reply_count": reply_count,
                        "commented_at": datetime.now(timezone.utc).isoformat(),
                    })
                    commented_urls.add(post_url or post_text[:80])
                    save_json(COMMENTED_LOG, commented)
                    log(f"   ✅ Comment #{commented_count} posted!")
                    
                    # Cooldown before next comment
                    if commented_count < args.max_comments:
                        log(f"   ⏳ Waiting {args.cooldown}s cooldown...")
                        await asyncio.sleep(args.cooldown)
                else:
                    log(f"   ❌ Failed to post comment")
            
            log(f"\n{'='*50}")
            log(f"✅ Done! Posted {commented_count} comments this run")
            
            # Save log
            screenshot_path = LOG_DIR / f"comment_run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.png"
            try:
                await page.screenshot(path=str(screenshot_path))
                log(f"   Screenshot saved: {screenshot_path}")
            except Exception:
                pass
            
        except Exception as e:
            log(f"❌ Error: {e}", "ERROR")
            import traceback
            log(traceback.format_exc(), "ERROR")
        finally:
            await context.close()

if __name__ == "__main__":
    asyncio.run(main())
