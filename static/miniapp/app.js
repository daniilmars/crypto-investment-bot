// Telegram Mini App — portfolio dashboard (v2)
// Groups positions by strategy, inline rationale expand, recent closed trades.

(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  // --- read dev_key from querystring for local preview ---
  const devKey = new URLSearchParams(location.search).get('dev_key') || '';

  const state = {
    days: 30,
    chart: null,
    timer: null,
    initData: (tg && tg.initData) || '',
    expanded: new Set(JSON.parse(localStorage.getItem('miniapp.v2.expanded') || '[]')),
    collapsedGroups: new Set(JSON.parse(localStorage.getItem('miniapp.v2.collapsedGroups') || '[]')),
    closedLimit: parseInt(localStorage.getItem('miniapp.v2.closedLimit') || '10', 10),
    lastPositions: null,
    lastClosed: null,
    lastCapital: null,             // {by_strategy: [...], total_value_usd, ...}
  };

  const STRATEGY_ORDER = ['auto', 'conservative', 'longterm'];

  // --------------------------------------------------------------- helpers

  const fmtUsd = (n) => {
    if (n == null || isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '−';
    return `${sign}$${Math.abs(n).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    })}`;
  };
  // Compact form for the capital bar / group header — no leading +/− sign,
  // since these are absolute values, not deltas.
  const fmtUsdAbs = (n) => {
    if (n == null || isNaN(n)) return '—';
    return `$${n.toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    })}`;
  };
  const fmtUsdK = (n) => {
    if (n == null || isNaN(n)) return '—';
    const abs = Math.abs(n);
    if (abs >= 1000) return `$${(n / 1000).toFixed(1)}k`;
    return `$${n.toFixed(0)}`;
  };
  const fmtPct = (n, digits = 1) => {
    if (n == null || isNaN(n)) return '—';
    const sign = n >= 0 ? '+' : '−';
    return `${sign}${Math.abs(n).toFixed(digits)}%`;
  };
  // 2-decimal variant used by return-on-capital badges (e.g. "+2.43%").
  // Always-signed so 0.00% is "+0.00%" not "0.00%" — matches the rest of the UI.
  const fmtPctSigned = (n) => fmtPct(n, 2);
  const fmtPrice = (n) => {
    if (n == null || isNaN(n)) return '—';
    const abs = Math.abs(n);
    const digits = abs >= 1000 ? 0 : abs >= 100 ? 1 : abs >= 1 ? 2 : 4;
    return `$${n.toLocaleString('en-US', {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    })}`;
  };
  const klass = (n) => (n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral');

  function escapeHtml(s) {
    return String(s ?? '')
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  function fmtHours(h) {
    if (h == null) return '—';
    if (h < 1) return `${Math.round(h * 60)}m`;
    if (h < 24) return `${h.toFixed(1)}h`;
    return `${(h / 24).toFixed(1)}d`;
  }

  // Relative for last 24h ("3h ago"), 1-6d ("3d ago"), else short date ("Apr 17").
  // Older than 365d includes year ("Apr 20, 2025").
  function fmtRelativeOrAbs(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d)) return '';
    const diffMs = Date.now() - d.getTime();
    const diffMin = diffMs / 60000;
    const diffH = diffMs / 3600000;
    const diffD = diffH / 24;
    if (diffMin < 1) return 'just now';
    if (diffH < 1) return `${Math.round(diffMin)}m ago`;
    if (diffH < 24) return `${Math.round(diffH)}h ago`;
    if (diffD < 7) return `${Math.round(diffD)}d ago`;
    const opts = { month: 'short', day: 'numeric' };
    if (diffD > 365) opts.year = 'numeric';
    return d.toLocaleDateString('en-US', opts);
  }

  // Currency symbols for local-currency PnL display
  const CCY_SYM = {
    USD: '$', USDT: '$', GBP: '£', EUR: '€', JPY: '¥',
    DKK: 'kr ', HKD: 'HK$', CHF: 'Fr ', AUD: 'A$', CAD: 'C$',
    KRW: '₩', CNY: '¥',
  };
  function fmtLocal(n, ccy) {
    if (n == null || isNaN(n)) return '—';
    const sym = CCY_SYM[ccy] || `${ccy || ''} `;
    const sign = n >= 0 ? '+' : '−';
    return `${sign}${sym}${Math.abs(n).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    })}`;
  }

  async function api(path) {
    const headers = { 'X-Telegram-Init-Data': state.initData };
    if (devKey) headers['X-Miniapp-Dev-Key'] = devKey;
    const resp = await fetch(path, { headers });
    if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
    return resp.json();
  }

  function toast(msg, autoHideMs = 2500) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    if (autoHideMs) setTimeout(() => el.classList.add('hidden'), autoHideMs);
  }

  // ---------------------------------------------------------- render: summary

  function renderSummary(s, positions) {
    if (!s || s.error) {
      document.getElementById('headline-realized').textContent = '—';
      document.getElementById('headline-realized-breakdown').textContent =
        s && s.error ? `data unavailable (${s.error})` : 'no data';
      return;
    }

    const realized = s.realized || {};
    // Non-overlapping bands so the row sums to lifetime and any losing
    // window appears explicitly in red.
    const today = realized.d1 || 0;
    const week2to7 = realized.d2_to_7 || 0;
    const month8to30 = realized.d8_to_30 || 0;
    const olderThan30 = realized.older_than_30d || 0;
    const lifetime = realized.all || 0;
    const unrealized = s.unrealized_now_usd || 0;

    // --- Header: lifetime realized as the hero, with total-return % badge ---
    const realEl = document.getElementById('headline-realized');
    realEl.className = `value ${klass(lifetime)}`;
    const retPctAgg = s.capital && s.capital.return_pct;
    if (retPctAgg != null) {
      realEl.innerHTML =
        `${escapeHtml(fmtUsd(lifetime))}` +
        ` <span class="return-pct ${klass(retPctAgg)}">${fmtPctSigned(retPctAgg)}</span>`;
    } else {
      realEl.textContent = fmtUsd(lifetime);
    }

    // --- Row 1: realized PnL over time (the connected period bar) ---
    const setTile = (id, value, useClass = true) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = fmtUsd(value);
      if (useClass) el.className = `tile-value ${klass(value)}`;
    };
    setTile('tile-today-realized', today);
    setTile('tile-2to7-realized', week2to7);
    setTile('tile-8to30-realized', month8to30);
    setTile('tile-older-realized', olderThan30);

    // --- Row 2: live portfolio snapshot ---
    const urEl = document.getElementById('tile-unrealized');
    if (urEl) {
      urEl.textContent = fmtUsd(unrealized);
      urEl.className = `tile-value ${klass(unrealized)}`;
    }

    const openBy = s.open_by_strategy || {};
    const openTotal = Object.values(openBy).reduce((a, b) => a + b, 0);
    const breakdown = Object.entries(openBy)
      .map(([k, v]) => `${k[0].toUpperCase()}${v}`)
      .join('/');
    const openEl = document.getElementById('tile-open');
    if (openEl) {
      openEl.textContent = openTotal ? `${openTotal}\u00A0(${breakdown})` : '0';
      openEl.className = 'tile-value';
    }

    const wrEl = document.getElementById('tile-winrate');
    if (wrEl) {
      if (s.win_rate_7d != null) {
        const wr = s.win_rate_7d;
        wrEl.textContent = `${(wr * 100).toFixed(0)}%\u00A0(${s.wins_7d}W/${s.losses_7d}L)`;
        wrEl.className = `tile-value ${wr >= 0.5 ? 'positive' : 'negative'}`;
      } else {
        wrEl.textContent = '—';
        wrEl.className = 'tile-value';
      }
    }

    // Biggest mover — computed from positions payload
    const movers = (positions && positions.positions) || [];
    const biggestEl = document.getElementById('tile-biggest');
    if (movers.length) {
      const biggest = movers.reduce((m, r) =>
        Math.abs(r.pnl_pct || 0) > Math.abs(m.pnl_pct || 0) ? r : m);
      const pctClass = klass(biggest.pnl_pct);
      // Use the raw ticker (not display name) so the tile width is bounded.
      // Display names like "Nvidia" + percent push the row past the viewport.
      biggestEl.innerHTML =
        `${escapeHtml(biggest.symbol || '')} <span class="${pctClass}">${fmtPct(biggest.pnl_pct)}</span>`;
    } else {
      biggestEl.textContent = '—';
    }

    // Regime pill
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

    // Capital section — render aggregate bar + stash per-strategy for group headers
    renderCapital(s.capital);
    state.lastCapital = (s.capital && s.capital.by_strategy) || [];
  }

  // ---------------------------------------------------------- render: capital

  function renderCapital(c) {
    const totalEl = document.getElementById('cap-total');
    const lockedEl = document.getElementById('cap-locked');
    const freeEl = document.getElementById('cap-free');
    const meterEl = document.getElementById('cap-meter-fill');
    const hintEl = document.getElementById('capital-util-hint');
    if (!c || c.total_value_usd == null) {
      totalEl.textContent = '—';
      lockedEl.textContent = '—';
      freeEl.textContent = '—';
      if (meterEl) meterEl.style.width = '0%';
      if (hintEl) hintEl.textContent = '—';
      return;
    }
    totalEl.textContent = fmtUsdAbs(c.total_value_usd);
    lockedEl.textContent = fmtUsdAbs(c.cash_locked_usd);
    freeEl.textContent = fmtUsdAbs(c.cash_free_usd);
    const util = c.utilization_pct;
    if (util != null) {
      if (meterEl) meterEl.style.width = `${util}%`;
      if (hintEl) hintEl.textContent = `${util.toFixed(1)}% deployed`;
    } else {
      if (meterEl) meterEl.style.width = '0%';
      if (hintEl) hintEl.textContent = '—';
    }
  }

  // -------------------------------------------------------- rationale render

  function renderRationale(rationale) {
    if (!rationale) {
      return '<div class="rat-empty">No rationale recorded (pre-WS1 trade).</div>';
    }
    const parts = ['<div class="rat-grid">'];

    // Gemini line
    if (rationale.gemini_direction || rationale.gemini_confidence != null) {
      const dir = rationale.gemini_direction || '?';
      const conf = rationale.gemini_confidence != null
        ? rationale.gemini_confidence.toFixed(2) : '—';
      const cat = rationale.catalyst_type || '?';
      const fresh = rationale.catalyst_freshness;
      const hvf = rationale.hype_vs_fundamental;
      let line = `<strong>${escapeHtml(dir)}</strong> · conf ${escapeHtml(conf)} · catalyst <strong>${escapeHtml(cat)}</strong>`;
      if (fresh) line += ` · ${escapeHtml(fresh)}`;
      if (hvf) line += ` · ${escapeHtml(hvf)}`;
      parts.push(`<div class="rat-label">Gemini</div><div class="rat-value">${line}</div>`);
    }

    // Key headline
    if (rationale.key_headline) {
      parts.push(`<div class="rat-headline">“${escapeHtml(rationale.key_headline)}”</div>`);
    }

    // Reasoning
    if (rationale.reasoning) {
      parts.push(`<div class="rat-reasoning">${escapeHtml(rationale.reasoning)}</div>`);
    }

    // Sources
    if (rationale.sources && rationale.sources.length) {
      const chips = rationale.sources
        .map((s) => `<span class="rat-chip">${escapeHtml(s)}</span>`).join('');
      parts.push(`<div class="rat-label">Sources</div><div class="rat-value">${chips}</div>`);
    }

    // Risks
    if (rationale.risk_factors && rationale.risk_factors.length) {
      const chips = rationale.risk_factors
        .map((r) => `<span class="rat-chip risk">${escapeHtml(r)}</span>`).join('');
      parts.push(`<div class="rat-label">Risks</div><div class="rat-value">${chips}</div>`);
    }

    // Legacy fallback (pre-WS1)
    if (!rationale.key_headline && !rationale.reasoning &&
        !(rationale.sources && rationale.sources.length) && rationale.trade_reason) {
      parts.push(`<div class="rat-reasoning">${escapeHtml(rationale.trade_reason)}</div>`);
    }

    parts.push('</div>');
    return parts.join('');
  }

  function renderPositionDetail(p) {
    const parts = ['<div class="rat-grid">'];

    parts.push(`<div class="rat-label">Entry → Now</div><div class="rat-value">${fmtPrice(p.entry_price)} → <strong>${fmtPrice(p.current_price)}</strong> · ${fmtPct(p.pnl_pct, 2)}</div>`);

    if (p.age_days != null) {
      parts.push(`<div class="rat-label">Age</div><div class="rat-value">${p.age_days}d</div>`);
    }

    if (p.sl_price != null) {
      const cls = p.sl_distance_pct != null && p.sl_distance_pct < 5 ? 'negative' : 'neutral';
      parts.push(`<div class="rat-label">Stop-loss</div><div class="rat-value ${cls}">${fmtPrice(p.sl_price)} · ${fmtPct(p.sl_distance_pct)} away</div>`);
    }
    if (p.tp_price != null) {
      parts.push(`<div class="rat-label">Take-profit</div><div class="rat-value">${fmtPrice(p.tp_price)} · ${fmtPct(p.tp_distance_pct)} to go</div>`);
    }

    parts.push('</div>');

    // Append rationale as its own grid
    return parts.join('') + '<div style="height:8px"></div>' + renderRationale(p.rationale);
  }

  // -------------------------------------------------------- render: positions

  function renderPositions(p) {
    state.lastPositions = p;
    const container = document.getElementById('positions-groups');
    const title = document.getElementById('positions-title');
    const rows = (p && p.positions) || [];

    if (!rows.length) {
      title.textContent = 'Open positions';
      container.innerHTML = '<div class="empty-state">No open positions.</div>';
      return;
    }

    title.textContent = `Open positions (${rows.length})`;

    // Group by strategy, fixed order. Any row with an unknown strategy
    // (including legacy 'manual' rows) goes to a catch-all bucket rendered
    // after the known strategies.
    const groups = {};
    for (const s of STRATEGY_ORDER) groups[s] = [];
    for (const r of rows) {
      const s = STRATEGY_ORDER.includes(r.strategy) ? r.strategy : 'unknown';
      if (!groups[s]) groups[s] = [];
      groups[s].push(r);
    }

    const stale = new Set(p.stale_prices || []);

    // Render known strategies first, then any 'unknown' bucket last.
    const renderOrder = [...STRATEGY_ORDER];
    if (groups.unknown && groups.unknown.length > 0) renderOrder.push('unknown');
    const html = renderOrder
      .filter((s) => groups[s] && groups[s].length > 0)
      .map((strat) => {
        const arr = groups[strat];
        // Sort within group by pnl_pct desc
        arr.sort((a, b) => (b.pnl_pct || 0) - (a.pnl_pct || 0));
        const subtotal = arr.reduce((a, r) => a + (r.pnl_usd || 0), 0);
        const subtotalClass = klass(subtotal);
        const isCollapsed = state.collapsedGroups.has(strat) ? 'collapsed' : '';

        const rowsHtml = arr.map((r) => {
          const isExpanded = state.expanded.has(r.order_id) ? 'expanded' : '';
          const staleMark = stale.has(r.symbol) ? '<span class="stale-dot" title="stale price"></span>' : '';
          const nameLine = r.display_name
            ? `<div class="pos-name">${escapeHtml(r.display_name)}</div>`
            : '';
          return `
            <div class="pos-row ${isExpanded}" data-order-id="${escapeHtml(r.order_id || '')}">
              <div class="pos-left">
                <div class="pos-symbol-row">
                  ${escapeHtml(r.symbol)}${staleMark}
                </div>
                ${nameLine}
                <div class="pos-meta">${escapeHtml(r.age_days ?? 0)}d · entry ${fmtPrice(r.entry_price)}</div>
              </div>
              <div class="pos-pnl-col">
                <div class="pos-pnl-usd ${klass(r.pnl_usd)}">${fmtUsd(r.pnl_usd)}</div>
                <div class="pos-pnl-pct ${klass(r.pnl_pct)}">${fmtPct(r.pnl_pct)}</div>
              </div>
              <div class="expand-arrow">▼</div>
            </div>
            <div class="expand-panel">${isExpanded ? renderPositionDetail(r) : ''}</div>
          `;
        }).join('');

        // Capital line for this strategy (free / total · util% · return%)
        // from the /summary capital.by_strategy payload.
        const stratCap = (state.lastCapital || []).find((x) => x.name === strat);
        let capLine = '';
        if (stratCap) {
          const parts = [
            `${fmtUsdK(stratCap.free_usd)} free / ${fmtUsdK(stratCap.total_usd)} total`,
          ];
          if (stratCap.utilization_pct != null) {
            parts.push(`${stratCap.utilization_pct.toFixed(0)}% deployed`);
          }
          if (stratCap.return_pct != null) {
            parts.push(
              `<span class="${klass(stratCap.return_pct)}">${fmtPctSigned(stratCap.return_pct)}</span>`
            );
          }
          if (stratCap.deployed_return_pct != null) {
            parts.push(
              `<span class="${klass(stratCap.deployed_return_pct)}" title="ROI on deployed capital (open + closed cost basis)">D ${fmtPctSigned(stratCap.deployed_return_pct)}</span>`
            );
          }
          capLine = `<div class="group-capital">${parts.join(' · ')}</div>`;
        }

        return `
          <div class="group ${isCollapsed}" data-strategy="${strat}">
            <div class="group-header">
              <div class="group-title-col">
                <div class="group-title">
                  <span class="group-chevron">▼</span>
                  ${strat}
                  <span style="color:var(--text-dim);font-weight:400;font-size:12px">(${arr.length})</span>
                </div>
                ${capLine}
              </div>
              <div class="group-meta-col">
                <span class="subtotal ${subtotalClass}">${fmtUsd(subtotal)}</span>
              </div>
            </div>
            <div class="group-rows">${rowsHtml}</div>
          </div>
        `;
      })
      .join('');

    container.innerHTML = html;
  }

  // ---------------------------------------------------- render: closed trades

  function renderClosed(c) {
    state.lastClosed = c;
    const list = document.getElementById('closed-list');
    const title = document.getElementById('closed-title');
    const rows = (c && c.trades) || [];

    if (!rows.length) {
      title.textContent = 'Recent closed trades';
      list.innerHTML = '<div class="empty-state">No closed trades yet.</div>';
      return;
    }

    title.textContent = `Recent closed trades (${rows.length})`;

    const exitClass = (reason) => {
      if (!reason) return 'exit-neutral';
      const r = reason.toLowerCase();
      if (r.includes('take_profit') || r.includes('trailing_stop')) return 'exit-win';
      if (r.includes('stop_loss') || r.includes('flash_analyst')) return 'exit-win'; // may be loss or win — depends on PnL
      return 'exit-neutral';
    };

    list.innerHTML = rows.map((t) => {
      const isExpanded = state.expanded.has(t.order_id) ? 'expanded' : '';
      const pnlCls = klass(t.pnl_usd);
      const exitTag = t.exit_reason
        ? `<span class="closed-exit ${t.pnl_usd >= 0 ? 'exit-win' : 'exit-loss'}">${escapeHtml(t.exit_reason)}</span>`
        : '';
      const nameLine = t.display_name
        ? `<div class="pos-name">${escapeHtml(t.display_name)}</div>`
        : '';
      const exitRel = fmtRelativeOrAbs(t.exit_timestamp);
      const exitRelStr = exitRel ? ` · ${escapeHtml(exitRel)}` : '';
      const exitTitle = t.exit_timestamp
        ? ` title="exit: ${escapeHtml(t.exit_timestamp)}"` : '';
      return `
        <div class="closed-row ${isExpanded}" data-order-id="${escapeHtml(t.order_id || '')}">
          <div class="pos-left">
            <div class="pos-symbol-row">
              ${escapeHtml(t.symbol)}
              <span style="font-size:11px;color:var(--text-dim);font-weight:400">${escapeHtml(t.strategy)}</span>
              ${exitTag}
            </div>
            ${nameLine}
            <div class="pos-meta"${exitTitle}>
              ${fmtPrice(t.entry_price)} → ${fmtPrice(t.exit_price)} · ${fmtHours(t.duration_hours)}${exitRelStr}
            </div>
          </div>
          <div class="pos-pnl-col">
            <div class="pos-pnl-usd ${pnlCls}">${fmtUsd(t.pnl_usd)}</div>
            <div class="pos-pnl-pct ${klass(t.pnl_pct)}">${fmtPct(t.pnl_pct)}</div>
          </div>
          <div class="expand-arrow">▼</div>
        </div>
        <div class="expand-panel">${isExpanded ? renderClosedDetail(t) : ''}</div>
      `;
    }).join('');
  }

  // Expand-panel renderer for closed trades: exit block first (what's new),
  // then entry rationale (reuses existing renderRationale).
  function renderClosedDetail(t) {
    const parts = ['<div class="rat-grid rat-exit">'];

    // Window: entry → exit dates
    if (t.entry_timestamp || t.exit_timestamp) {
      const entry = fmtRelativeOrAbs(t.entry_timestamp) || '?';
      const exit = fmtRelativeOrAbs(t.exit_timestamp) || '?';
      parts.push(
        `<div class="rat-label">Window</div>` +
        `<div class="rat-value">${escapeHtml(entry)} → ${escapeHtml(exit)}</div>`
      );
    }

    // Dual-currency PnL when foreign
    if (t.currency && t.currency !== 'USD' && t.currency !== 'USDT'
        && t.pnl_local != null) {
      parts.push(
        `<div class="rat-label">PnL</div>` +
        `<div class="rat-value">${escapeHtml(fmtLocal(t.pnl_local, t.currency))}` +
        ` · ${escapeHtml(fmtUsd(t.pnl_usd))}</div>`
      );
    }

    // Exit tag + prose reasoning
    parts.push(
      `<div class="rat-label">Exit</div>` +
      `<div class="rat-value"><strong>${escapeHtml(t.exit_reason || 'unknown')}</strong></div>`
    );
    if (t.exit_reasoning) {
      parts.push(`<div class="rat-reasoning">${escapeHtml(t.exit_reasoning)}</div>`);
    } else {
      parts.push(
        `<div class="rat-empty">No exit reasoning recorded (pre-PR trade).</div>`
      );
    }

    // Trailing peak — only for trailing exits
    if (t.trailing_stop_peak
        && typeof t.exit_reason === 'string'
        && t.exit_reason.indexOf('trailing') !== -1) {
      parts.push(
        `<div class="rat-label">Peak</div>` +
        `<div class="rat-value">${escapeHtml(fmtPrice(t.trailing_stop_peak))}</div>`
      );
    }

    parts.push('</div>');

    // Then the entry rationale (same renderer the open-positions panel uses)
    return parts.join('')
      + '<div style="height:10px"></div>'
      + renderRationale(t.rationale);
  }

  // ------------------------------------------------------------ render: chart

  function renderEquity(e) {
    const ctx = document.getElementById('equity-chart').getContext('2d');
    const pts = (e && e.points) || [];
    const nowPoint = e && e.now;

    const seriesRealized = pts.map((p) => ({ x: p.t, y: p.realized_usd }));
    if (nowPoint) seriesRealized.push({ x: nowPoint.t, y: nowPoint.realized_usd });
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
        { label: 'Realized', data: seriesRealized,
          borderColor: lineRealized, backgroundColor: lineRealized + '33',
          fill: 'origin', tension: 0.25, pointRadius: 0, borderWidth: 1.8 },
        { label: 'Realized + unrealized', data: seriesTotal,
          borderColor: lineTotal, backgroundColor: 'transparent',
          borderDash: [5, 4], tension: 0.25, pointRadius: 0, borderWidth: 1.5 },
      ],
    };
    const options = {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => fmtUsd(c.parsed.y) } } },
      scales: {
        x: { type: 'time',
             time: { unit: state.days <= 7 ? 'day' : state.days <= 30 ? 'day' : 'week' },
             ticks: { color: axisColor, maxRotation: 0, autoSkipPadding: 20 },
             grid: { display: false } },
        y: { ticks: { color: axisColor,
                      callback: (v) => fmtUsd(v).replace(/\.00$/, '') },
             grid: { color: 'rgba(128,128,128,0.12)' } },
      },
    };

    if (state.chart) {
      state.chart.data = data; state.chart.options = options;
      state.chart.update('none');
    } else {
      state.chart = new Chart(ctx, { type: 'line', data, options });
    }
  }

  // ------------------------------------------------------------ refresh loop

  async function refreshAll() {
    try {
      const [summary, positions, equity, closed] = await Promise.all([
        api('/api/miniapp/summary'),
        api('/api/miniapp/positions'),
        api(`/api/miniapp/equity?days=${state.days}`),
        api(`/api/miniapp/trades/recent?limit=${state.closedLimit}`),
      ]);
      renderPositions(positions);   // render positions first so biggest-mover is available
      renderSummary(summary, positions);
      renderEquity(equity);
      renderClosed(closed);
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

  // Range toggle (equity chart)
  document.querySelectorAll('.range-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.days = parseInt(btn.dataset.days, 10);
      refreshAll();
    });
  });

  // Closed trades limit toggle
  document.querySelectorAll('.closed-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.closed-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.closedLimit = parseInt(btn.dataset.limit, 10);
      localStorage.setItem('miniapp.v2.closedLimit', String(state.closedLimit));
      refreshAll();
    });
  });

  // Sync initial active state for closed-btn from localStorage
  document.querySelectorAll('.closed-btn').forEach((b) => {
    if (parseInt(b.dataset.limit, 10) === state.closedLimit) b.classList.add('active');
    else b.classList.remove('active');
  });

  // Delegated click handler for group collapse AND row expand
  document.addEventListener('click', (e) => {
    // Group header toggle
    const groupHeader = e.target.closest('.group-header');
    if (groupHeader) {
      const group = groupHeader.closest('.group');
      const strat = group.dataset.strategy;
      group.classList.toggle('collapsed');
      if (group.classList.contains('collapsed')) state.collapsedGroups.add(strat);
      else state.collapsedGroups.delete(strat);
      localStorage.setItem(
        'miniapp.v2.collapsedGroups',
        JSON.stringify([...state.collapsedGroups]));
      return;
    }

    // Position row expand/collapse
    const posRow = e.target.closest('.pos-row, .closed-row');
    if (posRow) {
      const id = posRow.dataset.orderId;
      if (!id) return;
      const wasExpanded = posRow.classList.contains('expanded');
      posRow.classList.toggle('expanded');
      const panel = posRow.nextElementSibling;
      if (!wasExpanded) {
        // Populate the panel with detail HTML on open
        const rec = (state.lastPositions?.positions || []).find((p) => p.order_id === id)
                 || (state.lastClosed?.trades || []).find((t) => t.order_id === id);
        if (rec) {
          panel.innerHTML = posRow.classList.contains('pos-row')
            ? renderPositionDetail(rec)
            : renderRationale(rec.rationale);
        }
        state.expanded.add(id);
      } else {
        state.expanded.delete(id);
      }
      localStorage.setItem('miniapp.v2.expanded', JSON.stringify([...state.expanded]));
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopTimer();
    else { refreshAll(); startTimer(); }
  });

  // Initial load
  refreshAll().then(startTimer);
})();
