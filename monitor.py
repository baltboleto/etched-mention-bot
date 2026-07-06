#!/usr/bin/env python3
"""
Etched mention bot
==================
Finds every new X/Twitter mention of Etched (the AI chip company) and posts it to Slack.
Designed to run on a schedule (GitHub Actions). Stateless except for state.json.

Detection is TIERED for high precision (only the chip company) AND high recall:

  strong structural signals   -> auto-accept                 (near-100% precision, no LLM)
  clear non-tech "etched"     -> drop cheaply                (regex, no LLM)
  bare link, no visible signal -> RESOLVE it:
        external page  -> fetch the article, judge its real content
        X-native page  -> can't read it -> send to review (never guess)
  ambiguous everything else   -> Claude Haiku judges         (relevant? confidence?)

Routing:
  confident Etched            -> MAIN channel (clean feed of real mentions)
  confident NOT Etched        -> dropped (we looked, it isn't us)
  unsure / unreadable         -> REVIEW thread (nothing vanishes; main stays clean)

Data source : twitterapi.io  /twitter/tweet/advanced_search  ($0.00015 / tweet)
Judge       : Anthropic Messages API, claude-haiku-4-5, structured outputs (guaranteed JSON)
Delivery    : Slack incoming webhook (main) + optional bot-token thread (review)

Required env:
  TWAPI_KEY            twitterapi.io API key
  SLACK_WEBHOOK_URL    incoming webhook for confirmed mentions (main channel)
Optional env:
  ANTHROPIC_API_KEY         enables the Claude judge (without it, ambiguous items go to review)
  SLACK_BOT_TOKEN           xoxb- token; enables threaded REVIEW replies via chat.postMessage
  SLACK_REVIEW_CHANNEL      channel id for the review thread (used with SLACK_BOT_TOKEN)
  SLACK_REVIEW_WEBHOOK_URL  fallback: a separate webhook for review items (if no bot token)
  (if none of the review options are set, review items are logged but NOT sent to main)
"""

import json, os, re, sys, time, html, datetime, urllib.parse, urllib.request, urllib.error

# ------------------------------------------------------------------ config
TWAPI_KEY     = os.environ.get("TWAPI_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_MAIN    = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_BOT_TOKEN     = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_REVIEW_CHANNEL = os.environ.get("SLACK_REVIEW_CHANNEL", "")
SLACK_REVIEW_WEBHOOK = os.environ.get("SLACK_REVIEW_WEBHOOK_URL", "")

SEARCH_BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"
JUDGE_MODEL = "claude-haiku-4-5-20251001"
STATE_FILE  = os.path.join(os.path.dirname(__file__), "state.json")

HANDLES  = {"etched", "ubertigavin", "robertwachen"}
FOUNDERS_RE = re.compile(r"\bgavin uberti\b|\brobert wachen\b", re.I)

FIRST_RUN_LOOKBACK_HOURS = 3
OVERLAP_SECONDS = 180
MAX_JUDGE_CALLS = 400
MAX_FETCH = 60            # cap link-resolution fetches per run (excess -> review)
CONF_ACCEPT = 0.6

QUERIES = [
    'etched -filter:retweets',
    '(@Etched OR from:Etched OR to:Etched OR url:etched.com) -filter:retweets',
    '("Etched AI" OR "Etched chip" OR etched.ai OR "Sohu chip" OR "Etched Sohu") -filter:retweets',
    '(Sohu (chip OR inference OR transformer OR ASIC OR Nvidia OR GPU OR datacenter OR Etched)) -filter:retweets',
    '("Gavin Uberti" OR "Robert Wachen" OR @UbertiGavin OR @robertwachen) -filter:retweets',
]

# ------------------------------------------------------------------ tier regexes
TECH_RE = re.compile(
    r"\b(chip|chips|inference|nvidia|tapeout|tape-out|asic|silicon|wafer|gpu|gpus|hbm|"
    r"datacenter|data ?center|megawatt|transformer|transformers|accelerator|compute|foundry|"
    r"tsmc|semiconductor|hardware|rack|flops|mfu|token|tokens|llama|h100|b200|startup|valuation|"
    r"funding|series [a-e]|raise|raised|billion|ai\b)", re.I)
NONTECH_RE = re.compile(
    r"\b(tattoo|glass|wood|wooden|ring|rings|necklace|bracelet|pendant|skin|stone|marble|granite|"
    r"crystal|jewel|jewellery|jewelry|engrav\w*|carv\w*|etched in(?:to)? (my |your |our |the |his |her |their )?"
    r"(memory|mind|heart|soul|brain|history|stone|memories))\b", re.I)
FIGURATIVE_RE = re.compile(
    r"\bforever etched\b|\betched forever\b"
    r"|\betched\s+(?:his|her|their|its|your|my|our)\s+name\b"
    r"|\betched\s+(?:in|into|on|onto)\b[\w\s,'’\-]{0,25}?\b("
    r"memor(?:y|ies)|mind|minds|heart|hearts|soul|souls|brain|bones?|"
    r"histor(?:y|ies)|legacy|folklore|record|records|time|stone|eternity|"
    r"psyche|walls?|skin|layers|writing)\b", re.I)
# Match the brand tokens even when wedged against non-Latin scripts (e.g. Chinese/
# Japanese/Korean have no spaces: "公司Etched宣布"). Lookarounds treat only ASCII
# letters as "part of a word" — so "sketched"/"wretched" still won't match, but
# CJK/space/punctuation neighbors do.
STRONG_TEXT_RE = re.compile(r"etched\.(com|ai)|@etched(?![a-z0-9_])", re.I)
ETCHED_WORD_RE = re.compile(r"(?<![a-z])etched(?![a-z])", re.I)
SOHU_WORD_RE   = re.compile(r"(?<![a-z])sohu(?![a-z])", re.I)
URL_SIGNAL_RE  = re.compile(r"etched\.(com|ai)|/[^ ]*\betched\b|\bsohu[-_]?chip\b", re.I)

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

def fetch_url_text(url):
    """Fetch a linked page and return (final_url, cleaned_text) or (None, None).
    Used to resolve bare-link tweets: we read the article to judge its real topic."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return (None, None)
    try:
        req = urllib.request.Request(url, headers={"User-Agent":
              "Mozilla/5.0 (compatible; EtchedMentionBot/1.0; +https://etched.com)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            final = r.geturl()
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                return (final, None)
            raw = r.read(400_000)
        t = raw.decode("utf-8", "ignore")
        t = re.sub(r"(?is)<(script|style|noscript|template).*?</\1>", " ", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", html.unescape(t)).strip()
        return (final, t[:4000])
    except Exception:
        return (None, None)

# ------------------------------------------------------------------ twitterapi.io
def search(query, since_epoch, max_pages=15):
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

def expanded_urls(t):
    ents = t.get("entities") or {}
    out = []
    for u in (ents.get("urls") or []):
        e = u.get("expanded_url") or u.get("url")
        if e: out.append(e)
    return out

def _is_x_native(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(h == host or host.endswith("." + h) for h in ("x.com", "twitter.com"))

# ------------------------------------------------------------------ tiered classifier
def structural(t):
    """Return (decision, extra) where decision is
    accept | reject | maybe | fetch | unresolved.  For 'fetch', extra=[url]."""
    reasons, txt = [], t.get("text") or ""
    ms = mentions(t)
    urls = expanded_urls(t)
    url_blob = " ".join(urls)
    if ms & HANDLES:                                         reasons.append("mentions:@" + ",@".join(sorted(ms & HANDLES)))
    if (t.get("inReplyToUsername") or "").lower() in HANDLES: reasons.append("reply_to_company")
    if author_of(t.get("quoted_tweet")) in HANDLES:          reasons.append("quotes_company")
    if author_of(t.get("retweeted_tweet")) in HANDLES:       reasons.append("retweets_company")
    if STRONG_TEXT_RE.search(txt):                           reasons.append("etched.com/@etched/etched.ai")
    if FOUNDERS_RE.search(txt):                              reasons.append("founder_name")
    if SOHU_WORD_RE.search(txt) and TECH_RE.search(txt):     reasons.append("sohu+tech")
    if URL_SIGNAL_RE.search(url_blob):                       reasons.append("etched_url")
    if reasons:
        return "accept", reasons

    # cheap reject for clear non-company "etched" (objects / figurative idioms)
    if ETCHED_WORD_RE.search(txt) and not TECH_RE.search(txt) and (
            NONTECH_RE.search(txt) or FIGURATIVE_RE.search(txt)):
        return "reject", ["nontech_context"]

    # visible "etched"/"sohu" but ambiguous -> judge on the tweet text
    if ETCHED_WORD_RE.search(txt) or SOHU_WORD_RE.search(txt):
        return "maybe", []

    # NO visible signal: the search matched hidden linked content. Resolve it.
    ext = [u for u in urls if not _is_x_native(u)]
    if ext:
        return "fetch", [ext[0]]
    return "unresolved", []          # X-native/unreadable link -> review, never guess

def judge(t, content=None):
    """Ask Claude Haiku with structured outputs (guaranteed JSON).
    If `content` is given (resolved article text), judge that instead of the tweet."""
    if not ANTHROPIC_KEY:
        return None
    author = ((t.get("author") or {}).get("userName")) or "unknown"
    if content:
        user = f"Author: @{author}\nThe tweet is just a link. Content of the linked page:\n{content[:3500]}"
    else:
        txt = (t.get("text") or "").strip()
        quoted = t.get("quoted_tweet") or {}
        qtxt = (quoted.get("text") or "").strip() if isinstance(quoted, dict) else ""
        user = f"Author: @{author}\nTweet: {txt}" + (f"\nQuoted tweet: {qtxt}" if qtxt else "")
    system = (
        "You classify tweets/links for a media-monitoring bot at Etched, an AI-chip startup. "
        "Etched builds 'Sohu', an ASIC specialized for transformer inference, and competes with "
        "Nvidia/GPUs; its founders are Gavin Uberti (CEO) and Robert Wachen. "
        "Decide whether this is ABOUT Etched the company, its chip Sohu, its people, or its news. "
        "Beware false positives: 'etched' is a common English word (tattoos, glass/wood engraving, "
        "'etched in history/memory', 'etched his name', and semiconductor etching as a manufacturing "
        "STEP are all NOT the company); 'Sohu' is also a large Chinese internet company (Sohu.com)."
    )
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"relevant": {"type": "boolean"},
                             "confidence": {"type": "number"},
                             "reason": {"type": "string"}},
              "required": ["relevant", "confidence", "reason"]}
    payload = {"model": JUDGE_MODEL, "max_tokens": 200, "system": system,
               "messages": [{"role": "user", "content": user}],
               "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    try:
        raw = _post_json("https://api.anthropic.com/v1/messages", payload,
                         headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
        data = json.loads(raw)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return json.loads(text)
    except Exception as e:
        print(f"  [judge error] {e}", flush=True)
        return None

# ------------------------------------------------------------------ slack
def build_msg(t, reasons, verdict, kind):
    txt = (t.get("text") or "").strip()
    if len(txt) > 700: txt = txt[:697] + "..."
    author = (t.get("author") or {})
    handle = author.get("userName") or "unknown"
    name   = author.get("name") or handle
    followers = author.get("followers") or author.get("followersCount") or 0
    url = t.get("url") or t.get("twitterUrl") or f"https://x.com/{handle}/status/{t.get('id')}"
    likes = t.get("likeCount", 0); rts = t.get("retweetCount", 0); views = t.get("viewCount", 0)
    tag = " · ".join(reasons) if reasons else ""
    if verdict:
        tag = (tag + " · " if tag else "") + f"judge:{verdict.get('confidence')} {verdict.get('reason','')}"
    header = "New Etched mention" if kind == "main" else "Possible mention — needs a look"
    if not txt: txt = "_(no text — shared a link)_"
    context = f"❤️ {likes}  🔁 {rts}  👁 {views}  |  {name} (@{handle}, {followers} followers)"
    if tag: context += f"  |  _{tag}_"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{header}* by *<{url}|@{handle}>*\n>{txt}\n<{url}|View tweet on X →>"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
    ]
    return f"{header} by @{handle}: {url}", blocks

def slack_webhook(webhook, fallback, blocks):
    _post_json(webhook, {"text": fallback, "blocks": blocks})

def slack_api(token, channel, fallback, blocks, thread_ts=None):
    payload = {"channel": channel, "text": fallback, "blocks": blocks}
    if thread_ts: payload["thread_ts"] = thread_ts
    raw = _post_json("https://slack.com/api/chat.postMessage", payload,
                     headers={"Authorization": f"Bearer {token}"})
    d = json.loads(raw)
    if not d.get("ok"):
        print(f"  [slack api error] {d.get('error')}", flush=True); return None
    return d.get("ts")

def review_anchor_ts(state):
    """One 'review queue' anchor message per UTC day; review items reply under it."""
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    anchors = state.setdefault("review_anchors", {})
    if anchors.get(date):
        return anchors[date]
    ts = slack_api(SLACK_BOT_TOKEN, SLACK_REVIEW_CHANNEL,
                   f"Etched mention — review queue ({date})",
                   [{"type": "section", "text": {"type": "mrkdwn",
                     "text": f":mag: *Review queue — {date}*\nItems I couldn't confirm either way. "
                             f"Replies below — glance and ignore if not relevant."}}])
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

    # 2) classify. Build a judge queue; resolve bare links along the way.
    to_main, to_review, judge_queue = [], [], []
    fetches = 0
    for tid, t in fetched.items():
        d, extra = structural(t)
        if d == "reject":
            continue
        if d == "accept":
            to_main.append((t, extra, None)); continue
        if d == "unresolved":
            to_review.append((t, ["unresolved_link"], None)); continue
        if d == "maybe":
            judge_queue.append((t, None, "judge")); continue
        if d == "fetch":
            if fetches >= MAX_FETCH:
                to_review.append((t, ["fetch_cap"], None)); continue
            fetches += 1
            _, content = fetch_url_text(extra[0] if extra else "")
            if content and (ETCHED_WORD_RE.search(content) or SOHU_WORD_RE.search(content)):
                judge_queue.append((t, content, "link"))
            else:
                to_review.append((t, ["unresolved_link"], None))   # couldn't read it -> review, never guess

    # judge most-promising first so a noise spike can't starve a real mention
    def _prio(item):
        t, content = item[0], item[1]
        foll = (t.get("author") or {}).get("followers") or 0
        return (1 if TECH_RE.search(content or t.get("text") or "") else 0, foll)
    judge_queue.sort(key=_prio, reverse=True)

    judged = 0
    for t, content, kind in judge_queue:
        if judged >= MAX_JUDGE_CALLS:
            to_review.append((t, ["cap_reached_unjudged"], None)); continue
        v = judge(t, content); judged += 1
        if v is None:
            to_review.append((t, ["judge_unavailable"], None)); continue
        conf = float(v.get("confidence", 0) or 0); rel = bool(v.get("relevant"))
        if conf < CONF_ACCEPT:
            to_review.append((t, ["uncertain"], v)); continue
        if not rel:
            continue                                   # we looked — it isn't Etched -> drop
        to_main.append((t, ["link_relevant" if kind == "link" else "judge_relevant"], v))

    print(f"[tier] main={len(to_main)}  review={len(to_review)}  judged={judged}  fetched_links={fetches}"
          f"{'  (JUDGE CAP HIT)' if judged >= MAX_JUDGE_CALLS else ''}", flush=True)

    # 3) deliver (oldest first)
    def keyf(x): return x[0].get("createdAt", "")
    if dry:
        def _show(title, rows):
            print(f"\n--- {title} ({len(rows)}) ---", flush=True)
            for t, reasons, v in sorted(rows, key=keyf):
                c = f" conf={v.get('confidence')}" if v else ""
                print(f"  @{(t.get('author') or {}).get('userName')}: "
                      f"{(t.get('text') or '')[:80]!r}  [{','.join(reasons)}]{c}", flush=True)
        _show("WOULD POST TO MAIN", to_main)
        _show("WOULD POST TO REVIEW", to_review)
    else:
        for t, reasons, v in sorted(to_main, key=keyf):
            try:
                fb, bl = build_msg(t, reasons, v, "main"); slack_webhook(SLACK_MAIN, fb, bl)
            except Exception as e: print(f"  [slack main error] {e}", flush=True)
        # review destination: threaded bot replies > separate webhook > held (never main)
        if to_review and SLACK_BOT_TOKEN and SLACK_REVIEW_CHANNEL:
            ts = review_anchor_ts(state)
            for t, reasons, v in sorted(to_review, key=keyf):
                try:
                    fb, bl = build_msg(t, reasons, v, "review"); slack_api(SLACK_BOT_TOKEN, SLACK_REVIEW_CHANNEL, fb, bl, thread_ts=ts)
                except Exception as e: print(f"  [slack review error] {e}", flush=True)
        elif to_review and SLACK_REVIEW_WEBHOOK:
            for t, reasons, v in sorted(to_review, key=keyf):
                try:
                    fb, bl = build_msg(t, reasons, v, "review"); slack_webhook(SLACK_REVIEW_WEBHOOK, fb, bl)
                except Exception as e: print(f"  [slack review error] {e}", flush=True)
        elif to_review:
            print(f"[review] {len(to_review)} item(s) held (no review destination configured; NOT sent to main)", flush=True)

    # 4) persist
    seen.update(fetched.keys())
    state["seen_ids"] = list(seen)
    state["last_run_epoch"] = now
    if not dry: save_state(state)
    print(f"[done] main={len(to_main)} review={len(to_review)}", flush=True)

if __name__ == "__main__":
    main()
