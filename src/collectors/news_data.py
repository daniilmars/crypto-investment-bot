import math
import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

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
    'AVGO': ['AVGO', 'Broadcom stock', 'Broadcom Inc'],
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
    'LLY': ['Eli Lilly'],  # "LLY" too short
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
    'DIS': ['Disney stock', 'Walt Disney'],  # "DIS" too short
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

# Pre-compile regex patterns for each keyword (word-boundary matching)
_KEYWORD_PATTERNS = {}
for _sym, _kws in SYMBOL_KEYWORDS.items():
    _KEYWORD_PATTERNS[_sym] = [
        re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
        for kw in _kws
    ]

RSS_FEEDS = [
    {'url': 'https://feeds.reuters.com/reuters/businessNews', 'category': 'financial'},
    {'url': 'https://feeds.bloomberg.com/markets/news.rss', 'category': 'financial'},
    {'url': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147', 'category': 'financial'},
    {'url': 'https://www.ft.com/rss/home', 'category': 'financial'},
    {'url': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', 'category': 'financial'},
    {'url': 'https://www.handelsblatt.com/contentexport/feed/', 'category': 'european'},
    {'url': 'https://www.faz.net/rss/aktuell/finanzen/', 'category': 'european'},
    {'url': 'https://feeds.bbci.co.uk/news/business/rss.xml', 'category': 'european'},
    {'url': 'https://www.cityam.com/feed/', 'category': 'european'},
    {'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'category': 'crypto'},
    {'url': 'https://cointelegraph.com/rss', 'category': 'crypto'},
    {'url': 'https://www.theblock.co/rss.xml', 'category': 'crypto'},
    {'url': 'https://feeds.arstechnica.com/arstechnica/technology-lab', 'category': 'tech'},
    {'url': 'https://www.wired.com/feed/category/business/latest/rss', 'category': 'tech'},
    {'url': 'https://apnews.com/business.rss', 'category': 'wire'},
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
    # Layer C — KOL / Key person feeds
    {'url': 'https://decrypt.co/feed', 'category': 'kol'},
    {'url': 'https://blockworks.co/feed', 'category': 'kol'},
    {'url': 'https://rekt.news/feed/', 'category': 'kol'},
    {'url': 'https://bitcoinmagazine.com/feed', 'category': 'kol'},
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
    {'url': 'https://ai.meta.com/blog/rss/', 'category': 'ai_research'},
    {'url': 'https://blogs.nvidia.com/feed/', 'category': 'ai_research'},
    # ── EU Market Feeds ──
    # UK / LSE
    {'url': 'https://www.investegate.co.uk/Rss.aspx?type=0', 'category': 'european'},
    # Germany
    {'url': 'https://www.dw.com/en/business/s-1431/rss.xml', 'category': 'european'},
    # EU general
    {'url': 'https://euobserver.com/rss', 'category': 'european'},
    {'url': 'https://www.swissinfo.ch/eng/business/rss', 'category': 'european'},
    # EU regulatory
    {'url': 'https://www.ecb.europa.eu/rss/pressconf.html', 'category': 'regulatory'},
    {'url': 'https://www.bankofengland.co.uk/rss/news', 'category': 'regulatory'},
    {'url': 'https://ec.europa.eu/eurostat/web/main/news/euro-indicators/rss', 'category': 'regulatory'},
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
    {'url': 'https://en.yna.co.kr/RSS/economy.xml', 'category': 'asia'},
    {'url': 'https://www.kedglobal.com/rss/', 'category': 'asia'},
    # Taiwan
    {'url': 'https://focustaiwan.tw/RSS/aECO', 'category': 'asia'},
    {'url': 'https://www.taipeitimes.com/xml/biz.rss', 'category': 'asia'},
    # India
    {'url': 'https://www.livemint.com/rss/markets', 'category': 'asia'},
    {'url': 'https://www.business-standard.com/rss/markets-106.rss', 'category': 'asia'},
    {'url': 'https://www.moneycontrol.com/rss/marketreports.xml', 'category': 'asia'},
    # Broader APAC
    {'url': 'https://www.cnbc.com/id/104568957/device/rss/rss.html', 'category': 'asia'},
    {'url': 'http://www.chinadaily.com.cn/rss/business_rss.xml', 'category': 'asia'},
    {'url': 'https://www3.nhk.or.jp/nhkworld/en/news/tags/200/list/rss.xml', 'category': 'asia'},
    {'url': 'https://stockhead.com.au/feed/', 'category': 'asia'},
    # Asia regulatory
    {'url': 'https://www.hkma.gov.hk/eng/news-and-media/press-releases/rss/', 'category': 'regulatory'},
    {'url': 'https://rbi.org.in/scripts/RSS_Feeds.aspx', 'category': 'regulatory'},
    {'url': 'https://www.rba.gov.au/rss/rss-cb-media-releases.xml', 'category': 'regulatory'},
    {'url': 'https://www.jpx.co.jp/english/news/rss/index.xml', 'category': 'regulatory'},
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
    {'url': 'https://www.bls.gov/feed/bls_latest.rss', 'category': 'regulatory'},
    {'url': 'https://seekingalpha.com/market_currents.xml', 'category': 'financial'},
]

_vader_analyzer = SentimentIntensityAnalyzer()

RSS_FETCH_TIMEOUT = 15


# --- Internal Functions ---


def _fetch_single_rss_feed(feed_info):
    """Fetches and parses a single RSS feed."""
    try:
        parsed = feedparser.parse(feed_info['url'],
                                  request_headers={'User-Agent': 'CryptoBot/1.0'})
        articles = []
        for entry in parsed.entries:
            articles.append({
                'title': getattr(entry, 'title', ''),
                'description': getattr(entry, 'summary', ''),
                'published_at': getattr(entry, 'published', ''),
                'source': getattr(parsed.feed, 'title', feed_info['url']),
                'source_url': getattr(entry, 'link', ''),
                'category': feed_info.get('category', 'unknown'),
            })
        return articles
    except Exception as e:
        log.warning(f"Failed to fetch RSS feed {feed_info['url']}: {e}")
        return []


def _fetch_rss_feeds():
    """Fetches all RSS feeds in parallel using a thread pool.

    Tries to load feeds from source_registry DB; falls back to hardcoded RSS_FEEDS.
    """
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

    all_articles = []
    with ThreadPoolExecutor(max_workers=14) as executor:
        futures = {executor.submit(_fetch_single_rss_feed, feed): feed for feed in feeds}
        try:
            done_iter = as_completed(futures, timeout=RSS_FETCH_TIMEOUT + 5)
            for future in done_iter:
                try:
                    articles = future.result(timeout=RSS_FETCH_TIMEOUT)
                    all_articles.extend(articles)
                    # Update source stats if using registry
                    if use_registry:
                        feed = futures[future]
                        source_id = feed.get('source_id')
                        if source_id:
                            try:
                                update_source_stats(source_id, articles_fetched=len(articles))
                            except Exception as e:
                                log.debug(f"Source stats update failed for {source_id}: {e}")
                except Exception as e:
                    feed = futures[future]
                    log.warning(f"RSS feed timed out or failed: {feed['url']}: {e}")
                    # Record error for registry sources
                    if use_registry:
                        source_id = feed.get('source_id')
                        if source_id:
                            try:
                                update_source_stats(source_id, errors=1)
                            except Exception as e:
                                log.debug(f"Source error recording failed for {source_id}: {e}")
        except TimeoutError:
            n_unfinished = sum(1 for fut in futures if not fut.done())
            log.warning(f"RSS fetch global timeout — {n_unfinished} feeds did not finish in time.")
    log.info(f"Fetched {len(all_articles)} articles from {len(feeds)} RSS feeds"
             f"{' (registry)' if use_registry else ' (hardcoded)'}.")
    return all_articles


def _is_likely_english(text):
    """Fast heuristic: reject text with >15% non-ASCII chars (non-English)."""
    if not text:
        return False
    non_ascii = sum(1 for c in text if ord(c) > 127)
    return non_ascii / len(text) < 0.15


def _deduplicate_articles(articles):
    """Removes near-duplicate and non-English headlines."""
    seen = set()
    unique = []
    for article in articles:
        title = article.get('title', '').strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        if not _is_likely_english(title):
            continue
        seen.add(key)
        unique.append(article)
    log.info(f"Deduplicated {len(articles)} articles to {len(unique)} unique articles.")
    return unique


def _match_article_to_symbols(title, description, symbols):
    """Matches an article to symbols using word-boundary regex matching.

    Only matches against keywords defined in SYMBOL_KEYWORDS with pre-compiled
    regex patterns. Symbols without keyword entries are skipped (they rely on
    RSS feeds for coverage instead of text matching).
    """
    matched = []
    text = f"{title} {description}"
    for symbol in symbols:
        patterns = _KEYWORD_PATTERNS.get(symbol)
        if not patterns:
            continue
        for pattern in patterns:
            if pattern.search(text):
                matched.append(symbol)
                break
    return matched



def _score_with_gemini(articles_with_hashes: list) -> dict:
    """Scores articles with Gemini, using DB cache for previously scored articles.

    Args:
        articles_with_hashes: list of dicts with 'title', 'description', 'title_hash'.

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


def _compute_freshness_weight(published_at_str, half_life_hours=6):
    """Compute exponential decay weight based on article age.

    weight = exp(-age_hours * ln(2) / half_life)
    0h → 1.0, 6h → 0.5, 12h → 0.25, 24h → 0.06 (with default half_life=6).
    Articles with unparseable timestamps get weight 1.0 (fail-safe).
    """
    if not published_at_str or half_life_hours <= 0:
        return 1.0
    try:
        from email.utils import parsedate_to_datetime
        pub_dt = parsedate_to_datetime(published_at_str)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600.0
        if age_hours < 0:
            return 1.0
        return math.exp(-age_hours * math.log(2) / half_life_hours)
    except Exception:
        return 1.0


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
    use_gemini_scoring = news_config.get('use_gemini_scoring', True)
    gemini_article_scores = {}
    if use_gemini_scoring:
        articles_for_scoring = []
        for article in all_articles:
            title = article.get('title', '')
            if not title:
                continue
            title_hash = compute_title_hash(title)
            matched = _match_article_to_symbols(title, article.get('description', ''), symbols)
            if matched:
                articles_for_scoring.append({
                    'title': title,
                    'description': article.get('description', ''),
                    'title_hash': title_hash,
                })
        gemini_article_scores = _score_with_gemini(articles_for_scoring)

    # 3. VADER score each headline, use Gemini score when available, match to symbols
    symbol_articles = {symbol: [] for symbol in symbols}
    archive_rows = []

    for article in all_articles:
        title = article.get('title', '')
        description = article.get('description', '')
        matched_symbols = _match_article_to_symbols(title, description, symbols)

        if not matched_symbols:
            continue

        # VADER scoring (always runs, serves as fallback)
        title_score = _vader_analyzer.polarity_scores(title)['compound'] if title else 0
        desc_score = _vader_analyzer.polarity_scores(description)['compound'] if description else 0

        if title and description:
            vader_score = title_score * 0.6 + desc_score * 0.4
        elif title:
            vader_score = title_score
        else:
            vader_score = desc_score

        title_hash = compute_title_hash(title) if title else None

        # Use Gemini score when available, fall back to VADER
        gemini_score = gemini_article_scores.get(title_hash) if title_hash else None
        effective_score = gemini_score if gemini_score is not None else vader_score

        for symbol in matched_symbols:
            symbol_articles[symbol].append({
                'title': title,
                'score': effective_score,
                'published_at': article.get('published_at', ''),
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
                    'vader_score': vader_score,
                    'gemini_score': gemini_score,
                    'category': article.get('category', 'unknown'),
                })

    # Archive articles to DB
    if archive_rows:
        try:
            save_articles_batch(archive_rows)
        except Exception as e:
            log.warning(f"Failed to archive articles: {e}")

    # 4. Compute aggregates per symbol (freshness-weighted)
    half_life = news_config.get('freshness_half_life_hours', 6)
    per_symbol = {}
    db_rows = []

    for symbol in symbols:
        articles = symbol_articles[symbol]
        if not articles:
            continue

        scores = [a['score'] for a in articles]
        weights = [_compute_freshness_weight(a.get('published_at', ''), half_life)
                   for a in articles]
        total_weight = sum(weights)
        if total_weight > 0:
            avg_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
        else:
            avg_score = statistics.mean(scores)
        volatility = statistics.stdev(scores) if len(scores) > 1 else 0.0
        positive_count = sum(1 for s in scores if s > 0.05)
        negative_count = sum(1 for s in scores if s < -0.05)
        total = len(scores)

        per_symbol[symbol] = {
            'avg_sentiment_score': avg_score,
            'news_volume': total,
            'sentiment_volatility': volatility,
            'positive_buzz_ratio': positive_count / total,
            'negative_buzz_ratio': negative_count / total,
            'headlines': [a['title'] for a in articles[:10]],
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
