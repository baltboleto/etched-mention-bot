#!/usr/bin/env python3
"""
Etched mention bot
==================
Finds every new X/Twitter mention of Etched (the AI chip company) and posts it to Slack.
Designed to run on a schedule (GitHub Actions). Stateless except for state.json.

Detection is TIERED so we get high precision (only the chip company) AND high recall
(don't miss mentions):

  Tier A  strong structural signals  -> auto-accept          (near-100% precision, no LLM)
  Tier C  clear non-tech "etched"    -> drop cheaply          (regex, no LLM)
  Tier B  ambiguous everything else  -> Claude Haiku decides  (relevant? confidence?)

Ambiguous / low-confidence items go to a REVIEW channel (if configured) instead of being
silently dropped -- that is the safety valve for "don't miss any".

Data source : twitterapi.io  /twitter/tweet/advanced_search  ($0.00015 / tweet)
Judge       : Anthropic Messages API, claude-haiku-4-5 (cheap, fast, accurate)
Delivery    : Slack Incoming Webhook(s)

Required env:
  TWAPI_KEY            twitterapi.io API key
  SLACK_WEBHOOK_URL    Slack incoming webhook for confirmed mentions
Optional env:
  ANTHROPIC_API_KEY    enables the Tier-B Claude judge (without it, Tier-B items go to review)
  SLACK_REVIEW_WEBHOOK_URL  separate webhook for "needs a human look" items (falls back to main)
"""

import json, os, re, sys, time, html, datetime, urllib.parse, urllib.request, urllib.error

# ------------------------------------------------------------------ config
TWAPI_KEY   = os.environ.get("TWAPI_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_MAIN   = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_REVIEW = os.environ.get("SLACK_REVIEW_WEBHOOK_URL", "") or SLACK_MAIN

SEARCH_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"
JUDGE_MODEL = "claude-haiku-4-5-20251001"
STATE_FILE  = os.path.join(os.path.dirname(__file__), "state.json")

# The company, its product, its people. Lowercase, no @.
HANDLES  = {"etched", "ubertigavin", "robertwachen"}
FOUNDERS_RE = re.compile(r"\bgavin uberti\b|\brobert wachen\b", re.I)

# How far back to look on the very first run (no state yet). Kept modest so the
# high-volume bare-"etched" query isn't page-capped on cold start; steady-state
# runs fetch only new tweets since the last run, so they're always complete.
FIRST_RUN_LOOKBACK_HOURS = 3
# Small overlap so nothing falls between polls; dedup removes repeats.
OVERLAP_SECONDS = 180
# Safety cap on LLM calls per run (logged, never silent). Bump if you hit it a lot.
MAX_JUDGE_CALLS = 400
# Confidence at/above which a "relevant" verdict auto-posts to the main channel.
CONF_ACCEPT = 0.6

# Wide-net queries. Each gets `since_time:<epoch>` appended at runtime.
# `-filter:retweets` drops bare re-shares (pure amplification, no new content);
# quotes & replies are kept. Flip this if you want raw RTs too.
QUERIES = [
    'etched -filter:retweets',
    '(@Etched OR from:Etched OR to:Etched OR url:etched.com) -filter:retweets',
    '("Etched AI" OR "Etched chip" OR etched.ai OR "Sohu chip" OR "Etched Sohu") -filter:retweets',
    '(Sohu (chip OR inference OR transformer OR ASIC OR Nvidia OR GPU OR datacenter OR Etched)) -filter:retweets',
    '("Gavin Uberti" OR "Robert Wachen" OR @UbertiGavin OR @robertwachen) -filter:retweets',
]

# ------------------------------------------------------------------ tier regexes
# tech / chip context -> supports "this is the company"
TECH_RE = re.compile(
    r"\b(chip|chips|inference|nvidia|tapeout|tape-out|asic|silicon|wafer|gpu|gpus|hbm|"
    r"datacenter|data ?center|megawatt|transformer|transformers|accelerator|compute|foundry|"
    r"tsmc|semiconductor|hardware|rack|flops|mfu|token|tokens|llama|h100|b200|startup|valuation|"
    r"funding|series [a-e]|raise|raised|billion|ai\b)", re.I)
# clear NON-company senses of the English word "etched"
NONTECH_RE = re.compile(
    r"\b(tattoo|glass|wood|wooden|ring|rings|necklace|bracelet|pendant|skin|stone|marble|granite|"
    r"crystal|jewel|jewellery|jewelry|engrav\w*|carv\w*|etched in(?:to)? (my |your |our |the |his |her |their )?"
    r"(memory|mind|heart|soul|brain|history|stone|memories))\b", re.I)
# figurative idioms that are essentially never the company ("etched in history",
# "forever etched", "etched his name", "etched into my bones") -> cheap reject
FIGURATIVE_RE = re.compile(
    r"\bforever etched\b|\betched forever\b"
    r"|\betched\s+(?:his|her|their|its|your|my|our)\s+name\b"
    r"|\betched\s+(?:in|into|on|onto)\b[\w\s,'’\-]{0,25}?\b("
    r"memor(?:y|ies)|mind|minds|heart|hearts|soul|souls|brain|bones?|"
    r"histor(?:y|ies)|legacy|folklore|record|records|time|stone|eternity|"
    r"psyche|walls?|skin|layers|writing)\b", re.I)
STRONG_TEXT_RE = re.compile(r"etched\.(com|ai)|@etched\b", re.I)
ETCHED_WORD_RE = re.compile(r"\betched\b", re.I)
SOHU_WORD_RE   = re.compile(r"\bsohu\b", re.I)

# ------------------------------------------------------------------ http helpers
def _get_json(url, headers, retries=4):
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1)); continue
            raise
        except Exception:
            if attempt == retries - 1: raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("retries exhausted: " + url)

def _post_json(url, payload, headers=None, retries=4):
    body = json.dumps(payload).encode()
    hdr = {"Content-Type": "application/json"}
    if headers: hdr.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1)); continue
            raise
        except Exception:
            if attempt == retries - 1: raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("retries exhausted: POST " + url)

# ------------------------------------------------------------------ twitterapi.io
def search(query, since_epoch, max_pages=15):
    """Return list of tweet dicts newer than since_epoch for one query."""
    out, cursor, pages = [], "", 0
    full_q = f"{query} since_time:{since_epoch}"
    while pages < max_pages:
        qs = urllib.parse.urlencode({"query": full_q, "queryType": "Latest", "cursor": cursor})
        d = _get_json(f"{SEARCH_BASE}?{qs}", {"X-API-Key": TWAPI_KEY})
        tweets = d.get("tweets") or (d.get("data", {}) or {}).get("tweets") or []
        if not tweets: break
        out.extend(tweets)
        pages += 1
        if not d.get("has_next_page"): break
        cursor = d.get("next_cursor") or ""
        if not cursor: break
    return out

def author_of(sub):
    if isinstance(sub, dict):
        a = sub.get("author")
        if isinstance(a, dict): return (a.get("userName") or "").lower()
    return ""

def mentions(t):
    out = set()
    ents = t.get("entities") or {}
    for m in (ents.get("user_mentions") or []):
        u = (m.get("screen_name") or m.get("username") or "").lower()
        if u: out.add(u)
    for m in re.findall(r"@([A-Za-z0-9_]{1,15})", t.get("text") or ""):
        out.add(m.lower())
    return out

# ------------------------------------------------------------------ tiered classifier
def structural(t):
    """Return ('accept'|'reject'|'maybe', reasons[])."""
    reasons, txt = [], t.get("text") or ""
    ms = mentions(t)
    if ms & HANDLES:                         reasons.append("mentions:@" + ",@".join(sorted(ms & HANDLES)))
    if (t.get("inReplyToUsername") or "").lower() in HANDLES: reasons.append("reply_to_company")
    if author_of(t.get("quoted_tweet")) in HANDLES:          reasons.append("quotes_company")
    if author_of(t.get("retweeted_tweet")) in HANDLES:       reasons.append("retweets_company")
    if STRONG_TEXT_RE.search(txt):           reasons.append("etched.com/@etched/etched.ai")
    if FOUNDERS_RE.search(txt):              reasons.append("founder_name")
    if SOHU_WORD_RE.search(txt) and TECH_RE.search(txt): reasons.append("sohu+tech")
    if reasons:
        return "accept", reasons
    # From here we only have soft signals. Cheap reject for clear non-company "etched"
    # (physical objects / figurative idioms) — but never when tech context is present.
    if ETCHED_WORD_RE.search(txt) and not TECH_RE.search(txt) and (
            NONTECH_RE.search(txt) or FIGURATIVE_RE.search(txt)):
        return "reject", ["nontech_context"]
    return "maybe", []

def judge(t):
    """Tier B: ask Claude Haiku. Returns dict {relevant, confidence, reason} or None on failure."""
    if not ANTHROPIC_KEY:
        return None
    txt = (t.get("text") or "").strip()
    author = ((t.get("author") or {}).get("userName")) or "unknown"
    quoted = t.get("quoted_tweet") or {}
    quoted_txt = (quoted.get("text") or "").strip() if isinstance(quoted, dict) else ""
    system = (
        "You classify tweets for a media-monitoring bot at Etched, an AI-chip startup. "
        "Etched builds 'Sohu', an ASIC specialized for transformer inference, and competes with "
        "Nvidia/GPUs; its founders are Gavin Uberti (CEO) and Robert Wachen. "
        "Decide whether the tweet is ABOUT Etched the company, its chip Sohu, its people, or its news. "
        "Beware false positives: 'etched' is a common English word (tattoos, glass/wood engraving, "
        "'etched in my memory', and semiconductor etching as a manufacturing STEP are all NOT the company); "
        "'Sohu' is also a large Chinese internet company (Sohu.com) unrelated to the chip. "
        "Respond with ONLY a compact JSON object: "
        '{"relevant": true|false, "confidence": 0.0-1.0, "reason": "<=12 words"}'
    )
    user = f"Author: @{author}\nTweet: {txt}"
    if quoted_txt:
        user += f"\nQuoted tweet: {quoted_txt}"
    payload = {
        "model": JUDGE_MODEL, "max_tokens": 120,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        raw = _post_json("https://api.anthropic.com/v1/messages", payload,
                         headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
        data = json.loads(raw)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        verdict = json.loads(m.group(0)) if m else None
        return verdict
    except Exception as e:
        print(f"  [judge error] {e}", flush=True)
        return None

# ------------------------------------------------------------------ slack
def post_slack(webhook, t, reasons, verdict=None):
    txt = (t.get("text") or "").strip()
    if len(txt) > 700: txt = txt[:697] + "..."
    author = (t.get("author") or {})
    handle = author.get("userName") or "unknown"
    name   = author.get("name") or handle
    followers = author.get("followers") or author.get("followersCount") or 0
    url = t.get("url") or t.get("twitterUrl") or f"https://x.com/{handle}/status/{t.get('id')}"
    likes = t.get("likeCount", 0); rts = t.get("retweetCount", 0); views = t.get("viewCount", 0)
    when = t.get("createdAt", "")
    tag = " · ".join(reasons) if reasons else ""
    if verdict:
        tag = (tag + " · " if tag else "") + f"judge:{verdict.get('confidence')} {verdict.get('reason','')}"
    context = (f"❤️ {likes}  🔁 {rts}  👁 {views}  |  {name} (@{handle}, {followers} followers)"
               f"  |  {when}")
    if tag: context += f"  |  _{tag}_"
    payload = {
        "text": f"New Etched mention by @{handle}: {url}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
                "text": f"*<{url}|New Etched mention>*  by *@{handle}*\n>{txt}"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
        ],
    }
    _post_json(webhook, payload)

# ------------------------------------------------------------------ state
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {"seen_ids": [], "last_run_epoch": 0}

def save_state(state):
    state["seen_ids"] = state["seen_ids"][-8000:]   # cap growth
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=1)

# ------------------------------------------------------------------ main
def main():
    if not TWAPI_KEY or not SLACK_MAIN:
        print("FATAL: set TWAPI_KEY and SLACK_WEBHOOK_URL", file=sys.stderr); sys.exit(1)
    dry = "--dry" in sys.argv

    state = load_state()
    seen = set(state.get("seen_ids", []))
    now = int(time.time())
    since = state.get("last_run_epoch", 0) or (now - FIRST_RUN_LOOKBACK_HOURS * 3600)
    since = max(0, since - OVERLAP_SECONDS)

    # 1) gather (wide net), dedup by id
    fetched, seen_this_run = {}, set()
    for q in QUERIES:
        try:
            for t in search(q, since):
                tid = str(t.get("id"))
                if tid and tid not in seen and tid not in seen_this_run:
                    fetched[tid] = t; seen_this_run.add(tid)
        except Exception as e:
            print(f"[search error] {q!r}: {e}", flush=True)
    print(f"[gather] {len(fetched)} new candidate tweets since {since}", flush=True)

    # 2a) structural pass: strong signals -> main, clear non-tech -> drop, rest -> maybe
    to_main, to_review, maybes = [], [], []
    for tid, t in fetched.items():
        decision, reasons = structural(t)
        if decision == "reject":
            continue
        if decision == "accept":
            to_main.append((t, reasons, None))
        else:
            maybes.append(t)

    # 2b) judge the most PROMISING maybes first, so a spike of figurative-"etched"
    #     noise can't burn the judge budget before a real mention gets seen.
    #     Priority: has chip/tech context, then higher follower count.
    def _prio(t):
        foll = (t.get("author") or {}).get("followers") or 0
        return (1 if TECH_RE.search(t.get("text") or "") else 0, foll)
    maybes.sort(key=_prio, reverse=True)

    judged = 0
    for t in maybes:
        if judged >= MAX_JUDGE_CALLS:
            to_review.append((t, ["cap_reached_unjudged"], None)); continue
        verdict = judge(t); judged += 1
        if verdict is None:
            to_review.append((t, ["judge_unavailable"], None)); continue
        conf = float(verdict.get("confidence", 0) or 0)
        rel  = bool(verdict.get("relevant"))
        if conf < CONF_ACCEPT:  to_review.append((t, ["uncertain"], verdict)); continue
        if not rel:             continue                  # confident NOT the company -> drop
        to_main.append((t, ["judge_relevant"], verdict))

    print(f"[tier] main={len(to_main)}  review={len(to_review)}  judged={judged}"
          f"{'  (CAP HIT — overflow went to review)' if judged >= MAX_JUDGE_CALLS else ''}", flush=True)

    # 3) deliver (oldest first so Slack reads chronologically)
    def keyf(x): return x[0].get("createdAt", "")
    if not dry:
        for t, reasons, v in sorted(to_main, key=keyf):
            try: post_slack(SLACK_MAIN, t, reasons, v)
            except Exception as e: print(f"  [slack main error] {e}", flush=True)
        for t, reasons, v in sorted(to_review, key=keyf):
            try: post_slack(SLACK_REVIEW, t, ["REVIEW"] + reasons, v)
            except Exception as e: print(f"  [slack review error] {e}", flush=True)
    else:
        def _show(title, rows):
            print(f"\n--- {title} ({len(rows)}) ---", flush=True)
            for t, reasons, v in sorted(rows, key=keyf):
                conf = f" conf={v.get('confidence')}" if v else ""
                print(f"  @{(t.get('author') or {}).get('userName')}: "
                      f"{(t.get('text') or '')[:88]!r}  [{','.join(reasons)}]{conf}", flush=True)
        _show("WOULD POST TO MAIN", to_main)
        _show("WOULD POST TO REVIEW", to_review)

    # 4) persist
    seen.update(fetched.keys())
    state["seen_ids"] = list(seen)
    state["last_run_epoch"] = now
    if not dry: save_state(state)
    print(f"[done] posted {len(to_main)} to main, {len(to_review)} to review", flush=True)

if __name__ == "__main__":
    main()
