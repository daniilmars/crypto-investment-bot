# News Scraper Task

You are a news scraper. Visit financial news websites using Chrome MCP tools, extract headlines and article details, and write structured JSON output.

## Instructions

1. Get browser context with `tabs_context_mcp(createIfEmpty=true)`
2. Create 4 tabs with `tabs_create_mcp()` (reuse existing ones if available)
3. Navigate each tab to `example.com` first, then use `javascript_tool` with `window.location.href` to go to news sites
4. **Scrape in parallel**: navigate multiple tabs simultaneously, wait 3s, then extract from all tabs at once
5. For the top 3 most relevant crypto articles, click into them and extract the first 3-4 paragraphs of article text
6. Write the combined JSON to: `/Users/daniil/Projects/crypto-investment-bot/data/chrome-scraped.json`

## Parallel Scraping Strategy

**Batch 1** (4 tabs simultaneously):
- Tab A → CoinDesk
- Tab B → CoinTelegraph
- Tab C → Reuters Business
- Tab D → The Block

**Batch 2** (reuse 4 tabs):
- Tab A → CNBC
- Tab B → AP News
- Tab C → MarketWatch
- Tab D → Decrypt

**Batch 3** (detail extraction):
- Click into top 3 crypto articles for full summaries

## Sites and Extraction Scripts

### CoinDesk (https://www.coindesk.com/)
```javascript
const h=[],s=new Set();
document.querySelectorAll('a[href*="/article/"],a[href*="/markets/"],a[href*="/policy/"],a[href*="/tech/"],a[href*="/business/"]').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>20&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'CoinDesk',source_url:u})}});
JSON.stringify({source:'CoinDesk',count:h.length,articles:h});
```

### CoinTelegraph (https://cointelegraph.com/)
```javascript
const h=[],s=new Set();
document.querySelectorAll('a[href*="/news/"],a[href*="/magazine/"]').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>20&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'CoinTelegraph',source_url:u})}});
JSON.stringify({source:'CoinTelegraph',count:h.length,articles:h});
```

### The Block (https://www.theblock.co/latest) — NEW
```javascript
const h=[],s=new Set();
document.querySelectorAll('a[class*="title"],a[href*="/post/"],h2 a,h3 a').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>20&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'The Block',source_url:u})}});
JSON.stringify({source:'The Block',count:h.length,articles:h});
```

### Reuters Business (https://www.reuters.com/business/)
```javascript
const h=[],s=new Set();
document.querySelectorAll('a[data-testid*="Heading"],a[data-testid*="Title"],h3 a').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>20&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'Reuters',source_url:u})}});
JSON.stringify({source:'Reuters',count:h.length,articles:h});
```

### CNBC (https://www.cnbc.com/world/?region=world) — NEW
```javascript
const h=[],s=new Set();
document.querySelectorAll('a[href*="/2026/"],a[href*="/2025/"]').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>25&&t.length<250&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'CNBC',source_url:u})}});
JSON.stringify({source:'CNBC',count:h.length,articles:h});
```

### AP News Business (https://apnews.com/hub/business) — NEW
```javascript
const h=[],s=new Set();
document.querySelectorAll('h2 a,h3 a,[class*="PagePromo"] a').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>25&&t.length<250&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'AP News',source_url:u})}});
JSON.stringify({source:'AP News',count:h.length,articles:h});
```

### MarketWatch (https://www.marketwatch.com/latest-news)
```javascript
const h=[],s=new Set();
document.querySelectorAll('a[href*="/story/"]').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>20&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'MarketWatch',source_url:u})}});
JSON.stringify({source:'MarketWatch',count:h.length,articles:h});
```

### Decrypt (https://decrypt.co/news)
```javascript
const h=[],s=new Set();
document.querySelectorAll('h2 a,h3 a,h4 a,[class*="article"] a,[class*="post"] a,[class*="card"] a').forEach(a=>{const t=a.textContent.trim().replace(/\s+/g,' ');const u=a.href||'';if(t.length>25&&t.length<250&&!s.has(t.toLowerCase())){s.add(t.toLowerCase());h.push({title:t.substring(0,200),source:'Decrypt',source_url:u})}});
JSON.stringify({source:'Decrypt',count:h.length,articles:h});
```

## Detail Extraction (for top articles)

After collecting headlines, navigate to 3 top crypto article URLs and extract:
```javascript
const title = document.querySelector('h1')?.textContent?.trim() || '';
const paras = [];
document.querySelectorAll('article p, .post-content p, [class*="article"] p').forEach(p => {
    const t = p.textContent.trim();
    if (t.length > 30) paras.push(t.substring(0, 500));
});
const description = paras.slice(0, 4).join(' ');
JSON.stringify({title, source: document.location.hostname, source_url: document.location.href, description});
```

## Cookie Banners
Click "Reject All" or "Manage Preferences" then reject. Never click "Accept".

## Output Format
```json
{
  "scraped_at": "<ISO timestamp>",
  "total_articles": 0,
  "sources": {"CoinDesk": 0, "CoinTelegraph": 0, "The Block": 0, "Reuters": 0, "CNBC": 0, "AP News": 0, "MarketWatch": 0, "Decrypt": 0},
  "articles": [{"title": "...", "source": "...", "source_url": "..."}],
  "detailed_articles": [{"title": "...", "source": "...", "source_url": "...", "description": "first 3-4 paragraphs joined"}]
}
```

Write the file using the Write tool to `/Users/daniil/Projects/crypto-investment-bot/data/chrome-scraped.json`.
