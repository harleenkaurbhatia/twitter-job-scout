"""Daily Twitter job scout: search X via twitterapi.io, rank with Claude, post to Slack."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
LOOKBACK_HOURS = 26  # a bit over a day so nothing slips between runs
MAX_CANDIDATES = 250

# X advanced-search queries. Edit freely; each costs roughly one page (~20 tweets).
QUERIES = [
    '("founder\'s office" OR "founders office" OR "founder office") (hiring OR "looking for" OR "join us")',
    '("chief of staff") (hiring OR "looking for") (startup OR founder OR CEO)',
    '("forward deployed" OR "forward-deployed") (hiring OR "looking for")',
    '("founding PM" OR "founding product" OR "product lead" OR "APM") (hiring OR "looking for")',
    'hiring remote India (founder OR startup) (product OR "chief of staff" OR bizops OR "founder\'s office" OR GTM)',
    '(hiring OR "looking for") India (relocate OR relocation OR "visa sponsorship") (Singapore OR Dubai OR SF OR NYC OR London OR Europe)',
    '(YC OR "Y Combinator") (hiring OR "we\'re hiring") ("chief of staff" OR "founder\'s office" OR product OR "forward deployed" OR "founding team")',
    '"high agency" (hiring OR "looking for") (product OR operator OR generalist OR "chief of staff")',
    '("just raised" OR "we raised" OR "series A" OR "seed round") ("we\'re hiring" OR "join us" OR "hiring")',
    '("generalist" OR "operator") hiring (founder OR startup) (remote OR India)',
]


def load_env():
    """Read a local .env if present (GitHub Actions passes real env vars instead)."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def parse_time(s):
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        return None


def fetch_tweets():
    key = os.environ["TWITTERAPI_KEY"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    since = (cutoff - timedelta(days=1)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for q in QUERIES:
        query = f"{q} -filter:retweets lang:en since:{since}"
        try:
            r = requests.get(
                "https://api.twitterapi.io/twitter/tweet/advanced_search",
                headers={"X-API-Key": key},
                params={"query": query, "queryType": "Latest"},
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"query failed: {q[:60]}... ({e})", file=sys.stderr)
            continue
        for t in r.json().get("tweets", []):
            created = parse_time(t.get("createdAt"))
            if t.get("id") in seen or (created and created < cutoff):
                continue
            seen.add(t.get("id"))
            a = t.get("author") or {}
            out.append({
                "id": t.get("id"),
                "url": t.get("url") or f"https://x.com/{a.get('userName')}/status/{t.get('id')}",
                "text": t.get("text", ""),
                "likes": t.get("likeCount", 0),
                "author": {
                    "handle": a.get("userName"),
                    "name": a.get("name"),
                    "bio": a.get("description") or (a.get("profile_bio") or {}).get("description", ""),
                    "location": a.get("location"),
                    "followers": a.get("followers"),
                },
            })
    print(f"fetched {len(out)} unique tweets from {len(QUERIES)} queries", file=sys.stderr)
    return out[:MAX_CANDIDATES]


RANK_PROMPT = """You are my job scout. Below is my profile, then a JSON list of recent tweets.

{profile}

For each tweet, decide if it is a genuinely relevant opportunity for me. Ignore: recruiters spamming,
job-board bots, engineering-only roles (unless FDE), internships, low-pay signals, people LOOKING for jobs,
and anything not in my target roles. Use the author's bio to judge founder pedigree (IIT, IIM, YC, Ivy, ex-FAANG, etc).

Return ONLY a JSON array (no prose, no code fences) of the relevant ones, best first, max 25 items:
[{{"id": "...", "category": "REMOTE_INTL|ONSITE_ABROAD|INDIA_TOP|REACH_OUT", "score": 1-10,
  "role": "...", "company": "...", "location": "...", "pay": "stated pay or 'not stated'",
  "founder": "who is hiring + pedigree evidence from bio, or 'unknown'",
  "why": "one line on why this fits me",
  "opener": "a one-line personalised DM opener I could send"}}]
Score higher for: my priority order, strong founder pedigree, direct founder contact, explicit high pay, high agency.
Refer to people by name or they/them, never guess pronouns. If nothing is relevant, return [].

TWEETS:
{tweets}
"""


def rank(tweets):
    if not tweets:
        return []
    prompt = RANK_PROMPT.format(
        profile=(ROOT / "profile.md").read_text(),
        tweets=json.dumps(tweets, ensure_ascii=False),
    )
    # Headless Claude Code; authenticates via CLAUDE_CODE_OAUTH_TOKEN (Claude Pro/Max plan).
    res = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", "sonnet"],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if res.returncode != 0:
        raise RuntimeError(f"claude failed: {res.stderr or res.stdout}")
    text = json.loads(res.stdout).get("result", "")
    start, end = text.find("["), text.rfind("]")
    if start == -1:
        raise RuntimeError(f"no JSON in Claude output: {text[:500]}")
    picks = json.loads(text[start:end + 1])
    by_id = {t["id"]: t for t in tweets}
    for p in picks:
        t = by_id.get(str(p.get("id")), {})
        p["url"] = t.get("url", "")
        p["handle"] = (t.get("author") or {}).get("handle", "")
    return picks


SECTIONS = [
    ("REMOTE_INTL", ":globe_with_meridians: Remote from India, intl companies"),
    ("ONSITE_ABROAD", ":airplane: Onsite abroad"),
    ("INDIA_TOP", ":flag-in: Top India roles"),
    ("REACH_OUT", ":wave: Founders / startups worth reaching out to"),
]


def esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_slack(picks, n_scanned):
    date = datetime.now(timezone.utc).strftime("%d %b %Y")
    header = f"*Job scout, {date}*: {len(picks)} picks from {n_scanned} tweets scanned"
    if not picks:
        return [header + "\nNothing worth your time today."]
    chunks = [header]
    for cat, title in SECTIONS:
        items = [p for p in picks if p.get("category") == cat]
        if not items:
            continue
        lines = [f"\n*{title}*"]
        for p in items:
            lines.append(
                f"*[{p.get('score')}/10] {esc(p.get('role'))} @ {esc(p.get('company'))}*"
                f" · {esc(p.get('location'))} · {esc(p.get('pay'))}\n"
                f"> :bust_in_silhouette: @{esc(p.get('handle'))}: {esc(p.get('founder'))}\n"
                f"> {esc(p.get('why'))}\n"
                f"> :speech_balloon: _{esc(p.get('opener'))}_\n"
                f"> <{p.get('url')}|Open tweet>"
            )
        chunks.append("\n".join(lines))
    # Slack caps a message around 40k chars; split conservatively.
    msgs, cur = [], ""
    for c in chunks:
        if len(cur) + len(c) > 3500 and cur:
            msgs.append(cur)
            cur = ""
        cur += c + "\n"
    msgs.append(cur)
    return msgs


def post_slack(msgs):
    url = os.environ["SLACK_WEBHOOK_URL"]
    for m in msgs:
        requests.post(url, json={"text": m, "unfurl_links": False}, timeout=30).raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", help="use tweets from a JSON file instead of calling twitterapi.io")
    ap.add_argument("--dry-run", action="store_true", help="print the Slack message instead of posting")
    args = ap.parse_args()
    load_env()

    tweets = json.loads(Path(args.sample).read_text()) if args.sample else fetch_tweets()
    picks = sorted(rank(tweets), key=lambda p: -int(p.get("score") or 0))
    msgs = format_slack(picks, len(tweets))
    if args.dry_run:
        print("\n---\n".join(msgs))
    else:
        post_slack(msgs)
        print(f"posted {len(picks)} picks to Slack", file=sys.stderr)


if __name__ == "__main__":
    main()
