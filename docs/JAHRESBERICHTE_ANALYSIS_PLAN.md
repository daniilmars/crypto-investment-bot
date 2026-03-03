# Jahresberichte (Annual Report) Analysis — Implementation Plan

## Goal

Analyze annual reports of stock companies to extract fundamental insights that go beyond what Alpha Vantage provides (P/E, earnings growth). Especially valuable for EU/Asia stocks where Alpha Vantage data is sparse. Feed structured analysis into signal generation as a conviction multiplier.

## What It Gives Us

Annual reports contain:
- Forward guidance and management outlook
- Margin trends (gross, operating, net) over multiple years
- R&D spending trajectory (especially relevant for AI/tech stocks)
- Debt structure and refinancing risk
- Segment breakdowns (which business lines are growing/shrinking)
- Risk factors and regulatory exposure
- Capital expenditure plans
- Dividend policy changes

These provide long-term conviction signals that complement the short-term technical + sentiment signals the bot already uses.

## Architecture

### Data Flow

```
IR page / SEC EDGAR / DGAP
        |
        v
  PDF download + text extraction
        |
        v
  Gemini analysis (structured JSON output)
        |
        v
  Cache in DB (per-symbol, 90-day TTL)
        |
        v
  Feed into generate_stock_signal() as fundamental_analysis
```

### Components

#### 1. Report Fetcher (`src/collectors/annual_report_fetcher.py`)

- For US stocks: SEC EDGAR API (free, structured, 10-K filings)
- For EU stocks: Company IR pages (SAP, Siemens, ASML etc. publish English PDFs)
- For Asia stocks: Company IR pages (English versions where available)
- Store downloaded PDFs in `data/annual_reports/{symbol}_{year}.pdf`
- Track last-fetched date per symbol to avoid re-downloading

#### 2. Report Analyzer (`src/analysis/annual_report_analyzer.py`)

- Extract text from PDF (PyPDF2 or pdfplumber)
- Chunk into manageable sections (Gemini handles ~100 pages but cost matters)
- Send to Gemini with structured extraction prompt
- Output: structured JSON with scores and extracted data points

**Gemini Prompt Structure:**
```
Analyze this annual report excerpt for {symbol} ({company_name}).
Extract the following in JSON format:

{
  "revenue_growth_yoy": float,        // % change
  "operating_margin_trend": "improving" | "stable" | "declining",
  "forward_guidance_tone": "optimistic" | "cautious" | "negative",
  "forward_guidance_confidence": float, // 0-1
  "rd_spending_trend": "increasing" | "stable" | "decreasing",
  "debt_to_equity_concern": bool,
  "key_risk_factors": [str],           // top 3
  "segment_highlights": str,           // 1-2 sentence summary
  "capex_outlook": "expanding" | "stable" | "contracting",
  "overall_fundamental_score": float,  // -1 (bearish) to +1 (bullish)
  "reasoning": str                     // 2-3 sentence summary
}
```

#### 3. DB Storage (`src/database.py`)

New table:
```sql
CREATE TABLE IF NOT EXISTS annual_report_analysis (
    symbol TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    analysis_json TEXT NOT NULL,
    fundamental_score REAL NOT NULL,
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    report_source TEXT,
    PRIMARY KEY (symbol, fiscal_year)
);
```

#### 4. Signal Integration (`src/analysis/stock_signal_engine.py`)

- Load cached analysis for the symbol (most recent fiscal year)
- Use `fundamental_score` as a conviction multiplier:
  - Score > 0.5: boost BUY signals (multiply confidence by 1.2)
  - Score < -0.5: boost SELL signals or suppress BUY
  - Score between -0.5 and 0.5: no adjustment
- Add `jahresbericht_score` to signal reason string for transparency

#### 5. Batch Job (`scripts/analyze_annual_reports.py`)

- Runs as a separate scheduled job (weekly or on-demand)
- Processes top N stocks by portfolio weight, skips if analysis < 90 days old
- Estimated cost: ~$0.01-0.05 per report (Gemini Flash on extracted text)
- Can be triggered via Telegram `/analyze_reports` command

## Implementation Phases

### Phase 1: US Stocks via SEC EDGAR (lowest effort)
- SEC EDGAR has structured API, 10-K filings are standardized
- Free, no scraping needed
- Start with top 20 US stocks in watchlist
- Estimated: 2-3 hours implementation

### Phase 2: EU Stocks via IR Pages
- SAP, Siemens, ASML, Novo Nordisk etc. all publish English annual reports
- Need per-company IR page URLs (maintain in config/annual_report_sources.yaml)
- PDF formats vary — may need section detection (ToC-based chunking)
- Estimated: 3-4 hours implementation

### Phase 3: Asia Stocks
- Lower priority — many reports are in local languages only
- Focus on companies that publish English versions (Toyota, Sony, Samsung)
- Skip others until translation pipeline is justified

## Challenges and Mitigations

| Challenge | Mitigation |
|-----------|------------|
| PDF format varies (scanned vs native) | Use pdfplumber for native; skip scanned PDFs (most modern reports are native) |
| German/Japanese language reports | Prioritize English versions; Gemini handles German well if needed |
| Cost with 228 stocks | Start with top 20, expand based on ROI; cache for 90 days |
| Report timing (once/year) | Long-term signal — use as conviction multiplier, not entry timing |
| Large PDF size (100+ pages) | Extract key sections (Management Discussion, Financial Highlights, Outlook) via ToC detection |
| Stale data between annual reports | Supplement with quarterly reports (10-Q / Quartalsberichte) in Phase 2+ |

## Cost Estimate

- Gemini Flash: ~$0.01-0.05 per report analysis (text extraction + analysis)
- 20 stocks x 1 report/year = $0.20-1.00/year
- Even at 100 stocks: < $5/year
- Negligible compared to current news analysis costs ($0.02/day = $7.30/year)

## Config Addition

```yaml
settings:
  annual_report_analysis:
    enabled: true
    max_symbols: 20              # analyze top N stocks by portfolio weight
    cache_ttl_days: 90           # re-analyze after 90 days
    score_buy_boost: 1.2         # multiply BUY confidence when score > 0.5
    score_sell_boost: 1.2        # multiply SELL confidence when score < -0.5
    sources:
      sec_edgar: true            # US stocks (10-K filings)
      ir_pages: true             # EU/Asia stocks (config-driven URLs)
    ir_page_config: "config/annual_report_sources.yaml"
```

## Relation to Auto-Trading Bot

The auto-trading shadow bot (`auto_trading.enabled: true`) executes all signals without confirmation. Annual report analysis adds a fundamental conviction layer that benefits both:

- **Manual bot**: Higher-confidence signals mean fewer rejected confirmations
- **Auto bot**: Better signal quality means fewer unprofitable auto-trades

The `fundamental_score` from Jahresberichte analysis flows through the same `generate_stock_signal()` path used by both bots — no special wiring needed.
