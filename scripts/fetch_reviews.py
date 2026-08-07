#!/usr/bin/env python3
"""
Fetch all reviews for a Steam app via the public appreviews API.

Docs (unofficial but stable): https://partner.steamgames.com/doc/store/getreviews
Endpoint: https://store.steampowered.com/appreviews/{appid}?json=1&...

Usage:
    python fetch_reviews.py --appid 4369490 --out data/raw_reviews.json

Notes:
- Steam paginates via an opaque `cursor` string. Loop until the cursor stops
  changing or no more reviews are returned.
- Steam rate-limits aggressively; we sleep between requests and retry on
  non-200 / malformed JSON.
- We fetch `language=all` and `filter=recent` (chronological, newest first)
  so incremental runs can stop early once they hit already-seen review ids.
- If --incremental is passed with --since-id, stop as soon as we see that
  recommendationid (means we've reached previously fetched data).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://store.steampowered.com/appreviews/{appid}"
USER_AGENT = "Mozilla/5.0 (compatible; steam-review-watch/1.0; +https://github.com/)"

FIELDS_KEEP_TOP = [
    "recommendationid",
    "language",
    "review",
    "timestamp_created",
    "timestamp_updated",
    "voted_up",
    "votes_up",
    "votes_funny",
    "weighted_vote_score",
    "comment_count",
    "steam_purchase",
    "received_for_free",
    "written_during_early_access",
]

FIELDS_KEEP_AUTHOR = [
    "steamid",
    "num_games_owned",
    "num_reviews",
    "playtime_forever",
    "playtime_last_two_weeks",
    "playtime_at_review",
    "last_played",
]


def fetch_page(appid: str, cursor: str, num_per_page: int = 100, language: str = "all",
               max_retries: int = 5) -> dict:
    params = {
        "json": 1,
        "filter": "recent",
        "language": language,
        "cursor": cursor,
        "num_per_page": num_per_page,
        "purchase_type": "all",
    }
    url = API_URL.format(appid=appid) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                return json.loads(data)
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            print(f"  [warn] attempt {attempt}/{max_retries} failed: {e}", file=sys.stderr)
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError("unreachable")


def slim_review(raw: dict) -> dict:
    """Keep only fields we actually use, to keep the JSON small."""
    out = {k: raw.get(k) for k in FIELDS_KEEP_TOP}
    author = raw.get("author", {}) or {}
    for k in FIELDS_KEEP_AUTHOR:
        out[k] = author.get(k)

    # When a developer has replied to a review, Steam includes these two
    # top-level fields on the review object: "developer_response" (the reply
    # text) and "timestamp_dev_responded" (unix timestamp of the reply).
    # Both are absent entirely when there's no reply.
    dev_response = raw.get("developer_response")
    out["developer_response"] = dev_response if dev_response else None
    out["timestamp_dev_responded"] = raw.get("timestamp_dev_responded") if dev_response else None

    return out


def fetch_all_reviews(appid: str, language: str = "all", sleep_s: float = 1.0,
                       stop_at_id: str | None = None, max_pages: int | None = None,
                       num_per_page: int = 100) -> list[dict]:
    reviews = []
    cursor = "*"
    seen_cursors = set()
    page = 0

    while True:
        page += 1
        if max_pages and page > max_pages:
            print(f"  [info] hit max_pages={max_pages}, stopping")
            break

        payload = fetch_page(appid, cursor, num_per_page=num_per_page, language=language)

        if payload.get("success") != 1:
            print(f"  [warn] API returned success != 1 at page {page}: {payload}", file=sys.stderr)
            break

        batch = payload.get("reviews", [])
        if not batch:
            print(f"  [info] no more reviews at page {page}, stopping")
            break

        reached_known = False
        for raw in batch:
            rid = raw.get("recommendationid")
            if stop_at_id is not None and rid == stop_at_id:
                reached_known = True
                break
            reviews.append(slim_review(raw))

        print(f"  [info] page {page}: +{len(batch)} raw reviews (total kept: {len(reviews)})", flush=True)

        if reached_known:
            print(f"  [info] reached previously-seen review {stop_at_id}, stopping incremental fetch")
            break

        next_cursor = payload.get("cursor")
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            print("  [info] cursor stopped advancing, stopping")
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

        time.sleep(sleep_s)

    return reviews


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appid", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--language", default="all")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--since-id", default=None,
                     help="stop once this recommendationid is encountered (incremental mode)")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--num-per-page", type=int, default=100)
    ap.add_argument("--report-out", default=None,
                     help="optional path to write a JSON run report for CI summaries")
    args = ap.parse_args()

    print(f"Fetching reviews for appid={args.appid} ...")
    error = None
    reviews = []
    try:
        reviews = fetch_all_reviews(
            appid=args.appid,
            language=args.language,
            sleep_s=args.sleep,
            stop_at_id=args.since_id,
            max_pages=args.max_pages,
            num_per_page=args.num_per_page,
        )
    except Exception as e:
        error = str(e)
        print(f"[error] fetch failed: {error}", file=sys.stderr)

    print(f"Fetched {len(reviews)} reviews total. Writing to {args.out}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=None, separators=(",", ":"))

    if args.report_out:
        report = {
            "step": "fetch_reviews",
            "ok": error is None,
            "error": error,
            "appid": args.appid,
            "reviews_fetched": len(reviews),
        }
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)

    if error:
        sys.exit(1)


if __name__ == "__main__":
    main()
