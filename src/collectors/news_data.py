import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from src.config import app_config
from src.database import get_latest_news_sentiment, save_news_sentiment_batch
from src.logger import log

# --- Constants ---

SYMBOL_KEYWORDS = {
    # Crypto
    'BTC': ['BTC', 'Bitcoin'],
    'ETH': ['ETH', 'Ethereum'],
    'SOL': ['SOL', 'Solana'],
    'XRP': ['XRP', 'Ripple'],
    'ADA': ['ADA', 'Cardano'],
    'AVAX': ['AVAX', 'Avalanche'],
    'DOGE': ['DOGE', 'Dogecoin'],
    'MATIC': ['MATIC', 'Polygon'],
    'BNB': ['BNB', 'Binance Coin'],
    'TRX': ['TRX', 'Tron'],
    # Stocks
    'AAPL': ['AAPL', 'Apple stock', 'Apple Inc'],
    'MSFT': ['MSFT', 'Microsoft stock', 'Microsoft Corp'],
    'GOOGL': ['GOOGL', 'Google stock', 'Alphabet'],
    'AMZN': ['AMZN', 'Amazon stock', 'Amazon.com'],
    'NVDA': ['NVDA', 'Nvidia stock', 'NVIDIA'],
    'META': ['META', 'Meta stock', 'Meta Platforms', 'Facebook'],
    'TSLA': ['TSLA', 'Tesla stock', 'Tesla Inc'],
}

RSS_FEEDS = [
    {'url': 'https://feeds.reuters.com/reuters/businessNews', 'category': 'financial'},
    {'url': 'https://feeds.bloomberg.com/markets/news.rss', 'category': 'financial'},
    {'url': 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147', 'category': 'financial'},
    {'url': 'https://www.ft.com/rss/home', 'category': 'financial'},
    {'url': 'https://feeds.a.dj.com/rss/RSSMarketsMain.xml', 'category': 'financial'},
    {'url': 'https://www.handelsblatt.com/contentexport/feed/', 'category': 'european'},
    {'url': 'https://www.faz.net/rss/aktuell/finanzen/', 'category': 'european'},
    {'url': 'https://www.coindesk.com/arc/outboundfeeds/rss/', 'category': 'crypto'},
    {'url': 'https://cointelegraph.com/rss', 'category': 'crypto'},
    {'url': 'https://www.theblock.co/rss.xml', 'category': 'crypto'},
    {'url': 'https://feeds.feedburner.com/TechCrunch/', 'category': 'tech'},
    {'url': 'https://www.theverge.com/rss/index.xml', 'category': 'tech'},
    {'url': 'https://apnews.com/business.rss', 'category': 'wire'},
    {'url': 'https://feeds.marketwatch.com/marketwatch/topstories/', 'category': 'financial'},
]

_vader_analyzer = SentimentIntensityAnalyzer()

RSS_FETCH_TIMEOUT = 10


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
            })
        return articles
    except Exception as e:
        log.warning(f"Failed to fetch RSS feed {feed_info['url']}: {e}")
        return []


def _fetch_rss_feeds():
    """Fetches all RSS feeds in parallel using a thread pool."""
    all_articles = []
    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(_fetch_single_rss_feed, feed): feed for feed in RSS_FEEDS}
        for future in as_completed(futures, timeout=RSS_FETCH_TIMEOUT + 5):
            try:
                articles = future.result(timeout=RSS_FETCH_TIMEOUT)
                all_articles.extend(articles)
            except Exception as e:
                feed = futures[future]
                log.warning(f"RSS feed timed out or failed: {feed['url']}: {e}")
    log.info(f"Fetched {len(all_articles)} articles from {len(RSS_FEEDS)} RSS feeds.")
    return all_articles


def _deduplicate_articles(articles):
    """Removes near-duplicate headlines by comparing lowercased stripped titles."""
    seen = set()
    unique = []
    for article in articles:
        key = article.get('title', '').lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(article)
    log.info(f"Deduplicated {len(articles)} articles to {len(unique)} unique articles.")
    return unique


def _match_article_to_symbols(title, description, symbols):
    """Matches an article to symbols based on keyword presence in title/description."""
    matched = []
    text = f"{title} {description}".upper()
    for symbol in symbols:
        keywords = SYMBOL_KEYWORDS.get(symbol, [symbol])
        for keyword in keywords:
            if keyword.upper() in text:
                matched.append(symbol)
                break
    return matched


def _build_query_string(symbols):
    """Builds an OR-joined query string from symbol keywords for NewsAPI."""
    all_keywords = []
    for symbol in symbols:
        keywords = SYMBOL_KEYWORDS.get(symbol, [symbol])
        all_keywords.extend(keywords)
    return ' OR '.join(all_keywords)


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

    # 1. Fetch from both sources
    query = _build_query_string(symbols)
    newsapi_articles = _fetch_newsapi_articles(query)
    rss_articles = _fetch_rss_feeds()

    # 2. Combine and deduplicate
    all_articles = _deduplicate_articles(newsapi_articles + rss_articles)

    if not all_articles:
        log.info("No news articles found.")
        return {'per_symbol': {}, 'triggered_symbols': []}

    # 3. VADER score each headline and match to symbols
    symbol_articles = {symbol: [] for symbol in symbols}

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

        for symbol in matched_symbols:
            symbol_articles[symbol].append({
                'title': title,
                'score': score,
            })

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
