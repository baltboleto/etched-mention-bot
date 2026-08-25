#!/usr/bin/env python3
"""
Backfill / audit the popular channel: what did the last N hours' mentions
actually score, and which would the channel surface?

Coverage is deeper than the live monitor's keyword sweep: besides monitor.py's
QUERIES it also pulls QUOTE-TWEETS of @Etched's own recent posts (keyword
search structurally misses those — a viral quote of our announcement often
never says "etched" in its own text).

Every run prints a scored LEADERBOARD of the window (top 40) so a human can
audit the cut line. Posting rules: score >= max(70, p95 of the window's real
mentions), our own posts never count, at most 2 posts per author, capped at 25
total, oldest first. The Claude judge only sees over-bar borderliners.

Touches NO state, so it can't affect the live tracker.

Usage: python backfill_popular.py [hours=48] [--dry] [--skip=id1,id2,...]
       --dry   print the leaderboard and would-posts, send nothing
       --skip  tweet ids to leave out of posting (e.g. already posted earlier)
"""

import sys, time, urllib.parse

import monitor as mon
import popular as pop

HOURS = next((float(a) for a in sys.argv[1:] if not a.startswith("-")), 48.0)
DRY = "--dry" in sys.argv
SKIP_IDS = set()
for a in sys.argv[1:]:
    if a.startswith("--skip="):
        SKIP_IDS.update(x.strip() for x in a[len("--skip="):].split(",") if x.strip())

FLOOR_FINAL = 70       # popular.py's 1h floor un-scaled to near-final engagement
MAX_POSTS = 25         # don't flood the channel even if the window went nuclear
MAX_PER_AUTHOR = 2     # a viral thread is one story, not three slots
QUOTE_SWEEP_OWN = 8    # our own posts whose quote-tweets get pulled

def own_handle(t):
    return ((t.get("author") or {}).get("userName") or "").lower()

def quotes_of(tid, max_pages=3):
    out, cursor = [], ""
    for _ in range(max_pages):
        qs = urllib.parse.urlencode({"tweetId": tid, "cursor": cursor})
        d = mon._get_json(f"https://api.twitterapi.io/twitter/tweet/quotes?{qs}",
                          {"X-API-Key": mon.TWAPI_KEY})
        tws = d.get("tweets") or []
        out.extend(tws)
        if not d.get("has_next_page"):
            break
        cursor = d.get("next_cursor") or ""
        if not cursor:
            break
    return out

def main():
    if not mon.TWAPI_KEY:
        print("FATAL: set TWAPI_KEY", file=sys.stderr)
        sys.exit(1)
    now = int(time.time())
    since = now - int(HOURS * 3600)

    # 1) keyword sweep (dedup by id)
    fetched = {}
    for q in mon.QUERIES:
        try:
            for t in mon.search(q, since, max_pages=90):
                tid = str(t.get("id"))
                if tid and tid not in fetched:
                    fetched[tid] = t
        except Exception as e:
            print(f"[search error] {q!r}: {e}", flush=True)
    print(f"[backfill] {len(fetched)} keyword candidates in the last {HOURS:.0f}h", flush=True)

    # 1b) structural sweep: quote-tweets of our own recent posts
    own = sorted((t for t in fetched.values() if own_handle(t) in mon.HANDLES),
                 key=pop.score_x, reverse=True)[:QUOTE_SWEEP_OWN]
    qt_new = 0
    for t in own:
        try:
            for q in quotes_of(str(t.get("id"))):
                qid = str(q.get("id"))
                if qid and qid not in fetched and own_handle(q) not in mon.HANDLES \
                        and pop.parse_created(q.get("createdAt") or "") >= since:
                    q["_quotes_us"] = True          # quotes our post -> a mention by construction
                    fetched[qid] = q
                    qt_new += 1
        except Exception as e:
            print(f"[quote sweep error] {t.get('id')}: {e}", flush=True)
    print(f"[backfill] +{qt_new} quote-tweets of our own posts ({len(own)} posts swept)", flush=True)

    # 2) tiers; the bar comes from THIS window's real mentions (own posts excluded)
    accepts, maybes = [], []
    for t in fetched.values():
        if own_handle(t) in mon.HANDLES:
            continue
        if t.get("_quotes_us"):
            accepts.append(t)
            continue
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

    # 3) winners: accepts over the bar + judge-confirmed maybes over the bar
    winners = [t for t in accepts if pop.score_x(t) >= bar]
    judged = 0
    judged_in = {}
    for t in maybes:
        if pop.score_x(t) < bar:
            continue
        v = mon.judge(t)
        judged += 1
        if v and v.get("relevant") and float(v.get("confidence", 0) or 0) >= mon.CONF_ACCEPT:
            winners.append(t)
            judged_in[str(t.get("id"))] = True
    winners.sort(key=pop.score_x, reverse=True)
    per_author, capped, dropped_author = {}, [], 0
    for t in winners:
        h = own_handle(t)
        if per_author.get(h, 0) >= MAX_PER_AUTHOR:
            dropped_author += 1
            continue
        per_author[h] = per_author.get(h, 0) + 1
        capped.append(t)
    winners = capped
    if dropped_author:
        print(f"[backfill] {dropped_author} dropped by the {MAX_PER_AUTHOR}/author cap", flush=True)
    if len(winners) > MAX_POSTS:
        print(f"[backfill] capping {len(winners)} winners -> top {MAX_POSTS} by score", flush=True)
        winners = winners[:MAX_POSTS]
    winner_ids = {str(t.get("id")) for t in winners}

    # 4) the leaderboard — the audit trail for where the cut line landed
    ranked = sorted(accepts + maybes, key=pop.score_x, reverse=True)[:40]
    print(f"\n{'':2} {'score':>6} {'❤':>6} {'RT':>5} {'👁':>7} {'foll':>7} {'age':>5}  tweet", flush=True)
    for t in ranked:
        tid = str(t.get("id"))
        a = t.get("author") or {}
        mark = "✓" if tid in winner_ids else (" " if pop.score_x(t) < bar else "·")
        tier = "Q" if t.get("_quotes_us") else ("A" if t in accepts else "B")
        if tier == "B" and pop.score_x(t) >= bar and tid not in judged_in and tid not in winner_ids:
            tier = "b"      # judge said not-us (or judge unavailable)
        age_h = max(0.0, (now - pop.parse_created(t.get("createdAt") or "")) / 3600.0)
        txt = " ".join((t.get("text") or "").split())[:70]
        print(f"{mark:2} {pop.score_x(t):>6.0f} {pop._int(t.get('likeCount')):>6} "
              f"{pop._int(t.get('retweetCount')):>5} {pop._int(t.get('viewCount')):>7} "
              f"{pop._int(a.get('followers')):>7} {pop.fmt_age(age_h):>5} "
              f"[{tier}] @{a.get('userName')}: {txt}", flush=True)
    print("", flush=True)

    # 5) post, oldest first
    winners.sort(key=lambda t: pop.parse_created(t.get("createdAt") or ""))
    posted = 0
    for t in winners:
        tid = str(t.get("id"))
        a = t.get("author") or {}
        handle = a.get("userName") or "unknown"
        if tid in SKIP_IDS:
            print(f"  SKIP (already posted) @{handle}: {tid}", flush=True)
            continue
        rec = {"author": handle,
               "followers": pop._int(a.get("followers") or a.get("followersCount")),
               "url": t.get("url") or t.get("twitterUrl") or f"https://x.com/{handle}/status/{tid}"}
        age_h = max(0.0, (now - pop.parse_created(t.get("createdAt") or "")) / 3600.0)
        if DRY:
            print(f"  WOULD POST score={pop.score_x(t):.0f} @{handle}: {rec['url']}", flush=True)
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
        time.sleep(1)
    print(f"[backfill] posted={posted} of {len(winners)} winners "
          f"(judged {judged} borderliners, bar {bar:.0f})", flush=True)

if __name__ == "__main__":
    main()
