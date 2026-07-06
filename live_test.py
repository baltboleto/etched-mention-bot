#!/usr/bin/env python3
"""
Live end-to-end test (keys via env). Runs the REAL pipeline in --dry mode:
no Slack posts, no state saved. Reduced lookback + low judge cap = fast & cheap.

  Part A: judge a few synthetic tweets  -> proves the Anthropic leg + JSON parsing
  Part B: live twitterapi.io search     -> classify + judge + route (what it WOULD post)
"""
import sys, monitor as m

# --- keep the test fast/cheap ---
m.FIRST_RUN_LOOKBACK_HOURS = 6
m.MAX_JUDGE_CALLS = 80

print("="*74); print("PART A — judge synthetic tweets (real Claude call)"); print("="*74)
FAKES = [
    "Etched is the transformer ASIC startup — one 8xSohu server replaces 160 H100s",
    "The circuit pattern was etched onto the silicon wafer at the TSMC fab",
    "Sohu.com posts a Q2 revenue beat, shares up 4% premarket",
    "that quote is forever etched into my brain honestly",
]
for txt in FAKES:
    v = m.judge({"text": txt, "author": {"userName": "tester"}})
    print(f"  {str(v):<70} | {txt[:52]}")

print("\n" + "="*74); print("PART B — live search, classify, judge, route (--dry)"); print("="*74)

# log each real judge verdict as the pipeline runs
_orig = m.judge
def logged(t):
    v = _orig(t)
    print(f"   judge-> {v}  | @{(t.get('author') or {}).get('userName')}: {(t.get('text') or '')[:60]!r}", flush=True)
    return v
m.judge = logged

sys.argv = ["monitor.py", "--dry"]
m.main()
