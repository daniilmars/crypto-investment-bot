import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from src.config import app_config
from src.database import (
    get_latest_news_sentiment, save_news_sentiment_batch,
    save_articles_batch, compute_title_hash,
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
]

_vader_analyzer = SentimentIntensityAnalyzer()

RSS_FETCH_TIMEOUT = 15


# --- Internal Functions ---

def _fetch_newsapi_articles(query):
    """Fetches articles from NewsAPI using a batched OR-joined query."""
    api_key = app_config.get('api_keys', {}).get('newsapi')
    if not api_key or api_key == 'YOUR_NEWSAPI_ORG_KEY':
        log.warning("NewsAPI key not configured. Skipping NewsAPI fetch.")
        return []

    try:
        url = 'https://newsapi.org/v2/everything'
        params = {
            'q': query,
            'language': 'en',
            'sortBy': 'publishedAt',
            'pageSize': 100,
            'apiKey': api_key,
        }
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        articles = []
        for article in data.get('articles', []):
            articles.append({
                'title': article.get('title', ''),
                'description': article.get('description', ''),
                'published_at': article.get('publishedAt', ''),
                'source': article.get('source', {}).get('name', 'NewsAPI'),
            })
        log.info(f"Fetched {len(articles)} articles from NewsAPI.")
        return articles
    except Exception as e:
        log.error(f"Error fetching from NewsAPI: {e}")
        return []


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
    """Fetches all RSS feeds in parallel using a thread pool."""
    all_articles = []
    with ThreadPoolExecutor(max_workers=14) as executor:
        futures = {executor.submit(_fetch_single_rss_feed, feed): feed for feed in RSS_FEEDS}
        try:
            done_iter = as_completed(futures, timeout=RSS_FETCH_TIMEOUT + 5)
            for future in done_iter:
                try:
                    articles = future.result(timeout=RSS_FETCH_TIMEOUT)
                    all_articles.extend(articles)
                except Exception as e:
                    feed = futures[future]
                    log.warning(f"RSS feed timed out or failed: {feed['url']}: {e}")
        except TimeoutError:
            n_unfinished = sum(1 for fut in futures if not fut.done())
            log.warning(f"RSS fetch global timeout — {n_unfinished} feeds did not finish in time.")
    log.info(f"Fetched {len(all_articles)} articles from {len(RSS_FEEDS)} RSS feeds.")
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


def _build_query_string(symbols):
    """Builds an OR-joined query string from symbol keywords for NewsAPI.

    NewsAPI has a ~500 char query limit. Only includes symbols that have
    SYMBOL_KEYWORDS entries (major names with recognizable keywords).
    Symbols without keyword mappings are covered by RSS feeds instead.
    """
    all_keywords = []
    for symbol in symbols:
        if symbol in SYMBOL_KEYWORDS:
            keywords = SYMBOL_KEYWORDS[symbol]
            all_keywords.extend(keywords)
    query = ' OR '.join(all_keywords)
    # NewsAPI max query length is ~500 chars; truncate at last complete OR term
    if len(query) > 500:
        truncated = query[:500]
        last_or = truncated.rfind(' OR ')
        if last_or > 0:
            query = truncated[:last_or]
    return query


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

    # 1. Fetch from all sources (NewsAPI + RSS + web scraping)
    query = _build_query_string(symbols)
    newsapi_articles = _fetch_newsapi_articles(query)
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
    all_articles = _deduplicate_articles(newsapi_articles + rss_articles + web_articles)

    if not all_articles:
        log.info("No news articles found.")
        return {'per_symbol': {}, 'triggered_symbols': []}

    # 2b. Deep scraping: enrich important articles with full body text
    if news_config.get('deep_scraping', {}).get('enabled', False):
        try:
            from src.collectors.article_enricher import enrich_articles_batch
            all_articles = enrich_articles_batch(all_articles)
        except Exception as e:
            log.warning(f"Deep scraping failed, continuing with original articles: {e}")

    # 3. VADER score each headline and match to symbols
    symbol_articles = {symbol: [] for symbol in symbols}
    archive_rows = []

    for article in all_articles:
        title = article.get('title', '')
        description = article.get('description', '')
        matched_symbols = _match_article_to_symbols(title, description, symbols)

        if not matched_symbols:
            continue

        # Score title and description separately, then combine with weighted average.
        # Title carries more weight as it's the editorial summary of the article.
        title_score = _vader_analyzer.polarity_scores(title)['compound'] if title else 0
        desc_score = _vader_analyzer.polarity_scores(description)['compound'] if description else 0

        if title and description:
            score = title_score * 0.6 + desc_score * 0.4
        elif title:
            score = title_score
        else:
            score = desc_score

        title_hash = compute_title_hash(title) if title else None

        for symbol in matched_symbols:
            symbol_articles[symbol].append({
                'title': title,
                'score': score,
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
                    'vader_score': score,
                    'category': article.get('category', 'unknown'),
                })

    # Archive articles to DB
    if archive_rows:
        try:
            save_articles_batch(archive_rows)
        except Exception as e:
            log.warning(f"Failed to archive articles: {e}")

    # 4. Compute aggregates per symbol
    per_symbol = {}
    db_rows = []

    for symbol in symbols:
        articles = symbol_articles[symbol]
        if not articles:
            continue

        scores = [a['score'] for a in articles]
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
