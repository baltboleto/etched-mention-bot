# Etched mention bot

Finds every new X/Twitter mention of **Etched** (the AI chip company) and posts it to Slack.
Runs itself every ~15 minutes on GitHub Actions — no server, always on.

## How it decides what's a real mention

Search casts a wide net (including the bare word `etched`), then a 3-tier filter keeps precision
high without dropping real mentions:

| Tier | What | Action |
|------|------|--------|
| **A** | Tags/replies/quotes of `@Etched`, `@UbertiGavin`, `@robertwachen`; `etched.com`/`etched.ai`; founder full names; "Sohu" + chip context | **auto-post** to main channel |
| **C** | "etched" clearly meaning tattoo / glass / "etched in memory" etc. with no tech context | **drop** (cheap regex) |
| **B** | Everything ambiguous | **Claude Haiku** judges "is this the chip company?" → confident-yes posts, confident-no drops, **unsure → review channel** |

Nothing ambiguous is ever silently dropped — it goes to review so a human sees it. That's the
"don't miss any" safety valve.

## Popular channel (the "actually getting traction" feed)

`popular.py` (X, runs after every monitor pass) and `popular_linkedin.py` (after every LinkedIn
pass) watch everything the monitors post to main and re-post ONLY the risers to a second channel
(#twitter-mentions-popular). Main stays the everything-feed; popular is the glance-feed.

How a post qualifies:

| Piece | X | LinkedIn |
|-------|---|----------|
| score | likes + 2·(RTs+quotes) + replies + bookmarks | reactions + 2·comments + 3·reposts |
| checked at | ~1h after posting (one look; hot-at-discovery checked right away) | ~24h after posting (one look) |
| the bar | max(absolute floor, **95th percentile** of what Etched mentions historically score at that age) | same |
| flood control | each promotion in the last 24h raises the bar 15% (compounding, capped 8×) | same |
| big-account rule | ≥100k followers qualifies at half the bar | — |

The percentile baseline self-seeds on the first run from recent history in `state.json`
(one-time ~$0.30 of twitterapi.io refetches); LinkedIn starts on floors and learns as it goes.
Each post is promoted at most once. State lives in `popular_state.json` / `popular_linkedin_state.json`.

Setup: just invite the bot (the `SLACK_BOT_TOKEN` one) to the popular channel — the channel id
defaults in code. Secrets `SLACK_POPULAR_CHANNEL` / `SLACK_POPULAR_WEBHOOK_URL` override it if
ever needed. Running cost: one fetch per mention — well under $1/month of twitterapi.io,
~$1-2/month of Apify (`apimaestro/linkedin-post-detail`, $5/1k).
Test offline with `python3 test_popular.py`; dry-run with `python3 popular.py --dry`.

## One-time setup (~10 minutes)

### 1. Put this folder in a GitHub repo
```bash
cd etched-mention-bot
git init && git add -A && git commit -m "Etched mention bot"
gh repo create etched-mention-bot --private --source=. --push   # or create via github.com
```

### 2. Create a Slack Incoming Webhook
- Slack → **Settings & administration → Manage apps** (or api.slack.com/apps → *Create New App → From scratch*).
- Add feature **Incoming Webhooks** → toggle **On** → **Add New Webhook to Workspace**.
- Pick the channel (e.g. `#etched-mentions`) → **Allow** → copy the `https://hooks.slack.com/...` URL.
- (Optional) repeat for a second channel like `#etched-mentions-review`.

### 3. Add repo secrets
GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value | Required |
|--------|-------|----------|
| `TWAPI_KEY` | your twitterapi.io key | yes |
| `SLACK_WEBHOOK_URL` | the webhook from step 2 | yes |
| `ANTHROPIC_API_KEY` | Anthropic API key (enables the smart Tier-B judge) | recommended |
| `SLACK_REVIEW_WEBHOOK_URL` | second webhook for "needs a look" items | optional |

Without `ANTHROPIC_API_KEY`, ambiguous tweets all route to the review channel instead of being
auto-classified (still no misses, just noisier).

### 4. Turn it on
Repo → **Actions** tab → enable workflows → **Etched mention monitor** → **Run workflow** to test.
After that it runs on its own every 15 min.

## Test locally first
```bash
export TWAPI_KEY=...            # required
export ANTHROPIC_API_KEY=...    # optional but recommended
python3 monitor.py --dry        # prints what it WOULD post, sends nothing, saves no state
```

## Tuning
All knobs are constants at the top of `monitor.py`:
- `QUERIES` — search terms (wide net). Add/remove as needed.
- `CONF_ACCEPT` — judge confidence needed to auto-post (default 0.6).
- `MAX_JUDGE_CALLS` — per-run cap on LLM calls (default 400; excess → review, logged, never silent).
- `FIRST_RUN_LOOKBACK_HOURS` — how far back the very first run looks (default 3h; kept small so the high-volume bare-`etched` query isn't page-capped on cold start — steady-state runs fetch only new tweets and are always complete).
- Retweets: queries use `-filter:retweets` (drops pure re-shares). Remove it to include raw RTs.

## Cost
Pennies/month. Search is $0.00015/tweet; the Haiku judge only runs on the ambiguous slice and each
call is a fraction of a cent. A quiet day is well under $1 all-in.
