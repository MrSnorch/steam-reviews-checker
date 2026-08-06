#!/usr/bin/env python3
"""
Enrich analyzed reviews with account-level data from the official Steam Web API:
  - ISteamUser/GetPlayerSummaries -> account creation date, profile visibility
  - IPlayerService/GetOwnedGames  -> total games owned, total playtime across
                                     the whole library (not just this game)

Requires a free Steam Web API key: https://steamcommunity.com/dev/apikey
Pass it via --api-key or the STEAM_WEB_API_KEY environment variable.

This is a separate, optional pipeline step because it costs one extra round
of API calls per unique author (batched 100 steamids at a time for
GetPlayerSummaries; GetOwnedGames must be called one steamid at a time,
so this step is the slow one and includes its own rate limiting).

Usage:
    python enrich_accounts.py --in tmp/analyzed_reviews.json \
        --out tmp/enriched_reviews.json --api-key $STEAM_WEB_API_KEY

If a profile is private, GetOwnedGames returns nothing useful for it -- this
is itself recorded as a signal (private profiles are disproportionately
common among throwaway/farm accounts).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
USER_AGENT = "Mozilla/5.0 (compatible; steam-review-watch/1.0)"


def http_get_json(url: str, params: dict, max_retries: int = 4) -> dict:
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
            print(f"  [warn] attempt {attempt}/{max_retries} failed for {url}: {e}", file=sys.stderr)
            if attempt == max_retries:
                return {}
            time.sleep(backoff)
            backoff *= 2
    return {}


def fetch_player_summaries(steamids: list[str], api_key: str, sleep_s: float = 0.6) -> dict:
    """Batched, up to 100 steamids per call. Returns {steamid: summary_dict}."""
    out = {}
    for i in range(0, len(steamids), 100):
        chunk = steamids[i:i + 100]
        data = http_get_json(SUMMARIES_URL, {
            "key": api_key,
            "steamids": ",".join(chunk),
        })
        players = data.get("response", {}).get("players", [])
        for p in players:
            out[p.get("steamid")] = p
        print(f"  [info] fetched summaries for {len(players)}/{len(chunk)} in batch "
              f"{i // 100 + 1}/{(len(steamids) + 99) // 100} ({len(out)} total so far)", flush=True)
        time.sleep(sleep_s)
    return out


def fetch_owned_games(steamid: str, api_key: str) -> dict | None:
    data = http_get_json(OWNED_GAMES_URL, {
        "key": api_key,
        "steamid": steamid,
        "include_appinfo": 0,
        "include_played_free_games": 1,
    })
    resp = data.get("response", {})
    if "game_count" not in resp:
        # private profile or no games / API error
        return None
    total_minutes = sum(g.get("playtime_forever", 0) for g in resp.get("games", []))
    return {
        "total_games_owned_api": resp.get("game_count", 0),
        "total_playtime_minutes_api": total_minutes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--api-key", default=None, help="Steam Web API key (or set STEAM_WEB_API_KEY env var)")
    ap.add_argument("--sleep", type=float, default=0.6, help="seconds between GetOwnedGames calls")
    ap.add_argument("--max-authors", type=int, default=None,
                     help="cap number of unique authors enriched, for testing / quota control")
    ap.add_argument("--report-out", default=None,
                     help="optional path to write a JSON run report for CI summaries")
    args = ap.parse_args()

    import os
    api_key = args.api_key or os.environ.get("STEAM_WEB_API_KEY")
    if not api_key:
        print("No Steam Web API key provided (--api-key or STEAM_WEB_API_KEY). "
              "Skipping enrichment, copying input to output unchanged.", file=sys.stderr)
        with open(args.inp, encoding="utf-8") as f:
            reviews = json.load(f)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(reviews, f, ensure_ascii=False, separators=(",", ":"))
        if args.report_out:
            report = {
                "step": "enrich_accounts", "ok": True, "skipped": True,
                "reason": "no_api_key", "authors_total": 0, "authors_enriched": 0,
                "private_profiles": 0,
            }
            with open(args.report_out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False)
        return

    with open(args.inp, encoding="utf-8") as f:
        reviews = json.load(f)

    unique_ids = sorted({r["steamid"] for r in reviews if r.get("steamid")})
    if args.max_authors:
        unique_ids = unique_ids[:args.max_authors]
    total_authors = len(unique_ids)
    print(f"Enriching {total_authors} unique authors out of {len(reviews)} reviews...", flush=True)

    est_total_s = total_authors * args.sleep + (total_authors / 100) * 0.6
    print(f"Estimated time for this step: ~{est_total_s / 60:.1f} min "
          f"({total_authors} GetOwnedGames calls @ {args.sleep}s apart, plus summary batches)", flush=True)

    print("::group::Fetching player summaries (batched, up to 100 ids per call)", flush=True)
    summaries = fetch_player_summaries(unique_ids, api_key)
    print("::endgroup::", flush=True)

    print(f"::group::Fetching owned games per author ({total_authors} calls, "
          f"~{args.sleep}s apart -- this is the slow step)", flush=True)
    start_t = time.time()
    owned_games = {}
    for idx, sid in enumerate(unique_ids, 1):
        og = fetch_owned_games(sid, api_key)
        if og is not None:
            owned_games[sid] = og

        elapsed = time.time() - start_t
        rate = idx / elapsed if elapsed > 0 else 0
        remaining = (total_authors - idx) / rate if rate > 0 else 0
        pct = 100 * idx / total_authors

        if idx % 10 == 0 or idx == total_authors or idx == 1:
            print(
                f"  [{idx}/{total_authors}, {pct:.0f}%] {sid} -> "
                f"{'public' if og is not None else 'private/no data'} | "
                f"public so far: {len(owned_games)} | "
                f"elapsed: {elapsed:.0f}s | ETA: ~{remaining:.0f}s",
                flush=True,
            )
        time.sleep(args.sleep)
    print("::endgroup::", flush=True)

    now = int(time.time())
    enriched_count = 0
    private_count = 0
    for r in reviews:
        sid = r.get("steamid")
        if not sid:
            continue
        summ = summaries.get(sid)
        if summ:
            r["account_created_ts"] = summ.get("timecreated")
            r["account_visibility"] = summ.get("communityvisibilitystate")  # 1=private, 3=public
            r["account_age_days_at_review"] = (
                round((r["timestamp_created"] - summ["timecreated"]) / 86400, 1)
                if summ.get("timecreated") and r.get("timestamp_created") else None
            )
            if summ.get("communityvisibilitystate") == 1:
                private_count += 1
        og = owned_games.get(sid)
        if og:
            r["total_games_owned_api"] = og["total_games_owned_api"]
            r["total_playtime_hours_api"] = round(og["total_playtime_minutes_api"] / 60, 1)
            enriched_count += 1

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Done. {enriched_count} reviews enriched with owned-games data, "
          f"{private_count} authors have private profiles. Written to {args.out}")

    if args.report_out:
        report = {
            "step": "enrich_accounts",
            "ok": True,
            "skipped": False,
            "authors_total": len(unique_ids),
            "authors_enriched": len(owned_games),
            "authors_with_summary": len(summaries),
            "private_profiles": private_count,
        }
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
