#!/usr/bin/env python3
"""
link_categorizer.py — Auto-categorize product links into link_knowledge.json

Usage:
    python3 link_categorizer.py [--dry-run] [--interactive] [--batch FILE] [URL] [PRODUCT_NAME]

Examples:
    python3 link_categorizer.py "https://shopee.co.id/xyz" "Kondom Ultra Tipis"
    python3 link_categorizer.py --dry-run "https://..." "Hijab Premium"
    python3 link_categorizer.py --batch links.txt
    python3 link_categorizer.py --interactive
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============== PATHS ==============
AUTOMATION_DIR = Path.home() / "automation"
THREADS_DIR = Path.home() / "threads-automation"
X_ACCOUNTS_DIR = AUTOMATION_DIR / "accounts"
THREADS_ACCOUNTS_DIR = THREADS_DIR / "accounts"

# ============== HELPERS ==============
def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path: Path, data: dict, backup: bool = True):
    if backup and path.exists():
        backup_path = path.with_suffix(f".json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        path.rename(backup_path)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def find_all_link_knowledge() -> List[Tuple[str, Path, dict]]:
    """Return list of (account_type, account_name, path, data) for all link_knowledge.json"""
    results = []
    
    # X accounts
    if X_ACCOUNTS_DIR.exists():
        for acc_dir in X_ACCOUNTS_DIR.iterdir():
            if not acc_dir.is_dir():
                continue
            kb_path = acc_dir / "link_knowledge.json"
            if kb_path.exists():
                results.append(("x_affiliate", acc_dir.name, kb_path, load_json(kb_path)))
    
    # Threads accounts
    if THREADS_ACCOUNTS_DIR.exists():
        for acc_dir in THREADS_ACCOUNTS_DIR.iterdir():
            if not acc_dir.is_dir():
                continue
            kb_path = acc_dir / "link_knowledge.json"
            if kb_path.exists():
                results.append(("threads_storyteller", acc_dir.name, kb_path, load_json(kb_path)))
    
    return results

def normalize_text(text: str) -> str:
    """Normalize for matching: lowercase, remove special chars"""
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def word_boundary_match(text: str, trigger: str) -> bool:
    """Check if trigger appears as whole word in text"""
    pattern = r'\b' + re.escape(trigger.lower()) + r'\b'
    return bool(re.search(pattern, text.lower()))

def score_category(product_name: str, category: dict) -> int:
    """Score how well product matches category (higher = better match)"""
    score = 0
    text = normalize_text(product_name)
    
    # Triggers (weight 2)
    for trigger in category.get("triggers", []):
        if word_boundary_match(text, trigger):
            score += 2
    
    # Exclude triggers (hard block)
    for exclude in category.get("exclude_triggers", []):
        if word_boundary_match(text, exclude):
            return -999
    
    # Context signals (weight 1)
    for signal in category.get("context_signals", []):
        if word_boundary_match(text, signal):
            score += 1
    
    # Priority bonus
    priority = category.get("priority", 1)
    score += priority
    
    return score

def match_product(product_name: str, kb_data: dict) -> List[Tuple[str, dict, int]]:
    """Return list of (category_id, category_data, score) sorted by score desc"""
    matches = []
    categories = kb_data.get("categories", {})
    
    for cat_id, cat_data in categories.items():
        score = score_category(product_name, cat_data)
        if score > 0:
            matches.append((cat_id, cat_data, score))
    
    matches.sort(key=lambda x: x[1].get("priority", 1), reverse=True)  # priority first
    matches.sort(key=lambda x: x[2], reverse=True)  # then score
    return matches

def find_best_match(product_name: str) -> List[Tuple[str, str, Path, str, dict, int]]:
    """Search ALL knowledge bases, return best matches across accounts"""
    all_kbs = find_all_link_knowledge()
    results = []
    
    for acc_type, acc_name, kb_path, kb_data in all_kbs:
        matches = match_product(product_name, kb_data)
        for cat_id, cat_data, score in matches:
            results.append((acc_type, acc_name, kb_path, cat_id, cat_data, score))
    
    # Sort by score desc
    results.sort(key=lambda x: x[5], reverse=True)
    return results

def inject_link(kb_path: Path, cat_id: str, url: str, product_name: str, platform: str = "") -> bool:
    """Add link to existing category"""
    kb_data = load_json(kb_path)
    categories = kb_data.setdefault("categories", {})
    
    if cat_id not in categories:
        return False
    
    cat = categories[cat_id]
    links = cat.setdefault("links", [])
    
    # Check duplicate
    for link in links:
        if link.get("url") == url:
            print(f"  ⚠️  Link already exists in {cat_id}")
            return False
    
    # Add link
    new_link = {
        "url": url,
        "platform": platform or "unknown",
        "added_at": datetime.now().strftime("%Y-%m-%d"),
        "product_name": product_name
    }
    links.append(new_link)
    
    save_json(kb_path, kb_data)
    print(f"  ✅ Injected into {kb_path.parent.name}/{cat_id}")
    return True

def create_new_category(kb_path: Path, cat_id: str, cat_name: str, url: str, product_name: str, triggers: List[str]) -> bool:
    """Create new category with first link"""
    kb_data = load_json(kb_path)
    categories = kb_data.setdefault("categories", {})
    
    if cat_id in categories:
        return inject_link(kb_path, cat_id, url, product_name)
    
    new_cat = {
        "category": cat_name,
        "subcategory": "",
        "links": [{
            "url": url,
            "platform": "unknown",
            "added_at": datetime.now().strftime("%Y-%m-%d"),
            "product_name": product_name
        }],
        "triggers": triggers,
        "exclude_triggers": [],
        "context_signals": [],
        "style_seeds": [],
        "priority": 2,
        "auto_mode": True
    }
    
    categories[cat_id] = new_cat
    save_json(kb_path, kb_data)
    print(f"  ✅ Created new category {cat_id} in {kb_path.parent.name}")
    return True

# ============== MAIN ==============
def process_link(url: str, product_name: str, dry_run: bool = False) -> bool:
    print(f"\n🔍 Processing: {product_name}")
    print(f"   URL: {url}")
    
    # Find matches
    matches = find_best_match(product_name)
    
    if not matches:
        print("  ❌ No matching category found in ANY account")
        return False
    
    # Show top matches
    print(f"\n  Top matches:")
    for i, (acc_type, acc_name, kb_path, cat_id, cat_data, score) in enumerate(matches[:5]):
        icon = "🎯" if i == 0 else "  "
        print(f"  {icon} [{acc_type}] {acc_name} → {cat_id} (score: {score})")
        print(f"      Triggers: {cat_data.get('triggers', [])[:5]}")
    
    best = matches[0]
    acc_type, acc_name, kb_path, cat_id, cat_data, score = best
    
    # Auto-match threshold
    if score >= 6:
        print(f"\n  ✅ AUTO-MATCH (score {score} ≥ 6): {acc_name}/{cat_id}")
        if not dry_run:
            inject_link(kb_path, cat_id, url, product_name)
        return True
    
    # Need confirmation
    print(f"\n  ⚠️  Low confidence (score {score}). Options:")
    print(f"  1) Confirm match → {acc_name}/{cat_id}")
    print(f"  2) Pick different account/category")
    print(f"  3) Create new category")
    print(f"  4) Skip")
    
    if dry_run:
        print("  🔍 Dry-run: would ask for confirmation")
        return False
    
    choice = input("  Choice [1-4]: ").strip()
    
    if choice == "1":
        inject_link(kb_path, cat_id, url, product_name)
        return True
    elif choice == "2":
        # Show all accounts
        all_kbs = find_all_link_knowledge()
        print("\n  Available accounts:")
        for i, (at, an, kp, kd) in enumerate(all_kbs):
            cats = list(kd.get("categories", {}).keys())
            print(f"  {i+1}) [{at}] {an} — categories: {', '.join(cats[:5])}{'...' if len(cats)>5 else ''}")
        
        acc_idx = int(input("  Account number: ")) - 1
        if 0 <= acc_idx < len(all_kbs):
            _, _, target_kb, target_kb_data = all_kbs[acc_idx]
            target_cats = list(target_kb_data.get("categories", {}).keys())
            print(f"  Categories: {', '.join(target_cats)}")
            cat_choice = input("  Category ID (or 'new'): ").strip()
            
            if cat_choice == "new":
                new_cat_id = input("  New category ID (snake_case): ").strip()
                new_cat_name = input("  Category name: ").strip()
                new_triggers = input("  Triggers (comma-separated): ").strip().split(",")
                create_new_category(target_kb, new_cat_id, new_cat_name, url, product_name, [t.strip() for t in new_triggers])
            elif cat_choice in target_cats:
                inject_link(target_kb, cat_choice, url, product_name)
        return True
    elif choice == "3":
        new_cat_id = input("  New category ID (snake_case): ").strip()
        new_cat_name = input("  Category name: ").strip()
        new_triggers = input("  Triggers (comma-separated): ").strip().split(",")
        create_new_category(kb_path, new_cat_id, new_cat_name, url, product_name, [t.strip() for t in new_triggers])
        return True
    
    return False

def interactive_mode():
    print("🔗 Link Categorizer — Interactive Mode")
    print("Format: URL | Product Name (or just URL, will prompt for name)")
    print("Type 'quit' to exit\n")
    
    while True:
        try:
            line = input("🔗 > ").strip()
            if line.lower() in ('quit', 'exit', 'q'):
                break
            if not line:
                continue
            
            if '|' in line:
                url, name = line.split('|', 1)
                url, name = url.strip(), name.strip()
            else:
                url = line
                name = input("  Product name: ").strip()
            
            if url and name:
                process_link(url, name)
        except KeyboardInterrupt:
            break
        except EOFError:
            break
    
    print("\n👋 Bye!")

def batch_mode(batch_file: Path, dry_run: bool):
    if not batch_file.exists():
        print(f"❌ File not found: {batch_file}")
        return
    
    with open(batch_file) as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    
    print(f"📦 Batch processing {len(lines)} links...")
    for line in lines:
        if '|' in line:
            url, name = line.split('|', 1)
            process_link(url.strip(), name.strip(), dry_run)
        else:
            print(f"  ⚠️  Skipping invalid line: {line}")

def main():
    parser = argparse.ArgumentParser(description="Auto-categorize product links into link_knowledge.json")
    parser.add_argument("url", nargs="?", help="Product URL")
    parser.add_argument("product_name", nargs="?", help="Product name")
    parser.add_argument("--dry-run", action="store_true", help="Show matches only, don't modify")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--batch", "-b", help="Batch file (one per line: URL | Product Name)")
    
    args = parser.parse_args()
    
    if args.interactive:
        interactive_mode()
    elif args.batch:
        batch_mode(Path(args.batch), args.dry_run)
    elif args.url and args.product_name:
        process_link(args.url, args.product_name, args.dry_run)
    elif args.url and not args.product_name:
        name = input("Product name: ").strip()
        if name:
            process_link(args.url, name, args.dry_run)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()