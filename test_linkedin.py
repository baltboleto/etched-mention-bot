#!/usr/bin/env python3
"""
Offline test for the LinkedIn classifier (no API keys needed).

Synthetic cases mirror test_monitor.py; optionally replays a real Apify
capture (pass a path to a JSON dump) to eyeball the tier split on live data.
"""
import json, sys
import linkedin as li

def post(text="", name="Some Person", atype="profile", universal=None, info="", pid="1"):
    return {
        "id": pid, "content": text,
        "author": {"name": name, "type": atype, "universalName": universal,
                   "info": info, "linkedinUrl": "https://linkedin.com/in/x"},
        "linkedinUrl": f"https://www.linkedin.com/posts/x-activity-{pid}",
        "engagement": {"likes": 5, "comments": 1, "shares": 0},
        "postedAt": {"timestamp": 1783489604371},
        "header": {"text": None},
    }

CASES = [
    # accept: strong signals
    ("links etched.com",       post("Congrats! etched.com/blog/sohu is live"),                        "accept"),
    ("founder name",           post("Great conversation with Gavin Uberti about inference"),          "accept"),
    ("Sohu + tech context",    post("The Sohu chip claims 10x inference throughput vs GPUs"),         "accept"),
    # reject: clear non-company
    ("etched in memory",       post("A day forever etched in my memory. Thanks team!"),               "reject"),
    ("laser etching business", post("Custom laser etched glass awards for your next corporate event"),"reject"),
    # own page posts are not mentions
    ("Etched company post",    post("We're hiring!", name="Etched", atype="company", universal="etchedai"), "own_post"),
    # maybe: judge decides
    ("bare company claim",     post("Etched is the most interesting bet in AI hardware right now"),   "maybe"),
    ("fab etching trap",       post("The pattern is etched onto the wafer during lithography"),       "maybe"),
    ("Sohu.com trap",          post("Sohu reported Q2 earnings today"),                               "maybe"),
    ("image-only post",        post(""),                                                             "maybe"),
]

def run():
    print("="*74)
    print("LINKEDIN TIER CLASSIFICATION TEST")
    print("="*74)
    passed = 0
    for label, p, expected in CASES:
        decision, reasons = li.structural_li(p)
        ok = decision == expected
        passed += ok
        rsn = ("  <" + "; ".join(reasons) + ">") if reasons else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {label:26s} -> {decision:8s} (want {expected}){rsn}")
    print("-"*74)
    print(f"{passed}/{len(CASES)} cases correct\n")

    # credit-guard logic (pure functions, no network)
    print("="*74)
    print("CREDIT GUARD TEST")
    print("="*74)
    checks = [
        ("1h window when on schedule",   li.pick_window(3600) == "1h"),
        ("24h window after cron lag",    li.pick_window(3 * 3600) == "24h"),
        ("week window after long pause", li.pick_window(2 * 24 * 3600) == "week"),
        ("month window after >1 week",   li.pick_window(9 * 24 * 3600) == "month"),
        ("no warning when credit ok",    li.credit_warning({"plan": "FREE", "remaining": 3.2, "max": 5, "resets": "2026-08-07"}) is None),
        ("warning when credit low",      "0.72" in (li.credit_warning({"plan": "FREE", "remaining": 0.72, "max": 5, "resets": "2026-08-07"}) or "")),
        ("no warning on paid plan",      li.credit_warning({"plan": "STARTER", "remaining": 0.10, "max": 29, "resets": "2026-08-07"}) is None),
        ("no warning if check failed",   li.credit_warning(None) is None),
        ("warn block added to message",  len(li.build_msg_li(post("Sohu chip"), ["sohu+tech"], None, "main", "WARN")[1]) == 3),
    ]
    cg = 0
    for label, ok in checks:
        cg += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print("-"*74)
    print(f"{cg}/{len(checks)} credit-guard checks correct\n")

    # Slack payload preview (built, not sent)
    sample = post("Etched came out of stealth with $1B in contracts. The Sohu chip is real.",
                  name="Chip Watcher", info="Semiconductor analyst", pid="7480000000000000000")
    dec, reasons = li.structural_li(sample)
    fb, blocks = li.build_msg_li(sample, reasons, {"confidence": 0.97, "reason": "clearly the company"}, "main")
    print("payload preview:", fb)

    # optional: replay a real Apify capture
    if len(sys.argv) > 1:
        items = json.load(open(sys.argv[1]))
        from collections import Counter
        split = Counter()
        for it in items:
            d, _ = li.structural_li(it)
            split[d] += 1
        print(f"\nreplay of {sys.argv[1]} ({len(items)} posts): {dict(split)}")
        for it in items:
            d, r = li.structural_li(it)
            if d == "accept":
                print(f"  ACCEPT [{','.join(r)}] {(it.get('author') or {}).get('name')}: {li.text_of(it)[:90]!r}")

if __name__ == "__main__":
    run()
