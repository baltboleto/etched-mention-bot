#!/usr/bin/env python3
"""
Measure real query volume over the last 24h to project running cost.
Reuses monitor's exact QUERIES + classifier so the numbers match production.

Bills to model:
  twitterapi.io search = per TWEET RETURNED ($0.00015). Queries overlap, so the
    same tweet can be returned by >1 query and billed >1x -> we sum RAW per-query.
  Claude judge (Haiku) = per DEDUPED "maybe" tweet (~$0.00056/call).
"""
import time, monitor as m

HOURS = 24
SEARCH_PRICE = 0.00015
JUDGE_PRICE  = 0.00056   # ~360 in + 40 out tokens on Haiku 4.5
CAP_PAGES = 90           # 1800 tweets/query; flag if hit (would mean undercount)

since = int(time.time()) - HOURS * 3600
raw_total = 0
deduped = {}
print(f"Measuring last {HOURS}h across {len(m.QUERIES)} queries...\n")
for q in m.QUERIES:
    tweets = m.search(q, since, max_pages=CAP_PAGES)
    raw_total += len(tweets)
    for t in tweets:
        tid = str(t.get("id"))
        if tid:
            deduped[tid] = t
    trunc = " (TRUNCATED - undercount!)" if len(tweets) >= CAP_PAGES * 20 else ""
    print(f"  {len(tweets):5d} raw  | {q[:60]}{trunc}")

# classify the deduped set the way production would
accept = reject = maybe = 0
for t in deduped.values():
    d, _ = m.structural(t)
    if   d == "accept": accept += 1
    elif d == "reject": reject += 1
    else:               maybe  += 1

# steady state re-fetches a small overlap window each run; pad search billing ~15%
overlap_factor = 1.15
search_day  = raw_total * overlap_factor
judge_day   = maybe                      # deduped; each judged once
search_mo   = search_day * 30
judge_mo    = judge_day * 30

print("\n" + "="*64)
print(f"RAW tweets returned (billable search), 24h : {raw_total}")
print(f"  + ~15% overlap re-fetch (15-min cadence)  : {search_day:.0f}/day")
print(f"DEDUPED candidates, 24h                     : {len(deduped)}")
print(f"  -> Tier A auto-accept (no LLM)            : {accept}")
print(f"  -> Tier C cheap-reject (no LLM)           : {reject}")
print(f"  -> Tier B judged by Claude               : {maybe}")
print("="*64)
print(f"SEARCH cost : {search_day:.0f}/day  x30 = {search_mo:.0f}/mo  x ${SEARCH_PRICE} = ${search_mo*SEARCH_PRICE:.2f}/mo")
print(f"JUDGE  cost : {judge_day}/day  x30 = {judge_mo:.0f}/mo  x ${JUDGE_PRICE} = ${judge_mo*JUDGE_PRICE:.2f}/mo")
print(f"TOTAL       : ${(search_mo*SEARCH_PRICE + judge_mo*JUDGE_PRICE):.2f}/month")
print("="*64)
