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
