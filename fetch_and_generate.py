#!/usr/bin/env python3
"""
Migration Research Dashboard Generator
Fetches data from free public sources and generates a self-contained index.html.

Sources:
  News    — RSS feeds (InfoMigrants, MPI, SWI, NZZ, RTS, SRF, TSRI, UNHCR, IOM)
            + GDELT API as fallback / supplement
  Papers  — Semantic Scholar API + CrossRef API
  Markets — Polymarket public API + Kalshi public API
"""

import feedparser
import requests
from datetime import datetime, timezone, timedelta
import json, re, sys, html as html_module
from urllib.parse import quote_plus

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

TODAY      = datetime.now(timezone.utc)
DATE_STR   = f"{TODAY.day} {TODAY.strftime('%B')} {TODAY.year}"   # e.g. "23 June 2026"
MAX_ITEMS  = 8    # max articles kept per source
DAYS_BACK  = 60   # look-back window for academic papers

MIGRATION_KEYWORDS = [
    'migrat', 'asylum', 'refugee', 'border control', 'border crossin',
    'immigr', 'displac', 'migrant', 'stateless', 'deportat',
    'undocumented', 'irregular arrival', 'asylum seeker',
]

# ── RSS Sources ────────────────────────────────────────────────────────────────

EU_FEEDS = [
    ('InfoMigrants',               'https://www.infomigrants.net/en/rss'),
    ('Migration Policy Institute', 'https://www.migrationpolicy.org/rss.xml'),
    ('EU Parliament',              'https://www.europarl.europa.eu/rss/en/pressreleases/index.xml'),
    ('EU Home Affairs',            'https://home-affairs.ec.europa.eu/system/files/rss.xml'),
]

SWISS_FEEDS = [
    ('SWI swissinfo.ch', 'https://www.swissinfo.ch/eng/rss/top_stories'),
    ('NZZ (English)',    'https://www.nzz.ch/international.rss'),
    ('NZZ (German)',     'https://www.nzz.ch/schweiz.rss'),
    ('RTS.ch',           'https://www.rts.ch/rss/info/index.rss'),
    ('SRF.ch',           'https://www.srf.ch/news/bnf/rss/1646'),
    ('TSRI',             'https://tsri.ch/feed/'),
]

GLOBAL_FEEDS = [
    ('UNHCR', 'https://www.unhcr.org/rss.xml'),
    ('IOM',   'https://www.iom.int/rss.xml'),
]

# ── Research Design Patterns ───────────────────────────────────────────────────

DESIGNS = [
    {
        'id': 'rdd',
        'label': 'RDD',
        'full': 'Regression Discontinuity',
        'color': '#2471A3', 'bg': '#EBF5FB',
        'patterns': [
            r'\b(quota|threshold|cutoff|cut-off|eligibility limit|age limit|legal cap|income limit)\b',
            r'\b(just (above|below)|marginally (eligible|ineligible))\b',
            r'\bsharp (rule|boundary|criterion|eligib)\b',
        ],
        'rationale': (
            'Article mentions a sharp threshold or eligibility cutoff. '
            'RDD can estimate the causal effect by comparing units just above and below it.'
        ),
    },
    {
        'id': 'did',
        'label': 'DiD',
        'full': 'Difference-in-Differences',
        'color': '#1E8449', 'bg': '#EAFAF1',
        'patterns': [
            r'\b(before.?and.?after|pre.?reform|post.?reform|pre-policy|post-policy)\b',
            r'\b(control (group|country|region)|comparison (group|country))\b',
            r'\b(new (law|policy|regulation|directive)).{0,60}(effect|impact)\b',
            r'\b(policy (reform|change|shift|introduction))\b',
        ],
        'rationale': (
            'A policy change with a plausible comparison group and pre/post variation — '
            'classic DiD setup.'
        ),
    },
    {
        'id': 'rct',
        'label': 'RCT',
        'full': 'Randomized Controlled Trial',
        'color': '#C0392B', 'bg': '#FDEDEC',
        'patterns': [
            r'\b(pilot (program|project|scheme))\b',
            r'\b(lottery|randomly? (assign|select|allocat|distribut))\b',
            r'\b(randomized (program|allocation|assignment))\b',
            r'\b(experimental (program|approach|design))\b',
        ],
        'rationale': (
            'Suggests a pilot program or randomized allocation — '
            'potentially clean experimental variation.'
        ),
    },
    {
        'id': 'staggered',
        'label': 'Staggered DiD',
        'full': 'Staggered Adoption',
        'color': '#B7950B', 'bg': '#FEF9E7',
        'patterns': [
            r'\b(phase[d]? (in|roll.?out|implementation))\b',
            r'\b(staggered (rollout|adoption|implementation))\b',
            r'\b(roll.?out (across|in) (different|multiple|various))\b',
            r'\b(different (cantons?|states?|regions?|countries?).{0,40}(different|varying).{0,20}(time|date|period))\b',
        ],
        'rationale': (
            'Policy rollout varies across regions or time — '
            'staggered adoption design can exploit differential timing.'
        ),
    },
    {
        'id': 'synthetic',
        'label': 'Synth. Control',
        'full': 'Synthetic Control Method',
        'color': '#76448A', 'bg': '#F4ECF7',
        'patterns': [
            r'\b(counterfactual|synthetic control)\b',
            r'\b(unique (case|event|shock|country))\b',
        ],
        'rationale': (
            'A unique or singular event without a natural comparison group — '
            'synthetic control can construct a counterfactual.'
        ),
    },
    {
        'id': 'event',
        'label': 'Event Study',
        'full': 'Event Study / Interrupted Time Series',
        'color': '#148F77', 'bg': '#E8F8F5',
        'patterns': [
            r'\b(sudden (surge|influx|crisis|shock|increase|drop))\b',
            r'\b(unexpected (event|shock|crisis|change|influx))\b',
            r'\b(crisis|conflict).{0,30}(trigger|caus|lead to).{0,30}(surge|influx|flow|wave)\b',
        ],
        'rationale': (
            'A sudden or unexpected shock — '
            'event study or interrupted time series can trace the causal effect over time.'
        ),
    },
]

# ── Target Journals ────────────────────────────────────────────────────────────

TOP_JOURNALS = [
    # Political Science
    'american political science review', 'apsr',
    'american journal of political science', 'ajps',
    'journal of politics',
    'international organization',
    'world politics',
    'comparative political studies',
    'political behavior',
    'political analysis',
    'journal of conflict resolution',
    'european journal of political research', 'ejpr',
    # Economics
    'american economic review', 'aer',
    'quarterly journal of economics', 'qje',
    'journal of political economy', 'jpe',
    'econometrica',
    'review of economic studies', 'restud',
    'journal of the european economic association', 'jeea',
    'american economic journal',
    'review of economics and statistics', 'restat',
    'economic journal',
    'journal of development economics',
]

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_migration(text):
    t = text.lower()
    return any(kw in t for kw in MIGRATION_KEYWORDS)

def match_designs(title, summary):
    text = (title + ' ' + summary).lower()
    return [d for d in DESIGNS if any(re.search(p, text, re.I) for p in d['patterns'])]

def clean_html(raw):
    return re.sub(r'<[^>]+>', '', raw).strip()

# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_rss(name, url, filter_kw=True):
    """Parse an RSS feed and return a list of article dicts."""
    try:
        feed = feedparser.parse(url)
        items = []
        for e in feed.entries:
            title   = e.get('title', '').strip()
            summary = clean_html(e.get('summary', e.get('description', '')))[:400]
            link    = e.get('link', '')
            pub     = e.get('published', e.get('updated', ''))
            if filter_kw and not is_migration(title + ' ' + summary):
                continue
            items.append({
                'title': title, 'summary': summary, 'link': link,
                'published': pub[:16].replace('T', ' '),
                'source': name,
                'designs': match_designs(title, summary),
            })
            if len(items) >= MAX_ITEMS:
                break
        return items
    except Exception as ex:
        print(f'  ⚠ RSS {name}: {ex}', file=sys.stderr)
        return []


def fetch_gdelt(extra_terms=''):
    """Fetch migration news from GDELT Doc 2.0 API (last 24h)."""
    base = 'migration OR asylum OR refugee OR migrant OR immigration'
    query = f'({base}) {extra_terms}'.strip()
    url = (
        'https://api.gdeltproject.org/api/v2/doc/doc?'
        f'query={quote_plus(query)}'
        '&mode=ArtList&maxrecords=15&timespan=1d&format=json&sort=DateDesc'
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        articles = r.json().get('articles', [])
        items = []
        for a in articles[:MAX_ITEMS]:
            title = a.get('title', '')
            items.append({
                'title': title, 'summary': '',
                'link': a.get('url', ''),
                'published': str(a.get('seendatetime', ''))[:16].replace('T', ' '),
                'source': a.get('domain', 'GDELT'),
                'designs': match_designs(title, ''),
            })
        return items
    except Exception as ex:
        print(f'  ⚠ GDELT: {ex}', file=sys.stderr)
        return []


def fetch_semantic_scholar():
    """Fetch recent migration papers from Semantic Scholar, filtered to target journals."""
    url = (
        'https://api.semanticscholar.org/graph/v1/paper/search'
        '?query=migration+asylum+refugees'
        '&fields=title,authors,year,venue,abstract,publicationDate,openAccessPdf'
        '&limit=100&sort=publicationDate:desc'
    )
    cutoff = TODAY - timedelta(days=DAYS_BACK)
    try:
        r = requests.get(url, timeout=20, headers={'User-Agent': 'migration-dashboard/1.0'})
        r.raise_for_status()
        papers = []
        for p in r.json().get('data', []):
            venue = (p.get('venue') or '').lower()
            if not any(j in venue for j in TOP_JOURNALS):
                continue
            pub = p.get('publicationDate', '')
            if pub:
                try:
                    if datetime.fromisoformat(pub).replace(tzinfo=timezone.utc) < cutoff:
                        continue
                except Exception:
                    pass
            authors = p.get('authors', [])
            a_str = ', '.join(a['name'] for a in authors[:3])
            if len(authors) > 3:
                a_str += ' et al.'
            pdf = (p.get('openAccessPdf') or {}).get('url', '')
            papers.append({
                'title': p.get('title', ''),
                'authors': a_str,
                'venue': p.get('venue', ''),
                'year': p.get('year', ''),
                'abstract': (p.get('abstract') or '')[:350],
                'link': pdf or f"https://www.semanticscholar.org/paper/{p.get('paperId','')}",
                'published': pub,
            })
        return papers
    except Exception as ex:
        print(f'  ⚠ Semantic Scholar: {ex}', file=sys.stderr)
        return []


def fetch_crossref():
    """Fetch recent migration papers from CrossRef, filtered to target journals."""
    from_date = (TODAY - timedelta(days=DAYS_BACK)).strftime('%Y-%m-%d')
    url = (
        'https://api.crossref.org/works'
        '?query=migration+asylum+refugees'
        f'&filter=from-pub-date:{from_date}'
        '&rows=100&sort=published&order=desc'
        '&mailto=mimitrompette@hotmail.fr'
    )
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        papers = []
        for item in r.json().get('message', {}).get('items', []):
            container = (item.get('container-title') or [''])[0]
            if not any(j in container.lower() for j in TOP_JOURNALS):
                continue
            title = (item.get('title') or [''])[0]
            authors_raw = item.get('author', [])
            a_str = ', '.join(
                f"{a.get('given','')} {a.get('family','')}".strip()
                for a in authors_raw[:3]
            )
            if len(authors_raw) > 3:
                a_str += ' et al.'
            pub_parts = item.get('published', {}).get('date-parts', [['']])[0]
            abstract = clean_html(item.get('abstract', ''))[:350]
            papers.append({
                'title': title,
                'authors': a_str,
                'venue': container,
                'year': pub_parts[0] if pub_parts else '',
                'abstract': abstract,
                'link': item.get('URL', ''),
                'published': '-'.join(str(x) for x in pub_parts if x),
            })
        return papers
    except Exception as ex:
        print(f'  ⚠ CrossRef: {ex}', file=sys.stderr)
        return []


def fetch_polymarket():
    """Fetch migration-related prediction markets from Polymarket."""
    keywords = ['migrat', 'asylum', 'refugee', 'border', 'immigr', 'deportat']
    try:
        r = requests.get(
            'https://gamma-api.polymarket.com/markets?active=true&limit=200',
            timeout=15
        )
        r.raise_for_status()
        results = []
        for m in r.json():
            question = m.get('question', '') or m.get('title', '')
            if not any(kw in question.lower() for kw in keywords):
                continue
            prob = None
            try:
                prices = json.loads(m.get('outcomePrices', '[]'))
                if prices:
                    prob = round(float(prices[0]) * 100, 1)
            except Exception:
                pass
            vol = m.get('volume', '')
            try:
                vol_fmt = f"${float(vol):,.0f}" if vol else ''
            except Exception:
                vol_fmt = ''
            results.append({
                'title': question,
                'probability': prob,
                'end_date': (m.get('endDate', '') or '')[:10],
                'volume': vol_fmt,
                'link': f"https://polymarket.com/event/{m.get('slug', '')}",
                'source': 'Polymarket',
            })
        return results
    except Exception as ex:
        print(f'  ⚠ Polymarket: {ex}', file=sys.stderr)
        return []


def fetch_kalshi():
    """Fetch migration-related prediction markets from Kalshi public API."""
    keywords = ['migrat', 'asylum', 'refugee', 'border', 'immigr', 'deportat']
    try:
        r = requests.get(
            'https://trading-api.kalshi.com/trade-api/v2/markets?status=open&limit=1000',
            timeout=15,
            headers={'Accept': 'application/json'}
        )
        r.raise_for_status()
        results = []
        for m in r.json().get('markets', []):
            title = m.get('title', '')
            if not any(kw in title.lower() for kw in keywords):
                continue
            prob = m.get('last_price')
            if prob is not None:
                prob = round(prob * 100, 1)
            vol = m.get('volume', '')
            try:
                vol_fmt = f"${float(vol):,.0f}" if vol else ''
            except Exception:
                vol_fmt = ''
            results.append({
                'title': title,
                'probability': prob,
                'end_date': (m.get('close_time', '') or '')[:10],
                'volume': vol_fmt,
                'link': f"https://kalshi.com/markets/{m.get('ticker', '')}",
                'source': 'Kalshi',
            })
        return results
    except Exception as ex:
        print(f'  ⚠ Kalshi: {ex}', file=sys.stderr)
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# HTML RENDERING
# ═══════════════════════════════════════════════════════════════════════════════

def esc(s):
    return html_module.escape(str(s))

def render_badge(d):
    return (
        f'<span class="badge" '
        f'style="background:{d["bg"]};color:{d["color"]};border:1px solid {d["color"]}40" '
        f'title="{esc(d["full"])}">{esc(d["label"])}</span>'
    )

def render_article(item):
    title   = esc(item.get('title', 'Untitled'))
    summary = esc(item.get('summary', ''))
    source  = esc(item.get('source', ''))
    link    = item.get('link', '#')
    pub     = esc(item.get('published', ''))
    badges  = ''.join(render_badge(d) for d in item.get('designs', []))

    summary_html = f'<p class="summary">{summary}</p>' if summary else ''
    badges_html  = f'<div class="badges">{badges}</div>' if badges else ''
    meta_parts   = [p for p in [source, pub] if p]

    return f'''<article class="card">
  <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
  {summary_html}
  <div class="meta">{' · '.join(meta_parts)}</div>
  {badges_html}
</article>'''


def render_paper(p):
    title    = esc(p.get('title', 'Untitled'))
    authors  = esc(p.get('authors', ''))
    venue    = esc(p.get('venue', ''))
    year     = esc(str(p.get('year', '')))
    abstract = esc(p.get('abstract', ''))
    link     = p.get('link', '#')

    abstract_html = f'<p class="summary">{abstract}…</p>' if abstract else ''
    venue_year    = ' · '.join(x for x in [venue, year] if x)

    return f'''<article class="card">
  <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
  <div class="authors">{authors}</div>
  <div class="meta">{venue_year}</div>
  {abstract_html}
</article>'''


def render_market(m):
    title    = esc(m.get('title', ''))
    prob     = m.get('probability')
    end_date = esc(m.get('end_date', '') or '')
    volume   = esc(m.get('volume', '') or '')
    source   = esc(m.get('source', ''))
    link     = m.get('link', '#')

    prob_html = ''
    if prob is not None:
        color = '#27AE60' if prob >= 60 else '#E74C3C' if prob < 40 else '#E67E22'
        prob_html = f'''<div class="prob-row">
  <div class="prob-bg"><div class="prob-bar" style="width:{int(prob)}%;background:{color}"></div></div>
  <span class="prob-val" style="color:{color}">{prob:.0f}%</span>
</div>'''

    meta_parts = [source]
    if end_date:
        meta_parts.append(f'Closes {end_date}')
    if volume:
        meta_parts.append(f'Vol {volume}')

    return f'''<article class="card">
  <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
  {prob_html}
  <div class="meta">{' · '.join(meta_parts)}</div>
</article>'''


def render_rd_tab(all_news):
    """Dedicated Research Design tab: all flagged items with methodology reasoning."""
    flagged = [i for i in all_news if i.get('designs')]
    if not flagged:
        return '<p class="empty">No research design opportunities flagged today.</p>'
    parts = []
    for item in flagged:
        title   = esc(item.get('title', ''))
        summary = esc(item.get('summary', ''))
        source  = esc(item.get('source', ''))
        link    = item.get('link', '#')
        badges  = ''.join(render_badge(d) for d in item['designs'])
        reasons = ''.join(
            f'<li><strong>{esc(d["full"])}</strong> — {esc(d["rationale"])}</li>'
            for d in item['designs']
        )
        summary_html = f'<p class="summary">{summary}</p>' if summary else ''
        parts.append(f'''<article class="card rd-card">
  <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
  {summary_html}
  <div class="meta">Source: {source} — <a href="{link}" target="_blank" rel="noopener">↗ Read article</a></div>
  <div class="badges">{badges}</div>
  <ul class="rationale">{reasons}</ul>
</article>''')
    return '\n'.join(parts)


def render_section(items, label):
    if not items:
        return f'<p class="empty">No {label} articles found today.</p>'
    return '\n'.join(render_article(i) for i in items)

# ═══════════════════════════════════════════════════════════════════════════════
# HTML PAGE ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════════

CSS = '''
:root {
  --bg:      #F8F9FA;
  --card:    #FFFFFF;
  --border:  #E2E8F0;
  --text:    #1A202C;
  --muted:   #718096;
  --accent:  #2D3748;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 15px; line-height: 1.55;
  background: var(--bg); color: var(--text);
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Header ── */
header {
  background: #fff; border-bottom: 1px solid var(--border);
  padding: 18px 32px; display: flex; align-items: baseline; gap: 14px;
  flex-wrap: wrap;
}
header h1 { font-size: 1.15rem; font-weight: 700; letter-spacing: -.2px; }
header .date { color: var(--muted); font-size: .82rem; }
header .stats { margin-left: auto; color: var(--muted); font-size: .78rem; }

/* ── Tabs ── */
nav.tabs {
  background: #fff; border-bottom: 1px solid var(--border);
  padding: 0 28px; display: flex; overflow-x: auto; gap: 0;
}
.tab {
  background: none; border: none; border-bottom: 2px solid transparent;
  padding: 11px 16px; cursor: pointer; font-size: .86rem; color: var(--muted);
  font-family: inherit; white-space: nowrap; transition: color .15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

/* ── Content ── */
main { max-width: 820px; margin: 0 auto; padding: 22px 16px; }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* ── Cards ── */
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 6px; padding: 14px 16px; margin-bottom: 10px;
}
.card h3 { font-size: .93rem; font-weight: 600; line-height: 1.35; margin-bottom: 5px; }
.card h3 a { color: var(--text); }
.card h3 a:hover { color: #4A5568; }
.summary { font-size: .82rem; color: #4A5568; margin-bottom: 7px; }
.meta { font-size: .75rem; color: var(--muted); }
.authors { font-size: .8rem; color: #4A5568; margin-bottom: 3px; }

/* ── Badges ── */
.badges { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.badge {
  font-size: .69rem; font-weight: 700; padding: 2px 7px;
  border-radius: 20px; white-space: nowrap; letter-spacing: .2px;
}

/* ── Research Design tab ── */
.rd-card { border-left: 3px solid #CBD5E0; }
.rationale { margin-top: 9px; padding-left: 16px; font-size: .8rem; color: #4A5568; }
.rationale li { margin-bottom: 4px; }

/* ── Prediction Markets ── */
.prob-row { display: flex; align-items: center; gap: 10px; margin: 8px 0 4px; }
.prob-bg { flex: 1; height: 5px; background: #E2E8F0; border-radius: 3px; overflow: hidden; }
.prob-bar { height: 100%; border-radius: 3px; }
.prob-val { font-size: .84rem; font-weight: 700; min-width: 36px; }

/* ── Empty state ── */
.empty { color: var(--muted); font-size: .88rem; padding: 20px 0; }
'''

JS = '''
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});
'''


def generate_html(eu, swiss, global_news, papers, markets):
    all_news = eu + swiss + global_news
    flagged  = sum(1 for i in all_news if i.get('designs'))

    # Deduplicate papers by title
    seen, unique_papers = set(), []
    for p in papers:
        t = p.get('title', '').lower().strip()
        if t and t not in seen:
            seen.add(t)
            unique_papers.append(p)

    total = len(all_news) + len(unique_papers) + len(markets)
    stats = f'{total} items · {flagged} research design flag{"s" if flagged != 1 else ""}'

    eu_html      = render_section(eu,         'EU migration')
    swiss_html   = render_section(swiss,      'Swiss migration')
    global_html  = render_section(global_news,'global migration')
    papers_html  = ('\n'.join(render_paper(p) for p in unique_papers)
                    if unique_papers else
                    '<p class="empty">No recent papers found in target journals.</p>')
    rd_html      = render_rd_tab(all_news)
    markets_html = ('\n'.join(render_market(m) for m in markets)
                    if markets else
                    '<p class="empty">No migration-related prediction markets found today.</p>')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration Research Dashboard — {esc(DATE_STR)}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Migration Research Dashboard</h1>
  <span class="date">{esc(DATE_STR)}</span>
  <span class="stats">{esc(stats)}</span>
</header>
<nav class="tabs">
  <button class="tab active" data-tab="eu">EU Migration</button>
  <button class="tab" data-tab="swiss">Switzerland</button>
  <button class="tab" data-tab="global">Global</button>
  <button class="tab" data-tab="papers">Academic Papers</button>
  <button class="tab" data-tab="rd">Research Design</button>
  <button class="tab" data-tab="markets">Prediction Markets</button>
</nav>
<main>
  <section id="eu"      class="tab-content active">{eu_html}</section>
  <section id="swiss"   class="tab-content">{swiss_html}</section>
  <section id="global"  class="tab-content">{global_html}</section>
  <section id="papers"  class="tab-content">{papers_html}</section>
  <section id="rd"      class="tab-content">{rd_html}</section>
  <section id="markets" class="tab-content">{markets_html}</section>
</main>
<script>{JS}</script>
</body>
</html>'''

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print(f'Migration Dashboard — {DATE_STR}')
    print('=' * 50)

    print('\n[1/5] EU migration news')
    eu = []
    for name, url in EU_FEEDS:
        print(f'  Fetching {name}…')
        eu.extend(parse_rss(name, url, filter_kw=True))
    if len(eu) < 3:
        print('  Supplementing with GDELT (EU)…')
        eu.extend(fetch_gdelt('Europe'))

    print('\n[2/5] Swiss migration news')
    swiss = []
    for name, url in SWISS_FEEDS:
        print(f'  Fetching {name}…')
        swiss.extend(parse_rss(name, url, filter_kw=True))
    if len(swiss) < 3:
        print('  Supplementing with GDELT (Switzerland)…')
        swiss.extend(fetch_gdelt('Switzerland OR Swiss'))

    print('\n[3/5] Global migration news')
    global_news = []
    for name, url in GLOBAL_FEEDS:
        print(f'  Fetching {name}…')
        global_news.extend(parse_rss(name, url, filter_kw=False))
    print('  Fetching GDELT (global)…')
    global_news.extend(fetch_gdelt())

    print('\n[4/5] Academic papers')
    papers = fetch_semantic_scholar()
    papers += fetch_crossref()
    print(f'  {len(papers)} papers in target journals (last {DAYS_BACK} days)')

    print('\n[5/5] Prediction markets')
    markets = fetch_polymarket() + fetch_kalshi()
    print(f'  {len(markets)} migration-related markets found')

    print('\nGenerating index.html…')
    page = generate_html(eu, swiss, global_news, papers, markets)
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(page)
    print(f'Done → index.html ({len(page):,} bytes)')
