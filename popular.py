#!/usr/bin/env python3
"""
Etched popular-mention tracker (X/Twitter)
==========================================
Watches every mention monitor.py posts to the main channel and re-posts the few
that gain REAL traction to #twitter-mentions-popular.

How "popular" is decided (three parts, no magic):

  score        likes + 2*(retweets+quotes) + replies + bookmarks. Views are
               excluded on purpose: they're bot-inflated and follower-driven;
               amplification and engagement are what we're after.
  one look     each watched post is re-fetched ONCE, ~1h after it was posted
               (one $0.00015 fetch per mention). A post that's already hot at
               discovery is checked immediately instead. If a workflow outage
               makes us miss the ~1h window, the post is skipped, not measured
               stale.
  the bar      the ~1h score must beat
                   max( absolute floor,
                        95th percentile of what Etched mentions historically
                        scored at that age )
               The floor stops a quiet week from promoting a 10-like post; the
               percentile keeps "top ~5%" honest as baseline volume shifts.
               Every promotion in the trailing 24h raises the bar 15%
               (compounding, capped at 8x), so a launch-day wave stays a
               trickle instead of flooding the channel. Authors with >=100k
               followers qualify at half the bar — a big account talking about
               us is traction even before the likes pile up.

Cold start: the first run seeds the percentile baseline from history. It
re-fetches the newest candidate ids in monitor's state.json (one-time, ~$0.30),
keeps the structural accepts, and back-scales their near-final scores to ~1h
age with a standard engagement-accrual factor.

Each post is measured once, promoted at most once, and forgotten after a few
hours. --dry prints would-be promotions and saves no state (a later real run
redoes the same checks).

Data source : twitterapi.io  /twitter/tweets  (batch of 50, $0.00015/tweet;
              steady state is a few dollars a month)
Delivery    : SLACK_POPULAR_WEBHOOK_URL webhook, or SLACK_BOT_TOKEN +
              SLACK_POPULAR_CHANNEL via chat.postMessage (invite the bot!)
State       : popular_state.json (committed by the workflow, like state.json)

Runs right after monitor.py in the same job. monitor.py appends its accepts to
the watchlist via watch_tweets(); this script re-checks and promotes.
"""

import json, os, sys, time, datetime, urllib.parse

import monitor as mon   # shared http helpers, slack senders, translate, fmt

STATE_FILE = os.path.join(os.path.dirname(__file__), "popular_state.json")

SLACK_POPULAR_WEBHOOK = os.environ.get("SLACK_POPULAR_WEBHOOK_URL", "")
# default: #twitter-mentions-popular (channel ids aren't credentials; the bot
# token is the secret, and it only works in channels the bot was invited to)
SLACK_POPULAR_CHANNEL = os.environ.get("SLACK_POPULAR_CHANNEL", "") or "C0BRL1RT7B9"

CHECKPOINTS = [1]         # measure each mention ONCE, ~1h after posting
FLOORS = {1: 25}
CHECK_BY = {1: 2.5}       # missed the measuring window (outage)? skip it, don't skew
PCTL          = 95        # "top 5%"
MIN_BASELINE  = 20        # samples before the percentile can outrank the floor
BASELINE_KEEP = 400       # per checkpoint (weeks of history at current volume)
BASELINE_MAX_AGE = 60 * 86400
DAMP_STEP = 1.15          # bar multiplier per promotion in the last 24h
DAMP_CAP  = 8.0
BIG_FOLLOWERS = 100_000   # accounts this big qualify at BIG_FACTOR * bar
BIG_FACTOR    = 0.5
RETIRE_H  = 3
WATCH_CAP = 1500          # sanity cap on the watchlist; oldest dropped first
BOOTSTRAP_IDS = 2000      # newest candidate ids refetched to seed the baseline

# X engagement accrues fast then flattens; a post has roughly this share of its
# final score at 1h. Used ONLY to back-scale near-final bootstrap data into the
# 1h seed — live measurements replace it organically.
ACCRUAL = {1: 0.35}

# ------------------------------------------------------------------ state
def empty_state():
    return {"watch": {}, "baseline": {}, "promoted": [], "seeded": False}

def load_state(path=None):
    try:
        with open(path or STATE_FILE) as f:
            s = json.load(f)
        for k, v in empty_state().items():
            s.setdefault(k, v)
        return s
    except Exception:
        return empty_state()

def save_state(state, path=None):
    now = int(time.time())
    for cp, rows in state["baseline"].items():
        state["baseline"][cp] = [r for r in rows if now - r[0] <= BASELINE_MAX_AGE][-BASELINE_KEEP:]
    del state["promoted"][:-500]
    with open(path or STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)

# ------------------------------------------------------------------ scoring
def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0

def score_x(t):
    return (_int(t.get("likeCount")) + 2 * (_int(t.get("retweetCount")) + _int(t.get("quoteCount")))
            + _int(t.get("replyCount")) + _int(t.get("bookmarkCount")))

CREATED_FMT = "%a %b %d %H:%M:%S %z %Y"   # "Tue Dec 10 07:00:30 +0000 2024"

def parse_created(s):
    try:
        return int(datetime.datetime.strptime(s, CREATED_FMT).timestamp())
    except Exception:
        return 0

# ------------------------------------------------------------------ watchlist
def watch_tweets(tweets, path=None):
    """monitor.py calls this right after posting accepts to main: start
    watching them. Safe against re-adds; never raises past the caller's log."""
    if not tweets:
        return 0
    now = int(time.time())
    state = load_state(path)
    added = 0
    for t in tweets:
        tid = str(t.get("id") or "")
        created = parse_created(t.get("createdAt") or "")
        if not tid or not created or tid in state["watch"]:
            continue
        a = t.get("author") or {}
        handle = a.get("userName") or "unknown"
        if handle.lower() in mon.HANDLES:
            continue        # our own posts aren't mentions; main can carry them, popular shouldn't
        state["watch"][tid] = {
            "created": created,
            "author": handle,
            "followers": _int(a.get("followers") or a.get("followersCount")),
            "url": t.get("url") or t.get("twitterUrl") or f"https://x.com/{handle}/status/{tid}",
            "disc_score": score_x(t),
            "checks": {}, "promoted": False, "miss": 0, "added": now,
        }
        added += 1
    if len(state["watch"]) > WATCH_CAP:
        for tid in sorted(state["watch"], key=lambda k: state["watch"][k]["created"])[:len(state["watch"]) - WATCH_CAP]:
            del state["watch"][tid]
    save_state(state, path)
    return added

# ------------------------------------------------------------------ the bar
def percentile(vals, p):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round((p / 100.0) * (len(vals) - 1))))]

def bar(state, cp, floors, now):
    """max(floor, p95-of-history-at-this-age), raised while we're promoting a
    lot (each promotion in the last 24h compounds the bar by DAMP_STEP)."""
    samples = [s for ts, s in state["baseline"].get(str(cp), []) if now - ts <= BASELINE_MAX_AGE]
    base = float(floors[cp])
    if len(samples) >= MIN_BASELINE:
        base = max(base, float(percentile(samples, PCTL)))
    recent = sum(1 for row in state["promoted"] if now - row[0] <= 86400)
    return base * min(DAMP_CAP, DAMP_STEP ** recent)

def record_baseline(state, cp, score, now):
    state["baseline"].setdefault(str(cp), []).append([now, score])

def due_cp(rec, age_h, checkpoints):
    """Largest checkpoint this post has aged past but not been measured at.
    (The caller marks the smaller missed ones as skipped, so a post that
    entered late isn't fetched once per missed checkpoint.)"""
    due = [cp for cp in checkpoints if cp <= age_h and str(cp) not in rec["checks"]]
    return due[-1] if due else None

# ------------------------------------------------------------------ twitterapi.io
def batch_fetch(ids):
    """Current engagement for up to 50 ids per call. Deleted/protected tweets
    simply don't come back."""
    out = {}
    for i in range(0, len(ids), 50):
        qs = urllib.parse.urlencode({"tweet_ids": ",".join(ids[i:i + 50])})
        d = mon._get_json(f"https://api.twitterapi.io/twitter/tweets?{qs}", {"X-API-Key": mon.TWAPI_KEY})
        for t in (d.get("tweets") or []):
            out[str(t.get("id"))] = t
    return out

# ------------------------------------------------------------------ bootstrap
def bootstrap(state, now):
    """One-time seed of the percentile baseline from what Etched mentions
    actually score, so day one isn't guesswork. Refetches the newest candidate
    ids from monitor's state.json (~$0.30 one-time), keeps the structural
    accepts at near-final age (1-45 days), back-scales with ACCRUAL."""
    try:
        with open(mon.STATE_FILE) as f:
            seen = json.load(f).get("seen_ids", [])
    except Exception:
        seen = []
    ids = sorted((str(i) for i in seen if str(i).isdigit()), key=int)[-BOOTSTRAP_IDS:]
    if not ids:
        state["seeded"] = True
        print("[bootstrap] no history to seed from; floors only until live data accrues", flush=True)
        return
    tweets = batch_fetch(ids)   # raises on hard failure -> caller retries next run
    finals = []
    for t in tweets.values():
        if mon.structural(t)[0] != "accept":
            continue
        created = parse_created(t.get("createdAt") or "")
        if not created:
            continue
        age_d = (now - created) / 86400.0
        if not (1.0 <= age_d <= 45):
            continue
        finals.append(score_x(t))
    if len(finals) < MIN_BASELINE:
        state["seeded"] = True
        print(f"[bootstrap] only {len(finals)} usable accepts; floors only until live data accrues", flush=True)
        return
    finals.sort()
    if len(finals) > BASELINE_KEEP:
        step = len(finals) / float(BASELINE_KEEP)
        finals = [finals[int(i * step)] for i in range(BASELINE_KEEP)]
    for cp in CHECKPOINTS:
        state["baseline"][str(cp)] = [[now, round(f * ACCRUAL[cp], 1)] for f in finals]
    state["seeded"] = True
    print(f"[bootstrap] seeded from {len(finals)} accepted mentions: "
          f"final-score p50={percentile(finals, 50):.0f} p95={percentile(finals, PCTL):.0f} "
          f"-> opening 1h bar {bar(state, 1, FLOORS, now):.0f}", flush=True)

# ------------------------------------------------------------------ slack
def fmt_age(age_h):
    if age_h < 1:
        return f"{age_h * 60:.0f}m"
    if age_h < 24:
        return f"{age_h:.0f}h"
    return (f"{age_h / 24:.1f}".rstrip("0").rstrip(".")) + "d"

def build_popular_msg(rec, t, age_h, note=None):
    """Numbers up top, the post itself quoted, everything else small print."""
    txt = (t.get("text") or "").strip()
    handle, url = rec["author"], rec["url"]
    tlabel = None
    if txt and mon.tweet_needs_translation(t):
        tr = mon.translate(txt)
        if tr:
            txt, tlabel = tr["translation"], f"translated from {tr['language']}"
    if len(txt) > 700:
        txt = txt[:697] + "..."
    if not txt:
        txt = "_(no text — shared a link)_"
    likes = mon.fmt_count(t.get("likeCount"))
    header = (f"*<{url}|@{handle}>*  ❤️ {likes} · 🔁 {mon.fmt_count(t.get('retweetCount'))}"
              f" · 💬 {mon.fmt_count(t.get('replyCount'))} · 👁 {mon.fmt_count(t.get('viewCount'))}")
    meta = f"{mon.fmt_count(rec['followers'])} followers · posted {fmt_age(age_h)} ago"
    for extra in (tlabel, note):
        if extra:
            meta += f" · {extra}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{header}\n>{mon.quote(txt)}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": meta}]},
    ]
    return f"@{handle} ❤️ {likes}: {url}", blocks

def deliver(fb, blocks):
    """Webhook first (repo convention), bot token + channel as the fallback.
    False = no destination configured; the caller retries next run."""
    if SLACK_POPULAR_WEBHOOK:
        mon.slack_webhook(SLACK_POPULAR_WEBHOOK, fb, blocks)
        return True
    if mon.SLACK_BOT_TOKEN and SLACK_POPULAR_CHANNEL:
        return mon.slack_api(mon.SLACK_BOT_TOKEN, SLACK_POPULAR_CHANNEL, fb, blocks) is not None
    return False

# ------------------------------------------------------------------ main
def run():
    if not mon.TWAPI_KEY:
        print("FATAL: set TWAPI_KEY", file=sys.stderr)
        sys.exit(1)
    dry = "--dry" in sys.argv
    state = load_state()
    now = int(time.time())

    if not state.get("seeded"):
        try:
            bootstrap(state, now)
        except Exception as e:
            print(f"[bootstrap] failed ({e}); running on floors, will retry next run", flush=True)

    # 1) who's due a look?
    due = {}
    for tid, rec in state["watch"].items():
        age_h = (now - rec["created"]) / 3600.0
        cp = due_cp(rec, age_h, CHECKPOINTS)
        if cp is not None and age_h > CHECK_BY[cp]:
            rec["checks"][str(cp)] = None   # missed the window (outage); don't measure stale
            cp = None
        if (cp is None and not rec["checks"] and not rec["promoted"]
                and rec.get("disc_score", 0) >= bar(state, CHECKPOINTS[0], FLOORS, now)):
            cp = CHECKPOINTS[0]   # already hot at discovery — don't wait for the 1h mark
        if cp is not None:
            due[tid] = cp

    # 2) re-fetch and judge against the bar
    fetched = batch_fetch(sorted(due)) if due else {}
    promoted_now = 0
    for tid, cp in due.items():
        rec = state["watch"][tid]
        t = fetched.get(tid)
        if not t:
            rec["miss"] = rec.get("miss", 0) + 1   # deleted/protected; dropped at 2
            continue
        rec["miss"] = 0
        age_h = (now - rec["created"]) / 3600.0
        s = score_x(t)
        for c in CHECKPOINTS:
            if c < cp and str(c) not in rec["checks"]:
                rec["checks"][str(c)] = None       # entered late; don't re-fetch per miss
        rec["checks"][str(cp)] = s
        if age_h >= 0.75 * cp:                     # early forced checks would skew the baseline
            record_baseline(state, cp, s, now)
        if rec["promoted"]:
            continue
        b = bar(state, cp, FLOORS, now)
        big = rec.get("followers", 0) >= BIG_FOLLOWERS
        if not (s >= b or (big and s >= BIG_FACTOR * b)):
            continue
        if dry:
            print(f"  WOULD PROMOTE @{rec['author']} score={s:.0f} bar={b:.0f} at {cp}h: {rec['url']}", flush=True)
            continue
        try:
            fb, blocks = build_popular_msg(rec, t, age_h)
            if deliver(fb, blocks):
                rec["promoted"] = True
                state["promoted"].append([now, tid, s])
                promoted_now += 1
            else:
                print("  [popular] no destination configured (set SLACK_POPULAR_WEBHOOK_URL or "
                      "SLACK_BOT_TOKEN + SLACK_POPULAR_CHANNEL); will retry next run", flush=True)
        except Exception as e:
            print(f"  [popular slack error] {e}", flush=True)

    # 3) retire the finished and the deleted
    for tid in [k for k, r in state["watch"].items()
                if (now - r["created"]) / 3600.0 > RETIRE_H or r.get("miss", 0) >= 2]:
        del state["watch"][tid]

    print(f"[popular] watching={len(state['watch'])} checked={len(due)} promoted_now={promoted_now} "
          f"bar 1h={bar(state, 1, FLOORS, now):.0f}", flush=True)
    if not dry:
        save_state(state)

if __name__ == "__main__":
    run()
