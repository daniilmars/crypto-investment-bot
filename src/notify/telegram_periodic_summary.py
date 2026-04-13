"""4-hour periodic summary + enhanced trade alerts.

Uses HTML parse_mode for reliable formatting (monospace tables via <pre>,
bold headers via <b>). HTML only needs &lt; &gt; &amp; escaping.
"""

from datetime import datetime, timezone, timedelta

from telegram import Bot

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log

_error_count: int = 0

# Ticker → short readable name. Derived from SYMBOL_KEYWORDS at first call.
_ticker_names: dict[str, str] = {}


def _get_name(ticker: str) -> str:
    """Return a human-readable short name for a ticker."""
    if not _ticker_names:
        _build_ticker_names()
    return _ticker_names.get(ticker, ticker)


_FALLBACK_NAMES = {
    # Tickers not in SYMBOL_KEYWORDS (match via sector groups, not articles)
    '1299.HK': 'AIA Group', '2318.HK': 'Ping An', '0388.HK': 'HKEX',
    '0005.HK': 'HSBC HK', '2020.HK': 'Anta Sports', '9999.HK': 'NetEase',
    '3968.HK': 'CMB', '2269.HK': 'WuXi Bio', '2382.HK': 'Sunny Optical',
    'BBVA.MC': 'BBVA', 'SAN.MC': 'Santander', 'ITX.MC': 'Inditex',
    'IBE.MC': 'Iberdrola', 'NESTE.HE': 'Neste', 'UPM.HE': 'UPM',
    'NOKIA.HE': 'Nokia', 'KNEBV.HE': 'Kone', 'DANSKE.CO': 'Danske Bank',
    'ORSTED.CO': 'Orsted', 'NOVO-B.CO': 'Novo Nordisk', 'MAERSK-B.CO': 'Maersk',
    'VOLV-B.ST': 'Volvo', 'ERIC-B.ST': 'Ericsson', 'ABB.ST': 'ABB',
    'SAND.ST': 'Sandvik', 'ATCO-A.ST': 'Atlas Copco', 'SAAB-B.ST': 'Saab',
    'IHG.L': 'IHG Hotels', 'BAES.L': 'BAE Systems', 'BARC.L': 'Barclays',
    'LLOY.L': 'Lloyds', 'LSEG.L': 'LSE Group', 'AAL.L': 'Anglo American',
    'GLEN.L': 'Glencore', 'PRU.L': 'Prudential', 'NG.L': 'National Grid',
    'DGE.L': 'Diageo', 'VOD.L': 'Vodafone', 'WPP.L': 'WPP',
    'FLTR.L': 'Flutter', 'EXPN.L': 'Experian', 'RKT.L': 'Reckitt',
    'BATS.L': 'BAT', 'RIO.L': 'Rio Tinto', 'ULVR.L': 'Unilever',
    'HO.PA': 'Thales', 'LDO.MI': 'Leonardo', 'RACE.MI': 'Ferrari',
    'STLAM.MI': 'Stellantis', 'ENI.MI': 'ENI', 'ENEL.MI': 'Enel',
    'UCG.MI': 'UniCredit', 'ISP.MI': 'Intesa', 'EL.PA': 'EssilorLuxottica',
    'KER.PA': 'Kering', 'RMS.PA': 'Hermes', 'DSY.PA': 'Dassault',
    'CS.PA': 'AXA', 'AI.PA': 'Air Liquide', 'SU.PA': 'Schneider',
    'MC.PA': 'LVMH', 'OR.PA': "L'Oreal", 'BNP.PA': 'BNP Paribas',
    'SAF.PA': 'Safran', 'AIR.PA': 'Airbus', 'TTE.PA': 'TotalEnergies',
    'SAN.PA': 'Sanofi', 'ADYEN.AS': 'Adyen', 'INGA.AS': 'ING',
    'PHIA.AS': 'Philips', 'WKL.AS': 'Wolters Kluwer', 'AD.AS': 'Ahold',
    'ASML.AS': 'ASML', 'NESN.SW': 'Nestle', 'ROG.SW': 'Roche',
    'NOVN.SW': 'Novartis', 'UBSG.SW': 'UBS', 'ZURN.SW': 'Zurich Ins.',
    'ALV.DE': 'Allianz', 'MUV2.DE': 'Munich Re', 'FRE.DE': 'Fresenius',
    'DB1.DE': 'Deutsche Boerse', 'RHM.DE': 'Rheinmetall', 'HEN3.DE': 'Henkel',
    'ADS.DE': 'adidas', 'IFX.DE': 'Infineon', 'BAS.DE': 'BASF',
    'MBG.DE': 'Mercedes', 'BMW.DE': 'BMW', 'VOW3.DE': 'Volkswagen',
    'SIE.DE': 'Siemens', 'SAP.DE': 'SAP', 'DTE.DE': 'Deutsche Telekom',
    'EQNR.OL': 'Equinor',
    '7203.T': 'Toyota', '6758.T': 'Sony', '6861.T': 'Keyence',
    '8306.T': 'MUFG', '8035.T': 'Tokyo Electron', '7267.T': 'Honda',
    '6501.T': 'Hitachi', '6367.T': 'Daikin', '6954.T': 'Fanuc',
    '6098.T': 'Recruit', '9983.T': 'Uniqlo', '4502.T': 'Takeda',
    '4519.T': 'Chugai Pharma', '7741.T': 'HOYA', '9432.T': 'NTT',
    '6902.T': 'Denso', '6273.T': 'SMC', '4568.T': 'Astellas',
    '7974.T': 'Nintendo', '8058.T': 'Mitsubishi', '8001.T': 'ITOCHU',
    '005930.KS': 'Samsung', '000660.KS': 'SK Hynix', '373220.KS': 'LG Energy',
    '005380.KS': 'Hyundai', '051910.KS': 'LG Chem', '066570.KS': 'LG Electronics',
    '035420.KS': 'Naver', '028260.KS': 'Samsung C&T',
    '2330.TW': 'TSMC', '2317.TW': 'Foxconn', '2454.TW': 'MediaTek',
    '2308.TW': 'Delta Electronics', '2603.TW': 'Evergreen Marine',
    'D05.SI': 'DBS Bank', 'O39.SI': 'OCBC', 'U11.SI': 'UOB',
    'BHP.AX': 'BHP', 'CBA.AX': 'CommBank', 'CSL.AX': 'CSL',
    'FMG.AX': 'Fortescue', 'NAB.AX': 'NAB', 'MQG.AX': 'Macquarie',
    'WBC.AX': 'Westpac', 'WDS.AX': 'Woodside', 'ALL.AX': 'Aristocrat',
    'RELIANCE.NS': 'Reliance', 'TCS.NS': 'TCS', 'INFY.NS': 'Infosys',
    'HDFCBANK.NS': 'HDFC Bank', 'ICICIBANK.NS': 'ICICI Bank',
    'WIPRO.NS': 'Wipro', 'BAJFINANCE.NS': 'Bajaj Finance',
    'LT.NS': 'L&T', 'HINDUNILVR.NS': 'HUL', 'SBIN.NS': 'SBI',
    # US stocks not in SYMBOL_KEYWORDS
    'UBER': 'Uber', 'ABNB': 'Airbnb', 'RIVN': 'Rivian',
    'SHOP': 'Shopify', 'MELI': 'MercadoLibre', 'COIN': 'Coinbase',
    'SOFI': 'SoFi', 'APP': 'AppLovin', 'SPOT': 'Spotify',
    'RBLX': 'Roblox', 'QCOM': 'Qualcomm', 'MU': 'Micron',
    'TXN': 'Texas Instruments', 'ADI': 'Analog Devices',
    'NOW': 'ServiceNow', 'ABT': 'Abbott', 'CL': 'Colgate',
    'MDLZ': 'Mondelez', 'MPC': 'Marathon Petrol', 'VLO': 'Valero',
    'ECL': 'Ecolab', 'NEM': 'Newmont', 'NUE': 'Nucor',
    'EQIX': 'Equinix', 'O': 'Realty Income', 'GEV': 'GE Vernova',
    'VST': 'Vistra', 'GD': 'General Dynamics', 'NOC': 'Northrop Grumman',
    'LHX': 'L3Harris', 'HII': 'Huntington Ingalls', 'HAL': 'Halliburton',
    'CEG': 'Constellation Energy', 'SMR': 'NuScale Power', 'OKLO': 'Oklo',
    'CCJ': 'Cameco', 'FRO': 'Frontline', 'STNG': 'Scorpio Tankers',
    'EURN': 'Euronav', 'GOLD': 'Barrick Gold', 'ALB': 'Albemarle',
    'URA': 'Uranium ETF', 'GLD': 'Gold ETF', 'SLV': 'Silver ETF',
    'USO': 'Oil ETF', 'COPX': 'Copper ETF', 'TLT': 'Treasury ETF',
    'HYG': 'High Yield ETF', 'LQD': 'Corp Bond ETF',
    'T': 'AT&T', 'VZ': 'Verizon', 'SCHW': 'Schwab', 'BLK': 'BlackRock',
    'C': 'Citigroup', 'PG': 'Procter & Gamble', 'PM': 'Philip Morris',
    'HD': 'Home Depot', 'LOW': "Lowe's", 'TJX': 'TJX', 'CMG': 'Chipotle',
    'BKNG': 'Booking', 'NEE': 'NextEra', 'SO': 'Southern Co.',
    'DUK': 'Duke Energy', 'AMT': 'American Tower', 'PLD': 'Prologis',
    'CCI': 'Crown Castle', 'LIN': 'Linde', 'APD': 'Air Products',
    'SHW': 'Sherwin-Williams', 'FCX': 'Freeport-McMoRan',
    'UPS': 'UPS', 'DE': 'Deere', 'HON': 'Honeywell',
    'CAT': 'Caterpillar', 'BA': 'Boeing', 'GE': 'GE Aerospace',
    'LMT': 'Lockheed Martin', 'RTX': 'RTX/Raytheon',
    'LRCX': 'Lam Research', 'KLAC': 'KLA Corp', 'AMAT': 'Applied Materials',
    'SNPS': 'Synopsys', 'CDNS': 'Cadence', 'MRVL': 'Marvell',
    'DDOG': 'Datadog', 'ZS': 'Zscaler', 'FTNT': 'Fortinet',
    'TTD': 'The Trade Desk', 'XYZ': 'Block',
    # Crypto
    'HNT': 'Helium', 'ONDO': 'Ondo Finance', 'PENDLE': 'Pendle',
    'ENA': 'Ethena', 'ETHFI': 'Ether.fi', 'ALGO': 'Algorand',
    'DYDX': 'dYdX', 'TAO': 'Bittensor', 'RENDER': 'Render',
    'CHZ': 'Chiliz', 'FET': 'Fetch.ai', 'JUP': 'Jupiter',
    'WLD': 'Worldcoin', 'STX': 'Stacks',
}


def _build_ticker_names():
    """Build the ticker→name map once from SYMBOL_KEYWORDS + fallback."""
    _ticker_names.update(_FALLBACK_NAMES)
    try:
        from src.collectors.news_data import SYMBOL_KEYWORDS
        for sym, keywords in SYMBOL_KEYWORDS.items():
            # Find the best human-readable name from the keywords list.
            # Skip the ticker itself and entries ending in "stock"/"Inc"/"Corp".
            best = sym
            for kw in keywords:
                if kw == sym or kw == sym.upper():
                    continue
                # Strip suffixes to get clean company name
                clean = kw
                for suffix in (' stock', ' Inc', ' Corp', ' Ltd', ' plc',
                               ' SA', ' SE', ' AG', ' NV', ' Holdings',
                               ' Group', ' crypto', ' coin', ' Protocol'):
                    if clean.endswith(suffix):
                        clean = clean[:-len(suffix)]
                        break
                if len(clean) >= 2 and clean != sym:
                    best = clean
                    break
            _ticker_names[sym] = best
    except Exception:
        pass


def increment_error_count():
    """Called by error handler to track critical errors between summaries."""
    global _error_count
    _error_count += 1


def _esc(text) -> str:
    """Escape HTML special characters."""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


async def send_periodic_summary():
    """Build and send the consolidated 4-hour summary."""
    global _error_count

    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    now = datetime.now(timezone.utc)
    ph = "?"
    parts = []

    parts.append(f"<b>4H Summary</b> ({now.strftime('%H:%M UTC')})\n")

    # --- Macro regime ---
    try:
        from src.analysis.macro_regime import get_macro_regime
        regime = get_macro_regime()
        r_name = regime.get('regime', '?')
        r_score = regime.get('score', 0)
        vix_raw = regime.get('indicators', {}).get('vix', {}).get('current')
        vix = f"{vix_raw:.1f}" if isinstance(vix_raw, (int, float)) else '?'
        parts.append(f"Regime: <b>{r_name}</b> ({r_score:+.1f}) | VIX {vix}\n")
    except Exception:
        parts.append("Regime: unavailable\n")

    # --- Per-strategy table (monospace) ---
    try:
        from src.execution.binance_trader import get_open_positions
        from src.orchestration import bot_state
        import psycopg2

        strategies = ['auto', 'momentum', 'conservative', 'longterm']
        rows = []

        for strat in strategies:
            realized = 0
            try:
                conn = get_db_connection()
                is_pg = isinstance(conn, psycopg2.extensions.connection)
                ph = "%s" if is_pg else "?"
                with _cursor(conn) as cur:
                    cur.execute(
                        f"SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        f"WHERE status='CLOSED' AND trading_strategy={ph}",
                        (strat,))
                    row = cur.fetchone()
                    realized = float(row[0] if isinstance(row, (list, tuple))
                                     else (row.get('coalesce') or row.get('sum')
                                           or 0)) or 0
                release_db_connection(conn)
            except Exception:
                pass

            positions = []
            try:
                positions = get_open_positions.sync(
                    asset_type='all', trading_strategy=strat)
            except Exception:
                pass
            open_count = len(positions)

            unrealized = 0.0
            try:
                conn2 = get_db_connection()
                with _cursor(conn2) as cur2:
                    for p in positions:
                        cur2.execute(
                            f"SELECT price FROM market_prices WHERE symbol={ph} "
                            f"ORDER BY id DESC LIMIT 1",
                            (p['symbol'],))
                        row = cur2.fetchone()
                        if row:
                            price = float(row[0] if isinstance(row, (list, tuple))
                                          else row['price'])
                            unrealized += (price - p['entry_price']) * p['quantity']
                release_db_connection(conn2)
            except Exception:
                pass

            streak = bot_state.strategy_get_streak_state(strat)
            cw = streak.get('consecutive_wins', 0)
            sk = f"{cw}W" if cw > 0 else "-"

            # Build per-position detail lines
            pos_details = []
            try:
                conn_p = get_db_connection()
                with _cursor(conn_p) as cur_p:
                    for p in positions:
                        cur_p.execute(
                            f"SELECT price FROM market_prices "
                            f"WHERE symbol={ph} ORDER BY id DESC LIMIT 1",
                            (p['symbol'],))
                        row = cur_p.fetchone()
                        if row:
                            price = float(
                                row[0] if isinstance(row, (list, tuple))
                                else row['price'])
                            pp = (price - p['entry_price']) / p['entry_price'] * 100
                            pos_details.append((p['symbol'], pp))
                        else:
                            pos_details.append((p['symbol'], 0.0))
                release_db_connection(conn_p)
            except Exception:
                pos_details = [(p['symbol'], 0.0) for p in positions]

            # Sort by PnL% descending
            pos_details.sort(key=lambda x: x[1], reverse=True)

            short = strat[:4].upper()
            r_s = f"{realized:+.0f}" if realized else "0"
            u_s = f"{unrealized:+.0f}" if unrealized else "0"
            rows.append(
                f"\n<b>{short}</b> realized ${r_s} | "
                f"{open_count} open ${u_s} unrl | streak {sk}")

            if pos_details:
                for sym, pp in pos_details:
                    name = _get_name(sym)
                    icon = "+" if pp >= 0 else ""
                    rows.append(f"  {name} {icon}{pp:.1f}%")

        parts.append("\n".join(rows))

    except Exception:
        parts.append("<i>Strategy data unavailable</i>\n")

    # --- Trades since last summary (4h) ---
    try:
        cutoff = (now - timedelta(hours=4)).isoformat()
        conn3 = get_db_connection()
        with _cursor(conn3) as cur3:
            cur3.execute(
                f"SELECT symbol, trading_strategy, entry_price, quantity "
                f"FROM trades WHERE status='OPEN' AND entry_timestamp >= {ph} "
                f"ORDER BY entry_timestamp", (cutoff,))
            opened = cur3.fetchall()
            cur3.execute(
                f"SELECT symbol, trading_strategy, pnl, exit_reason "
                f"FROM trades WHERE status='CLOSED' AND exit_timestamp >= {ph} "
                f"ORDER BY exit_timestamp", (cutoff,))
            closed = cur3.fetchall()
        release_db_connection(conn3)

        if opened or closed:
            trade_lines = []
            for r in opened:
                sym = r[0] if isinstance(r, (list, tuple)) else r['symbol']
                st = r[1] if isinstance(r, (list, tuple)) else r['trading_strategy']
                ep = float(r[2] if isinstance(r, (list, tuple)) else r['entry_price'])
                qty = float(r[3] if isinstance(r, (list, tuple)) else r['quantity'])
                trade_lines.append(f"  + {_get_name(sym)} ({st}) ${ep * qty:.0f}")
            for r in closed:
                sym = r[0] if isinstance(r, (list, tuple)) else r['symbol']
                st = r[1] if isinstance(r, (list, tuple)) else r['trading_strategy']
                pnl = float((r[2] if isinstance(r, (list, tuple)) else r['pnl']) or 0)
                reason = r[3] if isinstance(r, (list, tuple)) else r['exit_reason']
                tag = "+" if pnl >= 0 else ""
                trade_lines.append(
                    f"  - {_get_name(sym)} {tag}${pnl:.2f} ({reason})")
            parts.append("\n<b>Last 4h:</b>\n" + "\n".join(trade_lines))
        else:
            parts.append("\n<i>No trades in last 4h</i>")
    except Exception:
        pass

    # --- Errors ---
    if _error_count > 0:
        parts.append(f"\n<i>{_error_count} error(s) since last summary</i>")
        _error_count = 0

    text = "\n".join(parts)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        log.info("4h periodic summary sent.")
    except Exception as e:
        log.warning(f"Failed to send periodic summary: {e}")
        # Fallback: try plain text
        try:
            import re
            plain = re.sub(r'<[^>]+>', '', text)
            await bot.send_message(chat_id=chat_id, text=plain)
            log.info("4h summary sent (plain text fallback).")
        except Exception:
            pass


async def send_trade_alert(
    *,
    action: str,
    symbol: str,
    trading_strategy: str,
    entry_price: float = 0,
    exit_price: float = 0,
    quantity: float = 0,
    pnl: float = 0,
    pnl_pct: float = 0,
    hold_duration: str = "",
    exit_reason: str = "",
    signal_strength: float = 0,
    gemini_direction: str = "",
    gemini_confidence: float = 0,
    catalyst_freshness: str = "",
    catalyst_type: str = "",
    key_headline: str = "",
    reason: str = "",
    macro_multiplier: float = 1.0,
    streak_multiplier: float = 1.0,
    sma_override: bool = False,
):
    """Send a concise trade execution alert."""
    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    name = _get_name(symbol)

    if action == "BUY":
        cost = entry_price * quantity
        lines = [f"<b>BUY {_esc(name)}</b> ({_esc(trading_strategy)})"]
        lines.append(f"${entry_price:,.2f} x {quantity:.4f} (${cost:.0f})")

        if gemini_direction and gemini_confidence:
            lines.append(
                f"Gemini: {gemini_direction} {gemini_confidence:.2f} "
                f"({catalyst_freshness or '?'})")
        if key_headline:
            lines.append(f"<i>{_esc(key_headline[:100])}</i>")

        mods = []
        if macro_multiplier != 1.0:
            mods.append(f"macro {macro_multiplier:.1f}x")
        if streak_multiplier != 1.0:
            mods.append(f"streak {streak_multiplier:.1f}x")
        if sma_override:
            mods.append("SMA override")
        if signal_strength:
            mod_str = f" | {', '.join(mods)}" if mods else ""
            lines.append(f"Strength: {signal_strength:.2f}{mod_str}")

    elif action == "SELL":
        lines = [f"<b>SELL {_esc(name)}</b> ({_esc(trading_strategy)})"]
        if entry_price and exit_price:
            lines.append(
                f"${entry_price:,.2f} → ${exit_price:,.2f} | "
                f"{pnl_pct:+.1f}% | ${pnl:+.2f}")
        if hold_duration and exit_reason:
            lines.append(f"{hold_duration} | {exit_reason}")
        elif exit_reason:
            lines.append(exit_reason)
    else:
        lines = [f"<b>{_esc(action)} {_esc(name)}</b> ({_esc(trading_strategy)})"]
        if reason:
            lines.append(_esc(reason[:150]))

    text = "\n".join(lines)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        log.warning(f"Failed to send trade alert: {e}")
