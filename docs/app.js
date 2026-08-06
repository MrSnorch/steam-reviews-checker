/* Review Watch — no build step, no dependencies, plain SVG charts. */

const state = {
  data: null,
  reviews: [],
  filtered: [],
  sortKey: 'timestamp_created',
  sortDir: 'desc',
  page: 1,
  pageSize: 50,
  threshold: 60, // minutes
  filters: {
    vote: new Set(['all', 'up', 'down']),
    bucket: '',
    minScore: 0,
    suspiciousOnly: false,
    freeOnly: false,
    dupeOnly: false,
    editedOnly: false,
    search: '',
  },
};

const PLAYTIME_BUCKET_ORDER = ['<1h', '1-5h', '5-20h', '20-100h', '100h+'];

const REASON_LABELS = {
  positive_zero_playtime: 'позитив, 0 часов наиграно',
  positive_under_30min: 'позитив, <30 мин на момент отзыва',
  positive_under_1h: 'позитив, <1ч на момент отзыва',
  free_key_not_purchased: 'бесплатный ключ, не куплено в Steam',
  prolific_reviewer_few_games: 'много отзывов, мало игр в библиотеке',
  duplicate_text_cluster: 'повторяющийся/шаблонный текст',
  posted_during_review_burst: 'опубликовано во время всплеска активности',
  negative_zero_playtime: 'негатив, 0 часов наиграно',
  edited_days_later: 'отзыв отредактирован спустя дни (возможна смена позиции)',
  account_under_7d_old_at_review: 'аккаунту < 7 дней на момент отзыва',
  account_under_30d_old_at_review: 'аккаунту < 30 дней на момент отзыва',
  private_profile: 'приватный профиль',
  owns_2_or_fewer_games_total: 'во всей библиотеке ≤2 игры',
  low_effort_text: 'низкосодержательный текст',
  high_votes_low_effort_text: 'много votes при пустом тексте',
};

function fmtHours(minutes) {
  if (minutes === null || minutes === undefined) return '—';
  const h = minutes / 60;
  if (h < 1) return `${Math.round(minutes)} мин`;
  return `${h.toFixed(1)} ч`;
}

function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toISOString().slice(0, 10);
}

function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function loadData(appid) {
  const path = appid ? `data/${appid}.json` : 'data/latest.json';
  const res = await fetch(path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Не удалось загрузить ${path} (HTTP ${res.status})`);
  return res.json();
}

async function loadHistory() {
  try {
    const res = await fetch('data/snapshots/history.json', { cache: 'no-store' });
    if (!res.ok) return null;
    const hist = await res.json();
    return Array.isArray(hist) && hist.length >= 2 ? hist : null;
  } catch {
    return null;
  }
}

function init() {
  const params = new URLSearchParams(location.search);
  const appidParam = params.get('appid');
  if (appidParam) document.getElementById('appid-input').value = appidParam;

  loadData(appidParam)
    .then(data => {
      state.data = data;
      state.reviews = data.reviews || [];
      document.getElementById('loading').style.display = 'none';
      document.getElementById('content').style.display = 'block';
      renderHeader(data);
      applyFiltersAndRender();
      bindControls();
      renderSteamCrossCheck(data.summary);
      loadHistory().then(hist => {
        if (hist) {
          document.getElementById('history-panel').style.display = 'block';
          renderHistory(hist);
        }
      });
    })
    .catch(err => {
      document.getElementById('loading').style.display = 'none';
      const el = document.getElementById('error');
      el.style.display = 'block';
      el.textContent = `Ошибка загрузки данных: ${err.message}. Если это первый запуск — дождитесь первого прогона GitHub Actions, который создаст docs/data/latest.json.`;
    });

  document.getElementById('appid-form').addEventListener('submit', e => {
    e.preventDefault();
    const v = document.getElementById('appid-input').value.trim();
    const url = new URL(location.href);
    if (v) url.searchParams.set('appid', v); else url.searchParams.delete('appid');
    location.href = url.toString();
  });
}

function renderHeader(data) {
  document.getElementById('appname-sub').textContent = data.appname
    ? `${data.appname} — steam review forensics`
    : 'steam review forensics';
  document.getElementById('appid-line').textContent = `appid ${data.appid}`;
  document.getElementById('generated-at').textContent = `обновлено: ${data.generated_at}`;
  const repoLink = document.getElementById('repo-link');
  // leave default href as-is; user should point this at their own repo
}

function renderStats(summary) {
  const grid = document.getElementById('stats-grid');
  const items = [
    {
      label: 'Всего отзывов', value: summary.total_reviews, cls: '',
      sub: `${summary.positive_pct}% положительных`,
    },
    {
      label: 'Positive / Negative', value: `${summary.positive_count} / ${summary.negative_count}`, cls: 'green',
      sub: '',
    },
    {
      label: 'Playtime, позитив', value: `${summary.avg_playtime_hours_positive}ч`, cls: 'green',
      sub: `медиана ${summary.median_playtime_hours_positive}ч`,
    },
    {
      label: 'Playtime, негатив', value: `${summary.avg_playtime_hours_negative}ч`, cls: 'red',
      sub: `медиана ${summary.median_playtime_hours_negative}ч`,
    },
    {
      label: 'Подозрительные', value: summary.suspicious_count, cls: 'red',
      sub: `${summary.suspicious_pct_of_positive}% от позитивных`,
    },
    {
      label: 'Сильно подозрительные', value: summary.highly_suspicious_count, cls: 'amber',
      sub: 'score ≥ 60',
    },
  ];

  if (summary.enrichment_coverage) {
    items.push(
      {
        label: 'Новые аккаунты (позитив, <7д)', value: summary.new_accounts_positive_under_7d || 0, cls: 'red',
        sub: `из ${summary.enrichment_coverage} обогащённых`,
      },
      {
        label: 'Приватные профили', value: summary.private_profile_count || 0, cls: 'amber',
        sub: '',
      },
      {
        label: 'Отредактировано позже', value: summary.edited_review_later_count || 0, cls: 'amber',
        sub: 'возможна смена позиции',
      },
    );
  }
  grid.innerHTML = items.map(it => `
    <div class="stat">
      <div class="label">${it.label}</div>
      <div class="value ${it.cls}">${it.value}</div>
      ${it.sub ? `<div class="sub">${it.sub}</div>` : ''}
    </div>
  `).join('');
}

function renderSteamCrossCheck(summary) {
  if (!summary.steam_official_total_reviews) return; // fetch_steam_summary.py didn't run
  document.getElementById('steam-crosscheck-panel').style.display = 'block';
  const grid = document.getElementById('steam-crosscheck-grid');

  const velocity = summary.steam_velocity_change_pct_week_over_week;
  const velocityCls = velocity > 20 ? 'red' : (velocity < -20 ? 'green' : '');
  const velocitySign = velocity > 0 ? '+' : '';

  const items = [
    {
      label: 'Всего отзывов (Steam)', value: summary.steam_official_total_reviews, cls: '',
      sub: summary.steam_official_review_score_desc || '',
    },
    {
      label: 'Покрытие выборки', value: `${summary.our_sample_coverage_pct ?? '—'}%`, cls: '',
      sub: `собрано ${summary.total_reviews} из ${summary.steam_official_total_reviews}`,
    },
    {
      label: '% позитива, последние 30д', value: `${summary.steam_recent_30d_positive_pct ?? '—'}%`, cls: '',
      sub: `по данным Steam, ${summary.steam_recent_30d_total_reviews ?? '—'} отзывов`,
    },
    {
      label: 'Скорость отзывов, нед/нед', value: `${velocitySign}${velocity ?? '—'}%`, cls: velocityCls,
      sub: `~${summary.steam_avg_reviews_per_day_last_7d ?? '—'} отзывов/день сейчас`,
    },
    {
      label: 'Дней-всплесков (Steam)', value: summary.steam_spike_days_count ?? 0,
      cls: summary.steam_spike_days_count > 0 ? 'red' : '',
      sub: 'по официальной гистограмме',
    },
  ];

  grid.innerHTML = items.map(it => `
    <div class="stat">
      <div class="label">${it.label}</div>
      <div class="value ${it.cls}">${it.value}</div>
      ${it.sub ? `<div class="sub">${it.sub}</div>` : ''}
    </div>
  `).join('');
}

/* ---------------- Histogram (signature chart) ---------------- */

function computeHistogramBins(reviews, binSizeMin = 30, maxMin = 600) {
  const nBins = Math.ceil(maxMin / binSizeMin);
  const pos = new Array(nBins).fill(0);
  const neg = new Array(nBins).fill(0);
  for (const r of reviews) {
    const m = Math.min(r.playtime_at_review || 0, maxMin - 1);
    const bin = Math.floor(m / binSizeMin);
    if (r.voted_up) pos[bin]++; else neg[bin]++;
  }
  return { pos, neg, binSizeMin, nBins };
}

function renderHistogram(reviews, thresholdMin) {
  const svg = document.getElementById('histogram');
  const { pos, neg, binSizeMin, nBins } = computeHistogramBins(reviews);
  const W = 900, H = 280, padL = 36, padB = 24, padT = 10, padR = 10;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const maxVal = Math.max(1, ...pos, ...neg);
  const barW = plotW / nBins;

  let bars = '';
  for (let i = 0; i < nBins; i++) {
    const x = padL + i * barW;
    const hPos = (pos[i] / maxVal) * plotH;
    const hNeg = (neg[i] / maxVal) * plotH;
    const yPos = padT + plotH - hPos;
    const yNeg = padT + plotH - hNeg;
    bars += `<rect x="${x}" y="${yPos}" width="${barW * 0.42}" height="${hPos}" fill="var(--green)" opacity="0.85"><title>${pos[i]} позитивных, ${i * binSizeMin}-${(i + 1) * binSizeMin} мин</title></rect>`;
    bars += `<rect x="${x + barW * 0.46}" y="${yNeg}" width="${barW * 0.42}" height="${hNeg}" fill="var(--red)" opacity="0.85"><title>${neg[i]} отрицательных, ${i * binSizeMin}-${(i + 1) * binSizeMin} мин</title></rect>`;
  }

  // axis labels every ~2 hours
  let labels = '';
  for (let i = 0; i <= nBins; i += Math.round(120 / binSizeMin)) {
    const x = padL + i * barW;
    const hrs = Math.round((i * binSizeMin) / 60);
    labels += `<text x="${x}" y="${H - 6}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10">${hrs}ч</text>`;
  }

  const threshX = padL + (thresholdMin / binSizeMin) * barW;
  const thresholdLine = `<line x1="${threshX}" y1="${padT}" x2="${threshX}" y2="${padT + plotH}" stroke="var(--amber)" stroke-width="1.5" stroke-dasharray="4,3"/>`;

  const gridLines = [0.25, 0.5, 0.75, 1].map(f => {
    const y = padT + plotH * (1 - f);
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--border)" stroke-width="1"/>`;
  }).join('');

  svg.innerHTML = `${gridLines}${bars}${thresholdLine}${labels}`;
}

/* ---------------- Timeline chart ---------------- */

function renderTimeline(timeline) {
  const svg = document.getElementById('timeline');
  if (!timeline || !timeline.length) { svg.innerHTML = ''; return; }
  const W = 560, H = 220, padL = 30, padB = 20, padT = 10, padR = 10;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = timeline.length;
  const maxVal = Math.max(1, ...timeline.map(d => d.positive + d.negative));
  const stepX = n > 1 ? plotW / (n - 1) : 0;

  function pathFor(getter, color) {
    let d = '';
    timeline.forEach((pt, i) => {
      const x = padL + i * stepX;
      const y = padT + plotH - (getter(pt) / maxVal) * plotH;
      d += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
    });
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/>`;
  }

  const suspiciousDots = timeline.map((pt, i) => {
    if (!pt.suspicious) return '';
    const x = padL + i * stepX;
    const y = padT + plotH - ((pt.positive + pt.negative) / maxVal) * plotH;
    const r = Math.min(2 + pt.suspicious * 0.6, 8);
    return `<circle cx="${x}" cy="${y}" r="${r}" fill="var(--red)" opacity="0.55"><title>${pt.date}: ${pt.suspicious} подозрительных</title></circle>`;
  }).join('');

  const gridLines = [0.5, 1].map(f => {
    const y = padT + plotH * (1 - f);
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--border)" stroke-width="1"/>`;
  }).join('');

  const firstLabel = timeline[0].date;
  const lastLabel = timeline[n - 1].date;

  svg.innerHTML = `
    ${gridLines}
    ${pathFor(d => d.positive, 'var(--green)')}
    ${pathFor(d => d.negative, 'var(--red)')}
    ${suspiciousDots}
    <text x="${padL}" y="${H - 4}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10">${firstLabel}</text>
    <text x="${W - padR}" y="${H - 4}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10" text-anchor="end">${lastLabel}</text>
  `;
}

/* ---------------- History chart (across snapshot runs) ---------------- */

function renderHistory(history) {
  const svg = document.getElementById('history-chart');
  const W = 900, H = 220, padL = 40, padR = 40, padT = 10, padB = 24;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = history.length;
  const stepX = n > 1 ? plotW / (n - 1) : 0;

  const maxSuspicious = Math.max(1, ...history.map(d => d.suspicious_count || 0));

  function pathFor(getter, maxVal, color, dashed) {
    let d = '';
    history.forEach((pt, i) => {
      const x = padL + i * stepX;
      const v = getter(pt);
      const y = padT + plotH - (v == null ? 0 : (v / maxVal) * plotH);
      d += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1) + ' ';
    });
    return `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.8" ${dashed ? 'stroke-dasharray="5,4"' : ''}/>`;
  }

  const dots = history.map((pt, i) => {
    const x = padL + i * stepX;
    const y = padT + plotH - ((pt.suspicious_count || 0) / maxSuspicious) * plotH;
    return `<circle cx="${x}" cy="${y}" r="2.5" fill="var(--red)"><title>${pt.date}: ${pt.suspicious_count} подозрительных, ${pt.positive_pct}% позитив</title></circle>`;
  }).join('');

  const gridLines = [0.25, 0.5, 0.75, 1].map(f => {
    const y = padT + plotH * (1 - f);
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="var(--border)" stroke-width="1"/>`;
  }).join('');

  svg.innerHTML = `
    ${gridLines}
    ${pathFor(d => d.positive_pct, 100, 'var(--green)', false)}
    ${pathFor(d => d.suspicious_count, maxSuspicious, 'var(--red)', true)}
    ${dots}
    <text x="${padL}" y="${H - 4}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10">${history[0].date}</text>
    <text x="${W - padR}" y="${H - 4}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10" text-anchor="end">${history[n - 1].date}</text>
    <text x="${padL}" y="${padT + 4}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10">100%</text>
    <text x="${padL}" y="${padT + plotH}" fill="var(--text-dimmer)" font-family="var(--mono)" font-size="10">0%</text>
  `;
}

/* ---------------- Reasons bar chart ---------------- */

function renderReasons(reasonCounts) {
  const svg = document.getElementById('reasons');
  const entries = Object.entries(reasonCounts || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (!entries.length) { svg.innerHTML = '<text x="10" y="20" fill="var(--text-dim)" font-family="var(--mono)" font-size="12">Флагов не найдено</text>'; return; }
  const W = 560, H = 220, padL = 10, padR = 60, rowH = H / entries.length;
  const maxVal = Math.max(...entries.map(e => e[1]));

  let bars = '';
  entries.forEach(([key, count], i) => {
    const y = i * rowH + rowH * 0.2;
    const barH = rowH * 0.5;
    const barW = ((count / maxVal) * (W - padL - padR - 140));
    const label = REASON_LABELS[key] || key;
    bars += `
      <text x="0" y="${y + barH * 0.75}" fill="var(--text-dim)" font-family="var(--mono)" font-size="10.5">${escapeHtml(label)}</text>
      <rect x="140" y="${y}" width="${Math.max(barW, 2)}" height="${barH}" fill="var(--red)" opacity="0.75"/>
      <text x="${140 + barW + 8}" y="${y + barH * 0.75}" fill="var(--text)" font-family="var(--mono)" font-size="11" font-weight="700">${count}</text>
    `;
  });
  svg.innerHTML = bars;
}

/* ---------------- Filtering / sorting ---------------- */

function passesFilters(r) {
  const f = state.filters;

  if (!f.vote.has('all')) {
    if (r.voted_up && !f.vote.has('up')) return false;
    if (!r.voted_up && !f.vote.has('down')) return false;
  }
  if (f.bucket && r.playtime_bucket !== f.bucket) return false;
  if (f.minScore && (r.suspicion_score || 0) < f.minScore) return false;
  if (f.suspiciousOnly && (r.suspicion_score || 0) < 40) return false;
  if (f.freeOnly && !r.received_for_free) return false;
  if (f.dupeOnly && (!r.duplicate_cluster_size || r.duplicate_cluster_size < 2)) return false;
  if (f.editedOnly && !(r.suspicion_reasons || []).includes('edited_days_later')) return false;
  if (f.search) {
    const s = f.search.toLowerCase();
    if (!(r.review || '').toLowerCase().includes(s)) return false;
  }
  return true;
}

function sortReviews(list) {
  const { sortKey, sortDir } = state;
  const mul = sortDir === 'asc' ? 1 : -1;
  return [...list].sort((a, b) => {
    let va = a[sortKey], vb = b[sortKey];
    if (sortKey === 'voted_up') { va = va ? 1 : 0; vb = vb ? 1 : 0; }
    if (va === undefined || va === null) va = -Infinity;
    if (vb === undefined || vb === null) vb = -Infinity;
    if (va < vb) return -1 * mul;
    if (va > vb) return 1 * mul;
    return 0;
  });
}

function applyFiltersAndRender() {
  state.filtered = sortReviews(state.reviews.filter(passesFilters));
  state.page = 1;
  renderStats(state.data.summary);
  renderHistogram(state.reviews, state.threshold);
  renderTimeline(state.data.timeline);
  renderReasons(state.data.summary.suspicion_reason_counts);
  renderTable();
  updateThresholdCount();
}

function updateThresholdCount() {
  const count = state.reviews.filter(r => r.voted_up && (r.playtime_at_review || 0) < state.threshold).length;
  document.getElementById('threshold-count').textContent = `${count} позитивных отзывов ниже порога`;
}

/* ---------------- Table rendering ---------------- */

function renderTable() {
  const tbody = document.getElementById('table-body');
  const total = state.filtered.length;
  const totalPages = Math.max(1, Math.ceil(total / state.pageSize));
  state.page = Math.min(state.page, totalPages);
  const start = (state.page - 1) * state.pageSize;
  const pageItems = state.filtered.slice(start, start + state.pageSize);

  document.getElementById('result-count').innerHTML = `<b>${total}</b> отзывов найдено (из ${state.reviews.length} всего)`;

  if (!pageItems.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty">Ничего не найдено под текущие фильтры</td></tr>`;
  } else {
    tbody.innerHTML = pageItems.map(r => rowHtml(r)).join('');
  }

  renderPagination(totalPages);
  bindRowClicks();

  // update sort arrows
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.classList.toggle('sorted', th.dataset.sort === state.sortKey);
    th.querySelector('.arrow').textContent = th.dataset.sort === state.sortKey
      ? (state.sortDir === 'asc' ? '▲' : '▼') : '';
  });
}

function rowHtml(r) {
  const flagged = (r.suspicion_score || 0) >= 40;
  const tags = [];
  if (r.received_for_free) tags.push('<span class="tag free">FREE KEY</span>');
  if (r.duplicate_cluster_size >= 3) tags.push(`<span class="tag">DUPE×${r.duplicate_cluster_size}</span>`);
  if ((r.suspicion_reasons || []).includes('posted_during_review_burst')) tags.push('<span class="tag">BURST</span>');
  if ((r.suspicion_reasons || []).includes('prolific_reviewer_few_games')) tags.push('<span class="tag">FARM?</span>');
  if ((r.suspicion_reasons || []).includes('edited_days_later')) tags.push('<span class="tag">EDITED</span>');

  return `
    <tr class="${flagged ? 'flagged' : ''}" data-rid="${r.recommendationid}">
      <td class="td-date">${fmtDate(r.timestamp_created)}</td>
      <td><span class="td-vote ${r.voted_up ? 'up' : 'down'}">${r.voted_up ? '▲ Позитив' : '▼ Негатив'}</span></td>
      <td class="td-playtime">${fmtHours(r.playtime_at_review)}<span class="bucket">${r.playtime_bucket || ''}</span></td>
      <td class="td-score">
        <span class="score-bar"><i style="width:${r.suspicion_score || 0}%"></i></span>${r.suspicion_score || 0}
      </td>
      <td>${tags.join('') || '—'}</td>
      <td class="td-text">${escapeHtml((r.review || '').slice(0, 140))}</td>
    </tr>
  `;
}

function detailHtml(r) {
  const reasons = (r.suspicion_reasons || []).map(k => {
    const label = REASON_LABELS[k.split('_size_')[0]] || k;
    return `<span class="tag" style="color:var(--red);border-color:var(--red-dim);">${escapeHtml(label)}</span>`;
  }).join(' ');

  return `
    <tr class="detail-row"><td colspan="6">
      <div class="detail-grid">
        <div class="detail-text">${escapeHtml(r.review || '(пустой текст)')}</div>
        <div class="detail-meta">
          <div>steamid: ${r.steamid ? `<a href="https://steamcommunity.com/profiles/${r.steamid}" target="_blank" rel="noopener">${r.steamid}</a>` : '—'}</div>
          <div>playtime forever: ${fmtHours(r.playtime_forever)}</div>
          <div>playtime at review: ${fmtHours(r.playtime_at_review)}</div>
          <div>playtime last 2 weeks: ${fmtHours(r.playtime_last_two_weeks)}</div>
          <div>игр в библиотеке: ${r.num_games_owned ?? '—'}</div>
          <div>отзывов от автора: ${r.num_reviews ?? '—'}</div>
          <div>куплено в Steam: ${r.steam_purchase ? 'да' : 'нет'}</div>
          <div>получено бесплатно: ${r.received_for_free ? 'да' : 'нет'}</div>
          <div>votes up / funny: ${r.votes_up ?? 0} / ${r.votes_funny ?? 0}</div>
          <div>язык: ${r.language || '—'}</div>
          <div class="detail-reasons">${reasons || 'без флагов'}</div>
        </div>
      </div>
    </td></tr>
  `;
}

function bindRowClicks() {
  document.querySelectorAll('#table-body tr[data-rid]').forEach(tr => {
    tr.addEventListener('click', () => {
      const rid = tr.dataset.rid;
      const existing = tr.nextElementSibling;
      if (existing && existing.classList.contains('detail-row')) {
        existing.remove();
        return;
      }
      document.querySelectorAll('.detail-row').forEach(el => el.remove());
      const r = state.filtered.find(x => x.recommendationid === rid);
      if (!r) return;
      tr.insertAdjacentHTML('afterend', detailHtml(r));
    });
  });
}

function renderPagination(totalPages) {
  const el = document.getElementById('pagination');
  el.innerHTML = `
    <button id="pg-prev" ${state.page <= 1 ? 'disabled' : ''}>← назад</button>
    <span>стр. ${state.page} / ${totalPages}</span>
    <button id="pg-next" ${state.page >= totalPages ? 'disabled' : ''}>вперёд →</button>
  `;
  document.getElementById('pg-prev').addEventListener('click', () => { state.page--; renderTable(); });
  document.getElementById('pg-next').addEventListener('click', () => { state.page++; renderTable(); });
}

/* ---------------- Controls binding ---------------- */

function bindControls() {
  // sortable headers
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = key;
        state.sortDir = 'desc';
      }
      state.filtered = sortReviews(state.filtered);
      renderTable();
    });
  });

  // vote type chips (multi-toggle, 'all' resets to all three)
  document.querySelectorAll('#filter-vote .chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const val = chip.dataset.val;
      const f = state.filters.vote;
      if (val === 'all') {
        f.clear(); f.add('all'); f.add('up'); f.add('down');
      } else {
        f.delete('all');
        if (f.has(val)) f.delete(val); else f.add(val);
        if (!f.has('up') && !f.has('down')) { f.add('all'); f.add('up'); f.add('down'); }
      }
      document.querySelectorAll('#filter-vote .chip').forEach(c => {
        c.classList.toggle('active', f.has(c.dataset.val));
      });
      applyFiltersAndRender();
    });
  });

  document.getElementById('filter-bucket').addEventListener('change', e => {
    state.filters.bucket = e.target.value;
    applyFiltersAndRender();
  });

  document.getElementById('filter-minscore').addEventListener('input', e => {
    state.filters.minScore = parseInt(e.target.value, 10) || 0;
    applyFiltersAndRender();
  });

  const suspBtn = document.getElementById('filter-suspicious-only');
  suspBtn.addEventListener('click', () => {
    state.filters.suspiciousOnly = !state.filters.suspiciousOnly;
    suspBtn.classList.toggle('active', state.filters.suspiciousOnly);
    applyFiltersAndRender();
  });

  const freeBtn = document.getElementById('filter-free-only');
  freeBtn.addEventListener('click', () => {
    state.filters.freeOnly = !state.filters.freeOnly;
    freeBtn.classList.toggle('active', state.filters.freeOnly);
    applyFiltersAndRender();
  });

  const dupeBtn = document.getElementById('filter-dupe-only');
  dupeBtn.addEventListener('click', () => {
    state.filters.dupeOnly = !state.filters.dupeOnly;
    dupeBtn.classList.toggle('active', state.filters.dupeOnly);
    applyFiltersAndRender();
  });

  const editedBtn = document.getElementById('filter-edited-only');
  editedBtn.addEventListener('click', () => {
    state.filters.editedOnly = !state.filters.editedOnly;
    editedBtn.classList.toggle('active', state.filters.editedOnly);
    applyFiltersAndRender();
  });

  let searchTimer;
  document.getElementById('filter-search').addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.filters.search = e.target.value.trim();
      applyFiltersAndRender();
    }, 200);
  });

  document.getElementById('filter-reset').addEventListener('click', () => {
    state.filters = {
      vote: new Set(['all', 'up', 'down']),
      bucket: '', minScore: 0, suspiciousOnly: false, freeOnly: false, dupeOnly: false, editedOnly: false, search: '',
    };
    document.getElementById('filter-bucket').value = '';
    document.getElementById('filter-minscore').value = 0;
    document.getElementById('filter-search').value = '';
    document.querySelectorAll('#filter-vote .chip').forEach(c => c.classList.add('active'));
    document.getElementById('filter-suspicious-only').classList.remove('active');
    document.getElementById('filter-free-only').classList.remove('active');
    document.getElementById('filter-dupe-only').classList.remove('active');
    document.getElementById('filter-edited-only').classList.remove('active');
    applyFiltersAndRender();
  });

  document.getElementById('threshold-slider').addEventListener('input', e => {
    state.threshold = parseInt(e.target.value, 10);
    document.getElementById('threshold-val').textContent = `${state.threshold} мин`;
    renderHistogram(state.reviews, state.threshold);
    updateThresholdCount();
  });

  document.getElementById('export-csv').addEventListener('click', exportCsv);
}

function exportCsv() {
  const cols = ['recommendationid', 'timestamp_created', 'voted_up', 'playtime_at_review',
    'playtime_forever', 'suspicion_score', 'suspicion_reasons', 'received_for_free',
    'steam_purchase', 'num_reviews', 'num_games_owned', 'language', 'steamid', 'review'];
  const rows = [cols.join(',')];
  for (const r of state.filtered) {
    const row = cols.map(c => {
      let v = r[c];
      if (Array.isArray(v)) v = v.join('|');
      if (v === null || v === undefined) v = '';
      v = String(v).replace(/"/g, '""');
      return `"${v}"`;
    });
    rows.push(row.join(','));
  }
  const blob = new Blob([rows.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `reviews_export_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

init();
