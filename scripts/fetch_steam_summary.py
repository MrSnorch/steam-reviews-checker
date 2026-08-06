#!/usr/bin/env python3
"""
Fetch Steam's own official aggregates for an app, independent of our
paginated review collection:

  1. query_summary (from appreviews with num_per_page=0) -- Valve's own
     total_positive / total_negative / review_score_desc for ALL reviews
     ever, in every language, regardless of what our pagination managed
     to collect. Used as a sanity-check cross-reference: if our collected
     sample's positive % drifts far from Steam's own total, either our
     collection is incomplete or something changed very recently.

  2. appreviewhistogram (undocumented but stable, used by SteamDB-style
     tools) -- day-by-day and month-by-month recommendations_up/down
     counts computed by Steam itself. This lets us see review velocity
     (reviews per day) and spot spikes WITHOUT needing our own review
     text/timestamps to be complete -- Steam counts every review, even
     ones we may not have paginated to yet.

Usage:
    python fetch_steam_summary.py --appid 4369490 --out tmp/steam_summary.json
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
HISTOGRAM_URL = "https://store.steampowered.com/appreviewhistogram/{appid}"
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
            print(f"  [warn] attempt {attempt}/{max_retries} failed: {e}", file=sys.stderr)
            if attempt == max_retries:
                return {}
            time.sleep(backoff)
            backoff *= 2
    return {}


def fetch_query_summary(appid: str) -> dict | None:
    data = http_get_json(APPREVIEWS_URL.format(appid=appid), {
        "json": 1,
        "filter": "all",
        "language": "all",
        "purchase_type": "all",
        "num_per_page": 0,
    })
    if data.get("success") != 1:
        return None
    return data.get("query_summary")


def fetch_recent_summary(appid: str, day_range: int = 30) -> dict | None:
    """Official 'recent reviews' sentiment using Steam's own day_range filter."""
    data = http_get_json(APPREVIEWS_URL.format(appid=appid), {
        "json": 1,
        "filter": "all",
        "language": "all",
        "purchase_type": "all",
        "num_per_page": 0,
        "day_range": day_range,
    })
    if data.get("success") != 1:
        return None
    return data.get("query_summary")


def fetch_histogram(appid: str) -> dict | None:
    data = http_get_json(HISTOGRAM_URL.format(appid=appid), {"l": "english"})
    if data.get("success") != 1:
        return None
    return data.get("results")


def compute_velocity(histogram: dict | None) -> dict:
    """
    Compute reviews/day velocity from the histogram's daily 'recent' bucket
    (Steam typically returns ~30 days of daily granularity there), and flag
    days where volume is an outlier vs the rest of that window -- this is
    Valve's own count, so it catches spikes even in reviews our pagination
    hasn't reached yet (e.g. a very deep review history).
    """
    if not histogram:
        return {"available": False}

    recent = histogram.get("recent", [])
    if not recent:
        return {"available": False}

    daily = [
        {
            "date": r.get("date"),
            "up": r.get("recommendations_up", 0),
            "down": r.get("recommendations_down", 0),
            "total": r.get("recommendations_up", 0) + r.get("recommendations_down", 0),
        }
        for r in recent
    ]

    totals = [d["total"] for d in daily]
    if not totals:
        return {"available": False}

    import statistics
    med = statistics.median(totals) or 1
    spike_days = [d for d in daily if d["total"] >= med * 3 and d["total"] >= 5]

    last7 = daily[-7:] if len(daily) >= 7 else daily
    prev7 = daily[-14:-7] if len(daily) >= 14 else []
    avg_last7 = statistics.mean([d["total"] for d in last7]) if last7 else 0
    avg_prev7 = statistics.mean([d["total"] for d in prev7]) if prev7 else None
    acceleration_pct = (
        round(100 * (avg_last7 - avg_prev7) / avg_prev7, 1)
        if avg_prev7 else None
    )

    return {
        "available": True,
        "daily": daily,
        "median_reviews_per_day": med,
        "spike_days": spike_days,
        "avg_reviews_per_day_last_7d": round(avg_last7, 1),
        "avg_reviews_per_day_prior_7d": round(avg_prev7, 1) if avg_prev7 else None,
        "velocity_change_pct_week_over_week": acceleration_pct,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appid", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report-out", default=None)
    args = ap.parse_args()

    print(f"Fetching Steam's own review summary for appid={args.appid}...", flush=True)
    overall = fetch_query_summary(args.appid)
    print(f"  overall query_summary: {overall}", flush=True)

    time.sleep(1)
    print("Fetching Steam's 'recent 30 days' summary...", flush=True)
    recent30 = fetch_recent_summary(args.appid, day_range=30)
    print(f"  recent(30d) query_summary: {recent30}", flush=True)

    time.sleep(1)
    print("Fetching review histogram (daily/monthly velocity)...", flush=True)
    histogram = fetch_histogram(args.appid)
    velocity = compute_velocity(histogram)
    if velocity.get("available"):
        print(f"  {len(velocity['daily'])} days of histogram data, "
              f"{len(velocity['spike_days'])} spike days, "
              f"7d velocity change: {velocity.get('velocity_change_pct_week_over_week')}%", flush=True)
    else:
        print("  [warn] histogram unavailable or empty", file=sys.stderr, flush=True)

    result = {
        "appid": args.appid,
        "overall": overall,
        "recent_30d": recent30,
        "velocity": velocity,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {args.out}", flush=True)

    if args.report_out:
        report = {
            "step": "fetch_steam_summary",
            "ok": overall is not None,
            "overall": overall,
            "recent_30d": recent30,
            "spike_days": len(velocity.get("spike_days", [])) if velocity.get("available") else None,
            "velocity_change_pct_week_over_week": velocity.get("velocity_change_pct_week_over_week"),
        }
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
