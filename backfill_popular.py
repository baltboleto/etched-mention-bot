#!/usr/bin/env python3
"""
Backfill the popular channel: what would it have surfaced over the last N hours?

Searches the window fresh (monitor.py's QUERIES), keeps real Etched mentions
(structural accepts; the Claude judge only sees the few that are already over
the bar), scores them with popular.py's formula, and posts everything in the
top ~5% of the window (never below an absolute floor) to the popular channel,
oldest first, each labeled as backfill.

Touches NO state: the live tracker only watches post-deploy mentions, so a
backfill can't cause double-posts and doesn't raise the live bar's damping.

Usage: python backfill_popular.py [hours=48] [--dry]
"""

import sys, time

import monitor as mon
import popular as pop

HOURS = next((float(a) for a in sys.argv[1:] if not a.startswith("-")), 48.0)
DRY = "--dry" in sys.argv
FLOOR_FINAL = 70     # popular.py's 1h floor un-scaled to near-final engagement
MAX_POSTS = 25       # don't flood the channel even if the window went nuclear

def main():
    if not mon.TWAPI_KEY:
        print("FATAL: set TWAPI_KEY", file=sys.stderr)
        sys.exit(1)
    now = int(time.time())

    # 1) gather the whole window (dedup by id)
    fetched = {}
    for q in mon.QUERIES:
        try:
            for t in mon.search(q, now - int(HOURS * 3600), max_pages=90):
                tid = str(t.get("id"))
                if tid and tid not in fetched:
                    fetched[tid] = t
        except Exception as e:
            print(f"[search error] {q!r}: {e}", flush=True)
    print(f"[backfill] {len(fetched)} candidates in the last {HOURS:.0f}h", flush=True)

    # 2) split by tier; the bar comes from THIS window's accepted mentions
    accepts, maybes = [], []
    for t in fetched.values():
        d, _ = mon.structural(t)
        if d == "accept":
            accepts.append(t)
        elif d == "maybe":
            maybes.append(t)
    scores = [pop.score_x(t) for t in accepts]
    bar = float(FLOOR_FINAL)
    if len(scores) >= pop.MIN_BASELINE:
        bar = max(bar, float(pop.percentile(scores, pop.PCTL)))
    print(f"[backfill] accepts={len(accepts)} maybes={len(maybes)} bar={bar:.0f}", flush=True)

    # 3) winners: accepts over the bar, plus judge-confirmed maybes over the bar
    winners = [t for t in accepts if pop.score_x(t) >= bar]
    judged = 0
    for t in maybes:
        if pop.score_x(t) < bar:
            continue
        v = mon.judge(t)
        judged += 1
        if v and v.get("relevant") and float(v.get("confidence", 0) or 0) >= mon.CONF_ACCEPT:
            winners.append(t)
    if len(winners) > MAX_POSTS:
        winners.sort(key=pop.score_x, reverse=True)
        print(f"[backfill] capping {len(winners)} winners -> top {MAX_POSTS} by score", flush=True)
        winners = winners[:MAX_POSTS]
    winners.sort(key=lambda t: pop.parse_created(t.get("createdAt") or ""))

    # 4) post, oldest first
    posted = 0
    for t in winners:
        a = t.get("author") or {}
        handle = a.get("userName") or "unknown"
        rec = {"author": handle,
               "followers": pop._int(a.get("followers") or a.get("followersCount")),
               "url": t.get("url") or t.get("twitterUrl") or f"https://x.com/{handle}/status/{t.get('id')}"}
        age_h = max(0.0, (now - pop.parse_created(t.get("createdAt") or "")) / 3600.0)
        s = pop.score_x(t)
        if DRY:
            print(f"  WOULD POST score={s:.0f} @{handle}: {rec['url']}", flush=True)
            continue
        try:
            fb, blocks = pop.build_popular_msg(rec, t, age_h, note=f"backfill: last {HOURS:.0f}h")
            if pop.deliver(fb, blocks):
                posted += 1
            else:
                print("  [backfill] no destination configured; stopping", flush=True)
                break
        except Exception as e:
            print(f"  [backfill slack error] {e}", flush=True)
        time.sleep(1)   # be gentle with Slack rate limits
    print(f"[backfill] posted={posted} of {len(winners)} winners (judged {judged} borderliners)", flush=True)

if __name__ == "__main__":
    main()
