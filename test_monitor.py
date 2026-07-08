#!/usr/bin/env python3
"""
Offline test for the tiered classifier (no API keys needed).

Feeds realistic tweets through structural() and checks the tier decision:
  accept -> Tier A (auto-post to main)
  reject -> Tier C (dropped cheaply, no LLM)
  maybe  -> Tier B (escalated to the Claude judge)

Then simulates the Tier-B confidence routing and previews a Slack payload.
"""
import json
import monitor as m

def tw(text="", handle="someuser", name=None, mentions=None, reply_to=None,
       quoted_by=None, quoted_text="", retweeted_by=None, is_reply=False,
       urls=None, likes=12, rts=3, views=900, tid="1"):
    t = {
        "id": tid, "text": text,
        "author": {"userName": handle, "name": name or handle, "followers": 5000},
        "url": f"https://x.com/{handle}/status/{tid}",
        "likeCount": likes, "retweetCount": rts, "viewCount": views,
        "createdAt": "Sat Jul 05 12:00:00 +0000 2026",
        "isReply": is_reply,
    }
    if mentions or urls:
        t["entities"] = {}
        if mentions: t["entities"]["user_mentions"] = [{"screen_name": h} for h in mentions]
        if urls: t["entities"]["urls"] = [{"expanded_url": u} for u in urls]
    if reply_to:
        t["inReplyToUsername"] = reply_to
    if quoted_by is not None:
        t["quoted_tweet"] = {"author": {"userName": quoted_by}, "text": quoted_text}
    if retweeted_by is not None:
        t["retweeted_tweet"] = {"author": {"userName": retweeted_by}}
    return t

# (label, tweet, expected_tier)
CASES = [
    # ---- Tier A: strong signals, should auto-accept ----
    ("tags @Etched",                 tw("Just saw the @Etched demo, wild", mentions=["Etched"]),            "accept"),
    ("reply to @Etched",             tw("congrats!!", reply_to="Etched", is_reply=True),                    "accept"),
    ("quote-tweets @Etched",         tw("this is huge", quoted_by="Etched", quoted_text="Meet Sohu"),        "accept"),
    ("retweets @UbertiGavin",        tw("", retweeted_by="UbertiGavin"),                                    "accept"),
    ("links etched.com",             tw("read more at etched.com/sohu"),                                    "accept"),
    ("etched.ai in text",            tw("check etched.ai they are cooking"),                                "accept"),
    ("founder full name",            tw("Gavin Uberti is onto something with this chip"),                   "accept"),
    ("Sohu + chip context",          tw("The Sohu chip does 500k tokens/sec on Llama 70B"),                 "accept"),

    # ---- Tier C: clearly NOT the company, should drop cheaply ----
    ("etched in my memory",          tw("That sunset is etched into my memory forever"),                    "reject"),
    ("etched glass gift",            tw("Handmade etched glass vase, personalized engraving"),              "reject"),
    ("etched tattoo",                tw("New tattoo — his name etched on my wrist"),                        "reject"),
    ("etched wooden sign",           tw("Custom etched wooden sign for the wedding"),                       "reject"),
    ("etched name in history",       tw("Australia Women just etched their name in history, 7th title!"),   "reject"),
    ("forever etched",               tw("What a World Cup. Forever etched. ❤️"),                            "reject"),
    ("etched into my bones",         tw("this series will still be etched into my bones"),                  "reject"),

    # ---- Tier B: ambiguous, should escalate to the judge ----
    ("bare company claim",           tw("Etched is building the fastest inference chip on earth"),          "maybe"),
    ("semiconductor etching trap",   tw("The pattern is etched onto the silicon wafer at the fab"),         "maybe"),
    ("Sohu.com (Chinese co)",        tw("Sohu reports Q2 earnings, stock up 3%"),                           "maybe"),
    ("Sohu bare, no context",        tw("just watched something on Sohu lol"),                             "maybe"),
    ("etched, vague tech-ish",       tw("etched really changing the game huh"),                            "maybe"),

    # ---- NEW: bare-link handling (the Ronaldo class) ----
    # X-native links (X Articles, linked tweets) now resolve via FxTwitter
    ("bare link -> X-native article", tw("https://t.co/abc", urls=["http://x.com/i/article/2069149931655307264"]), "fetch_x"),
    ("bare link -> another tweet",    tw("https://t.co/def", urls=["https://x.com/somebody/status/123456789"]),     "fetch_x"),
    ("bare link -> external article", tw("https://t.co/xyz", urls=["https://espn.com/soccer/ronaldo-world-cup"]),   "fetch"),
    ("bare link -> etched.com",       tw("big news https://t.co/qq", urls=["https://etched.com/blog/sohu"]),        "accept"),
    # a real Etched article link no longer BLIND-accepts on the slug — it goes to
    # fetch+judge (which will confirm & post it). Recall preserved, precision up.
    ("etched-slug -> fetch+judge",    tw("https://t.co/rr", urls=["https://techcrunch.com/2026/07/06/etched-raises-800m/"]), "fetch"),
    # regression: the lion false positive. "etched" in a news slug, zero company link.
    ("lion slug (was false pos)",     tw("Gir's lions: legacy etched forever", urls=["https://navjeevanexpress.com/girs-immortal-kings-jay-veerus-legacy-etched-forever-asias-lion-habitat/"]), "reject"),

    # ---- NEW: non-Latin scripts (no spaces around brand tokens) ----
    ("Chinese Etched+Sohu",          tw("AI芯片初创公司Etched宣布Sohu芯片首次流片成功，累计融资8亿美元"),         "maybe"),
    ("sketched is NOT etched",       tw("I sketched a wretched little drawing today"),                     "fetch_x"),
]

def run():
    print("="*74)
    print("TIER CLASSIFICATION TEST")
    print("="*74)
    passed = 0
    for label, t, expected in CASES:
        decision, reasons = m.structural(t)
        ok = decision == expected
        passed += ok
        flag = "PASS" if ok else "FAIL"
        rsn = ("  <" + "; ".join(reasons) + ">") if reasons else ""
        print(f"[{flag}] {label:28s} -> {decision:7s} (want {expected}){rsn}")
    print("-"*74)
    print(f"{passed}/{len(CASES)} cases correct\n")

    # ---- X-native resolution (mocked FxTwitter, no network) ----
    print("="*74)
    print("X-ARTICLE RESOLUTION TEST (mocked FxTwitter)")
    print("="*74)
    def fake_get(url, headers, retries=4):
        if url.endswith("/status/111"):          # own tweet carries an X Article
            return {"tweet": {"text": "https://t.co/abc", "article": {
                "title": "AI Engineer World's Fair 2026 Wrapped",
                "preview_text": "short preview",
                "content": {"blocks": [
                    {"text": "Latent Space Live Podcast with Etched"},
                    {"text": "Etched is building frontier inference clusters."}]}}}}
        if url.endswith("/status/999"):          # linked tweet, plain text, no article
            return {"tweet": {"text": "Sohu chip racks ship this summer", "article": None}}
        raise RuntimeError("unexpected url " + url)
    orig = m._get_json
    m._get_json = fake_get
    try:
        art_txt = m.fetch_x_native_text({"id": "111"}, "http://x.com/i/article/222")
        link_txt = m.fetch_x_native_text({"id": "333"}, "https://x.com/somebody/status/999")
    finally:
        m._get_json = orig
    # deep-mention excerpt: brand name 10k chars in must survive for the judge
    long_page = "blah word " * 1000 + " The startup Etched makes the Sohu inference chip. " + "more filler " * 800
    ex = m.relevant_excerpt(long_page, 3500)
    checks = [
        ("article title + body extracted", art_txt is not None and "World's Fair" in art_txt
                                           and "frontier inference clusters" in art_txt),
        ("article text triggers keyword",  art_txt is not None and bool(m.ETCHED_WORD_RE.search(art_txt))),
        ("linked tweet text extracted",    link_txt is not None and "Sohu chip racks" in link_txt),
        ("deep mention survives excerpt",  len(ex) <= 3600 and "startup Etched" in ex and "Sohu inference" in ex),
    ]
    xr_passed = 0
    for label, ok in checks:
        xr_passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print("-"*74)
    print(f"{xr_passed}/{len(checks)} resolution checks correct\n")

    # ---- Tier B routing simulation (thresholds live in main()) ----
    print("="*74)
    print("TIER-B ROUTING SIMULATION (what the judge's verdict does)")
    print("="*74)
    sims = [
        ("Etched is building the fastest inference chip", {"relevant": True,  "confidence": 0.95, "reason": "names the AI-chip company"}),
        ("The pattern is etched onto the silicon wafer",  {"relevant": False, "confidence": 0.9,  "reason": "etching = fab process, not the co"}),
        ("Sohu reports Q2 earnings, stock up 3%",         {"relevant": False, "confidence": 0.85, "reason": "Sohu.com Chinese internet co"}),
        ("etched really changing the game huh",           {"relevant": True,  "confidence": 0.45, "reason": "plausibly the co but unclear"}),
    ]
    for text, v in sims:
        conf = float(v["confidence"]); rel = bool(v["relevant"])
        if conf < m.CONF_ACCEPT:  dest = "REVIEW (uncertain)"
        elif rel:                 dest = "MAIN  (confident yes)"
        else:                     dest = "DROP  (confident no)"
        print(f"  conf={conf:.2f} rel={str(rel):5s} -> {dest:22s} | {text[:44]}")
    print(f"\n  (CONF_ACCEPT threshold = {m.CONF_ACCEPT})\n")

    # ---- Slack payload preview (built, not sent) ----
    print("="*74)
    print("SLACK PAYLOAD PREVIEW (built, not sent)")
    print("="*74)
    sample = tw("The Sohu chip does 500k tokens/sec on Llama 70B", handle="chipwatcher",
                name="Chip Watcher", likes=340, rts=88, views=42000, tid="2074249196140437865")
    dec, reasons = m.structural(sample)
    fb, blocks = m.build_msg(sample, reasons, None, "main")
    print("fallback:", fb)
    print(json.dumps({"text": fb, "blocks": blocks}, indent=2))

if __name__ == "__main__":
    run()
