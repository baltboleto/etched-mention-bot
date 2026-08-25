#!/usr/bin/env python3
"""
Offline test for the popular-mention trackers (no API keys, nothing sent).

Covers the scoring math, the floor/percentile/damping bar, the single ~1h look
(X) and ~24h look (LinkedIn), the watchlist lifecycle end-to-end with a fake
clock (viral post promoted once, dud never, deleted post dropped, stale post
skipped, everything retired), the bootstrap seeding, the big-account rule, and
the LinkedIn stat extraction across the actor output shapes seen in the wild.
"""
import datetime, json, os, sys, tempfile, types

import monitor as m
import popular as pop
import linkedin as li
import popular_linkedin as pli

PASS = []
def check(label, ok):
    PASS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")

def created_str(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(pop.CREATED_FMT)

def tw(tid, created_epoch, likes=0, rts=0, quotes=0, replies=0, bookmarks=0,
       followers=5000, handle="someuser", text="talking about @Etched today"):
    return {"id": tid, "text": text, "lang": "en",
            "createdAt": created_str(created_epoch),
            "author": {"userName": handle, "followers": followers},
            "url": f"https://x.com/{handle}/status/{tid}",
            "likeCount": likes, "retweetCount": rts, "quoteCount": quotes,
            "replyCount": replies, "bookmarkCount": bookmarks, "viewCount": likes * 40,
            "entities": {"user_mentions": [{"screen_name": "Etched"}]}}

def run():
    tmp = tempfile.mkdtemp()
    print("=" * 74)
    print("UNIT CHECKS")
    print("=" * 74)

    # ---- scoring ----
    t = tw("1", 0, likes=10, rts=3, quotes=2, replies=4, bookmarks=1)
    check("score = likes + 2(rt+q) + replies + bookmarks", pop.score_x(t) == 10 + 2 * 5 + 4 + 1)
    check("score survives missing fields", pop.score_x({"likeCount": "7"}) == 7 and pop.score_x({}) == 0)
    check("createdAt roundtrip", abs(pop.parse_created(created_str(1_787_000_000)) - 1_787_000_000) < 2)
    check("bad createdAt -> 0", pop.parse_created("not a date") == 0)

    # ---- the bar: floors, percentile, damping ----
    st = pop.empty_state()
    now = 2_000_000_000
    check("empty baseline -> floor", pop.bar(st, 1, pop.FLOORS, now) == pop.FLOORS[1])
    st["baseline"]["1"] = [[now, s] for s in range(1, 101)]           # 1..100
    check("p95 of 1..100 beats the 25 floor", 94 <= pop.bar(st, 1, pop.FLOORS, now) <= 97)
    st["baseline"]["1"] = [[now, 1]] * 100
    check("percentile below floor -> floor wins", pop.bar(st, 1, pop.FLOORS, now) == pop.FLOORS[1])
    st["baseline"]["1"] = [[now - pop.BASELINE_MAX_AGE - 10, 500]] * 100
    check("stale samples ignored", pop.bar(st, 1, pop.FLOORS, now) == pop.FLOORS[1])
    st["baseline"]["1"] = [[now, 9]] * 10                             # < MIN_BASELINE
    check("too few samples -> floor", pop.bar(st, 1, pop.FLOORS, now) == pop.FLOORS[1])
    st["promoted"] = [[now - 100, "x", 1]] * 3
    got = pop.bar(st, 1, pop.FLOORS, now)
    check("3 recent promotions raise bar 1.15^3", abs(got - pop.FLOORS[1] * 1.15 ** 3) < 0.01)
    st["promoted"] = [[now - 100, "x", 1]] * 40
    check("damping capped at 8x", pop.bar(st, 1, pop.FLOORS, now) == pop.FLOORS[1] * 8)
    st["promoted"] = [[now - 90000, "x", 1]] * 40                     # older than 24h
    check("old promotions don't damp", pop.bar(st, 1, pop.FLOORS, now) == pop.FLOORS[1])

    # ---- the single look's scheduling ----
    check("age 0.5h -> nothing due", pop.due_cp({"checks": {}}, 0.5, pop.CHECKPOINTS) is None)
    check("age 1.2h -> the 1h look is due", pop.due_cp({"checks": {}}, 1.2, pop.CHECKPOINTS) == 1)
    check("already measured -> nothing due", pop.due_cp({"checks": {"1": 5}}, 1.5, pop.CHECKPOINTS) is None)
    check("skipped counts as handled", pop.due_cp({"checks": {"1": None}}, 2.0, pop.CHECKPOINTS) is None)

    # ---- bootstrap seeding (fake history + fake refetch) ----
    seen_file = os.path.join(tmp, "seen.json")
    ids = [str(2_000_000_000_000 + i) for i in range(60)]
    json.dump({"seen_ids": ids}, open(seen_file, "w"))
    old_state_file, old_batch = m.STATE_FILE, pop.batch_fetch
    m.STATE_FILE = seen_file
    five_days_ago = now - 5 * 86400
    pop.batch_fetch = lambda want: {i: tw(i, five_days_ago, likes=int(i) % 60 * 10) for i in want}
    st = pop.empty_state()
    pop.bootstrap(st, now)
    m.STATE_FILE, pop.batch_fetch = old_state_file, old_batch
    check("bootstrap marks seeded", st["seeded"])
    check("bootstrap seeds the 1h baseline", len(st["baseline"].get("1", [])) == 60)
    b1 = pop.bar(st, 1, pop.FLOORS, now)
    check("seeded 1h bar ~ 35% of final p95", 180 <= b1 <= 210)

    # ---- LinkedIn stat extraction across actor output shapes ----
    shapes = [
        ({"stats": {"total_reactions": 80, "comments": 10, "reposts": 5}}, (80, 10, 5)),
        ({"engagement": {"likes": 7, "comments": 2, "shares": 1}}, (7, 2, 1)),
        ({"numLikes": 12, "numComments": 3, "numShares": 0}, (12, 3, 0)),
        ({"post": {"stats": {"reactions": 40, "commentsCount": 6, "sharesCount": 2}}}, (40, 6, 2)),
        ({"something": "else"}, None),
    ]
    ok = all(pli.extract_stats(item) == want for item, want in shapes)
    check("extract_stats handles known shapes, refuses unknown", ok)
    check("LinkedIn score = r + 2c + 3s", pli.score_li(10, 4, 2) == 10 + 8 + 6)

    # ================================================================ lifecycle
    print()
    print("=" * 74)
    print("X LIFECYCLE SIMULATION (fake clock, fake fetch, fake slack)")
    print("=" * 74)
    t0 = 2_100_000_000
    clock = {"now": t0}
    fleet = {}          # id -> tweet the fake batch_fetch returns (None = deleted)
    delivered = []

    pop.STATE_FILE = os.path.join(tmp, "popular_state.json")
    old_time, old_batch, old_deliver = pop.time, pop.batch_fetch, pop.deliver
    old_seen, old_key = m.STATE_FILE, m.TWAPI_KEY
    pop.time = types.SimpleNamespace(time=lambda: clock["now"])
    pop.batch_fetch = lambda want: {i: fleet[i] for i in want if fleet.get(i)}
    pop.deliver = lambda fb, blocks: delivered.append(fb) or True
    m.STATE_FILE = os.path.join(tmp, "empty_seen.json")   # bootstrap: nothing to seed from
    m.TWAPI_KEY = "test-key"
    sys.argv = [a for a in sys.argv if a != "--dry"]

    # A: viral from minute one. B: dud. C: gets deleted. D: modest but 200k account.
    # E: entered stale (outage recovery) — must be skipped, not measured late.
    A = tw("100", t0 - 1800, likes=400, rts=80, handle="bignews")
    B = tw("200", t0 - 1800, likes=1, handle="quietguy")
    C = tw("300", t0 - 1800, likes=2, handle="deleter")
    D = tw("400", t0 - 1800, likes=3, followers=250_000, handle="vcwhale")
    E = tw("500", t0 - 4 * 3600, likes=900, handle="toolate")
    fleet.update({"100": A, "200": B, "300": C, "400": D, "500": E})
    pop.watch_tweets([A, B, C, D, E])
    s = pop.load_state()
    check("watchlist took all five", len(s["watch"]) == 5)
    pop.watch_tweets([A])
    check("re-add is a no-op", len(pop.load_state()["watch"]) == 5)

    pop.run()   # t0: A already over the bar at discovery -> instant promote; E skipped+retired
    s = pop.load_state()
    check("viral-at-discovery promoted immediately", len(delivered) == 1 and "@bignews" in delivered[0])
    check("stale post skipped, never promoted, retired", "500" not in s["watch"]
          and not any("toolate" in fb for fb in delivered))

    clock["now"] = t0 + int(0.9 * 3600)                    # B/C/D now ~1.4h old
    fleet["300"] = None                                    # C deleted
    fleet["400"] = tw("400", t0 - 1800, likes=14, replies=4, followers=250_000, handle="vcwhale")
    pop.run()   # the 1h look for B (dud), C (missing), D (big-account rule)
    s = pop.load_state()
    bar1 = pop.FLOORS[1] * 1.15                            # one promotion in last 24h
    check("dud not promoted", not s["watch"]["200"]["promoted"])
    check("deleted post got a miss", s["watch"]["300"]["miss"] == 1)
    check("big account promoted at half bar",
          s["watch"]["400"]["promoted"] and 18 >= bar1 * pop.BIG_FACTOR)
    check("big-account note in message", any("vcwhale" in fb for fb in delivered) and len(delivered) == 2)

    clock["now"] = t0 + int(1.4 * 3600)
    pop.run()   # C still in its window, still missing -> second miss -> dropped
    s = pop.load_state()
    check("deleted post dropped after 2 misses", "300" not in s["watch"])
    check("no double promotion", len(delivered) == 2)
    check("baseline collected live samples", len(s["baseline"].get("1", [])) >= 2)

    clock["now"] = t0 + int(3.1 * 3600)
    pop.run()   # everything past 3h -> retired
    s = pop.load_state()
    check("watchlist retired after 3h", len(s["watch"]) == 0)
    check("promotions logged", len(s["promoted"]) == 2)

    pop.time, pop.batch_fetch, pop.deliver = old_time, old_batch, old_deliver
    m.STATE_FILE, m.TWAPI_KEY = old_seen, old_key

    # ================================================================ linkedin lifecycle
    print()
    print("=" * 74)
    print("LINKEDIN LIFECYCLE SIMULATION")
    print("=" * 74)
    pli.STATE_FILE = os.path.join(tmp, "popular_li_state.json")
    lclock = {"now": t0}
    ldelivered = []
    lresults = []
    old_ltime, old_fetch, old_credit, old_tok = pli.time, pli.fetch_details, li.apify_credit, li.APIFY_TOKEN
    pli.time = types.SimpleNamespace(time=lambda: lclock["now"])
    pop.time = types.SimpleNamespace(time=lambda: lclock["now"])   # shared save_state clock
    pli.fetch_details = lambda urls: lresults
    li.apify_credit = lambda: None
    li.APIFY_TOKEN = "test-token"
    old_pdeliver = pop.deliver
    pop.deliver = lambda fb, blocks: ldelivered.append(fb) or True

    hot = {"id": "li1", "linkedinUrl": "https://www.linkedin.com/posts/foo_activity-2222333344445555666-AbCd",
           "postedAt": {"timestamp": (t0 - 3600) * 1000},
           "author": {"name": "Big Voice"}, "content": "Etched just changed inference economics.",
           "engagement": {"likes": 2, "comments": 0, "shares": 0}}
    dud = {"id": "li2", "linkedinUrl": "https://www.linkedin.com/posts/bar_activity-7777888899990000111-XyZw",
           "postedAt": {"timestamp": (t0 - 3600) * 1000},
           "author": {"name": "Quiet Person"}, "content": "etched thoughts on chips",
           "engagement": {"likes": 1, "comments": 0, "shares": 0}}
    stale = {"id": "li3", "linkedinUrl": "https://www.linkedin.com/posts/baz_activity-3333444455556666777-QqQq",
             "postedAt": {"timestamp": (t0 - 40 * 3600) * 1000},
             "author": {"name": "Old News"}, "content": "etched.ai from a while back",
             "engagement": {"likes": 500, "comments": 50, "shares": 20}}
    pli.watch_posts([hot, dud, stale])
    check("LinkedIn watchlist took all three", len(pop.load_state(pli.STATE_FILE)["watch"]) == 3)

    pli.run()   # nothing due yet; the stale one is skipped and retired, never promoted
    s = pop.load_state(pli.STATE_FILE)
    check("stale LinkedIn post skipped + retired", "li3" not in s["watch"] and not ldelivered)

    lclock["now"] = t0 + 25 * 3600   # both due their 24h look
    lresults = [
        {"url": "https://www.linkedin.com/feed/update/urn:li:activity:2222333344445555666/",
         "stats": {"total_reactions": 90, "comments": 12, "reposts": 4}},
        {"url": "https://www.linkedin.com/feed/update/urn:li:activity:7777888899990000111/",
         "stats": {"total_reactions": 4, "comments": 1, "reposts": 0}},
    ]
    pli.run()
    s = pop.load_state(pli.STATE_FILE)
    check("hot LinkedIn post promoted at 24h (126 >= 50)",
          s["watch"]["li1"]["promoted"] and len(ldelivered) == 1 and "Big Voice" in ldelivered[0])
    check("dud recorded, not promoted", s["watch"]["li2"]["checks"]["24"] == 6 and not s["watch"]["li2"]["promoted"])

    lclock["now"] = t0 + 38 * 3600
    lresults = []
    pli.run()
    check("LinkedIn watchlist retired after 36h", len(pop.load_state(pli.STATE_FILE)["watch"]) == 0)

    pli.time, pli.fetch_details, li.apify_credit, li.APIFY_TOKEN = old_ltime, old_fetch, old_credit, old_tok
    pop.deliver, pop.time = old_pdeliver, old_time

    # ================================================================ payload preview
    print()
    print("=" * 74)
    print("SLACK PAYLOAD PREVIEW (built, not sent)")
    print("=" * 74)
    rec = {"author": "chipwatcher", "followers": 8200,
           "url": "https://x.com/chipwatcher/status/2074249196140437865"}
    sample = tw("2074249196140437865", 0, likes=1240, rts=340, quotes=41, replies=55, bookmarks=88,
                handle="chipwatcher", text="The Sohu chip does 500k tokens/sec on Llama 70B. This changes everything.")
    fb, blocks = pop.build_popular_msg(rec, sample, 1.1)
    print("fallback:", fb)
    print(json.dumps({"text": fb, "blocks": blocks}, indent=2)[:900])

    print()
    print("-" * 74)
    print(f"{sum(PASS)}/{len(PASS)} checks passed")
    if not all(PASS):
        sys.exit(1)

if __name__ == "__main__":
    run()
