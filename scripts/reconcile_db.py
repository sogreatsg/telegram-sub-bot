#!/usr/bin/env python3
"""
Automated Database & Subscription Reconcile CLI Script.
Recalculates expiration dates for active members, deduplicates referral bonuses, and restores consistency.

Usage:
  python scripts/reconcile_db.py --dry-run
  python scripts/reconcile_db.py --apply
  python scripts/reconcile_db.py --user 123456789 --apply
"""

import sys
import asyncio
import argparse
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.stdout.reconfigure(encoding='utf-8')

from bot.config import get_settings
from bot.services.database import async_session_factory, init_db, close_db
from bot.services.reconciliation import reconcile_user, reconcile_all_users
from bot.utils.time_utils import format_thai_datetime


async def main():
    parser = argparse.ArgumentParser(description="Reconcile active subscriptions and referral bonus days.")
    parser.add_argument("--dry-run", action="store_true", help="Preview calculations without writing changes to DB")
    parser.add_argument("--apply", action="store_true", help="Apply changes and commit to database")
    parser.add_argument("--user", type=int, help="Reconcile a specific Telegram User ID only")
    parser.add_argument("--all", action="store_true", help="Reconcile all users (default is all)")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        print("[INFO] No mode specified. Defaulting to --dry-run (safe preview mode).")
        args.dry_run = True

    is_commit = bool(args.apply)
    mode_str = "APPLY (WRITING TO DB)" if is_commit else "DRY-RUN (PREVIEW ONLY)"

    print(f"\n========================================================")
    print(f"  Telegram Bot Subscription & Referral Reconciler")
    print(f"  Mode: {mode_str}")
    print(f"========================================================\n")

    # Initialize DB (run pragmas and schema migrations)
    await init_db()

    async with async_session_factory() as session:
        if args.user:
            print(f"[INFO] Reconciling User ID: {args.user}...")
            res = await reconcile_user(session, args.user, commit=is_commit)
            if not res:
                print(f"[ERROR] User ID {args.user} not found.")
                return
            results = [res]
        else:
            print(f"[INFO] Scanning all users in database...")
            results = await reconcile_all_users(session, only_active=False, commit=is_commit)

    print(f"\n[SUMMARY] Scanned {len(results)} user(s).\n")
    
    changed_count = 0
    ref_fixed_count = 0
    expiry_fixed_count = 0

    for r in results:
        is_changed = r.ref_stats_changed or r.expiry_changed or r.status_changed or (r.excess_ref_grants_deleted > 0)
        if is_changed:
            changed_count += 1
            if r.ref_stats_changed or r.excess_ref_grants_deleted > 0:
                ref_fixed_count += 1
            if r.expiry_changed or r.status_changed:
                expiry_fixed_count += 1

            uname = f"@{r.username}" if r.username else f"ID: {r.user_id}"
            print(f"👤 User: {r.full_name} ({uname}) [ID: {r.user_id}]")
            print(f"   • Joined / First Active: {format_thai_datetime(r.joined_at) if r.joined_at else 'N/A'}")
            print(f"   • Breakdown: Purchase={r.purchase_days}d, Admin={r.admin_days}d, Ref={r.referral_days}d, Trial={r.trial_minutes}m -> Total={r.total_days}d {r.total_minutes}m")
            if r.ref_stats_changed or r.excess_ref_grants_deleted > 0:
                print(f"   • 🎁 Referral: Friends {r.ref_count_old}->{r.ref_count_new} people | Bonus Days {r.ref_bonus_days_old}->{r.ref_bonus_days_new}d (Pruned {r.excess_ref_grants_deleted} excess grants)")
            if r.expiry_changed or r.status_changed:
                old_exp_str = format_thai_datetime(r.expires_at_old) if r.expires_at_old else "None"
                new_exp_str = format_thai_datetime(r.expires_at_new) if r.expires_at_new else "None"
                print(f"   • ⏳ Status: {r.status_old}->{r.status_new} | Expiry: {old_exp_str} -> {new_exp_str}")
            print(f"   • 📝 Action: {r.message}")
            print("   " + "-" * 50)

    print(f"\n========================================================")
    print(f"  Total Scanned:            {len(results)}")
    print(f"  Total Users with Fixes:   {changed_count}")
    print(f"  Referral Stats Fixed:     {ref_fixed_count}")
    print(f"  Expiry Dates Adjusted:    {expiry_fixed_count}")
    print(f"========================================================")

    if not is_commit and changed_count > 0:
        print(f"\n💡 Run with --apply to commit these adjustments to the database:")
        print(f"   python scripts/reconcile_db.py --apply\n")
    elif is_commit:
        print(f"\n✅ All adjustments have been successfully committed to the database!\n")

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
