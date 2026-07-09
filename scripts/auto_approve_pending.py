#!/usr/bin/env python3
"""
auto_approve_pending.py — Auto-approve pending replies older than X minutes.

Logika:
- Cari entry `awaiting` di pending_replies.json dengan notified_at > 30 menit
- Re-match kategori dari tweet text (pake match_category dari auto_reply.py)
- Auto-approve HANYA kalau kategori auto_mode=true
- Skip kalau: daily limit tercapai / cooldown aktif / tweet udah di-reply
- Post via auto_post.py --auto (langsung, gak lewat pending)

Usage:
    python3 scripts/auto_approve_pending.py --account akun2_dawnlingchild
    python3 scripts/auto_approve_pending.py --account akun2_dawnlingchild --dry-run
    python3 scripts/auto_approve_pending.py --all-accounts
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

AUTOMATION_DIR = Path(__file__).parent.parent

# Load .env so LLM_API_KEY etc are available even via cron
load_dotenv(AUTOMATION_DIR / ".env")
ACCOUNTS_DIR = AUTOMATION_DIR / "accounts"
DEFAULT_THRESHOLD_MINUTES = int(os.getenv("AUTO_APPROVE_MINUTES", "30"))

# Add scripts dir to path for match_category import
sys.path.insert(0, str(AUTOMATION_DIR / "scripts"))
from auto_reply import match_category  # noqa: E402
try:
    from semantic_matcher import semantic_match_category  # noqa: E402
    SEMANTIC_AVAILABLE = True
except Exception:
    SEMANTIC_AVAILABLE = False

# === LLM client (lazy init) ===
# LLM client (lazy init) — OpenAI-compatible with custom base_url.
_llm_client = None
_llm_client_failed = False


def get_llm_client():
    """Lazy-init OpenAI client. Returns None kalau key kosong / lib error."""
    global _llm_client, _llm_client_failed
    if _llm_client_failed:
        return None
    if _llm_client is not None:
        return _llm_client
    api_key = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip()
    if not api_key or api_key.startswith("***"):
        _llm_client_failed = True
        return None
    try:
        from openai import OpenAI
        _llm_client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "15")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        )
        return _llm_client
    except Exception as e:
        print(f"   ⚠️ LLM client init error: {e}")
        _llm_client_failed = True
        return None


def filter_indonesian_text(text):
    """[Fix 1] Strip karakter non-Indonesian dari LLM output.
    Reasoning model kadang leak Chinese/Japanese/Korean/emoji aneh.
    Indonesian pake Latin + standard punctuation, occasional emoji.
    Keep: ASCII printable + common emoji range + common punctuation.
    Strip: CJK, Hangul, Arabic, Hebrew, Cyrillic, Thai, dll.
    """
    import re as _re
    if not text:
        return text
    # Unicode ranges yang BUKAN bahasa Indonesia natural:
    #   CJK: 0x4E00-0x9FFF, 0x3400-0x4DBF, 0x20000-0x2A6DF
    #   Hangul: 0xAC00-0xD7AF
    #   Hiragana/Katakana: 0x3040-0x30FF
    #   Arabic: 0x0600-0x06FF
    #   Hebrew: 0x0590-0x05FF
    #   Cyrillic: 0x0400-0x04FF
    #   Thai: 0x0E00-0x0E7F
    non_id_ranges = _re.compile(
        r'[\u4E00-\u9FFF'      # CJK Unified Ideographs
        r'\u3400-\u4DBF'        # CJK Ext A
        r'\U00020000-\U0002A6DF' # CJK Ext B
        r'\u3040-\u30FF'        # Hiragana + Katakana
        r'\uAC00-\uD7AF'        # Hangul
        r'\u0600-\u06FF'        # Arabic
        r'\u0590-\u05FF'        # Hebrew
        r'\u0400-\u04FF'        # Cyrillic
        r'\u0E00-\u0E7F'        # Thai
        r']+'
    )
    filtered = non_id_ranges.sub('', text)
    # Collapse multiple spaces
    filtered = _re.sub(r'  +', ' ', filtered).strip()
    return filtered


def _build_niche_context(persona, cat_cfg=None):
    """[Multi-niche] Build niche context for LLM prompts from persona.niche + cat_cfg.

    Returns dict with keys:
      descriptor     : str  — short description of promoted product category
      avoid_brands   : list — brand names to NEVER mention (or generic guidance)
      benefit_style  : list — example benefit words in this niche
      tricky_cases   : list — example words that overlap with this niche's triggers
                          but usually belong to a different context
      fp_examples    : list — example false-positive contexts to watch for

    Backward-compat: kalau persona gak punya niche.llm_context, return wellness
    defaults (current behavior akun1+2).
    """
    persona = persona or {}
    persona_niche = persona.get("niche", {}) or {}
    cat_cfg = cat_cfg or {}

    # Per-persona override (niche.llm_context) wins.
    # Cat_cfg override (cat_cfg.llm_context) wins over persona.
    # Otherwise: fallback to wellness defaults (akun1+2 today).
    ctx = cat_cfg.get("llm_context") or persona_niche.get("llm_context") or {}

    return {
        "descriptor": ctx.get(
            "descriptor",
            "intimate wellness / personal care products",
        ),
        "avoid_brands": ctx.get(
            "avoid_brands",
            ["Durex", "Okamoto", "Sutra"],
        ),
        "benefit_style": ctx.get(
            "benefit_style",
            ["tipis", "aman", "adem", "hypoallergenic"],
        ),
        "tricky_cases": ctx.get(
            "tricky_cases",
            [
                "'pelumas' for treadmill/engine/medical",
                "'bikini' for vacation photos",
                "'kondom' for brand reviews",
                "'bra' for fashion",
                "massager for face/body massage (NOT intimate)",
            ],
        ),
        "fp_examples": ctx.get(
            "fp_examples",
            [
                "treadmill", "engine", "car/motorcycle maintenance",
                "medical equipment", "kitchen/cooking", "vacation photos",
                "fashion show", "anime convention", "beauty/skincare review",
            ],
        ),
    }


def is_indonesian_tweet(text, min_id_ratio=0.3):
    """Cek apakah tweet dominan bahasa Indonesia berdasarkan stopword ratio.

    Returns False kalau tweet English / asing (skip), True kalau Indonesian.
    min_id_ratio: minimal rasio kata yg match Indonesian stopwords terhadap total kata.
    """
    if not text:
        return False
    import re as _re
    id_stopwords = {
        'yang', 'dan', 'di', 'ke', 'dari', 'untuk', 'dengan', 'pada', 'ini', 'itu',
        'aku', 'kamu', 'gue', 'lo', 'gw', 'lu', 'gua', 'elo',
        'nggak', 'enggak', 'gak', 'ga', 'ngga', 'ndak',
        'udah', 'sudah', 'belum', 'lagi', 'juga', 'bisa', 'ada',
        'nih', 'deh', 'sih', 'dong', 'doang', 'aja', 'kali', 'ya', 'iya',
        'kok', 'kan', 'loh', 'lho', 'yah', 'yoi',
        'wkwk', 'wkwkwk', 'wkwkwkwk', 'haha', 'hehe', 'hihi',
        'banget', 'bangett', 'bgt', 'pake', 'pakai',
        'si', 'tu', 'tau', 'tahu', 'gitu', 'gini', 'begitu', 'begini',
        'kalo', 'kalau', 'soal', 'tentang', 'sama', 'bikin', 'buat',
        'tp', 'tapi', 'tpi', 'cuma', 'cm', 'cuman', 'cmn',
        'liat', 'lihat', 'lht', 'lwt', 'lewat', 'malah',
        'emang', 'memang', 'emg', 'mg', 'skrg', 'sekarang', 'skrng',
        'abis', 'habis', 'abis2', 'abis2an',
        'asli', 'anjir', 'anjrit', 'anjing', 'anjay',
        'sumpah', 'serius', 'jujur', 'bener', 'benaran',
        'enak', 'enakk', 'nikmat', 'mantap', 'mantul', 'maknyos',
        'kepo', 'baper', 'gaje', 'gabut', 'mager', 'galau',
        'receh', 'pede', 'percaya',
    }
    words = _re.findall(r'\w+', text.lower())
    if not words:
        return False
    match_count = sum(1 for w in words if w in id_stopwords)
    ratio = match_count / len(words)
    return ratio >= min_id_ratio


def llm_generate_draft(tweet_text, tweet_author, persona, category_id, link, cat_cfg, niche_ctx=None):
    """Generate a contextual reply via LLM. Returns (text, used_llm) tuple.
    Returns (None, False) kalau LLM unavailable/error dan caller should fallback.

    [Fix 4] Style target: Jakarta casual cewek 20-an, RELATE ke tweet spesifik
    (quote 1 hal dari tweet asli), NO copywriter voice, NO emoji bombastik.

    [Multi-niche] niche_ctx: dict dari build_niche_context(). Kalau None,
    fallback ke wellness defaults (backward-compat akun1+2).
    """
    if niche_ctx is None:
        niche_ctx = _build_niche_context(persona, cat_cfg)
    client = get_llm_client()
    if client is None:
        return None, False
    model = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
    voice = persona.get("persona", {})
    avoid_brands_str = ", ".join(niche_ctx.get("avoid_brands", [])[:5]) or "brand apapun"
    benefit_examples = ", ".join(niche_ctx.get("benefit_style", [])[:5]) or "benefit yang relevan"
    system = (
        f"BAHASA INDONESIA WAJIB — lo HARUS bales pake bahasa Indonesia. "
        f"JANGAN pake bahasa Inggris sekalipun tweet aslinya Inggris. "
        f"JANGAN template generic kayak 'yoi relate'/'setuju banget'/'nice share'/'mantap'. "
        f"Kalo lo ngerasa balesan lo generic, delete dan tulis ulang. "
        f"Kamu cewek 20-an Jakarta casual. Bales tweet kayak lo ngobrol di WA: "
        f"1-2 kalimat natural, max 180 char, NO link, NO hashtag, wajar pake emoji. "
        f"Tone: {voice.get('tone', 'temen ngetweet')}. "
        f"Gaya: {' '.join(voice.get('voice_rules', []))}. "
        f"Reference 1 hal SPESIFIK dari tweet — ukuran/bahan/masalah/pengalaman yg relate. "
        f"JANGAN panggil nama akun, jangan mention. "
        f"Kalimat terakhir hook natural bukan hard sell. "
        f"Output HANYA teks balesan, tanpa label, tanpa tanda kutip."
    )
    user = (
        f"@{tweet_author}: \"{tweet_text}\"\n\n"
        f"Kategori produk yg mau di-promote: {category_id}\n"
        f"Benefit produk: {cat_cfg.get('benefit_summary', 'premium')}\n\n"
        f"Balas dengan 1-2 kalimat: 1 kalimat SPESIFIK acknowledge isi tweet, "
        f"1 kalimat hook/CTA natural (bukan hard sell).\n"
        f"Jangan generic, harus nyambung ke konteks tweet di atas."
    )
    # Wall-clock cap (30s). Kalau LLM kelamaan, skip — biar
    # cron gak nge-block kalau reasoning model lagi lambat.
    deadline = time.monotonic() + 30.0
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=800,  # deepseek/deepseek-chat reasoning model
            temperature=0.9,
            timeout=20.0,    # match model baseline 4-22s with buffer; some calls hit 60s
        )
        text = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        # deepseek/deepseek-chat quirk: model kadang leak chain-of-thought di content.
        # Strip <think>...</think> blocks dan ambil text setelahnya.
        import re as _re
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        # Hapus prefix kayak "Reply:" / "Output:" / "Here's..." kalau masih ada
        text = _re.sub(r"^(reply\s*:\s*|output\s*:\s*|here'?s?\s+(?:a\s+)?(?:natural\s+)?reply\s*:\s*)",
                       "", text, flags=_re.IGNORECASE).strip()
        # [Fix 1] Filter karakter non-Indonesian
        text = filter_indonesian_text(text)
        if not text or len(text) < 8:
            print(f"   ⚠️ LLM returned empty/too short after filter, fallback")
            return None, False
        # [Fix] Post-generation Indonesian check — jamin reply beneran Indo, bukan Inggris
        if not is_indonesian_tweet(text, min_id_ratio=0.25):
            print(f"   🌏 LLM replied in non-Indonesian: '{text[:80]}...', fallback")
            return None, False
        # Wall-clock check (safety net kalau HTTP timeout gak kick in)
        if time.monotonic() > deadline:
            print(f"   ⏱️ LLM exceeded 30s budget, fallback to template")
            return None, False
        if len(text) > 220:
            text = text[:217] + "..."
        return text, True
    except Exception as e:
        err_msg = str(e)[:200]
        print(f"   ⚠️ LLM error: {err_msg}, fallback to template")
        return None, False


def llm_relevance_check(tweet_text, category_id, cat_cfg, persona=None, niche_ctx=None):
    """[Hybrid C] Cek apakah tweet beneran tentang kategori yang di-promote,
    ATAU cuma pake trigger word di konteks yang beda (mesin, treadmill, medis, dll).

    Returns:
        True  → tweet relevan, lanjut generate draft
        False → tweet false positive, skip
        None  → LLM unavailable/error, default allow (fallback safe)

    Kenapa perlu: trigger kayak 'pelumas' bisa match tweet treadmill/mesin/medis.
    LLM yang nge-generate reply nge-cover konteks tweet (jadi reply jadi ngaco).
    Gate ini nge-blok SEBELUM generate, jadi hemat LLM call + gak ada reply ngaco.

    [Multi-niche] niche_ctx: dict dari build_niche_context(). Kalau None,
    fallback ke wellness defaults (backward-compat akun1+2).
    """
    if niche_ctx is None:
        niche_ctx = _build_niche_context(persona, cat_cfg)
    client = get_llm_client()
    if client is None:
        return None
    model = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
    descriptor = niche_ctx.get("descriptor", "intimate wellness / personal care products")
    tricky_cases_str = "; ".join(niche_ctx.get("tricky_cases", [])[:5]) or "trigger word in a different context"
    fp_examples_str = ", ".join(niche_ctx.get("fp_examples", [])[:8]) or "unrelated contexts"
    subcat = cat_cfg.get("subcategory", descriptor)
    system = (
        f"You are a strict content moderator for an X (Twitter) auto-reply bot "
        f"promoting Indonesian {descriptor}. "
        f"Your job: decide if a tweet's PRIMARY SUBJECT is genuinely about "
        f"{descriptor}. "
        f"If the tweet is PRIMARILY about something else ({fp_examples_str}) "
        f"but just happens to use a word that overlaps with the promoted product, "
        f"you MUST reply NO. "
        f"Tricky cases: {tricky_cases_str}. "
        f"When in doubt, reply NO. False positives (irrelevant replies) are "
        f"much worse than false negatives (missed tweets). "
        f"Answer ONLY 'YES' or 'NO' — one word, no explanation."
    )
    user = (
        f"Tweet: \"{tweet_text}\"\n\n"
        f"Category to promote: {category_id}\n"
        f"Category description: {subcat}\n\n"
        f"Is this tweet genuinely about {category_id} / {descriptor}?\n"
        f"Reply with ONLY 'YES' or 'NO' (one word, no explanation)."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=200,  # deepseek/deepseek-chat reasoning model
            temperature=0.1,  # low temp = consistent moderation
            timeout=10.0,     # slow model baseline 4-22s, allow buffer
        )
        text = (resp.choices[0].message.content or "").strip()
        # Strip reasoning
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        import re as _re
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
        text = text.upper().strip('"').strip("'").strip()
        # Parse YES/NO
        first_token = text.split()[0] if text.split() else ""
        if first_token == "YES":
            return True
        if first_token == "NO":
            return False
        # Ambiguous response — default to allow (safer: let draft LLM try)
        return True
    except Exception as e:
        err_msg = str(e)[:80]
        print(f"   ⚠️ Relevance check error: {err_msg}")
        return None


def get_account_paths(account_name):
    account_dir = ACCOUNTS_DIR / account_name
    return {
        "persona": account_dir / "persona.json",
        "pending": account_dir / "pending_replies.json",
        "replied": account_dir / "replied.json",
        "daily_count": account_dir / "daily_count.json",
        "knowledge": account_dir / "link_knowledge.json",
    }


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def is_account_active(persona):
    return persona.get("account", {}).get("status", "ACTIVE") == "ACTIVE"


def get_daily_count_today(replied, daily_count, today_key):
    """Daily count = entries di replied.json dengan timestamp hari ini.

    Source of truth = replied.json (entries with timestamp starting with today_key).
    Cek 3 field names: `time` (yang ditulis auto_post), `replied_at`, `timestamp`.

    Fallback ke daily_count.json HANYA kalau replied.json benar2 kosong
    (legacy account, gak ada entries sama sekali).
    """
    candidates = ["replied_at", "time", "timestamp"]
    today_replies = 0
    for k, v in replied.items():
        if k.startswith("_"):
            continue
        if not isinstance(v, dict):
            continue
        for field in candidates:
            ts = v.get(field, "")
            if ts and ts.startswith(today_key):
                today_replies += 1
                break

    # Fallback HANYA kalau replied.json kosong (legacy compat)
    if today_replies == 0 and not replied:
        today_replies = daily_count.get(today_key, 0)

    return today_replies


def get_last_reply_time(replied):
    """Get most recent timestamp across all entries.

    Cek 3 field names: `time` (yang ditulis auto_post), `replied_at`, `timestamp`.
    """
    latest = None
    for k, v in replied.items():
        if k.startswith("_"):
            continue
        if not isinstance(v, dict):
            continue
        ts = v.get("time") or v.get("replied_at") or v.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if latest is None or dt > latest:
                latest = dt
        except Exception:
            continue
    return latest


def find_overdue(pending, threshold_minutes):
    """Find candidates older than threshold that need processing.

    2 status yang diproses:
    - `awaiting` > threshold min → auto-approve + post (cron normal flow)
    - `auto_approved` > threshold min → retry post (kalo cron sebelumnya gagal post
      karena daily limit ngaco / Chrome error / dll). Tanpa retry ini, mereka stuck
      di auto_approved limbo.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    candidates = []
    for tid, entry in pending.get("overrides", {}).items():
        status = entry.get("status")
        if status not in ("awaiting", "auto_approved"):
            continue
        # Use notified_at for awaiting, auto_approved_at for auto_approved
        ts_field = "auto_approved_at" if status == "auto_approved" else "notified_at"
        ts = entry.get(ts_field)
        if not ts:
            continue
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts_dt < cutoff:
            minutes_old = int(
                (datetime.now(timezone.utc) - ts_dt).total_seconds() / 60
            )
            candidates.append((tid, entry, minutes_old))
    return candidates


def process_account(account_name, threshold_minutes, dry_run=False):
    paths = get_account_paths(account_name)
    if not paths["persona"].exists():
        print(f"❌ {account_name}: persona.json not found")
        return False

    persona = load_json(paths["persona"], {})
    if not is_account_active(persona):
        status = persona.get("account", {}).get("status", "?")
        print(f"⏸️ {account_name}: status {status} — skip")
        return False

    pending = load_json(paths["pending"], {"overrides": {}})
    replied = load_json(paths["replied"], {})
    daily_count = load_json(paths["daily_count"], {})
    knowledge = load_json(paths["knowledge"], {})

    # Daily limit check
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_used = get_daily_count_today(replied, daily_count, today)
    daily_limit = persona.get("limits", {}).get("daily_reply_max", 5)
    daily_remaining = max(0, daily_limit - daily_used)

    # Cooldown check
    last_reply_dt = get_last_reply_time(replied)
    cooldown_sec = persona.get("limits", {}).get("cooldown_min_seconds", 7200)
    cooldown_active = False
    cooldown_remaining_min = 0
    if last_reply_dt:
        elapsed = (datetime.now(timezone.utc) - last_reply_dt).total_seconds()
        if elapsed < cooldown_sec:
            cooldown_active = True
            cooldown_remaining_min = int((cooldown_sec - elapsed) / 60)

    # Find candidates
    candidates = find_overdue(pending, threshold_minutes)
    print(f"\n📋 {account_name}:")
    print(f"   daily: {daily_used}/{daily_limit} (remaining {daily_remaining})")
    if cooldown_active:
        print(f"   cooldown: ACTIVE ({cooldown_remaining_min} min remaining)")
    else:
        print(f"   cooldown: ok")
    print(f"   candidates (>{threshold_minutes} min): {len(candidates)}")

    if not candidates:
        return True

    # Process each
    approved_count = 0
    skipped_count = 0
    # Safety tetap: cooldown antar post (prevent X spam detection)
    # + auto_mode per kategori (kategori tertentu auto_mode=false butuh manual).
    # NO per-account daily limit, NO per-category daily limit, NO per-cycle limit.
    # Konfirmasi 2026-06-15: "Untuk autopost jangan tergantung limit, walau limit
    # tapi jika memenuhi syarat langsung jalanin aja". Cooldown (2 jam) cukup untuk
    # spacing, gak perlu hard-cap jumlah.
    last_post_in_cycle = None  # track untuk enforce cooldown minimum antar post
    for tid, entry, minutes_old in candidates:
        # [Fix 2026-06-26] Cooldown hanya untuk NEW (awaiting) entries.
        # auto_approved entries are retries — sudah nunggu >30 min, jangan di-block lagi.
        is_retry = entry.get("status") == "auto_approved"
        if cooldown_active and last_post_in_cycle is None and not is_retry:
            print(
                f"   ⏸️ {tid} @{entry.get('tweet_author', '?')}: "
                f"cooldown active, will retry in {cooldown_remaining_min} min"
            )
            skipped_count += 1
            continue

        # Indonesian-only filter: skip tweet yg bukan bahasa Indonesia
        tweet_text = entry.get("tweet_text", "")
        if not is_indonesian_tweet(tweet_text):
            print(
                f"   🌏 {tid} @{entry.get('tweet_author', '?')}: "
                f"non-Indonesian tweet, skip"
            )
            skipped_count += 1
            continue

        # Re-match category (keyword first, then semantic LLM fallback)
        match = None
        match_source = None
        if knowledge:
            match = match_category({"text": tweet_text}, knowledge)
            if match:
                match_source = "keyword"

        # [Hybrid] Semantic fallback: if no keyword match, ask LLM to pick best category
        if not match and SEMANTIC_AVAILABLE:
            try:
                acc_dir = ACCOUNTS_DIR / account_name
                sem_result = semantic_match_category(tweet_text, acc_dir, persona=persona)
                if sem_result:
                    cat_id_sem, cat_cfg_sem, _ = sem_result
                    # Build match tuple in the same shape as match_category returns
                    # match returns (cat_id, link, style_seed) or (cat_id, cat, score, priority)
                    # We need (cat_id, link, style_seed) — use first link
                    if cat_cfg_sem.get("links"):
                        link_obj = cat_cfg_sem["links"][0]
                        url = link_obj if isinstance(link_obj, str) else link_obj.get("url", "")
                        style_seed = {"template": "", "fillers": [], "category": cat_id_sem}
                        match = (cat_id_sem, url, style_seed)
                        match_source = "semantic"
            except Exception as e:
                print(f"   ⚠️ Semantic match error: {str(e)[:100]}")

        if not match:
            print(
                f"   ⏸️ {tid} @{entry.get('tweet_author', '?')}: "
                f"no category match (will leave for manual review)"
            )
            skipped_count += 1
            continue

        category_id, link, style_seed = match
        if match_source == "semantic":
            print(f"   🧠 semantic match found: {category_id}")
        cat_cfg = knowledge.get("categories", {}).get(category_id, {})
        if not cat_cfg.get("auto_mode", False):
            print(
                f"   ⏸️ {tid} @{entry.get('tweet_author', '?')}: "
                f"category {category_id} auto_mode=false, skip"
            )
            skipped_count += 1
            continue

        # === HYBRID C: LLM relevance gate (filter false positives) ===
        # Trigger mungkin match (pelumas/kondom/bikini) tapi konteks tweet beda
        # (treadmill, mesin, medis, foto vacation, dll). LLM verify SEBELUM
        # generate draft — hemat 1 LLM call per false positive + gak ada reply ngaco.
        # Disable via env: LLM_RELEVANCE_CHECK=false
        # [Multi-niche] Build niche context from persona (1x per cycle, reuse).
        niche_ctx = _build_niche_context(persona, cat_cfg)
        relevance_enabled = os.getenv("LLM_RELEVANCE_CHECK", "true").lower() == "true"
        if relevance_enabled:
            t_rel = time.monotonic()
            relevant = llm_relevance_check(
                tweet_text, category_id, cat_cfg, persona=persona, niche_ctx=niche_ctx
            )
            rel_latency = time.monotonic() - t_rel
            if relevant is False:
                print(
                    f"   🚫 {tid} @{entry.get('tweet_author', '?')}: "
                    f"NOT RELEVANT for {category_id} ({rel_latency:.1f}s)"
                )
                entry["status"] = "not_relevant"
                entry["not_relevant_at"] = datetime.now(timezone.utc).isoformat()
                entry["not_relevant_reason"] = f"LLM relevance check rejected for {category_id}"
                entry["not_relevant_category"] = category_id
                save_json(paths["pending"], pending)
                skipped_count += 1
                continue
            elif relevant is None:
                # LLM error/unavailable — allow (fallback safe)
                pass
            else:
                # relevant is True — log but continue
                pass

        # NO per-category daily limit (sesuai konfirmasi 2026-06-15).
        # Cooldown antar post (2 jam) cukup untuk X spam safety.

        # Build the final reply text
        # PRIORITAS: LLM-generated (real contextual reply) > template fill
        # Selalu regenerate (rich, link appended).
        # User punya 30 min buat manual approve / inject custom link kalo mau.
        # [Fix 5/2026-06-26] LLM is always primary. LLM_FALLBACK_TO_TEMPLATE
        # only controls whether template is used as safety net when LLM fails.
        # Default: LLM-only, skip if LLM fails (no generic template replies).
        # Opt-in via env: LLM_FALLBACK_TO_TEMPLATE=true enables template fallback.
        draft, used_llm = llm_generate_draft(
            tweet_text=entry.get("tweet_text", ""),
            tweet_author=entry.get("tweet_author", "?"),
            persona=persona,
            category_id=category_id,
            link=link,
            cat_cfg=cat_cfg,
            niche_ctx=niche_ctx,
        )
        if draft is None:
            use_template_fallback = os.getenv("LLM_FALLBACK_TO_TEMPLATE", "false").lower() == "true"
            if use_template_fallback:
                # Generate template draft as safety net
                template = style_seed.get("template", "")
                if template:
                    draft = template.replace("{link}", link or "")
            if draft is None:
                llm_word = "LLM failed & no template fallback" if used_llm is False else "LLM failed"
                print(
                    f"   ⏭️ {tid} @{entry.get('tweet_author', '?')}: "
                    f"{llm_word}, skip"
                )
                entry["status"] = "llm_failed"
                entry["llm_failed_at"] = datetime.now(timezone.utc).isoformat()
                entry["llm_failed_category"] = category_id
                save_json(paths["pending"], pending)
                skipped_count += 1
                continue
            else:
                print(
                    f"   📋 {tid} @{entry.get('tweet_author', '?')}: "
                    f"LLM failed, using template fallback"
                )
        else:
            # LLM draft tidak include link, append manual dengan separator natural.
            if link and link not in draft:
                draft = f"{draft}\n\n{link}"

        # Mark as auto-approved
        entry["status"] = "auto_approved"
        entry["auto_approved_at"] = datetime.now(timezone.utc).isoformat()
        entry["auto_category"] = category_id

        if dry_run:
            print(
                f"   [DRY] would auto-approve: {tid} @{entry.get('tweet_author', '?')} "
                f"via {category_id} ({minutes_old} min old)"
            )
            print(f"        draft: {draft[:100]}")
            # Revert
            entry["status"] = "awaiting"
            approved_count += 1
            last_post_in_cycle = datetime.now(timezone.utc)
            continue

        save_json(paths["pending"], pending)
        print(
            f"   ✅ auto-approved: {tid} @{entry.get('tweet_author', '?')} "
            f"via {category_id} ({minutes_old} min old)"
        )
        approved_count += 1
        last_post_in_cycle = datetime.now(timezone.utc)

        # Post via auto_post.py --auto
        tweet_url = entry.get("tweet_url", "")
        cmd = [
            str(AUTOMATION_DIR / "venv" / "bin" / "python3"),
            str(AUTOMATION_DIR / "scripts" / "auto_post.py"),
            "--account", account_name,
            "--auto",
            "--tweet-id", tid,
            "--tweet-url", tweet_url,
            "--text", draft,
            "--link", link or "",
            "--category", category_id,
            # [Fix 2/3] Pass original tweet text + how reply was generated
            "--original-text", entry.get("tweet_text", "")[:500],
            "--generated-by", "llm" if used_llm else "template",
        ]
        try:
            # Capture replied.json state BEFORE post to detect real new post
            replied_before = load_json(paths["replied"], {})
            before_count = sum(1 for k, v in replied_before.items()
                               if not k.startswith("_") and isinstance(v, dict))

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180
            )

            # Re-read replied.json AFTER to see if real entry was added
            replied_after = load_json(paths["replied"], {})
            after_count = sum(1 for k, v in replied_after.items()
                              if not k.startswith("_") and isinstance(v, dict))
            real_new_post = after_count > before_count

            if result.returncode == 0 and real_new_post:
                print(f"      ✅ Posted (verified via replied.json: {before_count} → {after_count})")
                approved_count += 1
                # Update style seed usage (track buat rotation)
                style_seed["used_count"] = style_seed.get("used_count", 0) + 1
                style_seed["last_used"] = datetime.now(timezone.utc).isoformat()
                # Update link performance counter (track link rotation)
                # Cari link object di knowledge categories → bump performance.posted
                link_obj_updated = None
                for _l in cat_cfg.get("links", []):
                    if _l.get("url") == link:
                        _perf = _l.setdefault("performance", {"posted": 0, "engagement": 0})
                        _perf["posted"] = _perf.get("posted", 0) + 1
                        _l["last_used"] = datetime.now(timezone.utc).isoformat()
                        link_obj_updated = _l
                        break
                if link_obj_updated:
                    print(f"      📊 Link rotation: {link[:50]}... now at {link_obj_updated['performance']['posted']} posts")
                knowledge["updated_at"] = today
                save_json(paths["knowledge"], knowledge)
                # Note: daily_count.json gak di-update (no limit enforcement)
                # Source of truth: real replied.json entries (auto-detected)
            elif result.returncode == 0 and not real_new_post:
                # auto_post.py returned success but no new entry (idempotent retry)
                print(f"      ⏭️ Skipped (already in replied.json) — counted as success but not adding daily")
                # Mark pending status=posted untuk cleanup
                entry["status"] = "posted"
                entry["posted_at"] = datetime.now(timezone.utc).isoformat()
                entry["verified_via"] = "already_in_replied_at_post_time"
                save_json(paths["pending"], pending)
            else:
                print(f"      ❌ Post failed: {result.stderr[:200] or result.stdout[:200]}")
        except subprocess.TimeoutExpired:
            print(f"      ❌ Post timeout")
        except Exception as e:
            print(f"      ❌ Error: {e}")

    print(f"   Summary: {approved_count} approved, {skipped_count} skipped")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", help="Single account to process")
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="Process all ACTIVE accounts in accounts/",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=DEFAULT_THRESHOLD_MINUTES,
        help=f"Auto-approve after N minutes (default: {DEFAULT_THRESHOLD_MINUTES})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be approved without doing it",
    )
    args = parser.parse_args()

    if not args.account and not args.all_accounts:
        print("❌ Must specify --account or --all-accounts")
        sys.exit(1)

    if args.account:
        process_account(args.account, args.minutes, dry_run=args.dry_run)
    else:
        # Loop ACTIVE accounts
        for d in sorted(ACCOUNTS_DIR.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            try:
                process_account(name, args.minutes, dry_run=args.dry_run)
            except Exception as e:
                print(f"❌ {name}: {e}")


if __name__ == "__main__":
    main()
