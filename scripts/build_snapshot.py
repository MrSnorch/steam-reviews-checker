#!/usr/bin/env python3
"""
Build the final snapshot JSON consumed by the GitHub Pages site.

Produces:
  docs/data/latest.json                -> full analyzed review list + summary stats
  docs/data/snapshots/YYYY-MM-DD.json  -> lightweight daily summary (for history charts)

Usage:
    python build_snapshot.py --in data/analyzed_reviews.json \
        --appid 4369490 --appname "My Game" \
        --latest-out ../docs/data/latest.json \
        --snapshot-dir ../docs/data/snapshots
"""
import argparse
import datetime
import json
import os
import statistics
from collections import Counter, defaultdict


def build_summary(reviews: list[dict]) -> dict:
    total = len(reviews)
    positive = [r for r in reviews if r.get("voted_up")]
    negative = [r for r in reviews if not r.get("voted_up")]

    def avg_playtime(rs, field="playtime_at_review"):
        vals = [r.get(field) or 0 for r in rs]
        return round(statistics.mean(vals) / 60, 1) if vals else 0  # -> hours

    def median_playtime(rs, field="playtime_at_review"):
        vals = [r.get(field) or 0 for r in rs]
        return round(statistics.median(vals) / 60, 1) if vals else 0

    suspicious = [r for r in reviews if r.get("suspicion_score", 0) >= 40]
    highly_suspicious = [r for r in reviews if r.get("suspicion_score", 0) >= 60]

    bucket_counts = Counter(r.get("playtime_bucket", "unknown") for r in reviews)
    bucket_counts_pos = Counter(r.get("playtime_bucket", "unknown") for r in positive)
    bucket_counts_neg = Counter(r.get("playtime_bucket", "unknown") for r in negative)

    lang_counts = Counter(r.get("language", "unknown") for r in reviews)

    reason_counts = Counter()
    for r in reviews:
        for reason in r.get("suspicion_reasons", []):
            # strip numeric suffixes for grouping (e.g. duplicate_text_cluster_size_5)
            base = reason.split("_size_")[0] if "_size_" in reason else reason
            reason_counts[base] += 1

    dev_response_count = sum(1 for r in reviews if r.get("developer_response"))

    free_key_count = sum(1 for r in reviews if r.get("received_for_free"))
    not_steam_purchase = sum(1 for r in reviews if not r.get("steam_purchase"))

    # enrichment coverage + account-level signals (only meaningful if
    # enrich_accounts.py ran with an API key; fields absent otherwise)
    enriched = [r for r in reviews if r.get("total_games_owned_api") is not None]
    private_profiles = sum(1 for r in reviews if r.get("account_visibility") == 1)
    new_accounts_positive = sum(
        1 for r in reviews
        if r.get("voted_up") and r.get("account_age_days_at_review") is not None
        and r["account_age_days_at_review"] < 7
    )
    edited_later = sum(1 for r in reviews if "edited_days_later" in r.get("suspicion_reasons", []))

    return {
        "total_reviews": total,
        "positive_count": len(positive),
        "negative_count": len(negative),
        "positive_pct": round(100 * len(positive) / total, 1) if total else 0,
        "avg_playtime_hours_positive": avg_playtime(positive),
        "avg_playtime_hours_negative": avg_playtime(negative),
        "median_playtime_hours_positive": median_playtime(positive),
        "median_playtime_hours_negative": median_playtime(negative),
        "suspicious_count": len(suspicious),
        "highly_suspicious_count": len(highly_suspicious),
        "suspicious_pct_of_positive": round(100 * len(suspicious) / len(positive), 1) if positive else 0,
        "playtime_bucket_distribution": dict(bucket_counts),
        "playtime_bucket_distribution_positive": dict(bucket_counts_pos),
        "playtime_bucket_distribution_negative": dict(bucket_counts_neg),
        "language_distribution": dict(lang_counts.most_common(15)),
        "suspicion_reason_counts": dict(reason_counts.most_common()),
        "dev_response_count": dev_response_count,
        "free_key_count": free_key_count,
        "not_steam_purchase_count": not_steam_purchase,
        "enrichment_coverage": len(enriched),
        "private_profile_count": private_profiles,
        "new_accounts_positive_under_7d": new_accounts_positive,
        "edited_review_later_count": edited_later,
    }


def build_timeline(reviews: list[dict]) -> list[dict]:
    """Daily counts of positive/negative reviews, for the timeline chart."""
    by_day = defaultdict(lambda: {"positive": 0, "negative": 0, "suspicious": 0})
    for r in reviews:
        ts = r.get("timestamp_created")
        if not ts:
            continue
        day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
        if r.get("voted_up"):
            by_day[day]["positive"] += 1
        else:
            by_day[day]["negative"] += 1
        if r.get("suspicion_score", 0) >= 40:
            by_day[day]["suspicious"] += 1

    return [
        {"date": day, **counts}
        for day, counts in sorted(by_day.items())
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--appid", required=True)
    ap.add_argument("--appname", default="")
    ap.add_argument("--latest-out", required=True)
    ap.add_argument("--snapshot-dir", required=True)
    ap.add_argument("--steam-summary", default=None,
                     help="optional path to the JSON written by fetch_steam_summary.py, "
                          "merged in as an independent cross-check against our collected sample")
    ap.add_argument("--report-out", default=None,
                     help="optional path to write a JSON run report for CI summaries")
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8") as f:
        reviews = json.load(f)

    summary = build_summary(reviews)
    timeline = build_timeline(reviews)

    steam_summary = None
    if args.steam_summary and os.path.exists(args.steam_summary):
        with open(args.steam_summary, encoding="utf-8") as f:
            steam_summary = json.load(f)
        overall = steam_summary.get("overall") or {}
        steam_total = overall.get("total_reviews")
        if steam_total and summary.get("total_reviews"):
            coverage_pct = round(100 * summary["total_reviews"] / steam_total, 1)
            summary["steam_official_total_reviews"] = steam_total
            summary["steam_official_total_positive"] = overall.get("total_positive")
            summary["steam_official_total_negative"] = overall.get("total_negative")
            summary["steam_official_review_score_desc"] = overall.get("review_score_desc")
            summary["our_sample_coverage_pct"] = coverage_pct
            recent30 = steam_summary.get("recent_30d") or {}
            if recent30.get("total_reviews"):
                rp = recent30.get("total_positive", 0)
                rt = recent30.get("total_reviews", 1)
                summary["steam_recent_30d_positive_pct"] = round(100 * rp / rt, 1)
                summary["steam_recent_30d_total_reviews"] = rt
            velocity = steam_summary.get("velocity") or {}
            if velocity.get("available"):
                summary["steam_velocity_change_pct_week_over_week"] = velocity.get(
                    "velocity_change_pct_week_over_week")
                summary["steam_spike_days_count"] = len(velocity.get("spike_days", []))
                summary["steam_avg_reviews_per_day_last_7d"] = velocity.get("avg_reviews_per_day_last_7d")

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    latest = {
        "appid": args.appid,
        "appname": args.appname,
        "generated_at": now_iso,
        "summary": summary,
        "timeline": timeline,
        "steam_official": steam_summary,
        "reviews": reviews,
    }

    os.makedirs(os.path.dirname(args.latest_out), exist_ok=True)
    with open(args.latest_out, "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {args.latest_out} ({len(reviews)} reviews)")

    # lightweight daily snapshot (no full review text, just summary) for history
    os.makedirs(args.snapshot_dir, exist_ok=True)
    snapshot_path = os.path.join(args.snapshot_dir, f"{today}.json")
    snapshot = {
        "date": today,
        "generated_at": now_iso,
        "summary": summary,
    }
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {snapshot_path}")

    # maintain an index of available snapshot dates for the site to fetch history
    index_path = os.path.join(args.snapshot_dir, "index.json")
    existing_dates = []
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            existing_dates = json.load(f)
    if today not in existing_dates:
        existing_dates.append(today)
    existing_dates.sort()
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(existing_dates, f)
    print(f"Updated {index_path} ({len(existing_dates)} snapshot dates)")

    # build a single consolidated history.json (one summary per day) so the
    # site can render a "how did suspicion metrics change over time" chart
    # without fetching every individual daily snapshot file separately.
    history_path = os.path.join(args.snapshot_dir, "history.json")
    history = []
    for d in existing_dates:
        p = os.path.join(args.snapshot_dir, f"{d}.json")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            snap = json.load(f)
        s = snap.get("summary", {})
        history.append({
            "date": snap.get("date", d),
            "total_reviews": s.get("total_reviews"),
            "positive_pct": s.get("positive_pct"),
            "suspicious_count": s.get("suspicious_count"),
            "highly_suspicious_count": s.get("highly_suspicious_count"),
            "avg_playtime_hours_positive": s.get("avg_playtime_hours_positive"),
            "avg_playtime_hours_negative": s.get("avg_playtime_hours_negative"),
        })
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {history_path} ({len(history)} days)")

    # "hit list" -- top-N most suspicious reviews/authors, exported separately
    # so it's easy to hand to a human for manual verification or reporting to
    # Steam, without having to filter the full latest.json by hand.
    hitlist_path = os.path.join(args.snapshot_dir, "..", "hitlist.json")
    hitlist_path = os.path.normpath(hitlist_path)
    top_suspicious = sorted(
        (r for r in reviews if r.get("suspicion_score", 0) >= 40),
        key=lambda r: r.get("suspicion_score", 0),
        reverse=True,
    )[:100]
    hitlist = [{
        "recommendationid": r.get("recommendationid"),
        "steamid": r.get("steamid"),
        "profile_url": f"https://steamcommunity.com/profiles/{r['steamid']}" if r.get("steamid") else None,
        "suspicion_score": r.get("suspicion_score"),
        "suspicion_reasons": r.get("suspicion_reasons"),
        "voted_up": r.get("voted_up"),
        "playtime_at_review_minutes": r.get("playtime_at_review"),
        "timestamp_created": r.get("timestamp_created"),
        "review_excerpt": (r.get("review") or "")[:200],
    } for r in top_suspicious]
    with open(hitlist_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": now_iso, "appid": args.appid, "top_suspicious": hitlist},
                   f, ensure_ascii=False, indent=2)
    print(f"Wrote {hitlist_path} ({len(hitlist)} entries)")

    if args.report_out:
        report = {
            "step": "build_snapshot",
            "ok": True,
            "appid": args.appid,
            "appname": args.appname,
            "generated_at": now_iso,
            "summary": summary,
            "history_days": len(history),
        }
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
