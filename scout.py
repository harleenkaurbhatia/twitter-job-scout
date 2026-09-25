"""Daily Twitter job scout: search X via twitterapi.io, rank with Claude, post to Slack."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests

ROOT = Path(__file__).parent
LOOKBACK_HOURS = 26  # a bit over a day so nothing slips between runs
BATCH_SIZE = 150  # tweets per Claude call

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


def expanded_urls(obj):
    """Collect every expanded_url nested anywhere in a twitterapi.io entities blob."""
    found = []
    if isinstance(obj, dict):
        if obj.get("expanded_url"):
            found.append(obj["expanded_url"])
        for v in obj.values():
            found += expanded_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            found += expanded_urls(v)
    return list(dict.fromkeys(found))


def search_page(key, query, cursor):
    """One page of advanced search. Free tier allows ~1 request per 5s; pace calls and back off on 429."""
    for attempt in range(4):
        time.sleep(6 * (attempt + 1))
        try:
            r = requests.get(
                "https://api.twitterapi.io/twitter/tweet/advanced_search",
                headers={"X-API-Key": key},
                params={"query": query, "queryType": "Latest", "cursor": cursor},
                timeout=30,
            )
            if r.status_code == 429:
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"query failed: {query[:60]}... ({e})", file=sys.stderr)
            return None
    return None


def fetch_tweets(hours=LOOKBACK_HOURS, pages=1):
    key = os.environ["TWITTERAPI_KEY"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    since = (cutoff - timedelta(days=1)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for q in QUERIES:
        query = f"{q} -filter:retweets lang:en since:{since}"
        tweets, cursor = [], ""
        for _ in range(pages):
            data = search_page(key, query, cursor)
            if not data:
                print(f"query skipped: {q[:60]}...", file=sys.stderr)
                break
            tweets += data.get("tweets", [])
            cursor = data.get("next_cursor")
            if not data.get("has_next_page") or not cursor:
                break
        for t in tweets:
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
                "links": expanded_urls([t.get("entities"), (t.get("quoted_tweet") or {}).get("entities")]),
                "author": {
                    "handle": a.get("userName"),
                    "name": a.get("name"),
                    "bio": a.get("description") or (a.get("profile_bio") or {}).get("description", ""),
                    "links": expanded_urls([a.get("entities"), a.get("profile_bio")]),
                    "location": a.get("location"),
                    "followers": a.get("followers"),
                    "can_dm": a.get("canDm"),
                },
            })
    print(f"fetched {len(out)} unique tweets from {len(QUERIES)} queries", file=sys.stderr)
    return out


RANK_PROMPT = """You are my job scout. Below is my profile, then a JSON list of recent tweets.

{profile}

For each tweet, decide if it is a genuinely relevant opportunity for me. Ignore: recruiters spamming,
job-board bots, engineering-only roles (unless FDE), internships, low-pay signals, people LOOKING for jobs,
and anything not in my target roles. Use the author's bio to judge founder pedigree (IIT, IIM, YC, Ivy, ex-FAANG, etc).

Pick the relevant ones (best first, max {max_picks}). Then, for each pick, use WebSearch to research it:
- the LinkedIn profile URL of the person I should contact: the poster if they are the founder/hiring manager,
  otherwise the founder or hiring manager (only return a URL you are confident is the right person, else "")
- what the company does (one line)
- the application link: prefer a job/apply URL from the tweet's "links"; otherwise the company's careers page
  or the specific job listing you find; else "" (then I'll DM them).

Return ONLY a JSON array (no prose, no code fences):
[{{"id": "...", "category": "REMOTE_INTL|ONSITE_ABROAD|INDIA_TOP|REACH_OUT", "score": 1-10,
  "role": "...", "role_summary": "1-2 lines: what the job actually involves and who it's for",
  "company": "...", "company_blurb": "one line on what the company does, stage/funding if known",
  "location": "...", "pay": "stated pay or 'not stated'", "seniority": "e.g. 0-3 yrs / early career / senior",
  "poster": "poster's name + title, with pedigree evidence (IIT, YC, ex-Stripe...) or 'unknown'",
  "contact": "full name of the person I should contact",
  "linkedin": "that person's https://www.linkedin.com/in/... or ''",
  "apply_link": "https://... or ''",
  "why": "one line on why this fits me",
  "opener": "a one-line personalised DM opener I could send"}}]
Score higher for: my priority order, strong founder pedigree, direct founder contact, explicit high pay, high agency,
and roles open to early-career people (0-4 yrs). Score senior-only roles (8+ yrs, VP/Director) low or skip them.
Refer to people by name or they/them, never guess pronouns. If nothing is relevant, return [].

TWEETS:
{tweets}
"""


def rank(tweets, max_picks=15):
    """Rank in batches so a week of tweets fits in Claude's context; keep the best overall."""
    picks = []
    for i in range(0, len(tweets), BATCH_SIZE):
        batch = tweets[i:i + BATCH_SIZE]
        print(f"ranking tweets {i + 1}-{i + len(batch)} of {len(tweets)}", file=sys.stderr)
        try:
            picks += rank_batch(batch, min(max_picks, 10))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            print(f"batch failed: {e}", file=sys.stderr)
    return sorted(picks, key=lambda p: -int(p.get("score") or 0))[:max_picks]


def rank_batch(tweets, max_picks):
    if not tweets:
        return []
    prompt = RANK_PROMPT.format(
        max_picks=max_picks,
        profile=(ROOT / "profile.md").read_text(),
        tweets=json.dumps(tweets, ensure_ascii=False),
    )
    # Headless Claude Code; authenticates via CLAUDE_CODE_OAUTH_TOKEN (Claude Pro/Max plan).
    res = subprocess.run(
        ["claude", "-p", "--output-format", "json", "--model", "sonnet", "--allowedTools", "WebSearch"],
        input=prompt, capture_output=True, text=True, timeout=1500,
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
        p["text"] = t.get("text", "")
        p["handle"] = (t.get("author") or {}).get("handle", "")
        p["name"] = (t.get("author") or {}).get("name", "")
    return picks


SECTIONS = [
    ("REMOTE_INTL", ":globe_with_meridians: Remote from India, intl companies"),
    ("ONSITE_ABROAD", ":airplane: Onsite abroad"),
    ("INDIA_TOP", ":flag-in: Top India roles"),
    ("REACH_OUT", ":wave: Founders / startups worth reaching out to"),
]


def esc(s):
    return str(s or "").replace("<", "&lt;").replace(">", "&gt;")


def format_slack(picks, n_scanned, title_suffix=""):
    date = datetime.now(timezone.utc).strftime("%d %b %Y")
    header = f"*Job scout, {date}{title_suffix}*: {len(picks)} picks from {n_scanned} tweets scanned"
    if not picks:
        return [header + "\nNothing worth your time today."]
    chunks = [header]
    for cat, title in SECTIONS:
        items = [p for p in picks if p.get("category") == cat]
        if not items:
            continue
        chunks.append(f"\n*{title}*")
        for p in items:
            linkedin = p.get("linkedin") or (
                "https://www.linkedin.com/search/results/people/?keywords="
                + quote_plus(f"{p.get('contact') or p.get('name', '')} {p.get('company', '')}")
            )
            li_label = f"{p.get('contact') or 'Poster'} on LinkedIn" if p.get("linkedin") else f"Search {p.get('contact') or 'poster'} on LinkedIn"
            apply = (f"<{p['apply_link']}|Apply here>" if p.get("apply_link")
                     else f"No link found. DM <https://x.com/{p.get('handle')}|@{esc(p.get('handle'))}> on X")
            post = esc(p.get("text", "")).replace("\n", " ")
            post = post[:400] + ("…" if len(post) > 400 else "")
            chunks.append(
                f"*[{p.get('score')}/10] {esc(p.get('role'))} @ {esc(p.get('company'))}*"
                f" · {esc(p.get('location'))} · {esc(p.get('pay'))} · {esc(p.get('seniority'))}\n"
                f":bust_in_silhouette: *Posted by:* {esc(p.get('poster'))} · <{linkedin}|{li_label}>\n"
                f":page_facing_up: *Role:* {esc(p.get('role_summary'))}\n"
                f":office: *Company:* {esc(p.get('company'))}: {esc(p.get('company_blurb'))}\n"
                f":link: *Apply:* {apply}\n"
                f":bird: *Post:* <{p.get('url')}|Open on X>\n> {post}\n"
                f":dart: _{esc(p.get('why'))}_\n"
                f":speech_balloon: DM opener: _{esc(p.get('opener'))}_\n"
            )
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
    ap.add_argument("--days", type=float, default=LOOKBACK_HOURS / 24, help="how far back to scan")
    ap.add_argument("--pages", type=int, default=1, help="result pages per query (~20 tweets each)")
    ap.add_argument("--max-picks", type=int, default=15)
    args = ap.parse_args()
    load_env()

    tweets = json.loads(Path(args.sample).read_text()) if args.sample else fetch_tweets(args.days * 24, args.pages)
    picks = rank(tweets, args.max_picks)
    suffix = f" (past {args.days:g} days)" if args.days > 1.5 else ""
    msgs = format_slack(picks, len(tweets), suffix)
    if args.dry_run:
        print("\n---\n".join(msgs))
    else:
        post_slack(msgs)
        print(f"posted {len(picks)} picks to Slack", file=sys.stderr)


if __name__ == "__main__":
    main()
