#!/usr/bin/env python3
"""
Analyze raw reviews: bucket by playtime, flag suspicious reviews, detect
near-duplicate text clusters (possible botfarm / copy-paste campaigns).

Usage:
    python analyze.py --in data/raw_reviews.json --out data/analyzed_reviews.json

Suspicion scoring (0-100, higher = more suspicious) is a weighted sum of
independent signals. It is a heuristic, not proof — always eyeball flagged
reviews manually before drawing conclusions.
"""
import argparse
import json
import re
from collections import defaultdict, Counter

PLAYTIME_BUCKETS = [
    (0, 60, "<1h"),
    (60, 300, "1-5h"),
    (300, 1200, "5-20h"),
    (1200, 6000, "20-100h"),
    (6000, float("inf"), "100h+"),
]


def playtime_bucket(minutes: int) -> str:
    m = minutes or 0
    for lo, hi, label in PLAYTIME_BUCKETS:
        if lo <= m < hi:
            return label
    return "unknown"


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation/whitespace variance, for dedup shingling."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def shingles(text: str, k: int = 5) -> set:
    """Word k-shingles for near-duplicate detection (cheap, no external deps)."""
    words = text.split()
    if len(words) < k:
        return {text} if text else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def find_duplicate_clusters(reviews: list[dict], min_len_words: int = 8,
                             similarity_threshold: float = 0.6) -> dict:
    """
    Group reviews with near-identical text. Returns {review_index: cluster_id}.
    O(n^2) worst case on the normalized-text-bucket level; we pre-bucket by a
    coarse fingerprint (first+last shingle) to keep this tractable up to
    tens of thousands of reviews. For very large datasets this can be swapped
    for MinHash/LSH, but appreviews volumes rarely need that.
    """
    candidates = []
    for i, r in enumerate(reviews):
        norm = normalize_text(r.get("review", ""))
        words = norm.split()
        if len(words) < min_len_words:
            continue
        sh = shingles(norm)
        if not sh:
            continue
        candidates.append((i, sh, len(words)))

    # coarse bucket by rounded word count to cut comparisons
    buckets = defaultdict(list)
    for i, sh, wc in candidates:
        buckets[wc // 5].append((i, sh))

    cluster_of = {}
    next_cluster_id = 0
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for key in (list(buckets.keys())):
        for bk in (key - 1, key, key + 1):
            if bk not in buckets or bk < key:
                continue
            group_a = buckets[key]
            group_b = buckets[bk]
            for idx_a, (i, sh_i) in enumerate(group_a):
                start_b = idx_a + 1 if bk == key else 0
                for j, sh_j in group_b[start_b:]:
                    if i == j:
                        continue
                    if jaccard(sh_i, sh_j) >= similarity_threshold:
                        parent.setdefault(i, i)
                        parent.setdefault(j, j)
                        union(i, j)

    roots = {}
    for i, _sh, _wc in candidates:
        if i not in parent:
            continue
        root = find(i)
        if root not in roots:
            roots[root] = next_cluster_id
            next_cluster_id += 1
        cluster_of[i] = roots[root]

    return cluster_of


def compute_author_stats(reviews: list[dict]) -> dict:
    """Per-author aggregate: how many reviews in this dataset, timestamps."""
    by_author = defaultdict(list)
    for i, r in enumerate(reviews):
        sid = r.get("steamid")
        if sid:
            by_author[sid].append(i)
    return by_author


def suspicion_score(r: dict, cluster_id, cluster_sizes: dict, burst_days: set) -> tuple[int, list[str]]:
    """Returns (score 0-100, list of reason tags)."""
    score = 0
    reasons = []

    voted_up = r.get("voted_up", False)
    playtime_at_review = r.get("playtime_at_review") or 0
    playtime_forever = r.get("playtime_forever") or 0

    if voted_up:
        if playtime_at_review == 0:
            score += 40
            reasons.append("positive_zero_playtime")
        elif playtime_at_review < 30:
            score += 30
            reasons.append("positive_under_30min")
        elif playtime_at_review < 60:
            score += 15
            reasons.append("positive_under_1h")

    if r.get("received_for_free") and not r.get("steam_purchase"):
        score += 15
        reasons.append("free_key_not_purchased")

    num_reviews = r.get("num_reviews") or 0
    num_games = r.get("num_games_owned") or 0
    if num_reviews >= 50 and num_games <= 3:
        score += 20
        reasons.append("prolific_reviewer_few_games")

    if cluster_id is not None and cluster_sizes.get(cluster_id, 0) >= 3:
        score += 25
        reasons.append(f"duplicate_text_cluster_size_{cluster_sizes[cluster_id]}")

    ts = r.get("timestamp_created")
    if ts in burst_days:
        score += 10
        reasons.append("posted_during_review_burst")

    if playtime_forever == 0 and not voted_up:
        # negative + zero playtime is less suspicious (refund rage etc) but flag lightly
        score += 5
        reasons.append("negative_zero_playtime")

    # --- edited review: stance may have changed after the fact ---
    created = r.get("timestamp_created")
    updated = r.get("timestamp_updated")
    if created and updated and updated - created > 86400 * 3:
        score += 8
        reasons.append("edited_days_later")

    # --- signals from enrich_accounts.py (only present if that step ran) ---
    account_age_days = r.get("account_age_days_at_review")
    if account_age_days is not None:
        if voted_up and account_age_days < 7:
            score += 25
            reasons.append("account_under_7d_old_at_review")
        elif voted_up and account_age_days < 30:
            score += 12
            reasons.append("account_under_30d_old_at_review")

    visibility = r.get("account_visibility")
    if visibility == 1:
        score += 10
        reasons.append("private_profile")

    total_games_api = r.get("total_games_owned_api")
    if voted_up and total_games_api is not None and total_games_api <= 2:
        score += 15
        reasons.append("owns_2_or_fewer_games_total")

    # --- low-effort text: very short / near-empty positive reviews.
    # Distinct from duplicate-cluster detection: this flags a SINGLE review
    # with little to no substance (emoji-only, "10/10", etc), which alone
    # isn't proof of anything but combined with low playtime is a strong tell.
    text = (r.get("review") or "").strip()
    word_count = len(text.split())
    if voted_up and 0 < word_count <= 2:
        score += 10
        reasons.append("low_effort_text")

    # --- votes_up disproportionate to text length/effort: a handful of
    # words racking up many "helpful" votes suggests the votes themselves
    # were bought/farmed rather than earned by genuinely useful content.
    votes_up = r.get("votes_up") or 0
    if votes_up >= 20 and word_count <= 3:
        score += 12
        reasons.append("high_votes_low_effort_text")

    return min(score, 100), reasons


def detect_burst_days(reviews: list[dict], multiplier: float = 3.0) -> set:
    """
    Find calendar days where review volume is an outlier vs the dataset's
    median daily volume -> likely review-bombing or coordinated campaign.
    Returns a set of unix-day-bucket timestamps (day granularity, as the
    start-of-day epoch) flagged as bursts. We match against raw timestamps
    by day-bucketing both sides in the caller... kept simple here: returns
    set of day-bucket ints.
    """
    import statistics

    day_counts = defaultdict(int)
    for r in reviews:
        ts = r.get("timestamp_created")
        if not ts:
            continue
        day = ts - (ts % 86400)
        day_counts[day] += 1

    if len(day_counts) < 5:
        return set()

    counts = list(day_counts.values())
    med = statistics.median(counts)
    if med == 0:
        med = 1

    burst_days = {day for day, c in day_counts.items() if c >= med * multiplier and c >= 5}
    return burst_days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report-out", default=None,
                     help="optional path to write a JSON run report for CI summaries")
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8") as f:
        reviews = json.load(f)

    print(f"Loaded {len(reviews)} reviews")

    print("Detecting duplicate text clusters...")
    cluster_map = find_duplicate_clusters(reviews)
    cluster_sizes = defaultdict(int)
    for cid in cluster_map.values():
        cluster_sizes[cid] += 1
    dupe_clusters_3plus = sum(1 for s in cluster_sizes.values() if s >= 3)
    print(f"  found {len(cluster_sizes)} clusters, {dupe_clusters_3plus} with size>=3")

    print("Detecting burst days...")
    day_burst = detect_burst_days(reviews)
    burst_ts_set = set()
    for r in reviews:
        ts = r.get("timestamp_created")
        if ts and (ts - (ts % 86400)) in day_burst:
            burst_ts_set.add(ts)
    print(f"  {len(day_burst)} burst days flagged")

    print("Scoring reviews...")
    for i, r in enumerate(reviews):
        cid = cluster_map.get(i)
        score, reasons = suspicion_score(r, cid, cluster_sizes, burst_ts_set)
        r["playtime_bucket"] = playtime_bucket(r.get("playtime_at_review") or 0)
        r["duplicate_cluster_id"] = cid
        r["duplicate_cluster_size"] = cluster_sizes.get(cid, 0) if cid is not None else 0
        r["suspicion_score"] = score
        r["suspicion_reasons"] = reasons

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, separators=(",", ":"))

    flagged = sum(1 for r in reviews if r["suspicion_score"] >= 40)
    highly_flagged = sum(1 for r in reviews if r["suspicion_score"] >= 60)
    print(f"Done. {flagged} reviews with suspicion_score >= 40. Written to {args.out}")

    if args.report_out:
        reason_counts = Counter()
        for r in reviews:
            for reason in r.get("suspicion_reasons", []):
                base = reason.split("_size_")[0] if "_size_" in reason else reason
                reason_counts[base] += 1
        report = {
            "step": "analyze",
            "ok": True,
            "reviews_total": len(reviews),
            "flagged_40plus": flagged,
            "flagged_60plus": highly_flagged,
            "duplicate_clusters_3plus": dupe_clusters_3plus,
            "burst_days": len(day_burst),
            "top_reasons": reason_counts.most_common(6),
        }
        with open(args.report_out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
