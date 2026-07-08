#!/usr/bin/env python3
"""
daily_content.py — Generate Threads content from observer trends + LLM

Usage:
    python3 scripts/daily_content.py                    # generate + queue today's posts
    python3 scripts/daily_content.py --count 5           # generate N posts
    python3 scripts/daily_content.py --dry-run           # preview without queueing

Generates content by:
    1. Loading latest observer data (trending X tweets)
    2. Adapting trending topics/insights for Threads audience
    3. Including relevant affiliate links from link_knowledge.json
    4. Queuing generated posts for posting schedule
"""
import argparse
import json
import os
import random
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
from openai import OpenAI

# Load env — check threads-automation/.env first, then automation/.env
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(env_path)
else:
    load_dotenv(Path.home() / "automation" / ".env")

# ============== PATHS ==============
ACCOUNT_DIR = BASE_DIR / "accounts" / "threads_akun1"
PENDING_FILE = ACCOUNT_DIR / "pending_posts.json"
POSTED_LOG = ACCOUNT_DIR / "posted.json"
CONFIG_FILE = ACCOUNT_DIR / "config.json"
LINK_KB = ACCOUNT_DIR / "link_knowledge.json"
LOG_DIR = BASE_DIR / "logs"
OBSERVER_DIR = Path(os.path.expanduser("~")) / "automation" / "data" / "viral_research"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ============== LLM CONFIG ==============
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# ============== LOGGING ==============
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")
    with open(LOG_DIR / "daily_content.log", "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


# ============== LOADERS ==============
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


def load_latest_observer():
    """Load the most recent observer data for trends."""
    if not OBSERVER_DIR.exists():
        log(f"Observer data dir not found: {OBSERVER_DIR}", "WARN")
        return None
    
    raw_files = sorted(OBSERVER_DIR.glob("raw_*.json"))
    comparison_files = sorted(OBSERVER_DIR.glob("comparison_*.json"))
    
    data = {}
    
    if raw_files:
        latest_raw = raw_files[-1]
        data["raw"] = load_json(latest_raw, {})
        log(f"Loaded observer raw: {latest_raw.name}")
    
    if comparison_files:
        latest_comp = comparison_files[-1]
        data["comparison"] = load_json(latest_comp, {})
        log(f"Loaded observer comparison: {latest_comp.name}")
    
    return data if data else None


def load_link_knowledge():
    """Load affiliate links for content."""
    kb = load_json(LINK_KB, {})
    categories = kb.get("categories", {})
    
    usable = []
    for cat_name, cat_cfg in categories.items():
        auto_mode = cat_cfg.get("auto_mode", True)
        for link in cat_cfg.get("links", []):
            url = link.get("url", "")
            if url:
                usable.append({
                    "category": cat_name,
                    "url": url,
                    "triggers": cat_cfg.get("triggers", []),
                })
    
    return usable


def get_persona_context():
    """Get persona details for content generation."""
    config = load_json(CONFIG_FILE, {})
    persona = config.get("persona", {})
    niche = config.get("niche", {})
    
    info = {
        "tone": persona.get("tone", "casual Indonesian"),
        "language": persona.get("language", "Jakarta casual"),
        "lifestyle": persona.get("lifestyle", "anak muda urban"),
        "niche": niche.get("primary", "wellness/intimate"),
        "sub_niches": niche.get("sub_niches", []),
        "voice_rules": persona.get("voice_rules", []),
    }
    return info


# ============== LLM GENERATION ==============
def generate_threads_content(persona, trends, links, count=5) -> list:
    """
    Generate Threads posts using LLM.
    
    Returns: list of dicts {content, link, category}
    """
    if not LLM_API_KEY:
        log("LLM_API_KEY not set!", "ERROR")
        return None

    client = OpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
    )

    # Build trend context summary
    trend_summary = ""
    if trends:
        raw = trends.get("raw", {})
        creators = raw.get("top_creators", [])
        if creators:
            top_trends = [f"- {c.get('author','?')}: {c.get('tweets', 1)} tweets" for c in creators[:5]]
            trend_summary = "\n".join(top_trends)
        posts = raw.get("posts", [])
        if posts:
            sample_posts = [f"- \"{p.get('text','')[:100]}\" (by {p.get('author','?')})" for p in posts[:3]]
            trend_summary += "\n" + "\n".join(sample_posts)

    # Build link list
    link_options = []
    for l in links:
        link_options.append(f"- {l['category']}: {l['url']}")

    link_list = "\n".join(link_options) if link_options else ""

    # Build link section (optional — pure storytelling when empty)
    link_section = ""
    if link_list:
        link_section = f"\nLINK PRODUK TERSEDIA (pilih SATU per post, integrasikan natural):\n{link_list}\n"
    
    # Build prompt
    prompt = f"""Kamu adalah content writer Threads. Generate {count} postingan Threads unik berdasarkan tren dan pengalaman sehari-hari.

PERSONA:
- Tone: {persona['tone']}
- Bahasa: {persona['language']}
- Lifestyle: {persona['lifestyle']}
- Niche: {persona['niche']} ({', '.join(persona['sub_niches'])})
- Voice rules: {'; '.join(persona['voice_rules'])}

TREN HARI INI (dari X, adaptasi untuk audiens Threads):
{trend_summary if trend_summary else "(Tidak ada tren spesifik — pakai insight lifestyle sehari-hari)"}

{link_section}

FORMAT WAJIB:
- Tiap post: 100-280 karakter (Threads max 500, tapi lebih pendek lebih engaging)
- Bahasa Jakarta casual (doang, sih, emang, ntah, gini, kek, gpp)
- NO link — pure storytelling, NO promosi produk apapun
- Mulai dengan insight atau situasi relatable (macet, weekend, kos, warteg, kantor, gym, café)
- Akhiri dengan micro-lesson/refleksi, BUKAN hard CTA
- NO affiliate link, NO "link in bio", NO promosi brand
- NO copywriter voice / template-feeling
- Pake referensi sensory spesifik kalo bisa
- SETIAP post HARUS topik yang beda (gak boleh 2 post topik sama)

BUDAYAKAN MENULIS DALAM BAHASA INDONESIA, JANGAN PAKE BAHASA INGGRIS.

OUTPUT FORMAT (JSON array):
[
  {{
    "content": "teks post dengan link...",
    "link": "https://...",
    "category": "kondom_premium",
    "theme": "insight/trend apa yang menginspirasi ini"
  }}
]

RESPON HANYA DENGAN JSON ARRAY, TIDAK ADA TEKS LAIN.
"""

    log("Generating Threads content with LLM...")

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
            temperature=0.8,
        )
        
        text = (resp.choices[0].message.content or "").strip()
        
        # Strip chain-of-thought
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        
        # Parse JSON
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'```(?:json)?\n?', '', text)
        text = text.strip()
        
        posts = json.loads(text)
        if isinstance(posts, dict):
            posts = [posts]
        
        log(f"Generated {len(posts)} posts")
        return posts
        
    except Exception as e:
        log(f"LLM generation failed: {e}", "ERROR")
        # Fallback posts
        fallback_posts = []
        for link in links[:2]:
            fallback_posts.append({
                "content": f"baru nyadar something abis switch ke ini... beda banget rasanya honestly 🙌 {link['url']}",
                "link": link["url"],
                "category": link["category"],
                "theme": "personal insight",
            })
        log(f"Using fallback: {len(fallback_posts)} posts")
        return fallback_posts


# ============== QUEUE MANAGER ==============
def queue_posts(posts):
    """Add generated posts to the pending queue."""
    pending = load_json(PENDING_FILE, [])
    
    now = datetime.now(timezone.utc)
    for post in posts:
        pending.append({
            "content": post.get("content", ""),
            "link": post.get("link", ""),
            "category": post.get("category", "general"),
            "theme": post.get("theme", ""),
            "created_at": now.isoformat(),
        })
    
    save_json(PENDING_FILE, pending)
    log(f"Queued {len(posts)} posts. Total pending: {len(pending)}")
    return len(posts)


# ============== MAIN ==============
def main():
    parser = argparse.ArgumentParser(description="Generate Threads content from trends")
    parser.add_argument("--count", type=int, default=3, help="Number of posts to generate (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without queueing")
    parser.add_argument("--no-trends", action="store_true", help="Generate without observer trends")
    args = parser.parse_args()

    # Load data
    persona = get_persona_context()
    links = load_link_knowledge()
    
    if not links:
        log("No links in link_knowledge.json — pure storytelling mode")

    trends = None
    if not args.no_trends:
        trends = load_latest_observer()
        if not trends:
            log("No observer data found, generating without trends")
    else:
        log("Trends disabled by --no-trends flag")

    # Generate posts (ask LLM for more than needed to have variety)
    posts = generate_threads_content(persona, trends, links, count=min(args.count + 2, 8))
    
    if not posts:
        log("No posts generated!", "ERROR")
        return

    # Limit to requested count
    posts = posts[:args.count]

    # Display
    print(f"\n{'='*60}")
    print(f"GENERATED {len(posts)} POSTS")
    print(f"{'='*60}")
    
    for i, post in enumerate(posts, 1):
        content = post.get("content", "")
        link = post.get("link", "")
        category = post.get("category", "?")
        theme = post.get("theme", "")
        
        print(f"\n--- Post #{i} ({category}) ---")
        print(f"Theme: {theme}")
        print(f"Content ({len(content)} chars):")
        print(content)
        print(f"Link: {link}")
    
    print(f"\n{'='*60}")

    # Queue posts
    if not args.dry_run:
        queued = queue_posts(posts)
        print(f"\n✅ Queued {queued} posts for publishing")
    else:
        print(f"\n🔍 Dry-run — posts NOT queued")


if __name__ == "__main__":
    main()
