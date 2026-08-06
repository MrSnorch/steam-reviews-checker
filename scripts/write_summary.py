#!/usr/bin/env python3
"""
Assemble a markdown run summary from the JSON reports written by each
pipeline step (fetch_reviews.py, enrich_accounts.py, analyze.py,
build_snapshot.py), and print it to stdout.

In the workflow this is piped straight into $GITHUB_STEP_SUMMARY, which
GitHub renders as markdown on the run's summary page -- no need to expand
individual step logs to see what happened.

Usage:
    python write_summary.py \
        --fetch tmp/report_fetch.json \
        --enrich tmp/report_enrich.json \
        --analyze tmp/report_analyze.json \
        --snapshot tmp/report_snapshot.json
Any of the report paths can be missing (step failed before writing it /
was skipped) -- the summary degrades gracefully and says so.
"""
import argparse
import json
import os


def load(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def fmt_reason(key, count):
    labels = {
        "positive_zero_playtime": "позитив, 0ч наиграно",
        "positive_under_30min": "позитив, <30 мин",
        "positive_under_1h": "позитив, <1ч",
        "free_key_not_purchased": "бесплатный ключ, не куплено",
        "prolific_reviewer_few_games": "много отзывов, мало игр",
        "duplicate_text_cluster": "повторяющийся текст",
        "posted_during_review_burst": "всплеск активности",
        "negative_zero_playtime": "негатив, 0ч наиграно",
        "edited_days_later": "отредактировано спустя дни",
        "account_under_7d_old_at_review": "аккаунту < 7 дней",
        "account_under_30d_old_at_review": "аккаунту < 30 дней",
        "private_profile": "приватный профиль",
        "owns_2_or_fewer_games_total": "≤2 игры в библиотеке",
        "low_effort_text": "низкосодержательный текст",
        "high_votes_low_effort_text": "много votes при пустом тексте",
    }
    return f"{labels.get(key, key)}: **{count}**"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", default=None)
    ap.add_argument("--enrich", default=None)
    ap.add_argument("--analyze", default=None)
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--steam-summary", default=None)
    args = ap.parse_args()

    fetch = load(args.fetch)
    enrich = load(args.enrich)
    analyze = load(args.analyze)
    snapshot = load(args.snapshot)
    steam_summary = load(args.steam_summary)

    lines = []
    lines.append("## 📊 Review Watch — отчёт о запуске\n")

    if snapshot:
        appname = snapshot.get("appname") or ""
        appid = snapshot.get("appid", "?")
        title = f"{appname} (appid {appid})" if appname else f"appid {appid}"
        lines.append(f"**Игра:** {title}  ")
        lines.append(f"**Время (UTC):** {snapshot.get('generated_at', '?')}\n")

    # --- fetch step ---
    lines.append("### 1️⃣ Сбор отзывов")
    if fetch and fetch.get("ok"):
        lines.append(f"✅ Собрано отзывов: **{fetch.get('reviews_fetched', 0)}**\n")
    elif fetch and not fetch.get("ok"):
        lines.append(f"❌ Ошибка сбора: `{fetch.get('error', 'unknown')}`\n")
    else:
        lines.append("⚠️ Отчёт о сборе не найден (шаг мог упасть раньше записи отчёта)\n")

    # --- steam official cross-check ---
    lines.append("### 2️⃣ Сверка с официальными данными Steam")
    if steam_summary and steam_summary.get("ok"):
        overall = steam_summary.get("overall") or {}
        recent30 = steam_summary.get("recent_30d") or {}
        lines.append(
            f"Всего отзывов по данным Steam: **{overall.get('total_reviews', '—')}** "
            f"({overall.get('review_score_desc', '—')})  \n"
        )
        if fetch and fetch.get("reviews_fetched") and overall.get("total_reviews"):
            coverage = round(100 * fetch["reviews_fetched"] / overall["total_reviews"], 1)
            lines.append(f"Покрытие нашей выборки: **{coverage}%** от всех отзывов игры  \n")
        if recent30.get("total_reviews"):
            rp = recent30.get("total_positive", 0)
            rt = recent30.get("total_reviews", 1)
            lines.append(f"Последние 30 дней (по Steam): **{round(100*rp/rt,1)}%** позитивных "
                         f"из {rt} отзывов\n")
        vel = steam_summary.get("velocity_change_pct_week_over_week")
        spikes = steam_summary.get("spike_days")
        if vel is not None:
            arrow = "📈" if vel > 0 else "📉"
            lines.append(f"{arrow} Изменение скорости поступления отзывов неделя к неделе: **{vel}%**  \n")
        if spikes:
            lines.append(f"⚡ Дней с аномальным всплеском (по данным Steam): **{spikes}**\n")
    else:
        lines.append("⚠️ Не удалось получить официальную сводку Steam (появится, если запускать "
                      "`fetch_steam_summary.py` в пайплайне)\n")

    # --- enrich step ---
    lines.append("### 3️⃣ Обогащение данными об аккаунтах")
    if enrich and enrich.get("skipped"):
        lines.append("⏭️ Пропущено — не задан секрет `STEAM_WEB_API_KEY`. "
                      "Suspicion score считается без сигналов по возрасту аккаунта, "
                      "приватности профиля и размеру библиотеки игр.\n")
    elif enrich and enrich.get("ok"):
        lines.append(
            f"✅ Обогащено авторов: **{enrich.get('authors_enriched', 0)}** "
            f"из **{enrich.get('authors_total', 0)}** уникальных  \n"
            f"Приватных профилей найдено: **{enrich.get('private_profiles', 0)}**\n"
        )
    else:
        lines.append("⚠️ Отчёт об обогащении не найден\n")

    # --- analyze step ---
    lines.append("### 4️⃣ Анализ и suspicion score")
    if analyze and analyze.get("ok"):
        lines.append(
            f"Всего отзывов проанализировано: **{analyze.get('reviews_total', 0)}**  \n"
            f"🚩 Подозрительных (score ≥ 40): **{analyze.get('flagged_40plus', 0)}**  \n"
            f"🔴 Сильно подозрительных (score ≥ 60): **{analyze.get('flagged_60plus', 0)}**  \n"
            f"Кластеров повторяющегося текста (≥3 отзыва): **{analyze.get('duplicate_clusters_3plus', 0)}**  \n"
            f"Дней с аномальным всплеском активности: **{analyze.get('burst_days', 0)}**\n"
        )
        top_reasons = analyze.get("top_reasons") or []
        if top_reasons:
            lines.append("**Топ причин флагов:**\n")
            for key, count in top_reasons:
                lines.append(f"- {fmt_reason(key, count)}")
            lines.append("")
    else:
        lines.append("⚠️ Отчёт об анализе не найден\n")

    # --- snapshot / site step ---
    lines.append("### 5️⃣ Публикация на сайт")
    if snapshot and snapshot.get("ok"):
        s = snapshot.get("summary", {})
        lines.append(
            f"✅ Данные записаны в `docs/data/latest.json`  \n"
            f"История снапшотов: **{snapshot.get('history_days', 0)}** дн.\n"
        )
        lines.append("| Метрика | Значение |")
        lines.append("|---|---|")
        lines.append(f"| Всего отзывов | {s.get('total_reviews', '—')} |")
        lines.append(f"| Позитивных | {s.get('positive_count', '—')} ({s.get('positive_pct', '—')}%) |")
        lines.append(f"| Негативных | {s.get('negative_count', '—')} |")
        lines.append(f"| Ср. playtime, позитив | {s.get('avg_playtime_hours_positive', '—')}ч |")
        lines.append(f"| Ср. playtime, негатив | {s.get('avg_playtime_hours_negative', '—')}ч |")
        lines.append(f"| 🚩 Подозрительных | {s.get('suspicious_count', '—')} "
                      f"({s.get('suspicious_pct_of_positive', '—')}% от позитивных) |")
        lines.append(f"| Бесплатных ключей | {s.get('free_key_count', '—')} |")
        if s.get("enrichment_coverage"):
            lines.append(f"| Новых аккаунтов (<7д, позитив) | {s.get('new_accounts_positive_under_7d', '—')} |")
            lines.append(f"| Приватных профилей | {s.get('private_profile_count', '—')} |")
            lines.append(f"| Отредактировано позже | {s.get('edited_review_later_count', '—')} |")
        lines.append("")
    else:
        lines.append("⚠️ Отчёт о публикации не найден — данные сайта могли не обновиться\n")

    overall_ok = all(
        r is None or r.get("ok", False)
        for r in (fetch, analyze, snapshot)
    )
    lines.append("---")
    lines.append("✅ **Запуск завершён успешно.**" if overall_ok else
                  "❌ **Запуск завершился с ошибками — проверьте шаги выше.**")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
