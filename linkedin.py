#!/usr/bin/env python3
"""
Etched LinkedIn mention bot
===========================
LinkedIn sibling of monitor.py: finds new public LinkedIn posts mentioning
Etched (the AI chip company) and posts them to the same Slack channel.

Data source : Apify actor harvestapi/linkedin-post-search (public posts,
              no LinkedIn account or cookies involved, ~$2 / 1,000 posts)
Filtering   : reuses monitor.py's tiered classifier — strong signals auto-
              accept, figurative/non-tech "etched" drops cheaply, ambiguous
              posts go to the Claude Haiku judge; unsure -> review thread.
Delivery    : same Slack incoming webhook (main) + bot-token review thread,
              messages labeled LinkedIn.

Scheduling  : hourly. Each run scrapes the past hour; if the previous run is
              older than 2h (cron lag, first run), it widens to the past 24h
              so scheduler gaps self-heal. Dedup via linkedin_state.json.

Required env:
  APIFY_TOKEN          Apify API token
  SLACK_WEBHOOK_URL    incoming webhook for confirmed mentions (main channel)
Optional env (same as monitor.py):
  ANTHROPIC_API_KEY, SLACK_BOT_TOKEN, SLACK_REVIEW_CHANNEL, SLACK_REVIEW_WEBHOOK_URL
"""

import json, os, sys, time, datetime

import monitor as mon   # shared regexes, http helpers, slack senders, thresholds

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
ACTOR       = "harvestapi~linkedin-post-search"
STATE_FILE  = os.path.join(os.path.dirname(__file__), "linkedin_state.json")

QUERIES = ["etched", "Gavin Uberti", "Robert Wachen"]
MAX_POSTS_PER_QUERY = 100      # cost ceiling per query per run
MAX_JUDGE_CALLS = 200
WIDE_WINDOW_AFTER = 2 * 3600   # widen 1h -> 24h when the last run is older than this
LOW_CREDIT_USD  = 1.00         # below this, stamp a warning on every Slack message
DEAD_CREDIT_USD = 0.10         # below this, don't scrape; alert once a day instead

# posts BY our own page aren't mentions (Baltazar writes those)
OWN_PAGES = {"etched", "etchedai", "etched-ai"}

# ------------------------------------------------------------------ apify
def pick_window(gap_seconds):
    """Search window per gap since the last successful run. Widens so cron lag
    and credit-exhaustion pauses self-heal without losing mentions."""
    if gap_seconds > 7 * 24 * 3600:
        return "month"
    if gap_seconds > 24 * 3600:
        return "week"
    if gap_seconds > WIDE_WINDOW_AFTER:
        return "24h"
    return "1h"

def apify_credit():
    """Return {plan, max, used, remaining, resets} or None if the check fails.
    Only the FREE plan can hard-stop mid-month (paid plans bill overage), so
    the caller skips all warnings on paid plans."""
    try:
        me = mon._get_json(f"https://api.apify.com/v2/users/me?token={APIFY_TOKEN}", {}, 2)["data"]
        lim = mon._get_json(f"https://api.apify.com/v2/users/me/limits?token={APIFY_TOKEN}", {}, 2)["data"]
        mx = float(lim["limits"]["maxMonthlyUsageUsd"])
        used = float(lim["current"]["monthlyUsageUsd"])
        return {"plan": (me.get("plan") or {}).get("id", ""), "max": mx, "used": used,
                "remaining": max(0.0, mx - used),
                "resets": (lim.get("monthlyUsageCycle") or {}).get("endAt", "")[:10]}
    except Exception as e:
        print(f"  [credit check error] {e}", flush=True)
        return None

def credit_warning(credit):
    """Warning line for Slack messages when the free credit is running low."""
    if not credit or credit["plan"] != "FREE" or credit["remaining"] >= LOW_CREDIT_USD:
        return None
    return (f":warning: *Apify credit low: ${credit['remaining']:.2f} of ${credit['max']:.0f} left* — "
            f"LinkedIn monitoring stops when it hits $0 (credit resets {credit['resets']}). "
            f"Upgrade: https://console.apify.com/billing")

def paused_alert(state, credit, dry=False):
    """Once per UTC day while out of credit: tell the channel monitoring is off."""
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if state.get("paused_alert_date") == date:
        return
    text = (f":rotating_light: *LinkedIn mention monitoring is PAUSED — Apify credit exhausted* "
            f"(${credit['used']:.2f} of ${credit['max']:.0f} used; resets {credit['resets']}).\n"
            f"LinkedIn mentions are NOT being collected right now. To resume, upgrade the Apify plan: "
            f"https://console.apify.com/billing — when it's back, I'll catch up on missed posts "
            f"(up to a week). X/Twitter monitoring is unaffected. This reminder repeats daily until fixed.")
    if dry:
        print(f"[paused alert would post] {text}", flush=True)
    else:
        mon.slack_webhook(mon.SLACK_MAIN, "LinkedIn monitoring paused — Apify credit exhausted",
                          [{"type": "section", "text": {"type": "mrkdwn", "text": text}}])
    state["paused_alert_date"] = date

def li_search(posted_limit):
    payload = {"searchQueries": QUERIES, "maxPosts": MAX_POSTS_PER_QUERY,
               "sortBy": "date", "postedLimit": posted_limit,
               "scrapeReactions": False, "scrapeComments": False}
    url = (f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
           f"?token={APIFY_TOKEN}&timeout=240")
    items = json.loads(mon._post_json(url, payload))
    return items if isinstance(items, list) else []

def text_of(item):
    parts = []
    c = item.get("content")
    if isinstance(c, str):
        parts.append(c)
    h = (item.get("header") or {}).get("text")
    if h:
        parts.append(h)     # repost/celebration context line
    return "\n".join(parts).strip()

# ------------------------------------------------------------------ tiers
def structural_li(item):
    """accept | reject | maybe | own_post, mirroring monitor.structural()."""
    a = item.get("author") or {}
    if a.get("type") == "company" and (
            (a.get("universalName") or "").lower() in OWN_PAGES
            or (a.get("name") or "").strip().lower() == "etched"):
        return "own_post", []
    txt = text_of(item)
    if not txt:
        return "maybe", []          # image-only post the search still matched -> judge/review
    reasons = []
    if mon.STRONG_TEXT_RE.search(txt):                           reasons.append("etched.com/etched.ai")
    if mon.FOUNDERS_RE.search(txt):                              reasons.append("founder_name")
    if mon.SOHU_WORD_RE.search(txt) and mon.TECH_RE.search(txt): reasons.append("sohu+tech")
    if reasons:
        return "accept", reasons
    if mon.ETCHED_WORD_RE.search(txt) and not mon.TECH_RE.search(txt) and (
            mon.NONTECH_RE.search(txt) or mon.FIGURATIVE_RE.search(txt)):
        return "reject", ["nontech_context"]
    return "maybe", []

def judge_li(item):
    """Claude Haiku with structured outputs, tailored to LinkedIn posts."""
    if not mon.ANTHROPIC_KEY:
        return None
    a = item.get("author") or {}
    who = f"{a.get('name') or 'unknown'} ({a.get('type') or 'profile'}; {(a.get('info') or '')[:120]})"
    user = f"Author: {who}\nLinkedIn post:\n{text_of(item)[:3500]}"
    system = (
        "You classify LinkedIn posts for a media-monitoring bot at Etched, an AI-chip startup. "
        "Etched builds 'Sohu', an ASIC specialized for transformer inference, and competes with "
        "Nvidia/GPUs; its founders are Gavin Uberti (CEO) and Robert Wachen. "
        "Decide whether this post is ABOUT Etched the company, its chip Sohu, its people, or its news. "
        "Beware false positives: 'etched' is a common English word (laser etching businesses, glass/wood "
        "engraving, awards, 'etched in history/memory', and semiconductor etching as a manufacturing STEP "
        "are all NOT the company); 'Sohu' is also a large Chinese internet company (Sohu.com)."
    )
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"relevant": {"type": "boolean"},
                             "confidence": {"type": "number"},
                             "reason": {"type": "string"}},
              "required": ["relevant", "confidence", "reason"]}
    payload = {"model": mon.JUDGE_MODEL, "max_tokens": 200, "system": system,
               "messages": [{"role": "user", "content": user}],
               "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    try:
        raw = mon._post_json("https://api.anthropic.com/v1/messages", payload,
                             headers={"x-api-key": mon.ANTHROPIC_KEY,
                                      "anthropic-version": "2023-06-01"})
        data = json.loads(raw)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return json.loads(text)
    except Exception as e:
        print(f"  [judge error] {e}", flush=True)
        return None

# ------------------------------------------------------------------ slack
def build_msg_li(item, reasons, verdict, kind, warn=None):
    txt = text_of(item)
    if len(txt) > 700: txt = txt[:697] + "..."
    a = item.get("author") or {}
    name = a.get("name") or "unknown"
    info = (a.get("info") or "").strip()
    url = item.get("linkedinUrl") or item.get("shareLinkedinUrl") or (a.get("linkedinUrl") or "")
    eng = item.get("engagement") or {}
    likes, comments, shares = eng.get("likes", 0), eng.get("comments", 0), eng.get("shares", 0)
    tag = " · ".join(reasons) if reasons else ""
    if verdict:
        tag = (tag + " · " if tag else "") + f"judge:{verdict.get('confidence')} {verdict.get('reason','')}"
    header = ("New Etched mention (LinkedIn)" if kind == "main"
              else "Possible LinkedIn mention — needs a look")
    if not txt: txt = "_(no text — media-only post)_"
    context = f"👍 {likes}  💬 {comments}  🔁 {shares}  |  {name}" + (f" — {info}" if info else "")
    if tag: context += f"  |  _{tag}_"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{header}* by *<{url}|{name}>*\n>{txt}\n<{url}|View post on LinkedIn →>"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
    ]
    if warn:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": warn}]})
    return f"{header} by {name}: {url}", blocks

def review_anchor_ts_li(state):
    """One LinkedIn review-queue anchor per UTC day (separate from the X one)."""
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    anchors = state.setdefault("review_anchors", {})
    if anchors.get(date):
        return anchors[date]
    ts = mon.slack_api(mon.SLACK_BOT_TOKEN, mon.SLACK_REVIEW_CHANNEL,
                       f"LinkedIn mentions — review queue ({date})",
                       [{"type": "section", "text": {"type": "mrkdwn",
                         "text": f":mag: *LinkedIn review queue — {date}*\nPosts I couldn't confirm "
                                 f"either way. Replies below — glance and ignore if not relevant."}}])
    if ts: anchors[date] = ts
    return ts

# ------------------------------------------------------------------ state
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {"seen_ids": [], "last_run_epoch": 0}

def save_state(state):
    state["seen_ids"] = state["seen_ids"][-8000:]
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=1)

# ------------------------------------------------------------------ main
def main():
    if not APIFY_TOKEN or not mon.SLACK_MAIN:
        print("FATAL: set APIFY_TOKEN and SLACK_WEBHOOK_URL", file=sys.stderr); sys.exit(1)
    dry = "--dry" in sys.argv

    state = load_state()
    seen = set(state.get("seen_ids", []))
    now = int(time.time())
    gap = now - (state.get("last_run_epoch") or 0)
    posted_limit = pick_window(gap)

    # 0) credit guard: on the FREE plan the scrape hard-fails at $0, so don't
    #    burn the last cents — pause loudly (daily Slack alert) instead.
    #    last_run_epoch is NOT advanced while paused, so the window keeps
    #    widening and the catch-up scrape on revival misses nothing (<=1 week).
    credit = apify_credit()
    if credit and credit["plan"] == "FREE" and credit["remaining"] < DEAD_CREDIT_USD:
        print(f"[paused] Apify credit exhausted (${credit['used']:.2f}/${credit['max']:.0f})", flush=True)
        paused_alert(state, credit, dry=dry)
        if not dry: save_state(state)
        return
    warn = credit_warning(credit)
    if warn: print(f"[credit] {warn}", flush=True)

    # 1) gather (dedup by post id)
    try:
        raw = li_search(posted_limit)
    except Exception as e:
        print(f"FATAL: apify search failed: {e}", file=sys.stderr); sys.exit(1)
    fetched = {}
    for it in raw:
        pid = str(it.get("id") or it.get("entityId") or "")
        if pid and pid not in seen and pid not in fetched:
            fetched[pid] = it
    print(f"[gather] {len(raw)} scraped, {len(fetched)} new (window={posted_limit})", flush=True)

    # 2) classify
    to_main, to_review, judge_queue = [], [], []
    for pid, it in fetched.items():
        d, extra = structural_li(it)
        if d in ("reject", "own_post"):
            continue
        if d == "accept":
            to_main.append((it, extra, None)); continue
        judge_queue.append(it)

    # judge most-promising first (tech context, then reach) so noise can't starve a real one
    def _prio(it):
        eng = it.get("engagement") or {}
        reach = (eng.get("likes") or 0) + 10 * (eng.get("comments") or 0)
        return (1 if mon.TECH_RE.search(text_of(it)) else 0, reach)
    judge_queue.sort(key=_prio, reverse=True)

    judged = 0
    for it in judge_queue:
        if judged >= MAX_JUDGE_CALLS:
            to_review.append((it, ["cap_reached_unjudged"], None)); continue
        v = judge_li(it); judged += 1
        if v is None:
            to_review.append((it, ["judge_unavailable"], None)); continue
        conf = float(v.get("confidence", 0) or 0); rel = bool(v.get("relevant"))
        if conf < mon.CONF_ACCEPT:
            to_review.append((it, ["uncertain"], v)); continue
        if not rel:
            continue                                   # we looked — it isn't Etched -> drop
        to_main.append((it, ["judge_relevant"], v))

    print(f"[tier] main={len(to_main)}  review={len(to_review)}  judged={judged}"
          f"{'  (JUDGE CAP HIT)' if judged >= MAX_JUDGE_CALLS else ''}", flush=True)

    # 3) deliver (oldest first)
    def keyf(x): return ((x[0].get("postedAt") or {}).get("timestamp")) or 0
    if dry:
        def _show(title, rows):
            print(f"\n--- {title} ({len(rows)}) ---", flush=True)
            for it, reasons, v in sorted(rows, key=keyf):
                c = f" conf={v.get('confidence')}" if v else ""
                print(f"  {(it.get('author') or {}).get('name')}: "
                      f"{text_of(it)[:80]!r}  [{','.join(reasons)}]{c}", flush=True)
        _show("WOULD POST TO MAIN", to_main)
        _show("WOULD POST TO REVIEW", to_review)
    else:
        for it, reasons, v in sorted(to_main, key=keyf):
            try:
                fb, bl = build_msg_li(it, reasons, v, "main", warn); mon.slack_webhook(mon.SLACK_MAIN, fb, bl)
            except Exception as e: print(f"  [slack main error] {e}", flush=True)
        if to_review and mon.SLACK_BOT_TOKEN and mon.SLACK_REVIEW_CHANNEL:
            ts = review_anchor_ts_li(state)
            for it, reasons, v in sorted(to_review, key=keyf):
                try:
                    fb, bl = build_msg_li(it, reasons, v, "review", warn)
                    mon.slack_api(mon.SLACK_BOT_TOKEN, mon.SLACK_REVIEW_CHANNEL, fb, bl, thread_ts=ts)
                except Exception as e: print(f"  [slack review error] {e}", flush=True)
        elif to_review and mon.SLACK_REVIEW_WEBHOOK:
            for it, reasons, v in sorted(to_review, key=keyf):
                try:
                    fb, bl = build_msg_li(it, reasons, v, "review", warn); mon.slack_webhook(mon.SLACK_REVIEW_WEBHOOK, fb, bl)
                except Exception as e: print(f"  [slack review error] {e}", flush=True)
        elif to_review:
            print(f"[review] {len(to_review)} item(s) held (no review destination; NOT sent to main)", flush=True)

    # 4) persist
    seen.update(fetched.keys())
    state["seen_ids"] = list(seen)
    state["last_run_epoch"] = now
    if not dry: save_state(state)
    print(f"[done] main={len(to_main)} review={len(to_review)}", flush=True)

if __name__ == "__main__":
    main()
