import math
import random
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser

from src.config import app_config
from src.database import (
    get_latest_news_sentiment, save_news_sentiment_batch,
    save_articles_batch, compute_title_hash,
    get_gemini_scores_for_hashes, update_gemini_scores_batch,
)
from src.logger import log

# --- Constants ---

# Keywords for symbol matching. Each keyword is matched with word boundaries
# (\b) to prevent false positives from substrings. Short/ambiguous tickers
# (GE, BA, SO, etc.) are excluded — only multi-word phrases are used for those.
SYMBOL_KEYWORDS = {
    # Crypto — tickers are distinctive enough for word-boundary matching
    'BTC': ['BTC', 'Bitcoin'],
    'ETH': ['ETH', 'Ethereum'],
    'SOL': ['Solana'],  # "SOL" too short — matches "solution", "solar"
    'XRP': ['XRP', 'Ripple'],
    'ADA': ['Cardano'],  # "ADA" matches Americans with Disabilities Act
    'AVAX': ['AVAX', 'Avalanche crypto'],
    'DOGE': ['DOGE', 'Dogecoin'],
    'MATIC': ['MATIC', 'Polygon crypto'],
    'BNB': ['BNB', 'Binance Coin'],
    'TRX': ['TRON crypto', 'TRX crypto'],  # "TRX" matches exercise abbreviation
    'LINK': ['Chainlink'],  # "LINK" too common
    'DOT': ['Polkadot'],  # "DOT" too common
    'UNI': ['Uniswap'],  # "UNI" too common
    'ATOM': ['Cosmos crypto'],  # "ATOM" too common
    'NEAR': ['NEAR Protocol'],  # "NEAR" too common
    'AAVE': ['AAVE', 'Aave'],
    'MKR': ['MakerDAO', 'Maker crypto'],
    'HBAR': ['HBAR', 'Hedera'],
    'ICP': ['Internet Computer'],
    'PEPE': ['PEPE coin', 'PEPE crypto'],
    'SHIB': ['SHIB', 'Shiba Inu'],
    'TON': ['Toncoin'],  # "TON" too common
    'SUI': ['SUI crypto', 'Sui blockchain'],
    'ARB': ['Arbitrum'],
    'OP': ['Optimism crypto'],  # "OP" too common
    # Stocks — use full company names; short tickers only if 4+ chars
    'AAPL': ['AAPL', 'Apple stock', 'Apple Inc'],
    'MSFT': ['MSFT', 'Microsoft stock', 'Microsoft Corp'],
    'GOOGL': ['GOOGL', 'Google stock', 'Alphabet stock'],
    'AMZN': ['AMZN', 'Amazon stock', 'Amazon.com'],
    'NVDA': ['NVDA', 'Nvidia stock', 'NVIDIA'],
    'META': ['Meta Platforms', 'Meta stock'],  # "META" alone too common
    'TSLA': ['TSLA', 'Tesla stock', 'Tesla Inc'],
    'AVGO': ['AVGO', 'Broadcom stock', 'Broadcom Inc', 'Broadcom'],
    'CRM': ['Salesforce stock', 'Salesforce Inc'],  # "CRM" too common
    'ORCL': ['ORCL', 'Oracle stock', 'Oracle Corp'],
    'AMD': ['AMD stock', 'Advanced Micro Devices'],  # "AMD" needs context
    'ADBE': ['ADBE', 'Adobe stock', 'Adobe Inc'],
    'INTC': ['INTC', 'Intel stock', 'Intel Corp'],
    # Financials
    'JPM': ['JPMorgan', 'JP Morgan'],
    'BAC': ['Bank of America'],  # "BAC" matches "back"
    'GS': ['Goldman Sachs'],  # "GS" too short
    'MS': ['Morgan Stanley'],  # "MS" too short
    'V': ['Visa stock', 'Visa Inc'],
    'MA': ['Mastercard stock', 'Mastercard Inc'],
    'BRK-B': ['BRK-B', 'Berkshire Hathaway', 'Warren Buffett'],
    'WFC': ['Wells Fargo'],
    # Healthcare
    'UNH': ['UnitedHealth', 'UnitedHealth Group'],
    'JNJ': ['Johnson & Johnson', 'Johnson and Johnson'],
    'LLY': ['Eli Lilly', 'Lilly stock'],  # bare LLY gated by SYMBOL_REQUIRED_CONTEXT
    'PFE': ['Pfizer stock', 'Pfizer Inc'],
    'ABBV': ['ABBV', 'AbbVie stock', 'AbbVie Inc'],
    'MRK': ['Merck stock', 'Merck & Co'],
    'TMO': ['Thermo Fisher', 'Thermo Fisher Scientific'],
    # Energy
    'XOM': ['Exxon', 'ExxonMobil'],
    'CVX': ['Chevron stock', 'Chevron Corp'],
    'COP': ['ConocoPhillips'],  # "COP" matches police
    'SLB': ['Schlumberger'],
    # Consumer Discretionary
    'HD': ['Home Depot'],  # "HD" too short
    'MCD': ['McDonald\'s stock', 'McDonalds'],
    'NKE': ['Nike stock', 'Nike Inc'],
    'SBUX': ['SBUX', 'Starbucks stock', 'Starbucks Corp'],
    # Consumer Staples
    'WMT': ['Walmart stock', 'Walmart Inc'],
    'COST': ['Costco stock', 'Costco Wholesale'],  # "COST" too common
    'KO': ['Coca-Cola stock', 'Coca Cola'],  # "KO" too short
    # Industrials
    'CAT': ['Caterpillar stock', 'Caterpillar Inc'],  # "CAT" too common
    'BA': ['Boeing stock', 'Boeing Co'],  # "BA" too short
    'GE': ['General Electric', 'GE Aerospace'],  # "GE" too short
    'HON': ['Honeywell stock', 'Honeywell International'],  # "HON" too short
    'RTX': ['RTX Corp', 'Raytheon'],
    # Communication
    'DIS': ['Disney', 'Walt Disney', 'Disney+'],  # bare DIS gated by SYMBOL_REQUIRED_CONTEXT
    'NFLX': ['NFLX', 'Netflix stock', 'Netflix Inc'],
    'CMCSA': ['CMCSA', 'Comcast stock', 'Comcast Corp'],
    # Utilities
    'NEE': ['NextEra Energy', 'NextEra'],
    'SO': ['Southern Company stock', 'Southern Co'],
    # REITs
    'AMT': ['American Tower Corp'],  # "AMT" too short
    # AI Pure-Play Stocks
    'AI': ['C3.ai stock', 'C3 AI'],  # "AI" alone too ambiguous
    'BBAI': ['BBAI', 'BigBear.ai', 'BigBear AI'],
    'UPST': ['UPST', 'Upstart stock', 'Upstart Holdings'],
    'PATH': ['UiPath stock', 'UiPath Inc'],  # "PATH" too common
    'SOUN': ['SOUN', 'SoundHound AI', 'SoundHound stock'],
    'SMCI': ['SMCI', 'Super Micro Computer', 'Supermicro'],
    'VRT': ['Vertiv stock', 'Vertiv Holdings'],  # "VRT" too short
    'DELL': ['DELL', 'Dell Technologies'],
    'ARM': ['Arm Holdings', 'ARM stock'],  # "ARM" needs context
    'ANET': ['ANET', 'Arista Networks'],
    'MDB': ['MongoDB stock', 'MongoDB Inc'],  # "MDB" too short
    'ESTC': ['ESTC', 'Elastic stock', 'Elastic NV'],
    'CFLT': ['CFLT', 'Confluent stock', 'Confluent Inc'],
    'IONQ': ['IONQ', 'IonQ stock', 'IonQ Inc'],
    'RGTI': ['RGTI', 'Rigetti Computing', 'Rigetti stock'],
    'PLTR': ['Palantir stock', 'Palantir Technologies'],
    'SNOW': ['Snowflake stock', 'Snowflake Inc'],
    'NET': ['Cloudflare stock', 'Cloudflare Inc'],  # "NET" too common
    'CRWD': ['CRWD', 'CrowdStrike stock', 'CrowdStrike Holdings'],
    'DDOG': ['DDOG', 'Datadog stock', 'Datadog Inc'],
    # --- European Stocks ---
    # UK FTSE
    'HSBA.L': ['HSBC Holdings', 'HSBC stock'],
    'BP.L': ['BP stock', 'BP plc'],
    'SHEL.L': ['Shell stock', 'Shell plc', 'Royal Dutch Shell'],
    'AZN.L': ['AstraZeneca stock', 'AstraZeneca plc'],
    'GSK.L': ['GSK stock', 'GlaxoSmithKline'],
    'ULVR.L': ['Unilever stock', 'Unilever plc'],
    'RIO.L': ['Rio Tinto stock', 'Rio Tinto plc'],
    'BARC.L': ['Barclays stock', 'Barclays plc'],
    'LLOY.L': ['Lloyds Banking', 'Lloyds stock'],
    'LSEG.L': ['LSEG', 'London Stock Exchange Group'],
    # Germany DAX
    'SAP.DE': ['SAP stock', 'SAP SE'],
    'SIE.DE': ['Siemens stock', 'Siemens AG'],
    'ALV.DE': ['Allianz stock', 'Allianz SE'],
    'MBG.DE': ['Mercedes-Benz stock', 'Mercedes Benz'],
    'BMW.DE': ['BMW stock', 'BMW AG'],
    'VOW3.DE': ['Volkswagen stock', 'Volkswagen AG'],
    'IFX.DE': ['Infineon stock', 'Infineon Technologies'],
    'BAS.DE': ['BASF stock', 'BASF SE'],
    # France CAC
    'MC.PA': ['LVMH stock', 'LVMH Moet Hennessy'],
    'TTE.PA': ['TotalEnergies stock', 'TotalEnergies SE'],
    'SAN.PA': ['Sanofi stock', 'Sanofi SA'],
    'OR.PA': ["L'Oreal stock", "L'Oreal SA"],
    'BNP.PA': ['BNP Paribas stock', 'BNP Paribas'],
    'AIR.PA': ['Airbus stock', 'Airbus SE'],
    # Netherlands
    'ASML.AS': ['ASML stock', 'ASML Holding'],
    # Switzerland
    'NESN.SW': ['Nestle stock', 'Nestle SA'],
    'ROG.SW': ['Roche stock', 'Roche Holding'],
    'NOVN.SW': ['Novartis stock', 'Novartis AG'],
    # Nordics
    'NOVO-B.CO': ['Novo Nordisk stock', 'Novo Nordisk'],
    'ERIC-B.ST': ['Ericsson stock', 'Ericsson AB'],
    'NOKIA.HE': ['Nokia stock', 'Nokia Corp'],
    # --- Asian Stocks ---
    # Japan
    '7203.T': ['Toyota stock', 'Toyota Motor'],
    '6758.T': ['Sony stock', 'Sony Group'],
    '9984.T': ['SoftBank stock', 'SoftBank Group'],
    '6861.T': ['Keyence stock', 'Keyence Corp'],
    '8306.T': ['Mitsubishi UFJ', 'MUFG stock'],
    '8035.T': ['Tokyo Electron stock', 'Tokyo Electron'],
    '7267.T': ['Honda stock', 'Honda Motor'],
    '6501.T': ['Hitachi stock', 'Hitachi Ltd'],
    # Hong Kong
    '0700.HK': ['Tencent stock', 'Tencent Holdings'],
    '9988.HK': ['Alibaba stock', 'Alibaba Group'],
    '3690.HK': ['Meituan stock', 'Meituan Dianping'],
    '1211.HK': ['BYD stock', 'BYD Company'],
    '9618.HK': ['JD.com stock', 'JD stock'],
    '1810.HK': ['Xiaomi stock', 'Xiaomi Corp'],
    '0005.HK': ['HSBC Hong Kong'],
    # South Korea
    '005930.KS': ['Samsung Electronics', 'Samsung stock'],
    '000660.KS': ['SK Hynix stock', 'SK Hynix'],
    '373220.KS': ['LG Energy stock', 'LG Energy Solution'],
    '005380.KS': ['Hyundai Motor stock', 'Hyundai Motor'],
    # Taiwan
    '2330.TW': ['TSMC stock', 'Taiwan Semiconductor'],
    '2317.TW': ['Foxconn stock', 'Hon Hai Precision'],
    '2454.TW': ['MediaTek stock', 'MediaTek Inc'],
    # India
    'RELIANCE.NS': ['Reliance Industries stock', 'Reliance Industries'],
    'TCS.NS': ['TCS stock', 'Tata Consultancy'],
    'INFY.NS': ['INFY', 'Infosys stock', 'Infosys Ltd'],
    'HDFCBANK.NS': ['HDFC Bank stock', 'HDFC Bank'],
    'WIPRO.NS': ['Wipro stock', 'Wipro Ltd'],
    # Australia
    'BHP.AX': ['BHP stock', 'BHP Group'],
    'CBA.AX': ['Commonwealth Bank stock', 'CommBank'],
    'CSL.AX': ['CSL stock', 'CSL Limited'],
    'FMG.AX': ['Fortescue stock', 'Fortescue Metals'],
    # Singapore
    'D05.SI': ['DBS Group stock', 'DBS Bank'],
}

# Boundary guards using lookaround: `\b...\b` breaks when a keyword ends in
# a non-word character (e.g. "Disney+", "Amazon.com"). These lookarounds
# test the character just outside the match, so any transition to a
# non-word character (or string boundary) counts as a boundary.
_LEFT_GUARD = r'(?:(?<=^)|(?<=[^A-Za-z0-9_]))'
_RIGHT_GUARD = r'(?:(?=$)|(?=[^A-Za-z0-9_]))'

# All-caps ticker shape: 2-6 alphanumerics, optional .XX or -X suffix.
_ALL_CAPS_TICKER_RE = re.compile(r'^[A-Z0-9]{2,6}(?:[.\-][A-Z0-9]{1,4})?$')


def _compile_keyword(kw: str) -> re.Pattern:
    """Compile a keyword pattern. All-caps tickers are case-sensitive; any
    keyword with a lowercase letter (company name) is case-insensitive. Uses
    lookaround guards so keywords ending in `+`, `.`, etc. match correctly."""
    flags = 0 if _ALL_CAPS_TICKER_RE.match(kw) else re.IGNORECASE
    return re.compile(_LEFT_GUARD + re.escape(kw) + _RIGHT_GUARD, flags)


_KEYWORD_PATTERNS = {
    sym: [_compile_keyword(kw) for kw in kws]
    for sym, kws in SYMBOL_KEYWORDS.items()
}

# Short / ambiguous tickers that are only allowed to match when the text
# ALSO contains a disambiguating company-name phrase. Anchors are
# case-sensitive (because they're all-caps tickers); context phrases are
# matched case-insensitively via _compile_keyword. Bare short tickers
# without this entry cannot match at all.
SYMBOL_REQUIRED_CONTEXT = {
    'DIS': {
        'anchor': _compile_keyword('DIS'),
        'context': [_compile_keyword('Disney'), _compile_keyword('Walt Disney')],
    },
    'BA': {
        'anchor': _compile_keyword('BA'),
        'context': [_compile_keyword('Boeing')],
    },
    'GE': {
        'anchor': _compile_keyword('GE'),
        'context': [_compile_keyword('General Electric'),
                    _compile_keyword('GE Aerospace')],
    },
    'HD': {
        'anchor': _compile_keyword('HD'),
        'context': [_compile_keyword('Home Depot')],
    },
    'KO': {
        'anchor': _compile_keyword('KO'),
        'context': [_compile_keyword('Coca-Cola'), _compile_keyword('Coca Cola')],
    },
    'MA': {
        'anchor': _compile_keyword('MA'),
        'context': [_compile_keyword('Mastercard')],
    },
    'V': {
        'anchor': _compile_keyword('V'),
        'context': [_compile_keyword('Visa Inc'), _compile_keyword('Visa stock')],
    },
    'SO': {
        'anchor': _compile_keyword('SO'),
        'context': [_compile_keyword('Southern Company'),
                    _compile_keyword('Southern Co')],
    },
    'GS': {
        'anchor': _compile_keyword('GS'),
        'context': [_compile_keyword('Goldman Sachs')],
    },
    'MS': {
        'anchor': _compile_keyword('MS'),
        'context': [_compile_keyword('Morgan Stanley')],
    },
    'LLY': {
        'anchor': _compile_keyword('LLY'),
        'context': [_compile_keyword('Eli Lilly')],
    },
}

# Macro keyword → sector group routing. When an article matches no specific
# symbol but contains macro keywords, it gets routed to all symbols in the
# matched sector group(s). Applied as a fallback only.
_MACRO_KEYWORD_SECTORS_RAW = {
    r'oil\b|crude|petroleum|Hormuz|OPEC|Brent\b|WTI\b': ['energy'],
    r'tariff|trade war|China trade|import dut|export ban': ['tech_mega', 'semiconductors', 'consumer_disc', 'industrials'],
    r'fertilizer|commodit': ['materials', 'commodities'],
    r'defense|military|Pentagon|NATO\b|weapons?\b|missile|drone\b|Power-to-X|synthetic fuel|MoD\b|Bundeswehr|defence procurement': ['defense'],
    r'interest rate|Fed |federal reserve|inflation\b|CPI\b': ['financials_banks', 'utilities_reits'],
    r'crypto|Bitcoin|digital asset|SEC crypto|stablecoin': ['l1_major', 'defi'],
    r'nuclear|uranium': ['nuclear_power'],
    r'shipping|tanker|freight': ['shipping_tankers'],
    r'semiconductor|chip ban|chip export': ['semiconductors'],
}
_MACRO_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), groups)
    for pattern, groups in _MACRO_KEYWORD_SECTORS_RAW.items()
]


def _match_article_to_macro_sectors(title: str, description: str) -> list[str]:
    """Match article to sector groups via macro keyword detection.

    Returns list of matched sector group names. Only used as fallback
    when _match_article_to_symbols returns empty.
    """
    text = f"{title} {description}"
    matched = set()
    for pattern, groups in _MACRO_PATTERNS:
        if pattern.search(text):
            matched.update(groups)
    return list(matched)


def _expand_sectors_to_symbols(sector_groups: list[str],
                               active_symbols: list[str],
                               max_symbols: int = 20) -> list[str]:
    """Expand sector group names to individual symbols from the watch list."""
    from src.analysis.sector_limits import _ensure_loaded, _sector_config
    _ensure_loaded()
    if not _sector_config:
        return []
    groups = _sector_config.get('groups', {})
    active_set = set(s.upper() for s in active_symbols)
    expanded = []
    for group_name in sector_groups:
        for sym in groups.get(group_name, {}).get('symbols', []):
            sym_str = str(sym).upper()
            if sym_str in active_set and sym_str not in expanded:
                expanded.append(sym_str)
                if len(expanded) >= max_symbols:
                    return expanded
    return expanded


RSS_FEEDS = [
    {'url': 'https://feeds.reuters.com/reuters/businessNews', 'category': 'financial'},
    {'url': 'https://feeds.bloomberg.com/markets/news.rss', 'category': 'financial'},
    {'url': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147', 'category': 'financial'},
    {'url': 'https://www.ft.com/rss/home', 'category': 'financial'},
    {'url': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', 'category': 'financial'},
    {'url': 'https://www.faz.net/rss/aktuell/finanzen/', 'category': 'european'},
    {'url': 'https://feeds.bbci.co.uk/news/business/rss.xml', 'category': 'european'},
    {'url': 'https://www.cityam.com/feed/', 'category': 'european'},
    {'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'category': 'crypto'},
    {'url': 'https://cointelegraph.com/rss', 'category': 'crypto'},
    {'url': 'https://www.theblock.co/rss.xml', 'category': 'crypto'},
    {'url': 'https://feeds.arstechnica.com/arstechnica/technology-lab', 'category': 'tech'},
    {'url': 'https://www.wired.com/feed/category/business/latest/rss', 'category': 'tech'},
    {'url': 'https://feeds.marketwatch.com/marketwatch/topstories/', 'category': 'financial'},
    # Press release wire feeds (origin point of corporate news)
    {'url': 'https://www.globenewswire.com/RssFeed/subjectcode/01-MNA/feedTitle/GlobeNewsWire%20-%20Mergers%20and%20Acquisitions', 'category': 'press_release'},
    {'url': 'https://www.globenewswire.com/RssFeed/subjectcode/25-PER/feedTitle/GlobeNewsWire%20-%20Public%20Companies', 'category': 'press_release'},
    {'url': 'https://www.prnewswire.com/rss/news-releases-list.rss', 'category': 'press_release'},
    {'url': 'https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss', 'category': 'press_release'},
    # Google News sector-grouped feeds (one per sector, when:1d filter)
    {'url': 'https://news.google.com/rss/search?q=AAPL+MSFT+GOOGL+AMZN+NVDA+META+TSLA+AVGO+CRM+ORCL+AMD+ADBE+INTC+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=JPMorgan+%22Goldman+Sachs%22+%22Bank+of+America%22+%22Morgan+Stanley%22+Visa+Mastercard+%22Berkshire+Hathaway%22+%22Wells+Fargo%22+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=UnitedHealth+%22Eli+Lilly%22+Pfizer+AbbVie+Merck+%22Thermo+Fisher%22+%22Johnson+%26+Johnson%22+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=ExxonMobil+Chevron+ConocoPhillips+Schlumberger+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=%22Home+Depot%22+McDonald%27s+Nike+Starbucks+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Walmart+Costco+Coca-Cola+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Caterpillar+Boeing+%22General+Electric%22+Honeywell+Raytheon+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Disney+Netflix+Comcast+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=%22NextEra+Energy%22+%22Southern+Company%22+%22American+Tower%22+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Bitcoin+Ethereum+Solana+XRP+Cardano+Avalanche+Dogecoin+Polygon+BNB+Tron+crypto+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'google_news'},
    # Layer A — Regulatory origin-point feeds
    {'url': 'https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml', 'category': 'regulatory'},
    {'url': 'https://www.federalreserve.gov/feeds/press_all.xml', 'category': 'regulatory'},
    {'url': 'https://www.federalreserve.gov/feeds/press_monetary.xml', 'category': 'regulatory'},
    {'url': 'https://www.federalreserve.gov/feeds/s_t_powell.xml', 'category': 'regulatory'},
    {'url': 'https://www.ecb.europa.eu/rss/press.html', 'category': 'regulatory'},
    {'url': 'https://www.sec.gov/news/pressreleases.rss', 'category': 'regulatory'},
    {'url': 'https://www.eia.gov/rss/todayinenergy.xml', 'category': 'regulatory'},
    # Layer B — Sector depth feeds
    {'url': 'https://www.statnews.com/feed/', 'category': 'sector'},
    {'url': 'https://endpts.com/feed/', 'category': 'sector'},
    {'url': 'https://www.rigzone.com/news/rss/rigzone_latest.aspx', 'category': 'sector'},
    {'url': 'https://www.eetimes.com/feed/', 'category': 'sector'},
    {'url': 'https://deadline.com/feed/', 'category': 'sector'},
    # Defense / aerospace coverage — closes the European-defense blind spot.
    # Verified working Apr 2026; carries Rheinmetall, ITM, Anduril, Destinus stories
    # that none of our existing financial feeds covered.
    {'url': 'https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml', 'category': 'sector'},
    {'url': 'https://www.defensenews.com/arc/outboundfeeds/rss/category/industry/?outputType=xml', 'category': 'sector'},
    {'url': 'https://breakingdefense.com/feed/', 'category': 'sector'},
    {'url': 'https://defence-industry.eu/feed/', 'category': 'sector'},
    {'url': 'https://www.navalnews.com/feed/', 'category': 'sector'},
    {'url': 'https://www.gov.uk/government/organisations/ministry-of-defence.atom', 'category': 'regulatory'},
    # Pharma / biotech — closes the healthcare blind spot (MRK open position
    # had only 2 articles/30d; Pfizer/AbbVie/AstraZeneca zero). Verified Apr 2026.
    {'url': 'https://www.fiercebiotech.com/rss/xml', 'category': 'sector'},
    {'url': 'https://www.fiercepharma.com/rss/xml', 'category': 'sector'},
    {'url': 'https://www.biopharmadive.com/feeds/news/', 'category': 'sector'},
    # Nuclear / utilities — VST, CEG, OKLO, SMR are active conservative positions
    # but got near-zero direct coverage. Verified Apr 2026.
    {'url': 'https://www.utilitydive.com/feeds/news/', 'category': 'sector'},
    {'url': 'https://www.powermag.com/feed/', 'category': 'sector'},
    {'url': 'https://www.world-nuclear-news.org/rss', 'category': 'sector'},
    # European / offshore energy — BP.L, NESTE.HE, EQNR.OL had <5 articles/30d.
    # Verified Apr 2026.
    {'url': 'https://www.offshore-technology.com/feed/', 'category': 'sector'},
    # Layer C — KOL / Key person feeds
    {'url': 'https://decrypt.co/feed', 'category': 'kol'},
    {'url': 'https://blockworks.co/feed', 'category': 'kol'},
    {'url': 'https://bitcoinmagazine.com/feed', 'category': 'kol'},
    {'url': 'https://www.trumpstruth.org/feed', 'category': 'kol'},
    {'url': 'https://vitalik.eth.limo/feed.xml', 'category': 'kol'},
    # Layer D — Macro policy feeds (government/regulatory not covered above)
    {'url': 'https://www.cftc.gov/PressRoom/PressReleases/rss.xml', 'category': 'regulatory'},
    {'url': 'https://news.google.com/rss/search?q=White+House+executive+order+OR+tariff+OR+sanctions+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'regulatory'},
    {'url': 'https://news.google.com/rss/search?q=Treasury+Department+sanctions+OR+debt+OR+%22interest+rate%22+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'regulatory'},
    # Layer E — Asia-Pacific market feeds (English-language)
    {'url': 'https://asia.nikkei.com/rss/feed/nar', 'category': 'asia'},
    {'url': 'https://www.scmp.com/rss/5/feed', 'category': 'asia'},
    {'url': 'https://www.straitstimes.com/news/business/rss.xml', 'category': 'asia'},
    {'url': 'https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6511', 'category': 'asia'},
    {'url': 'https://www.abc.net.au/news/feed/51120/rss.xml', 'category': 'asia'},
    {'url': 'https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms', 'category': 'asia'},
    # Layer F — Asia-Pacific central bank feeds
    {'url': 'https://www.boj.or.jp/en/rss/whatsnew.xml', 'category': 'regulatory'},
    # Layer D — IPO / New listings feeds
    {'url': 'https://news.google.com/rss/search?q=IPO+%22initial+public+offering%22+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'ipo'},
    {'url': 'https://news.google.com/rss/search?q=Binance+OR+Coinbase+%22new+listing%22+crypto+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'ipo'},
    {'url': 'https://news.google.com/rss/search?q=%22S-1+filing%22+OR+%22SEC+filing%22+IPO+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'ipo'},
    {'url': 'https://news.google.com/rss/search?q=%22AI+IPO%22+OR+%22artificial+intelligence%22+%22goes+public%22+OR+%22public+offering%22+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'ipo'},
    # Layer G — AI industry & startup feeds
    {'url': 'https://techcrunch.com/category/artificial-intelligence/feed/', 'category': 'ai'},
    {'url': 'https://venturebeat.com/category/ai/feed/', 'category': 'ai'},
    {'url': 'https://www.theverge.com/rss/ai-artificial-intelligence/index.xml', 'category': 'ai'},
    {'url': 'https://the-decoder.com/feed/', 'category': 'ai'},
    {'url': 'https://www.marktechpost.com/feed/', 'category': 'ai'},
    {'url': 'https://syncedreview.com/feed/', 'category': 'ai'},
    {'url': 'https://news.google.com/rss/search?q=%22artificial+intelligence%22+OR+%22AI+startup%22+OR+%22machine+learning%22+funding+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'ai'},
    {'url': 'https://news.google.com/rss/search?q=%22generative+AI%22+OR+%22large+language+model%22+OR+%22AI+chip%22+stock+when:1d&hl=en-US&gl=US&ceid=US:en', 'category': 'ai'},
    # Layer H — AI research & corporate blogs
    {'url': 'https://blog.research.google/feeds/posts/default?alt=rss', 'category': 'ai_research'},
    {'url': 'https://openai.com/blog/rss.xml', 'category': 'ai_research'},
    {'url': 'https://blogs.nvidia.com/feed/', 'category': 'ai_research'},
    # ── EU Market Feeds ──
    # UK / LSE
    {'url': 'https://www.investegate.co.uk/Rss.aspx?type=0', 'category': 'european'},
    # EU general
    {'url': 'https://euobserver.com/rss', 'category': 'european'},
    {'url': 'https://www.swissinfo.ch/eng/business/rss', 'category': 'european'},
    # EU regulatory
    {'url': 'https://www.bankofengland.co.uk/rss/news', 'category': 'regulatory'},
    {'url': 'https://www.bankofengland.co.uk/rss/speeches', 'category': 'regulatory'},
    # Google News — EU sectors
    {'url': 'https://news.google.com/rss/search?q=HSBC+%22BP+stock%22+Shell+AstraZeneca+Unilever+%22Rio+Tinto%22+Barclays+FTSE+when:1d&hl=en-GB&gl=GB&ceid=GB:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=SAP+Siemens+Allianz+Mercedes+BMW+Volkswagen+%22Deutsche+Telekom%22+DAX+when:1d&hl=en&gl=DE&ceid=DE:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=LVMH+TotalEnergies+Sanofi+%22L%27Oreal%22+%22BNP+Paribas%22+Airbus+CAC+when:1d&hl=en&gl=FR&ceid=FR:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=ASML+Novo+Nordisk+Ericsson+Nokia+Spotify+%22Nordic+stock%22+when:1d&hl=en&gl=NL&ceid=NL:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=BMW+Mercedes+Volkswagen+Stellantis+Renault+%22auto+stock%22+%22European+auto%22+when:1d&hl=en&gl=DE&ceid=DE:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=AstraZeneca+Novartis+Roche+%22Novo+Nordisk%22+Sanofi+GSK+%22European+pharma%22+when:1d&hl=en-GB&gl=GB&ceid=GB:en', 'category': 'google_news'},
    # ── Asia-Pacific Market Feeds ──
    # Korea
    {'url': 'http://www.koreaherald.com/rss/020200030000.xml', 'category': 'asia'},
    {'url': 'https://www.kedglobal.com/rss/', 'category': 'asia'},
    # India
    {'url': 'https://www.livemint.com/rss/markets', 'category': 'asia'},
    {'url': 'https://www.moneycontrol.com/rss/marketreports.xml', 'category': 'asia'},
    # Broader APAC
    {'url': 'https://stockhead.com.au/feed/', 'category': 'asia'},
    # Asia regulatory
    {'url': 'https://rbi.org.in/scripts/RSS_Feeds.aspx', 'category': 'regulatory'},
    # Google News — Asia sectors
    {'url': 'https://news.google.com/rss/search?q=Toyota+Sony+SoftBank+Keyence+Hitachi+%22Tokyo+Electron%22+Honda+Nikkei+when:1d&hl=en&gl=JP&ceid=JP:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Samsung+%22SK+Hynix%22+%22LG+Energy%22+Hyundai+KOSPI+when:1d&hl=en&gl=KR&ceid=KR:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Tencent+Alibaba+Meituan+BYD+%22JD.com%22+Xiaomi+%22Hang+Seng%22+when:1d&hl=en&gl=HK&ceid=HK:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=TSMC+Foxconn+MediaTek+%22Taiwan+stock%22+TAIEX+when:1d&hl=en&gl=TW&ceid=TW:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Reliance+TCS+Infosys+%22HDFC+Bank%22+Wipro+Sensex+Nifty+when:1d&hl=en&gl=IN&ceid=IN:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=BHP+Commonwealth+Bank+CSL+Fortescue+ASX+when:1d&hl=en&gl=AU&ceid=AU:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=%22China+stock%22+%22Shanghai+index%22+%22Hang+Seng%22+%22Chinese+market%22+when:1d&hl=en&gl=HK&ceid=HK:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=DBS+%22Singapore+Exchange%22+%22Straits+Times+Index%22+when:1d&hl=en&gl=SG&ceid=SG:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=TSMC+Samsung+%22SK+Hynix%22+%22Tokyo+Electron%22+%22semiconductor+stock%22+Asia+when:1d&hl=en&gl=US&ceid=US:en', 'category': 'google_news'},
    {'url': 'https://news.google.com/rss/search?q=Toyota+Hyundai+BYD+%22Asian+auto%22+%22EV+stock%22+Asia+when:1d&hl=en&gl=US&ceid=US:en', 'category': 'google_news'},
    # ── Global / Cross-Region Feeds ──
    {'url': 'https://www.investing.com/rss/news_301.rss', 'category': 'financial'},
    {'url': 'https://www.investing.com/rss/news_285.rss', 'category': 'financial'},
    {'url': 'https://seekingalpha.com/market_currents.xml', 'category': 'financial'},
]


RSS_FETCH_TIMEOUT = 15
_RSS_BATCH_SIZE = 10       # feeds per batch
_RSS_BATCH_DELAY = 0.5     # seconds between batches
_CONSECUTIVE_ERROR_LIMIT = 5  # auto-disable after this many failures
_ERROR_COOLDOWN_CYCLES = 4    # re-enable after N cycles (~1 hour at 15-min intervals)

# Browser-like User-Agents for RSS (matching web_news_scraper.py)
_RSS_USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
]

# Domains known to serve spam/promotional content via Google News
_SPAM_DOMAINS = frozenset({
    'openpr.com', 'mexc.com', 'bitget.com', 'swikblog.com',
    'blockonomi.com', 'bez-kabli.pl',
})

# In-memory error tracking (reset on restart, good enough for cycle-to-cycle)
_feed_errors: dict[str, int] = {}       # url -> consecutive error count
_feed_disabled_at: dict[str, int] = {}  # url -> cycle number when disabled
_cycle_counter = 0


# --- Internal Functions ---


def _get_rss_headers():
    """Return request headers with a randomly selected browser User-Agent."""
    return {
        'User-Agent': random.choice(_RSS_USER_AGENTS),
        'Accept': 'application/rss+xml, application/xml, text/xml, */*;q=0.1',
        'Accept-Language': 'en-US,en;q=0.9',
    }


def _is_spam_article(article: dict) -> bool:
    """Check if an article comes from a known spam domain."""
    url = article.get('source_url', '')
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).hostname or ''
        domain = domain.lstrip('www.')
        return domain in _SPAM_DOMAINS
    except Exception:
        return False


def _fetch_single_rss_feed(feed_info):
    """Fetches and parses a single RSS feed."""
    url = feed_info['url']

    # Skip feeds that are temporarily disabled due to consecutive errors
    error_count = _feed_errors.get(url, 0)
    if error_count >= _CONSECUTIVE_ERROR_LIMIT:
        disabled_cycle = _feed_disabled_at.get(url, 0)
        if _cycle_counter - disabled_cycle < _ERROR_COOLDOWN_CYCLES:
            return []  # Still in cooldown
        # Cooldown expired — re-enable and try again
        _feed_errors[url] = 0
        log.info(f"Re-enabling RSS feed after cooldown: {url}")

    try:
        parsed = feedparser.parse(url, request_headers=_get_rss_headers())

        # Check for HTTP errors in feedparser
        status = getattr(parsed, 'status', None)
        if isinstance(status, int) and status >= 400:
            _record_feed_error(url, f"HTTP {status}")
            return []

        articles = []
        for entry in parsed.entries:
            article = {
                'title': getattr(entry, 'title', ''),
                'description': getattr(entry, 'summary', ''),
                'published_at': getattr(entry, 'published', ''),
                'source': getattr(parsed.feed, 'title', url),
                'source_url': getattr(entry, 'link', ''),
                'category': feed_info.get('category', 'unknown'),
            }
            if not _is_spam_article(article):
                articles.append(article)

        # Reset error count on success (even if 0 articles — feed is alive)
        if url in _feed_errors:
            if _feed_errors[url] > 0:
                log.info(f"RSS feed recovered after {_feed_errors[url]} errors: {url}")
            _feed_errors[url] = 0

        return articles
    except Exception as e:
        _record_feed_error(url, str(e))
        return []


def _record_feed_error(url: str, reason: str):
    """Track consecutive errors and auto-disable feeds that keep failing."""
    _feed_errors[url] = _feed_errors.get(url, 0) + 1
    count = _feed_errors[url]
    if count >= _CONSECUTIVE_ERROR_LIMIT:
        _feed_disabled_at[url] = _cycle_counter
        log.warning(f"RSS feed auto-disabled after {count} consecutive errors "
                    f"(cooldown {_ERROR_COOLDOWN_CYCLES} cycles): {url} — {reason}")
    elif count >= 3:
        log.warning(f"RSS feed failing ({count}x): {url} — {reason}")
    else:
        log.debug(f"RSS feed error ({count}x): {url} — {reason}")


def _fetch_rss_feeds():
    """Fetches all RSS feeds in staggered batches with error tracking.

    Tries to load feeds from source_registry DB; falls back to hardcoded RSS_FEEDS.
    """
    global _cycle_counter
    _cycle_counter += 1

    try:
        from src.collectors.source_registry import (
            load_rss_feeds_from_registry, update_source_stats,
        )
        registry_feeds = load_rss_feeds_from_registry()
    except Exception as e:
        log.debug(f"Source registry not available, using hardcoded feeds: {e}")
        registry_feeds = None

    feeds = registry_feeds if registry_feeds else RSS_FEEDS
    use_registry = registry_feeds is not None

    # Shuffle feeds to avoid always hitting the same servers first
    feeds_shuffled = list(feeds)
    random.shuffle(feeds_shuffled)

    all_articles = []
    n_skipped = 0
    n_failed = 0

    # Process in staggered batches to avoid rate limiting
    for batch_start in range(0, len(feeds_shuffled), _RSS_BATCH_SIZE):
        batch = feeds_shuffled[batch_start:batch_start + _RSS_BATCH_SIZE]

        if batch_start > 0:
            time.sleep(_RSS_BATCH_DELAY)

        with ThreadPoolExecutor(max_workers=min(len(batch), 6)) as executor:
            futures = {executor.submit(_fetch_single_rss_feed, feed): feed
                       for feed in batch}
            try:
                done_iter = as_completed(futures, timeout=RSS_FETCH_TIMEOUT + 5)
                for future in done_iter:
                    feed = futures[future]
                    try:
                        articles = future.result(timeout=RSS_FETCH_TIMEOUT)
                        if not articles and _feed_errors.get(feed['url'], 0) >= _CONSECUTIVE_ERROR_LIMIT:
                            n_skipped += 1
                        all_articles.extend(articles)
                        # Update source stats if using registry
                        if use_registry and articles:
                            source_id = feed.get('source_id')
                            if source_id:
                                try:
                                    update_source_stats(source_id,
                                                        articles_fetched=len(articles))
                                except Exception:
                                    pass
                    except Exception as e:
                        n_failed += 1
                        _record_feed_error(feed['url'], str(e))
                        if use_registry:
                            source_id = feed.get('source_id')
                            if source_id:
                                try:
                                    update_source_stats(source_id, errors=1)
                                except Exception:
                                    pass
            except TimeoutError:
                n_unfinished = sum(1 for fut in futures if not fut.done())
                log.warning(f"RSS batch timeout — {n_unfinished} feeds did not finish.")

    status_parts = [f"Fetched {len(all_articles)} articles from {len(feeds)} RSS feeds"]
    if use_registry:
        status_parts.append("(registry)")
    if n_skipped:
        status_parts.append(f"({n_skipped} disabled)")
    if n_failed:
        status_parts.append(f"({n_failed} failed)")
    log.info(" ".join(status_parts) + ".")
    return all_articles


def _is_likely_english(text):
    """Fast heuristic: reject text with >15% non-ASCII chars (non-English)."""
    if not text:
        return False
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return non_ascii / len(text) < 0.15


def _normalize_title_for_dedup(title: str) -> str:
    """Normalize a title for fuzzy dedup.

    Strips common prefixes/suffixes, removes source tags, collapses whitespace,
    and extracts the first N significant words to catch wire story reprints.
    """
    import re
    t = title.lower().strip()
    # Remove common source prefixes: "Reuters: ...", "AP: ...", "CNBC: ..."
    t = re.sub(r'^(?:reuters|ap|bloomberg|cnbc|cnn|bbc)\s*[:—–-]\s*', '', t)
    # Remove trailing source tags: "... - Reuters", "... | CNBC"
    t = re.sub(r'\s*[-|–—]\s*(?:reuters|ap|bloomberg|cnbc|yahoo finance|marketwatch|google news)\s*$', '', t)
    # Collapse whitespace and punctuation
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # Take first 8 significant words (catches same story with different endings)
    words = t.split()[:8]
    return ' '.join(words)


def _deduplicate_articles(articles):
    """Removes near-duplicate and non-English headlines.

    Uses normalized title prefixes to catch wire story reprints
    (e.g., Reuters original + CNBC reprint + Google News summary).
    """
    seen_exact = set()
    seen_normalized = set()
    unique = []
    for article in articles:
        title = article.get('title', '').strip()
        if not title:
            continue
        # Exact dedup (fast path)
        key_exact = title.lower()
        if key_exact in seen_exact:
            continue
        if not _is_likely_english(title):
            continue
        # Fuzzy dedup (catches reprints with slightly different titles)
        key_norm = _normalize_title_for_dedup(title)
        if key_norm in seen_normalized:
            continue
        seen_exact.add(key_exact)
        seen_normalized.add(key_norm)
        unique.append(article)
    deduped = len(articles) - len(unique)
    if deduped > 0:
        log.info(f"Deduplicated {len(articles)} articles to {len(unique)} "
                 f"({deduped} duplicates removed, {len(seen_normalized)} unique normalized titles).")
    return unique


def _match_article_to_symbols(title, description, symbols):
    """Matches an article to symbols using word-boundary regex matching.

    Two paths:
    - Regular keyword match via SYMBOL_KEYWORDS (word-bounded, case-sensitive
      for all-caps tickers, case-insensitive for company names).
    - Short/ambiguous ticker co-occurrence via SYMBOL_REQUIRED_CONTEXT: the
      bare ticker is allowed to match only if a disambiguating company name
      also appears in the same text block.

    Prioritises the primary subject: if a symbol matches in the title, it
    ranks higher than one matching only in the description. When multiple
    symbols match in the title, the one appearing earliest wins. Uses the
    globally-earliest match position across ALL patterns for a symbol (not
    the first pattern that matched).
    """
    title_matches = []   # (position_in_title, symbol)
    desc_only = []
    matched_symbols = set()

    for symbol in symbols:
        # Path A: required-context (short-ticker co-occurrence)
        ctx = SYMBOL_REQUIRED_CONTEXT.get(symbol)
        if ctx is not None:
            anchor_m = ctx['anchor'].search(title)
            if anchor_m and any(p.search(title) for p in ctx['context']):
                title_matches.append((anchor_m.start(), symbol))
                matched_symbols.add(symbol)
                continue
            anchor_m_d = ctx['anchor'].search(description)
            if anchor_m_d and any(p.search(description) for p in ctx['context']):
                if symbol not in matched_symbols:
                    desc_only.append(symbol)
                    matched_symbols.add(symbol)
            # Short tickers with a required-context entry rely entirely on
            # this path — skip the keyword path even if SYMBOL_KEYWORDS has
            # company-name entries for them (those would double-match).

        # Path B: regular keyword patterns (find the globally-earliest
        # title position across all patterns for this symbol).
        patterns = _KEYWORD_PATTERNS.get(symbol)
        if not patterns:
            continue
        earliest_title_pos = None
        for pattern in patterns:
            m = pattern.search(title)
            if m and (earliest_title_pos is None or m.start() < earliest_title_pos):
                earliest_title_pos = m.start()
        if earliest_title_pos is not None:
            if symbol not in matched_symbols:
                title_matches.append((earliest_title_pos, symbol))
                matched_symbols.add(symbol)
            continue
        for pattern in patterns:
            if pattern.search(description):
                if symbol not in matched_symbols:
                    desc_only.append(symbol)
                    matched_symbols.add(symbol)
                break

    title_matches.sort(key=lambda x: x[0])
    return [sym for _, sym in title_matches] + desc_only



def _score_with_gemini(articles_with_hashes: list) -> dict:
    """Scores articles with Gemini, using DB cache for previously scored articles.

    Args:
        articles_with_hashes: list of dicts with 'title', 'description', 'title_hash'.
            Optional fields: 'collected_at' (for age context), 'source' (for attribution).

    Returns:
        {title_hash: score} for all articles that could be scored (cached + new).
    """
    if not articles_with_hashes:
        return {}

    all_hashes = [a['title_hash'] for a in articles_with_hashes]

    # Check DB cache for existing scores
    cached_scores = get_gemini_scores_for_hashes(all_hashes)
    log.info(f"Gemini article score cache: {len(cached_scores)}/{len(all_hashes)} hits.")

    # Filter to unscored articles
    unscored = [a for a in articles_with_hashes if a['title_hash'] not in cached_scores]

    if not unscored:
        return cached_scores

    # Score new articles with Gemini
    try:
        from src.analysis.gemini_news_analyzer import score_articles_batch
        new_scores = score_articles_batch(unscored)
    except Exception as e:
        log.warning(f"Gemini article scoring failed: {e}")
        new_scores = {}

    # Persist new scores to DB
    if new_scores:
        try:
            update_gemini_scores_batch(new_scores)
        except Exception as e:
            log.warning(f"Failed to persist Gemini article scores: {e}")

    # Merge cached + new
    merged = {**cached_scores, **new_scores}
    return merged


# Article age thresholds and weights. Defaults chosen so a month-old
# article without a timestamp still contributes ~zero to the aggregate.
# _UNKNOWN_TIMESTAMP_WEIGHT was previously 0.3 — tightened so timestamp-less
# articles can no longer meaningfully inflate sentiment.
_UNKNOWN_TIMESTAMP_WEIGHT = 0.1
# _COLLECTED_AT_FALLBACK_WEIGHT is applied when we fall back to scrape time
# (which is an upper bound on article age, not ground truth).
_COLLECTED_AT_FALLBACK_WEIGHT = 0.5


def _article_age_hours(published_at_str):
    """Parse an article timestamp string and return its age in hours, or None
    if the input is empty / unparseable / future-dated. Shared by both the
    freshness-weight decay and the hard-cutoff filter so we don't maintain
    two parsers with subtly different rules.
    """
    if not published_at_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        pub_dt = parsedate_to_datetime(str(published_at_str))
        if pub_dt is None:
            return None
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600.0
        if age_hours < 0:
            # Future timestamp — likely timezone error. Treat as unknown
            # so the caller uses the fallback weight rather than 1.0.
            return None
        return age_hours
    except Exception:
        log.debug(f"Unparseable article timestamp: {published_at_str!r}")
        return None


def _compute_freshness_weight(article_or_str, half_life_hours=6):
    """Compute exponential decay weight based on article age, with a
    fallback chain so timestamp-less articles still get a reasonable weight.

    Accepts either a legacy plain string (backward-compatible) or the full
    article dict.

    Resolution order:
      1. published / published_at
      2. updated (many RSS entries populate this when published is empty)
      3. collected_at × _COLLECTED_AT_FALLBACK_WEIGHT penalty
      4. _UNKNOWN_TIMESTAMP_WEIGHT (default 0.1)

    Returns a weight in [0, 1].
    """
    if half_life_hours <= 0:
        return _UNKNOWN_TIMESTAMP_WEIGHT

    # Back-compat path: caller passed a bare timestamp string
    if article_or_str is None or isinstance(article_or_str, str):
        age = _article_age_hours(article_or_str)
        if age is None:
            return _UNKNOWN_TIMESTAMP_WEIGHT
        return math.exp(-age * math.log(2) / half_life_hours)

    # Rich path: caller passed the article dict — walk the fallback chain
    a = article_or_str
    for field in ("published", "published_at", "updated"):
        age = _article_age_hours(a.get(field))
        if age is not None:
            return math.exp(-age * math.log(2) / half_life_hours)

    # Fall back to collected_at with a penalty — scrape time is an upper
    # bound on article age, not ground truth.
    age = _article_age_hours(a.get("collected_at"))
    if age is not None:
        weight = math.exp(-age * math.log(2) / half_life_hours)
        return weight * _COLLECTED_AT_FALLBACK_WEIGHT

    return _UNKNOWN_TIMESTAMP_WEIGHT


def collect_news_sentiment(symbols):
    """
    Main entry point: fetches news from NewsAPI + RSS, scores with VADER,
    groups by symbol, saves to DB, and detects trigger conditions.

    Returns:
        dict: {
            'per_symbol': { symbol: { avg_sentiment_score, news_volume, ... } },
            'triggered_symbols': [symbol, ...]
        }
    """
    news_config = app_config.get('settings', {}).get('news_analysis', {})
    if not news_config.get('enabled', False):
        log.info("News analysis is disabled. Skipping news collection.")
        return {'per_symbol': {}, 'triggered_symbols': []}

    volume_spike_multiplier = news_config.get('volume_spike_multiplier', 3.0)
    sentiment_shift_threshold = news_config.get('sentiment_shift_threshold', 0.3)

    # 1. Fetch from all sources (RSS + web scraping)
    rss_articles = _fetch_rss_feeds()

    # Web scraping for richer content beyond RSS
    web_articles = []
    web_scraping_enabled = news_config.get('web_scraping', {}).get('enabled', False)
    if web_scraping_enabled:
        try:
            from src.collectors.web_news_scraper import scrape_all_sources
            web_articles = scrape_all_sources()
        except Exception as e:
            log.warning(f"Web scraping failed, continuing with RSS: {e}")

    # 2. Combine and deduplicate
    all_articles = _deduplicate_articles(rss_articles + web_articles)

    # 2a. Freshness hard cutoff — drop articles whose parsed age exceeds
    # max_article_age_hours. Exponential decay alone would weight these
    # ~1e-6, but filtering here saves Gemini tokens and protects against
    # feed backfills suddenly emitting months of old posts. Articles with
    # unparseable/missing timestamps are kept (handled by weight fallback).
    max_age_h = news_config.get('max_article_age_hours', 72)
    if max_age_h and max_age_h > 0:
        original_count = len(all_articles)
        kept = []
        dropped_stale = 0
        for a in all_articles:
            age = _article_age_hours(
                a.get('published') or a.get('published_at') or a.get('updated'))
            if age is not None and age > max_age_h:
                dropped_stale += 1
                continue
            kept.append(a)
        if dropped_stale:
            log.info(f"Freshness filter: dropped {dropped_stale}/"
                     f"{original_count} articles older than {max_age_h}h")
        all_articles = kept

    if not all_articles:
        log.info("No news articles found.")
        return {'per_symbol': {}, 'triggered_symbols': []}

    # 2d. IPO event detection
    ipo_tracking_config = app_config.get('settings', {}).get('ipo_tracking', {})
    if ipo_tracking_config.get('enabled', False):
        try:
            from src.collectors.ipo_detector import detect_ipo_events
            from src.database import save_ipo_event
            ipo_events = detect_ipo_events(all_articles)
            for event in ipo_events:
                save_ipo_event(**event)
        except Exception as e:
            log.warning(f"IPO detection failed, continuing: {e}")

    # 2b. Deep scraping: enrich important articles with full body text
    if news_config.get('deep_scraping', {}).get('enabled', False):
        try:
            from src.collectors.article_enricher import enrich_articles_batch
            all_articles = enrich_articles_batch(all_articles)
        except Exception as e:
            log.warning(f"Deep scraping failed, continuing with original articles: {e}")

    # 2c. Gemini per-article scoring (DB-cached, batched)
    # Score every article whose title matches EITHER a specific symbol OR a
    # macro sector keyword — macro-routed articles are archived either way,
    # so skipping them here leaves them in the DB with gemini_score=NULL.
    use_gemini_scoring = news_config.get('use_gemini_scoring', True)
    macro_routing_enabled = news_config.get('macro_routing', {}).get('enabled', True)
    macro_max = news_config.get('macro_routing', {}).get('max_symbols_per_article', 20)
    gemini_article_scores = {}
    if use_gemini_scoring:
        articles_for_scoring = []
        for article in all_articles:
            title = article.get('title', '')
            if not title:
                continue
            description = article.get('description', '')
            matched = _match_article_to_symbols(title, description, symbols)
            if not matched and macro_routing_enabled:
                macro_sectors = _match_article_to_macro_sectors(title, description)
                if macro_sectors and _expand_sectors_to_symbols(
                        macro_sectors, symbols, max_symbols=macro_max):
                    matched = True  # will be archived via macro routing
            if matched:
                articles_for_scoring.append({
                    'title': title,
                    'description': description,
                    'title_hash': compute_title_hash(title),
                    'collected_at': article.get('published') or article.get('collected_at'),
                    'source': article.get('source', ''),
                })
        gemini_article_scores = _score_with_gemini(articles_for_scoring)

    # 3. Score each headline with Gemini, match to symbols
    symbol_articles = {symbol: [] for symbol in symbols}
    archive_rows = []
    macro_routed_count = 0

    # PR-E.1: optional semantic relevance filter (between keyword router and
    # per-article scoring). When enabled, asks Gemini flash-lite to drop
    # candidates the article isn't materially about (KO/Coca-Cola Zone case,
    # sector-basket over-routing). Gated by config; falls open on errors.
    srf_cfg = news_config.get('symbol_relevance_filter') or {}
    srf_enabled = bool(srf_cfg.get('enabled', False))
    srf_shadow = bool(srf_cfg.get('shadow_log_only', True))
    srf_min_candidates = int(srf_cfg.get('min_candidates_to_filter', 2))
    srf_drop_count = 0
    srf_kept_count = 0
    srf_evaluated = 0
    business_desc_map = None
    if srf_enabled:
        try:
            from src.config import _load_business_descriptions
            business_desc_map = _load_business_descriptions()
        except Exception:
            business_desc_map = None

    for article in all_articles:
        title = article.get('title', '')
        description = article.get('description', '')
        matched_symbols = _match_article_to_symbols(title, description, symbols)

        # Macro routing fallback: unmatched articles with macro keywords
        # get routed to all symbols in the relevant sector group(s).
        if not matched_symbols and macro_routing_enabled:
            macro_sectors = _match_article_to_macro_sectors(title, description)
            if macro_sectors:
                matched_symbols = _expand_sectors_to_symbols(
                    macro_sectors, symbols, max_symbols=macro_max)
                if matched_symbols:
                    macro_routed_count += 1

        if not matched_symbols:
            continue

        title_hash = compute_title_hash(title) if title else None

        # Semantic relevance gate (PR-E.1). Runs only when ≥N candidates
        # matched (skip single-candidate articles where filter can't help).
        if srf_enabled and len(matched_symbols) >= srf_min_candidates:
            try:
                from src.analysis.symbol_relevance_filter import filter_by_relevance
                article_view = {
                    'title': title,
                    'description': description,
                    'title_hash': title_hash,
                    'source': article.get('source', ''),
                }
                kept, verdicts = filter_by_relevance(
                    article_view, matched_symbols, business_desc_map)
                dropped = [s for s in matched_symbols if s not in kept]
                srf_evaluated += 1
                srf_drop_count += len(dropped)
                srf_kept_count += len(kept)
                if dropped:
                    log.info(
                        f"[SYMRELFILTER_{'SHADOW' if srf_shadow else 'LIVE'}] "
                        f"'{title[:60]}' kept={kept} drop={dropped}")
                if not srf_shadow:
                    matched_symbols = kept
            except Exception as e:
                log.debug(f"symbol_relevance_filter raised, falling open: {e}")

        gemini_score = gemini_article_scores.get(title_hash) if title_hash else None

        for symbol in matched_symbols:
            if gemini_score is not None:
                symbol_articles[symbol].append({
                    'title': title,
                    'score': gemini_score,
                    'published_at': article.get('published_at', ''),
                    'source': article.get('source', ''),
                    'title_hash': title_hash,
                })

            # Accumulate archive rows for DB storage
            if title_hash:
                archive_rows.append({
                    'title': title,
                    'title_hash': title_hash,
                    'source': article.get('source', ''),
                    'source_url': article.get('source_url', ''),
                    'description': description,
                    'symbol': symbol,
                    'gemini_score': gemini_score,
                    'category': article.get('category', 'unknown'),
                })

    # Archive articles to DB
    if archive_rows:
        try:
            save_articles_batch(archive_rows)
        except Exception as e:
            log.warning(f"Failed to archive articles: {e}")

    # PR-E.1: cycle-level summary of what the relevance filter did
    if srf_enabled and srf_evaluated > 0:
        mode = 'SHADOW' if srf_shadow else 'LIVE'
        total_pairs = srf_kept_count + srf_drop_count
        drop_pct = (srf_drop_count / total_pairs * 100) if total_pairs else 0
        log.info(
            f"[SYMRELFILTER_{mode}] Cycle summary: evaluated {srf_evaluated} "
            f"articles, kept {srf_kept_count} pairs, dropped {srf_drop_count} "
            f"({drop_pct:.0f}%)")

    # 4. Compute aggregates per symbol (freshness-weighted)
    half_life = news_config.get('freshness_half_life_hours', 6)
    per_symbol = {}
    db_rows = []

    for symbol in symbols:
        articles = symbol_articles[symbol]
        if not articles:
            continue

        scores = [a['score'] for a in articles]
        # Pass the full article dict so the weight function can walk
        # published → updated → collected_at fallback.
        weights = [_compute_freshness_weight(a, half_life) for a in articles]
        total_weight = sum(weights)
        if total_weight > 0:
            avg_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
        else:
            avg_score = statistics.mean(scores)
        volatility = statistics.stdev(scores) if len(scores) > 1 else 0.0
        positive_count = sum(1 for s in scores if s > 0.05)
        negative_count = sum(1 for s in scores if s < -0.05)
        total = len(scores)

        # Top-scored articles (|score| > 0.3) for downstream Gemini prompt
        top_scored = sorted(
            [a for a in articles if abs(a['score']) > 0.3],
            key=lambda a: abs(a['score']), reverse=True)[:10]

        per_symbol[symbol] = {
            'avg_sentiment_score': avg_score,
            'news_volume': total,
            'sentiment_volatility': volatility,
            'positive_buzz_ratio': positive_count / total,
            'negative_buzz_ratio': negative_count / total,
            'headlines': [a['title'] for a in articles[:10]],
            'articles': [
                {'title': a['title'], 'score': a['score']}
                for a in articles
            ],
            'top_scored_articles': [
                {
                    'title': a['title'],
                    'score': a['score'],
                    'source': a.get('source', ''),
                    'title_hash': a.get('title_hash'),
                }
                for a in top_scored
            ],
        }

        db_rows.append({
            'symbol': symbol,
            'avg_sentiment_score': avg_score,
            'news_volume': total,
            'sentiment_volatility': volatility,
            'positive_buzz_ratio': positive_count / total,
            'negative_buzz_ratio': negative_count / total,
        })

    # 5. Save to DB
    save_news_sentiment_batch(db_rows)

    # 6. Compare against previous cycle to detect triggers
    previous = get_latest_news_sentiment(symbols)
    triggered_symbols = []

    for symbol, data in per_symbol.items():
        prev = previous.get(symbol)
        if not prev:
            continue

        prev_volume = prev.get('news_volume', 0)
        prev_sentiment = prev.get('avg_sentiment_score', 0)

        # Volume spike trigger
        if prev_volume > 0 and data['news_volume'] > prev_volume * volume_spike_multiplier:
            log.info(f"[{symbol}] News volume spike: {data['news_volume']} vs previous {prev_volume}")
            triggered_symbols.append(symbol)
            continue

        # Sentiment shift trigger
        if abs(data['avg_sentiment_score'] - prev_sentiment) >= sentiment_shift_threshold:
            log.info(f"[{symbol}] Sentiment shift: {data['avg_sentiment_score']:.3f} vs previous {prev_sentiment:.3f}")
            triggered_symbols.append(symbol)

    log.info(f"News collection complete. {len(per_symbol)} symbols with data, {len(triggered_symbols)} triggered.")
    return {
        'per_symbol': per_symbol,
        'triggered_symbols': triggered_symbols,
    }
