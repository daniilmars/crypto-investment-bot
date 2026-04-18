// Telegram Mini App — portfolio dashboard
// Thin vanilla-JS client: three fetches, 30 s auto-refresh, pauses when hidden.

(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const state = {
    days: 7,
    sort: 'pnl',
    chart: null,
    timer: null,
    initData: (tg && tg.initData) || '',
  };

  // --------------------------------------------------------------- helpers

  const fmtUsd = (n) => {
    if (n == null || isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '−';
    return `${sign}$${Math.abs(n).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    })}`;
  };
  const fmtPct = (n) => {
    if (n == null || isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '−';
    return `${sign}${Math.abs(n).toFixed(1)}%`;
  };
  const klass = (n) => (n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral');

  async function api(path) {
    const resp = await fetch(path, {
      headers: { 'X-Telegram-Init-Data': state.initData },
    });
    if (!resp.ok) {
      throw new Error(`${resp.status} ${resp.statusText}`);
    }
    return resp.json();
  }

  function toast(msg, autoHideMs = 2500) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    if (autoHideMs) setTimeout(() => el.classList.add('hidden'), autoHideMs);
  }

  // ---------------------------------------------------------- render: summary

  function renderSummary(s) {
    if (!s || s.error) {
      document.getElementById('headline-total').textContent = '—';
      document.getElementById('headline-breakdown').textContent = s && s.error
        ? `data unavailable (${s.error})`
        : 'no data';
      return;
    }
    const realized = (s.realized && s.realized.all) || 0;
    const unrealized = s.unrealized_now_usd || 0;
    const total = realized + unrealized;

    const totalEl = document.getElementById('headline-total');
    totalEl.textContent = fmtUsd(total);
    totalEl.className = `value ${klass(total)}`;

    document.getElementById('headline-breakdown').textContent =
      `realized ${fmtUsd(realized)} · unrealized ${fmtUsd(unrealized)}`;

    const d1El = document.getElementById('tile-d1');
    const d7El = document.getElementById('tile-d7');
    const d30El = document.getElementById('tile-d30');
    d1El.textContent = fmtUsd(s.realized && s.realized.d1);
    d1El.className = `tile-value ${klass(s.realized && s.realized.d1)}`;
    d7El.textContent = fmtUsd(s.realized && s.realized.d7);
    d7El.className = `tile-value ${klass(s.realized && s.realized.d7)}`;
    d30El.textContent = fmtUsd(s.realized && s.realized.d30);
    d30El.className = `tile-value ${klass(s.realized && s.realized.d30)}`;

    const openBy = s.open_by_strategy || {};
    const openTotal = Object.values(openBy).reduce((a, b) => a + b, 0);
    const breakdown = Object.entries(openBy)
      .map(([k, v]) => `${k[0].toUpperCase()}${v}`)
      .join('/');
    document.getElementById('tile-open').textContent =
      openTotal ? `${openTotal}\u00A0(${breakdown})` : '0';

    const regime = s.regime;
    const pill = document.getElementById('regime-pill');
    if (regime && regime.regime) {
      pill.textContent = `${regime.regime} ${Number(regime.score).toFixed(1)}`;
      pill.className = 'pill pill-' + regime.regime.toLowerCase().replace('_', '-');
    } else {
      pill.textContent = 'REGIME —';
      pill.className = 'pill pill-neutral';
    }

    const asOf = s.as_of_ts ? new Date(s.as_of_ts).toLocaleTimeString() : '—';
    document.getElementById('as-of').textContent = `updated ${asOf}`;
  }

  // -------------------------------------------------------- render: positions

  function renderPositions(p) {
    const list = document.getElementById('positions-list');
    const title = document.getElementById('positions-title');
    const rows = (p && p.positions) || [];

    if (!rows.length) {
      title.textContent = 'Open positions';
      list.innerHTML = '<div class="empty-state">No open positions.</div>';
      return;
    }

    const sorted = [...rows];
    if (state.sort === 'age') {
      sorted.sort((a, b) => (b.age_days || 0) - (a.age_days || 0));
    } else {
      sorted.sort((a, b) => (b.pnl_usd || 0) - (a.pnl_usd || 0));
    }

    title.textContent = `Open positions (${rows.length})`;

    const stale = new Set(p.stale_prices || []);
    list.innerHTML = sorted.map((r) => {
      const staleMark = stale.has(r.symbol) ? '<span class="stale-dot" title="stale price"></span>' : '';
      return `
        <div class="pos-row">
          <div>
            <div class="pos-symbol">${escapeHtml(r.symbol)}${staleMark}</div>
            <div class="pos-strategy">${escapeHtml(r.strategy || '')}</div>
          </div>
          <div class="pos-age">${r.age_days ?? 0}d</div>
          <div class="pos-pnl ${klass(r.pnl_usd)}">${fmtUsd(r.pnl_usd)}</div>
          <div class="pos-pct ${klass(r.pnl_pct)}">${fmtPct(r.pnl_pct)}</div>
        </div>`;
    }).join('');
  }

  // ------------------------------------------------------------ render: chart

  function renderEquity(e) {
    const ctx = document.getElementById('equity-chart').getContext('2d');
    const pts = (e && e.points) || [];
    const nowPoint = e && e.now;

    const seriesRealized = pts.map((p) => ({ x: p.t, y: p.realized_usd }));
    if (nowPoint) {
      seriesRealized.push({ x: nowPoint.t, y: nowPoint.realized_usd });
    }
    const seriesTotal = nowPoint
      ? [
          ...pts.map((p) => ({ x: p.t, y: p.realized_usd })),
          { x: nowPoint.t, y: nowPoint.portfolio_delta_usd },
        ]
      : [];

    const cssVar = (name, fallback) =>
      getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
    const axisColor = cssVar('--text-dim', '#8c8d91');
    const lineRealized = cssVar('--accent', '#2481cc');
    const lineTotal = cssVar('--positive', '#21a26b');

    const data = {
      datasets: [
        {
          label: 'Realized',
          data: seriesRealized,
          borderColor: lineRealized,
          backgroundColor: lineRealized + '33',
          fill: 'origin',
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 1.8,
        },
        {
          label: 'Realized + unrealized',
          data: seriesTotal,
          borderColor: lineTotal,
          backgroundColor: 'transparent',
          borderDash: [5, 4],
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 1.5,
        },
      ],
    };

    const options = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => fmtUsd(c.parsed.y),
          },
        },
      },
      scales: {
        x: {
          type: 'time',
          time: { unit: state.days <= 7 ? 'day' : state.days <= 30 ? 'day' : 'week' },
          ticks: { color: axisColor, maxRotation: 0, autoSkipPadding: 20 },
          grid: { display: false },
        },
        y: {
          ticks: {
            color: axisColor,
            callback: (v) => fmtUsd(v).replace(/\.00$/, ''),
          },
          grid: { color: 'rgba(128,128,128,0.12)' },
        },
      },
    };

    if (state.chart) {
      state.chart.data = data;
      state.chart.options = options;
      state.chart.update('none');
    } else {
      state.chart = new Chart(ctx, { type: 'line', data, options });
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  // ------------------------------------------------------------ refresh loop

  async function refreshAll() {
    try {
      const [summary, positions, equity] = await Promise.all([
        api('/api/miniapp/summary'),
        api('/api/miniapp/positions'),
        api(`/api/miniapp/equity?days=${state.days}`),
      ]);
      renderSummary(summary);
      renderPositions(positions);
      renderEquity(equity);
    } catch (err) {
      toast(`Update failed: ${err.message}`);
      console.error('refreshAll error', err);
    }
  }

  function startTimer() {
    stopTimer();
    state.timer = setInterval(refreshAll, 30_000);
  }
  function stopTimer() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  // ---------------------------------------------------------------- wire-up

  document.querySelectorAll('.range-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.days = parseInt(btn.dataset.days, 10);
      refreshAll();
    });
  });
  document.querySelectorAll('.sort-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.sort-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.sort = btn.dataset.sort;
      // Re-render using last-fetched positions if we have them; otherwise refresh
      refreshAll();
    });
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopTimer();
    else { refreshAll(); startTimer(); }
  });

  refreshAll().then(startTimer);
})();
