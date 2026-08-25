#!/usr/bin/env python3
"""
Etched popular-mention tracker (LinkedIn)
=========================================
LinkedIn sibling of popular.py: re-checks each accepted LinkedIn mention's
engagement after it has had time to travel, and promotes the few that clear
the same floor + percentile + damping bar to the popular channel.

LinkedIn engagement accrues over days, not minutes, so the single look happens
~24h after posting (one detail-fetch per mention). A post that's already hot at
discovery is checked immediately instead; a post whose window was missed
(outage, credit pause) is skipped, not measured stale.

  score = reactions + 2*comments + 3*reposts   (comments and reposts are rarer
          and stronger signals on LinkedIn than reactions)

Re-checks use the Apify actor apimaestro/linkedin-post-detail (post_urls in,
engagement out, no cookies, $5/1k results — at our volume ~$1-2/month). Its
output shape isn't pinned by a contract, so stat extraction is defensive and
gives up loudly (item logged, retried next run) rather than guessing zeros.

No bootstrap here: LinkedIn history can't be refetched cheaply, so the first
weeks run on floors while the live percentile baseline accrues.

Runs right after linkedin.py in the same hourly job; linkedin.py appends its
accepts via watch_posts(). Honors the same Apify credit guard as linkedin.py.
State: popular_linkedin_state.json.
"""

import json, os, re, sys, time

import monitor as mon      # http helpers, slack senders, translate, fmt
import linkedin as li      # APIFY_TOKEN, credit guard, looks_english
import popular as pop      # the shared bar/baseline/watch machinery

STATE_FILE = os.path.join(os.path.dirname(__file__), "popular_linkedin_state.json")
DETAIL_ACTOR = "apimaestro~linkedin-post-detail"

CHECKPOINTS = [24]        # measure each mention ONCE, ~24h after posting
FLOORS = {24: 50}
CHECK_BY = {24: 36}       # missed the window (outage/credit pause)? skip it, don't skew
RETIRE_H = 36
MAX_MISS = 3                  # unparseable/absent results tolerated before giving up

ACTIVITY_ID_RE = re.compile(r"(\d{15,25})")

# ------------------------------------------------------------------ scoring
def score_li(likes, comments, shares):
    return pop._int(likes) + 2 * pop._int(comments) + 3 * pop._int(shares)

# ------------------------------------------------------------------ watchlist
def watch_posts(items, path=None):
    """linkedin.py calls this right after posting accepts to main."""
    if not items:
        return 0
    now = int(time.time())
    state = pop.load_state(path or STATE_FILE)
    added = 0
    for it in items:
        pid = str(it.get("id") or it.get("entityId") or "")
        url = it.get("linkedinUrl") or it.get("shareLinkedinUrl") or ""
        created = pop._int((it.get("postedAt") or {}).get("timestamp"))
        if created > 10**12:
            created //= 1000          # harvestapi timestamps are milliseconds
        if not pid or not url or not created or pid in state["watch"]:
            continue
        eng = it.get("engagement") or {}
        state["watch"][pid] = {
            "created": created,
            "author": (it.get("author") or {}).get("name") or "unknown",
            "url": url,
            "text": li.text_of(it)[:700],
            "disc_score": score_li(eng.get("likes"), eng.get("comments"), eng.get("shares")),
            "checks": {}, "promoted": False, "miss": 0, "added": now,
        }
        added += 1
    pop.save_state(state, path or STATE_FILE)
    return added

# ------------------------------------------------------------------ apify detail fetch
def fetch_details(urls):
    payload = {"post_urls": urls}
    url = (f"https://api.apify.com/v2/acts/{DETAIL_ACTOR}/run-sync-get-dataset-items"
           f"?token={li.APIFY_TOKEN}&timeout=180")
    items = json.loads(mon._post_json(url, payload))
    return items if isinstance(items, list) else []

def _first(d, keys):
    for k in keys:
        v = d.get(k)
        try:
            if v is not None:
                return int(v)
        except (TypeError, ValueError):
            continue
    return None

def extract_stats(item):
    """(likes, comments, shares) from whatever shape the actor returns, or None.
    None means 'could not read' — the caller logs and retries, never guesses 0."""
    nodes = [item]
    for k in ("post", "data", "result"):
        if isinstance(item.get(k), dict):
            nodes.append(item[k])
    for node in nodes:
        for stats in (node.get("stats"), node.get("engagement"), node.get("socialActivity"), node):
            if not isinstance(stats, dict):
                continue
            likes = _first(stats, ("total_reactions", "totalReactions", "reactions",
                                   "reactionsCount", "numLikes", "likes", "like", "likes_count"))
            comments = _first(stats, ("comments", "numComments", "commentsCount", "comments_count"))
            shares = _first(stats, ("reposts", "shares", "numShares", "repostsCount",
                                    "sharesCount", "shares_count", "reposts_count"))
            if likes is not None and (comments is not None or shares is not None):
                return likes, comments or 0, shares or 0
    return None

def match_result(items, rec):
    """Find the actor result for this post: its activity id appears somewhere
    in the item (url/urn/id — field name varies, so search the whole item)."""
    m = ACTIVITY_ID_RE.search(rec["url"])
    if not m:
        return None
    aid = m.group(1)
    for it in items:
        try:
            if aid in json.dumps(it):
                return it
        except (TypeError, ValueError):
            continue
    return None

# ------------------------------------------------------------------ slack
def build_popular_msg_li(rec, txt, likes, comments, shares, age_h, note=None):
    tlabel = None
    if txt and not li.looks_english(txt):
        tr = mon.translate(txt)
        if tr:
            txt, tlabel = tr["translation"], f":globe_with_meridians: Translated from {tr['language']}"
    if len(txt) > 700:
        txt = txt[:697] + "..."
    if not txt:
        txt = "_(no text — media-only post)_"
    header = f":fire: *<{rec['url']}|LinkedIn Post by {rec['author']}>* is getting traction"
    if note:
        header += f" _({note})_"
    stats = (f"👍 {mon.fmt_count(likes)} · 💬 {mon.fmt_count(comments)} · 🔁 {mon.fmt_count(shares)}"
             f" · {pop.fmt_age(age_h)} after posting")
    ctx = ([{"type": "mrkdwn", "text": tlabel}] if tlabel else []) + [{"type": "mrkdwn", "text": stats}]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{header}\n>{mon.quote(txt)}"}},
        {"type": "context", "elements": ctx},
    ]
    return f"Getting traction — LinkedIn post by {rec['author']}: {rec['url']}", blocks

# ------------------------------------------------------------------ main
def run():
    if not li.APIFY_TOKEN:
        print("FATAL: set APIFY_TOKEN", file=sys.stderr)
        sys.exit(1)
    dry = "--dry" in sys.argv
    state = pop.load_state(STATE_FILE)
    now = int(time.time())

    # 1) who's due their one look?
    due = {}
    for pid, rec in state["watch"].items():
        age_h = (now - rec["created"]) / 3600.0
        cp = pop.due_cp(rec, age_h, CHECKPOINTS)
        if cp is not None and age_h > CHECK_BY[cp]:
            rec["checks"][str(cp)] = None   # missed the window; don't measure stale
            cp = None
        if (cp is None and not rec["checks"] and not rec["promoted"]
                and rec.get("disc_score", 0) >= pop.bar(state, 24, FLOORS, now)):
            cp = 24               # already hot at discovery — check now
        if cp is not None:
            due[pid] = cp

    # 2) one actor run for everything due, then judge against the bar
    items = []
    if due:
        # same guard as linkedin.py: don't burn the last cents of the Apify cap
        credit = li.apify_credit()
        if credit and credit["plan"] == "FREE" and credit["remaining"] < li.DEAD_CREDIT_USD:
            print(f"[popular-li] paused — Apify credit exhausted "
                  f"(${credit['used']:.2f}/${credit['max']:.0f})", flush=True)
            if not dry:
                pop.save_state(state, STATE_FILE)
            return
        try:
            items = fetch_details([state["watch"][pid]["url"] for pid in sorted(due)])
        except Exception as e:
            print(f"[popular-li] detail fetch failed: {e}", flush=True)

    promoted_now = 0
    for pid, cp in due.items():
        rec = state["watch"][pid]
        it = match_result(items, rec)
        stats = extract_stats(it) if it else None
        if stats is None:
            rec["miss"] = rec.get("miss", 0) + 1
            if it and rec["miss"] == 1:   # log the shape once so it's fixable from Actions logs
                print(f"  [popular-li] unreadable stats, keys={sorted(it)[:15]} url={rec['url']}", flush=True)
            continue
        rec["miss"] = 0
        likes, comments, shares = stats
        age_h = (now - rec["created"]) / 3600.0
        s = score_li(likes, comments, shares)
        for c in CHECKPOINTS:
            if c < cp and str(c) not in rec["checks"]:
                rec["checks"][str(c)] = None
        rec["checks"][str(cp)] = s
        if age_h >= 0.75 * cp:
            pop.record_baseline(state, cp, s, now)
        if rec["promoted"]:
            continue
        if s < pop.bar(state, cp, FLOORS, now):
            continue
        if dry:
            print(f"  WOULD PROMOTE {rec['author']} score={s:.0f} "
                  f"bar={pop.bar(state, cp, FLOORS, now):.0f} at {cp}h: {rec['url']}", flush=True)
            continue
        try:
            txt = ""
            nodes = [it] if it else []
            if it and isinstance(it.get("post"), dict):
                nodes.append(it["post"])
            for node in nodes:
                for k in ("text", "commentary", "content"):
                    v = node.get(k)
                    if isinstance(v, str) and v.strip():
                        txt = v.strip()
                        break
                if txt:
                    break
            fb, blocks = build_popular_msg_li(rec, txt or rec.get("text", ""),
                                              likes, comments, shares, age_h)
            if pop.deliver(fb, blocks):
                rec["promoted"] = True
                state["promoted"].append([now, pid, s])
                promoted_now += 1
            else:
                print("  [popular-li] no destination configured (set SLACK_POPULAR_WEBHOOK_URL or "
                      "SLACK_BOT_TOKEN + SLACK_POPULAR_CHANNEL); will retry next run", flush=True)
        except Exception as e:
            print(f"  [popular-li slack error] {e}", flush=True)

    # 3) retire the finished and the unreadable
    for pid in [k for k, r in state["watch"].items()
                if (now - r["created"]) / 3600.0 > RETIRE_H or r.get("miss", 0) >= MAX_MISS]:
        del state["watch"][pid]

    print(f"[popular-li] watching={len(state['watch'])} checked={len(due)} promoted_now={promoted_now} "
          f"bar 24h={pop.bar(state, 24, FLOORS, now):.0f}", flush=True)
    if not dry:
        pop.save_state(state, STATE_FILE)

if __name__ == "__main__":
    run()
