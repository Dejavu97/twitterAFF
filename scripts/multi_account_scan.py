#!/usr/bin/env python3
"""
multi_account_scan.py — Orchestrator: scan + auto-post for all ACTIVE accounts.

Runs auto_reply.py sequentially for each ACTIVE account in accounts/.
Per-account isolation:
- Chrome profile per akun (port from persona.fingerprint.chrome_port)
- Daily counters per akun
- Knowledge base per akun
- Telegram notif per akun (or aggregated)

Usage:
    python3 scripts/multi_account_scan.py              # scan all ACTIVE accounts
    python3 scripts/multi_account_scan.py --dry-run    # scan but don't auto-post
    python3 scripts/multi_account_scan.py --only akun2_dawnlingchild
    python3 scripts/multi_account_scan.py --skip akun1_nunani

Output:
    Per-account summary
    Aggregated summary (sent to Telegram)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AUTOMATION_DIR = Path(__file__).parent.parent
ACCOUNTS_DIR = AUTOMATION_DIR / "accounts"
SCRIPT_DIR = AUTOMATION_DIR / "scripts"


def discover_accounts():
    """Discover all accounts and their status. Returns list of dicts."""
    accounts = []
    if not ACCOUNTS_DIR.exists():
        return accounts
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

        acc_status = persona.get("account", {}).get("status", "UNKNOWN")
        fp = persona.get("fingerprint", {})
        accounts.append({
            "name": acc_dir.name,
            "dir": acc_dir,
            "persona": persona,
            "status": acc_status,
            "handle": persona.get("account", {}).get("handle", "?"),
            "niche": persona.get("niche", {}).get("primary", "?"),
            "chrome_port": fp.get("chrome_port", "?"),
            "daily_max": persona.get("limits", {}).get("daily_reply_max", 0),
            "auto_mode": (acc_dir / "link_knowledge.json").exists(),
        })
    return accounts


def run_account_scan(account, dry_run=False, verbose=False, from_cache=None):
    """Run auto_reply.py for one account. Returns dict with result.

    from_cache: optional path to viral_research cache file. If provided,
                auto_reply.py uses --from-cache instead of doing its own
                X.com scan. Used in grouped mode.
    """
    name = account["name"]
    # `-u` harus python flag (BEFORE script), bukan script arg
    py_flags = ["-u"] if verbose else []
    cmd = [
        str(AUTOMATION_DIR / "venv" / "bin" / "python3"),
        *py_flags,
        str(SCRIPT_DIR / "auto_reply.py"),
        "--account", name,
    ]
    if dry_run:
        cmd.append("--dry-run")
    if from_cache:
        cmd.extend(["--from-cache", from_cache])

    print(f"\n{'='*60}")
    print(f"🤖 [{name}] @ {account['handle']} | port {account['chrome_port']} | {account['niche']}")
    print(f"   Status: {account['status']} | Daily max: {account['daily_max']}")
    print(f"{'='*60}")

    started = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=600,  # 10 min per account
        )
        elapsed = time.time() - started
        return {
            "name": name,
            "handle": account["handle"],
            "exit_code": result.returncode,
            "elapsed_seconds": round(elapsed, 1),
            "stdout_tail": result.stdout[-2000:] if result.stdout else "",
            "stderr_tail": result.stderr[-500:] if result.stderr else "",
            "success": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "handle": account["handle"],
            "exit_code": -1,
            "elapsed_seconds": time.time() - started,
            "stdout_tail": "(timed out)",
            "stderr_tail": "",
            "success": False,
            "error": "timeout_600s",
        }
    except Exception as e:
        return {
            "name": name,
            "handle": account["handle"],
            "exit_code": -1,
            "elapsed_seconds": time.time() - started,
            "stdout_tail": "",
            "stderr_tail": str(e),
            "success": False,
            "error": str(e),
        }


def extract_stats(result):
    """Extract key stats from a scan result (auto_reply.py output)."""
    out = result.get("stdout_tail", "")
    stats = {
        "candidates_found": 0,
        "auto_posted": 0,
        "auto_skipped": 0,
        "pending_saved": 0,
    }
    # Parse output for key lines
    for line in out.splitlines():
        if "Total unique new candidates:" in line:
            try:
                stats["candidates_found"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        if "Auto-posted:" in line and "✅" in line:
            # "⚡ Auto-posted: 3" format
            try:
                stats["auto_posted"] += 1
            except ValueError:
                pass
        elif "Auto-posted: " in line:
            try:
                stats["auto_posted"] = int(line.split("Auto-posted:")[-1].strip())
            except ValueError:
                pass
        if "Auto-skipped:" in line and "⏭️" in line:
            try:
                stats["auto_skipped"] = int(line.split("Auto-skipped:")[-1].strip())
            except ValueError:
                pass
        if "Saved " in line and "to pending" in line:
            try:
                n = int(line.split("Saved ")[1].split(" ")[0])
                stats["pending_saved"] = n
            except (ValueError, IndexError):
                pass
    return stats


def send_telegram_summary(results):
    """Send aggregated summary to Telegram via send_to_role.py subprocess.

    Cron env (shell) doesn't have `hermes_tools` module — it's an MCP server
    that only exists in the agent runtime. Standard pattern across stack:
    shell-side scripts use `send_to_role.py` (subprocess) instead.
    """
    send_to_role = Path.home() / ".hermes/skills/autonomous-ai-agents/affiliate-agent-ecosystem/scripts/send_to_role.py"
    if not send_to_role.exists():
        print(f"⚠️ send_to_role.py not found at {send_to_role}")
        return

    lines = ["📊 *Multi-Account Scan Report*\n"]
    for r in results:
        name = r["name"]
        handle = r.get("handle", "?")
        stats = r.get("stats", {})
        status = "✅" if r["success"] else "❌"
        lines.append(
            f"{status} *{name}* (@{handle})\n"
            f"   candidates: {stats.get('candidates_found', 0)} | "
            f"auto-posted: {stats.get('auto_posted', 0)} | "
            f"pending: {stats.get('pending_saved', 0)} | "
            f"skipped: {stats.get('auto_skipped', 0)}\n"
            f"   ⏱ {r['elapsed_seconds']}s"
        )

    try:
        proc = subprocess.run(
            ["/home/anggar221/automation/venv/bin/python3", str(send_to_role), "general", "\n".join(lines)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            print(f"✅ Telegram summary sent (topic=1, general)")
        else:
            print(f"⚠️ send_to_role failed: {proc.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired:
        print(f"⚠️ send_to_role timeout (>30s)")
    except Exception as e:
        print(f"⚠️ Telegram summary failed: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't auto-post, just scan and save to pending")
    parser.add_argument("--only", help="Only run this account (by name)")
    parser.add_argument("--skip", help="Skip this account (by name)")
    parser.add_argument("--include-paused", action="store_true",
                        help="Include PAUSED accounts in the loop (not recommended)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Stream auto_reply output in real-time")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram summary")
    parser.add_argument(
        "--grouped",
        action="store_true",
        help="Use niche-grouped research: 1 viral_research run per niche_group, "
             "then auto_reply consumes from cache. Reduces search volume by ~50% "
             "and ensures grouped accounts never post to the same tweet via interleave. "
             "Requires viral_research_grouped.py to be runnable.",
    )
    args = parser.parse_args()

    # Discover accounts
    accounts = discover_accounts()
    print(f"📂 Discovered {len(accounts)} accounts:")
    for a in accounts:
        print(f"   - {a['name']} | @{a['handle']} | status={a['status']} | port={a['chrome_port']} | auto_mode={a['auto_mode']}")

    # Filter
    if args.only:
        accounts = [a for a in accounts if a["name"] == args.only]
        if not accounts:
            print(f"❌ Account '{args.only}' not found")
            sys.exit(1)
    if args.skip:
        accounts = [a for a in accounts if a["name"] != args.skip]
    if not args.include_paused:
        before = len(accounts)
        accounts = [a for a in accounts if a["status"] not in ("PAUSED", "DRAFT", "UNKNOWN")]
        skipped = before - len(accounts)
        if skipped:
            print(f"⏭️  Skipped {skipped} non-ACTIVE accounts")

    if not accounts:
        print("❌ No ACTIVE accounts to process")
        sys.exit(0)

    # Group accounts by niche_group for grouped mode
    grouped_accounts = {}  # niche_group -> [accounts]
    solo_accounts = []     # accounts without niche_group
    for a in accounts:
        ng = a["persona"].get("niche_group")
        if ng:
            grouped_accounts.setdefault(ng, []).append(a)
        else:
            solo_accounts.append(a)

    if args.grouped:
        # ====== GROUPED MODE: 1 research per niche_group, then from-cache per account ======
        print(f"\n{'='*60}")
        print(f"🔬 GROUPED MODE: research {len(grouped_accounts)} group(s) + {len(solo_accounts)} solo")
        print(f"{'='*60}")
        for g, accs in grouped_accounts.items():
            print(f"   - {g}: {len(accs)} accounts ({', '.join(a['handle'] for a in accs)})")
        for s in solo_accounts:
            print(f"   - solo: {s['handle']} ({s['name']})")

        # Step 1: run viral_research_grouped for each group
        cache_paths = {}  # niche_group -> latest.json path
        for group_name in grouped_accounts:
            print(f"\n{'='*60}")
            print(f"🔬 Research group: {group_name}")
            print(f"{'='*60}")
            try:
                result = subprocess.run(
                    [
                        str(AUTOMATION_DIR / "venv" / "bin" / "python3"),
                        str(SCRIPT_DIR / "viral_research_grouped.py"),
                        "--only", group_name,
                        "--count", "5",
                    ],
                    capture_output=True, text=True, timeout=900,  # 15 min per group
                )
                if result.returncode == 0:
                    # latest.json path: data/viral_research/{group_name}/latest.json
                    cache_path = f"data/viral_research/{group_name}/latest.json"
                    cache_paths[group_name] = cache_path
                    print(f"   ✅ Cache written: {cache_path}")
                else:
                    print(f"   ⚠️ Research failed for {group_name} (exit={result.returncode})")
                    print(f"      stderr: {result.stderr[-500:]}")
            except subprocess.TimeoutExpired:
                print(f"   ⚠️ Research timeout for {group_name}")

        # Step 2: run auto_reply per account with --from-cache
        print(f"\n{'='*60}")
        print(f"🚀 Auto-reply phase (with from-cache)")
        print(f"{'='*60}")
        results = []
        for acc in accounts:
            ng = acc["persona"].get("niche_group")
            from_cache = cache_paths.get(ng) if ng else None
            result = run_account_scan(
                acc, dry_run=args.dry_run, verbose=args.verbose,
                from_cache=from_cache,
            )
            result["stats"] = extract_stats(result)
            results.append(result)

            stats = result["stats"]
            print(f"\n📊 [{acc['name']}] Result: "
                  f"candidates={stats['candidates_found']}, "
                  f"auto_posted={stats['auto_posted']}, "
                  f"pending={stats['pending_saved']}, "
                  f"skipped={stats['auto_skipped']}, "
                  f"exit={result['exit_code']}, "
                  f"⏱ {result['elapsed_seconds']}s")
    else:
        # ====== LEGACY MODE: per-account scan ======
        print(f"\n🚀 Running {len(accounts)} accounts sequentially...")
        results = []
        for acc in accounts:
            result = run_account_scan(acc, dry_run=args.dry_run, verbose=args.verbose)
            result["stats"] = extract_stats(result)
            results.append(result)

            # Print summary
            stats = result["stats"]
            print(f"\n📊 [{acc['name']}] Result: "
                  f"candidates={stats['candidates_found']}, "
                  f"auto_posted={stats['auto_posted']}, "
                  f"pending={stats['pending_saved']}, "
                  f"skipped={stats['auto_skipped']}, "
                  f"exit={result['exit_code']}, "
                  f"⏱ {result['elapsed_seconds']}s")

    # Aggregated summary
    total_candidates = sum(r["stats"]["candidates_found"] for r in results)
    total_auto_posted = sum(r["stats"]["auto_posted"] for r in results)
    total_pending = sum(r["stats"]["pending_saved"] for r in results)
    total_elapsed = sum(r["elapsed_seconds"] for r in results)

    print(f"\n{'='*60}")
    print(f"📊 AGGREGATED SUMMARY")
    print(f"   Accounts run: {len(results)}")
    print(f"   Total candidates: {total_candidates}")
    print(f"   Total auto-posted: {total_auto_posted}")
    print(f"   Total pending (manual approve): {total_pending}")
    print(f"   Total time: {total_elapsed:.1f}s")
    print(f"{'='*60}")

    if not args.no_telegram:
        send_telegram_summary(results)

    # Exit code: 0 if all success, 1 if any failed
    sys.exit(0 if all(r["success"] for r in results) else 1)


if __name__ == "__main__":
    main()
