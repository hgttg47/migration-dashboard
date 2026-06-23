#!/usr/bin/env python3
"""
Migration Research Dashboard Generator
Fetches data from free public sources and generates a self-contained index.html.
"""

import feedparser
import requests
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import json, re, sys, html as html_module
from urllib.parse import quote_plus

# Optional: language detection + translation for Swiss sources
try:
    from langdetect import detect as detect_lang
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False

try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

TODAY    = datetime.now(timezone.utc)
DATE_STR = f"{TODAY.day} {TODAY.strftime('%B')} {TODAY.year}"
MAX_ITEMS = 9    # 9 per section → fills 3 rows of 3
DAYS_BACK = 60

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
    {'id':'rdd',       'label':'RDD',           'full':'Regression Discontinuity',      'color':'#2471A3','bg':'#EBF5FB',
     'rationale':'Article mentions a sharp threshold or eligibility cutoff. RDD can estimate the causal effect by comparing units just above and below it.',
     'patterns':[r'\b(quota|threshold|cutoff|cut-off|eligibility limit|age limit|legal cap|income limit)\b',
                 r'\b(just (above|below)|marginally (eligible|ineligible))\b',
                 r'\bsharp (rule|boundary|criterion|eligib)\b']},
    {'id':'did',       'label':'DiD',            'full':'Difference-in-Differences',    'color':'#1E8449','bg':'#EAFAF1',
     'rationale':'A policy change with a plausible comparison group and pre/post variation — classic DiD setup.',
     'patterns':[r'\b(before.?and.?after|pre.?reform|post.?reform|pre-policy|post-policy)\b',
                 r'\b(control (group|country|region)|comparison (group|country))\b',
                 r'\b(new (law|policy|regulation|directive)).{0,60}(effect|impact)\b',
                 r'\b(policy (reform|change|shift|introduction))\b']},
    {'id':'rct',       'label':'RCT',            'full':'Randomized Controlled Trial',  'color':'#C0392B','bg':'#FDEDEC',
     'rationale':'Suggests a pilot program or randomized allocation — potentially clean experimental variation.',
     'patterns':[r'\b(pilot (program|project|scheme))\b',
                 r'\b(lottery|randomly? (assign|select|allocat|distribut))\b',
                 r'\b(randomized (program|allocation|assignment))\b']},
    {'id':'staggered', 'label':'Staggered DiD',  'full':'Staggered Adoption',           'color':'#B7950B','bg':'#FEF9E7',
     'rationale':'Policy rollout varies across regions or time — staggered adoption design can exploit differential timing.',
     'patterns':[r'\b(phase[d]? (in|roll.?out|implementation))\b',
                 r'\b(staggered (rollout|adoption|implementation))\b',
                 r'\b(roll.?out (across|in) (different|multiple|various))\b']},
    {'id':'synthetic', 'label':'Synth. Control', 'full':'Synthetic Control Method',     'color':'#76448A','bg':'#F4ECF7',
     'rationale':'A unique or singular event without a natural comparison group — synthetic control can construct a counterfactual.',
     'patterns':[r'\b(counterfactual|synthetic control)\b',
                 r'\b(unique (case|event|shock|country))\b']},
    {'id':'event',     'label':'Event Study',    'full':'Event Study / ITS',            'color':'#148F77','bg':'#E8F8F5',
     'rationale':'A sudden or unexpected shock — event study or interrupted time series can trace the causal effect over time.',
     'patterns':[r'\b(sudden (surge|influx|crisis|shock|increase|drop))\b',
                 r'\b(unexpected (event|shock|crisis|change|influx))\b',
                 r'\b(crisis|conflict).{0,30}(trigger|caus|lead to).{0,30}(surge|influx|flow|wave)\b']},
]

TOP_JOURNALS = [
    'american political science review','apsr','american journal of political science','ajps',
    'journal of politics','international organization','world politics',
    'comparative political studies','political behavior','political analysis',
    'journal of conflict resolution','european journal of political research','ejpr',
    'american economic review','aer','quarterly journal of economics','qje',
    'journal of political economy','jpe','econometrica',
    'review of economic studies','restud','journal of the european economic association','jeea',
    'american economic journal','review of economics and statistics','restat',
    'economic journal','journal of development economics',
]

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_migration(text):
    return any(kw in text.lower() for kw in MIGRATION_KEYWORDS)

def match_designs(title, summary):
    text = (title + ' ' + summary).lower()
    return [d for d in DESIGNS if any(re.search(p, text, re.I) for p in d['patterns'])]

def clean_html(raw):
    return re.sub(r'<[^>]+>', '', raw or '').strip()

def clean_summary(text):
    """Remove photo credits and skip the first sentence."""
    if not text:
        return ''
    # Remove photo/image/credit attributions
    text = re.sub(r'\(?\s*(Photo|Image|Credit|Picture|AFP|Getty|Reuters|AP|iStock)[^\)]*\)?\.?', '', text, flags=re.I)
    text = re.sub(r'©[^.]*\.', '', text)
    text = text.strip()
    # Skip the first sentence
    match = re.search(r'[.!?]\s+', text)
    if match:
        text = text[match.end():].strip()
    return text[:350]

def parse_date(raw):
    """Parse RFC 2822 or ISO date strings into a readable format."""
    if not raw:
        return ''
    try:
        dt = parsedate_to_datetime(raw)
        return dt.strftime('%-d %b %Y')
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw[:19]).replace(tzinfo=timezone.utc)
        return dt.strftime('%-d %b %Y')
    except Exception:
        pass
    # Last resort: return first 10 chars (YYYY-MM-DD) if present
    return raw[:10] if len(raw) >= 10 else raw

def extract_image(entry):
    """Try to extract an image URL from an RSS entry."""
    # 1. media:content
    mc = getattr(entry, 'media_content', [])
    for m in mc:
        url = m.get('url', '')
        if url and any(ext in url.lower() for ext in ['.jpg','.jpeg','.png','.webp','.gif']):
            return url
    # 2. media:thumbnail
    mt = getattr(entry, 'media_thumbnail', [])
    for m in mt:
        url = m.get('url', '')
        if url:
            return url
    # 3. enclosures
    for enc in getattr(entry, 'enclosures', []):
        if 'image' in enc.get('type', '') or any(ext in enc.get('href','').lower() for ext in ['.jpg','.jpeg','.png','.webp']):
            return enc.get('href', '')
    # 4. scan HTML content for first <img>
    for field in ['content', 'summary', 'description']:
        raw = ''
        val = getattr(entry, field, None)
        if isinstance(val, list) and val:
            raw = val[0].get('value', '')
        elif isinstance(val, str):
            raw = val
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw, re.I)
        if m:
            url = m.group(1)
            if url.startswith('http'):
                return url
    return ''

def translate_title(title, source_lang):
    """Translate a title to English if a translator is available."""
    if not TRANSLATOR_AVAILABLE:
        return title
    try:
        translated = GoogleTranslator(source=source_lang, target='en').translate(title)
        return translated or title
    except Exception:
        return title

def process_swiss_title(title):
    """Detect language of Swiss title; translate if non-English."""
    if not LANGDETECT_AVAILABLE or not title:
        return title, None
    try:
        lang = detect_lang(title)
        if lang == 'de':
            translated = translate_title(title, 'de')
            return translated, 'German'
        elif lang == 'fr':
            translated = translate_title(title, 'fr')
            return translated, 'French'
    except Exception:
        pass
    return title, None

# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_rss(name, url, filter_kw=True, swiss=False):
    try:
        feed = feedparser.parse(url)
        items = []
        for e in feed.entries:
            title   = e.get('title', '').strip()
            raw_sum = clean_html(e.get('summary', e.get('description', '')))
            summary = clean_summary(raw_sum)
            link    = e.get('link', '')
            pub     = parse_date(e.get('published', e.get('updated', '')))
            img     = extract_image(e)

            if filter_kw and not is_migration(title + ' ' + raw_sum):
                continue

            lang_note = None
            if swiss:
                title, lang_note = process_swiss_title(title)

            items.append({
                'title': title, 'summary': summary, 'link': link,
                'published': pub, 'source': name, 'image': img,
                'lang_note': lang_note,
                'designs': match_designs(title, summary),
            })
            if len(items) >= MAX_ITEMS:
                break
        return items
    except Exception as ex:
        print(f'  ⚠ RSS {name}: {ex}', file=sys.stderr)
        return []


def fetch_gdelt(extra=''):
    q = f'(migration OR asylum OR refugee OR migrant OR immigration) {extra}'.strip()
    url = (f'https://api.gdeltproject.org/api/v2/doc/doc?'
           f'query={quote_plus(q)}&mode=ArtList&maxrecords=15&timespan=1d&format=json&sort=DateDesc')
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        items = []
        for a in r.json().get('articles', [])[:MAX_ITEMS]:
            title = a.get('title', '')
            items.append({
                'title': title, 'summary': '', 'link': a.get('url', ''),
                'published': parse_date(str(a.get('seendatetime', ''))[:8]),
                'source': a.get('domain', 'GDELT'), 'image': '',
                'lang_note': None,
                'designs': match_designs(title, ''),
            })
        return items
    except Exception as ex:
        print(f'  ⚠ GDELT: {ex}', file=sys.stderr)
        return []


def fetch_semantic_scholar():
    url = ('https://api.semanticscholar.org/graph/v1/paper/search'
           '?query=migration+asylum+refugees'
           '&fields=title,authors,year,venue,abstract,publicationDate,openAccessPdf'
           '&limit=100&sort=publicationDate:desc')
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
            if len(authors) > 3: a_str += ' et al.'
            pdf = (p.get('openAccessPdf') or {}).get('url', '')
            papers.append({
                'title': p.get('title',''), 'authors': a_str,
                'venue': p.get('venue',''), 'year': p.get('year',''),
                'abstract': (p.get('abstract') or '')[:350],
                'link': pdf or f"https://www.semanticscholar.org/paper/{p.get('paperId','')}",
                'published': parse_date(pub),
            })
        return papers
    except Exception as ex:
        print(f'  ⚠ Semantic Scholar: {ex}', file=sys.stderr)
        return []


def fetch_crossref():
    from_date = (TODAY - timedelta(days=DAYS_BACK)).strftime('%Y-%m-%d')
    url = (f'https://api.crossref.org/works?query=migration+asylum+refugees'
           f'&filter=from-pub-date:{from_date}&rows=100&sort=published&order=desc'
           f'&mailto=mimitrompette@hotmail.fr')
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
            a_str = ', '.join(f"{a.get('given','')} {a.get('family','')}".strip() for a in authors_raw[:3])
            if len(authors_raw) > 3: a_str += ' et al.'
            pub_parts = item.get('published', {}).get('date-parts', [['']])[0]
            abstract = clean_html(item.get('abstract', ''))[:350]
            papers.append({
                'title': title, 'authors': a_str, 'venue': container,
                'year': pub_parts[0] if pub_parts else '',
                'abstract': abstract, 'link': item.get('URL',''),
                'published': '-'.join(str(x) for x in pub_parts if x),
            })
        return papers
    except Exception as ex:
        print(f'  ⚠ CrossRef: {ex}', file=sys.stderr)
        return []


def fetch_polymarket():
    keywords = ['migrat','asylum','refugee','border','immigr','deportat']
    try:
        r = requests.get('https://gamma-api.polymarket.com/markets?active=true&limit=200', timeout=15)
        r.raise_for_status()
        results = []
        for m in r.json():
            q = m.get('question','') or m.get('title','')
            if not any(kw in q.lower() for kw in keywords):
                continue
            prob = None
            try:
                prices = json.loads(m.get('outcomePrices','[]'))
                if prices: prob = round(float(prices[0])*100, 1)
            except Exception:
                pass
            vol = m.get('volume','')
            try: vol_fmt = f"${float(vol):,.0f}" if vol else ''
            except: vol_fmt = ''
            results.append({'title':q,'probability':prob,
                'end_date':(m.get('endDate','') or '')[:10],
                'volume':vol_fmt,'source':'Polymarket',
                'link':f"https://polymarket.com/event/{m.get('slug','')}"})
        return results
    except Exception as ex:
        print(f'  ⚠ Polymarket: {ex}', file=sys.stderr)
        return []


def fetch_kalshi():
    keywords = ['migrat','asylum','refugee','border','immigr','deportat']
    try:
        r = requests.get('https://trading-api.kalshi.com/trade-api/v2/markets?status=open&limit=1000',
            timeout=15, headers={'Accept':'application/json'})
        r.raise_for_status()
        results = []
        for m in r.json().get('markets',[]):
            title = m.get('title','')
            if not any(kw in title.lower() for kw in keywords):
                continue
            prob = m.get('last_price')
            if prob is not None: prob = round(prob*100,1)
            vol = m.get('volume','')
            try: vol_fmt = f"${float(vol):,.0f}" if vol else ''
            except: vol_fmt = ''
            results.append({'title':title,'probability':prob,
                'end_date':(m.get('close_time','') or '')[:10],
                'volume':vol_fmt,'source':'Kalshi',
                'link':f"https://kalshi.com/markets/{m.get('ticker','')}"})
        return results
    except Exception as ex:
        print(f'  ⚠ Kalshi: {ex}', file=sys.stderr)
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# HTML RENDERING
# ═══════════════════════════════════════════════════════════════════════════════

def esc(s): return html_module.escape(str(s))

def render_badge(d):
    return (f'<span class="badge" style="background:{d["bg"]};color:{d["color"]};'
            f'border:1px solid {d["color"]}40" title="{esc(d["full"])}">{esc(d["label"])}</span>')

def render_article(item):
    title    = esc(item.get('title','Untitled'))
    summary  = esc(item.get('summary',''))
    source   = esc(item.get('source',''))
    link     = item.get('link','#')
    pub      = esc(item.get('published',''))
    img_url  = item.get('image','')
    lang_note= item.get('lang_note')
    badges   = ''.join(render_badge(d) for d in item.get('designs',[]))

    img_html  = f'<a href="{link}" target="_blank" rel="noopener"><img class="card-img" src="{esc(img_url)}" alt="" loading="lazy" onerror="this.style.display=\'none\'"></a>' if img_url else ''
    sum_html  = f'<p class="summary">{summary}</p>' if summary else ''
    bdg_html  = f'<div class="badges">{badges}</div>' if badges else ''
    lang_html = f' <span class="lang-tag">({lang_note})</span>' if lang_note else ''
    meta      = ' · '.join(x for x in [source, pub] if x)

    return f'''<article class="card">
  {img_html}
  <div class="card-body">
    <a href="{link}" target="_blank" rel="noopener"><h3>{title}{lang_html}</h3></a>
    {sum_html}
    <div class="meta">{meta}</div>
    {bdg_html}
  </div>
</article>'''


def render_paper(p):
    title   = esc(p.get('title','Untitled'))
    authors = esc(p.get('authors',''))
    venue   = esc(p.get('venue',''))
    year    = esc(str(p.get('year','')))
    abstract= esc(p.get('abstract',''))
    link    = p.get('link','#')
    vy      = ' · '.join(x for x in [venue, year] if x)
    abs_html= f'<p class="summary">{abstract}…</p>' if abstract else ''
    return f'''<article class="card paper-card">
  <div class="card-body">
    <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
    <div class="authors">{authors}</div>
    <div class="meta">{vy}</div>
    {abs_html}
  </div>
</article>'''


def render_market(m):
    title = esc(m.get('title',''))
    prob  = m.get('probability')
    end   = esc(m.get('end_date','') or '')
    vol   = esc(m.get('volume','') or '')
    src   = esc(m.get('source',''))
    link  = m.get('link','#')

    prob_html = ''
    if prob is not None:
        color = '#2D7D32' if prob >= 60 else '#C62828' if prob < 40 else '#E65100'
        prob_html = f'''<div class="prob-row">
  <div class="prob-bg"><div class="prob-bar" style="width:{int(prob)}%;background:{color}"></div></div>
  <span class="prob-val" style="color:{color}">{prob:.0f}%</span>
</div>'''
    meta = ' · '.join(x for x in [src, f'Closes {end}' if end else '', f'Vol {vol}' if vol else ''] if x)
    return f'''<article class="card">
  <div class="card-body">
    <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
    {prob_html}
    <div class="meta">{meta}</div>
  </div>
</article>'''


def render_rd_tab(all_news):
    flagged = [i for i in all_news if i.get('designs')]
    if not flagged:
        return '<p class="empty">No research design opportunities flagged today.</p>'
    parts = []
    for item in flagged:
        title   = esc(item.get('title',''))
        summary = esc(item.get('summary',''))
        source  = esc(item.get('source',''))
        link    = item.get('link','#')
        badges  = ''.join(render_badge(d) for d in item['designs'])
        reasons = ''.join(
            f'<li><strong>{esc(d["full"])}</strong> — {esc(d["rationale"])}</li>'
            for d in item['designs']
        )
        sum_html = f'<p class="summary">{summary}</p>' if summary else ''
        parts.append(f'''<article class="card rd-card">
  <div class="card-body">
    <a href="{link}" target="_blank" rel="noopener"><h3>{title}</h3></a>
    {sum_html}
    <div class="meta">Source: {source} — <a href="{link}" target="_blank" rel="noopener">↗ Read article</a></div>
    <div class="badges">{badges}</div>
    <ul class="rationale">{reasons}</ul>
  </div>
</article>''')
    return '\n'.join(parts)


def render_section(items, label):
    if not items:
        return f'<p class="empty">No {label} articles found today.</p>'
    cards = '\n'.join(render_article(i) for i in items)
    return f'<div class="news-grid">{cards}</div>'

# ═══════════════════════════════════════════════════════════════════════════════
# MONTHLY ARCHIVE (JavaScript-powered, queries GDELT client-side)
# ═══════════════════════════════════════════════════════════════════════════════

def render_archive_tab():
    """Generates month-picker buttons for last 24 months; GDELT fetch runs in browser JS."""
    now = TODAY
    months_html = ''
    for i in range(24):
        d = now.replace(day=1) - timedelta(days=i*28)
        # normalise to first of month
        d = d.replace(day=1)
        label = d.strftime('%b %Y')
        start = d.strftime('%Y%m%d') + '000000'
        # last day of month
        if d.month == 12:
            end_d = d.replace(year=d.year+1, month=1, day=1) - timedelta(days=1)
        else:
            end_d = d.replace(month=d.month+1, day=1) - timedelta(days=1)
        end = end_d.strftime('%Y%m%d') + '235959'
        months_html += f'<button class="month-btn" data-start="{start}" data-end="{end}">{esc(label)}</button>\n'

    return f'''<div class="archive-wrapper">
  <p class="archive-intro">Select a month to load the top migration headlines from GDELT's global media archive.</p>
  <div class="month-grid">{months_html}</div>
  <div id="archive-results">
    <p class="empty">Click a month above to load headlines.</p>
  </div>
</div>'''

ARCHIVE_JS = r"""
document.querySelectorAll('.month-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    document.querySelectorAll('.month-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const el = document.getElementById('archive-results');
    el.innerHTML = '<p class="empty">Loading…</p>';
    const start = btn.dataset.start;
    const end   = btn.dataset.end;
    const query = encodeURIComponent('(migration OR asylum OR refugee OR migrant OR immigration)');
    const url = `https://api.gdeltproject.org/api/v2/doc/doc?query=${query}&mode=ArtList&maxrecords=20&startdatetime=${start}&enddatetime=${end}&format=json&sort=DateDesc`;
    try {
      const res  = await fetch(url);
      const data = await res.json();
      const articles = data.articles || [];
      if (!articles.length) {
        el.innerHTML = '<p class="empty">No articles found for this month.</p>';
        return;
      }
      el.innerHTML = '<div class="news-grid">' + articles.map(a => `
        <article class="card">
          <div class="card-body">
            <a href="${a.url}" target="_blank" rel="noopener"><h3>${a.title || 'Untitled'}</h3></a>
            <div class="meta">${a.domain || ''} · ${(a.seendatetime||'').slice(0,8)}</div>
          </div>
        </article>`).join('') + '</div>';
    } catch(e) {
      el.innerHTML = '<p class="empty">Error loading articles. GDELT may be temporarily unavailable.</p>';
    }
  });
});
"""

# ═══════════════════════════════════════════════════════════════════════════════
# CSS + PAGE ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════════

CSS = """
/* ── Google Font (Inter — clean, Tortoise-style sans-serif) ── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  --bg:       #F5EFE8;   /* warm cream — land-stories.org */
  --card:     #FFFFFF;
  --border:   #E0D8CE;
  --text:     #1A1A1A;
  --muted:    #6B6560;
  --accent:   #2D4F00;   /* Tortoise Media dark green */
  --accent2:  #3D6B00;
  --radius:   4px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 14px; line-height: 1.6;
  background: var(--bg); color: var(--text);
}

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; color: var(--accent2); }

/* ── Header ── */
header {
  background: var(--accent); color: #fff;
  padding: 18px 32px; display: flex; align-items: baseline;
  gap: 14px; flex-wrap: wrap;
}
header h1 { font-size: 1.1rem; font-weight: 700; letter-spacing: .3px; color: #fff; }
header .date { font-size: .78rem; opacity: .8; }
header .stats { margin-left: auto; font-size: .75rem; opacity: .7; }

/* ── Tabs ── */
nav.tabs {
  background: #fff; border-bottom: 1px solid var(--border);
  padding: 0 28px; display: flex; overflow-x: auto;
}
.tab {
  background: none; border: none; border-bottom: 2px solid transparent;
  padding: 11px 15px; cursor: pointer; font-size: .82rem;
  color: var(--muted); font-family: inherit; white-space: nowrap;
  font-weight: 500; transition: color .15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

/* ── Content ── */
main { max-width: 1080px; margin: 0 auto; padding: 22px 20px; }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* ── News grid: 3 per row ── */
.news-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
}
@media (max-width: 800px) { .news-grid { grid-template-columns: repeat(2,1fr); } }
@media (max-width: 520px) { .news-grid { grid-template-columns: 1fr; } }

/* ── Cards ── */
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); overflow: hidden;
  display: flex; flex-direction: column;
}
.card-img {
  width: 100%; height: 160px; object-fit: cover; display: block;
}
.card-body { padding: 12px 14px; flex: 1; display: flex; flex-direction: column; gap: 6px; }
.card h3 { font-size: .88rem; font-weight: 600; line-height: 1.35; }
.card h3 a { color: var(--text); }
.card h3 a:hover { color: var(--accent); }
.summary { font-size: .79rem; color: #4A4540; flex: 1; }
.meta { font-size: .72rem; color: var(--muted); }
.authors { font-size: .78rem; color: #4A4540; }
.lang-tag { font-size: .7rem; color: var(--muted); font-style: italic; font-weight: 400; }

/* ── Paper cards (single column) ── */
.paper-card { display: block; margin-bottom: 10px; }

/* ── Badges ── */
.badges { display: flex; flex-wrap: wrap; gap: 4px; }
.badge { font-size: .67rem; font-weight: 700; padding: 2px 6px; border-radius: 20px; white-space: nowrap; }

/* ── Research Design tab ── */
.rd-card { border-left: 3px solid #A0AEC0; margin-bottom: 10px; }
.rationale { margin-top: 8px; padding-left: 16px; font-size: .78rem; color: #4A4540; }
.rationale li { margin-bottom: 3px; }

/* ── Prediction Markets ── */
.prob-row { display: flex; align-items: center; gap: 10px; margin: 6px 0 2px; }
.prob-bg { flex:1; height:5px; background:#E0D8CE; border-radius:3px; overflow:hidden; }
.prob-bar { height:100%; border-radius:3px; }
.prob-val { font-size:.82rem; font-weight:700; min-width:34px; }

/* ── Monthly Archive ── */
.archive-intro { font-size:.82rem; color:var(--muted); margin-bottom:14px; }
.month-grid { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:20px; }
.month-btn {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 3px; padding: 5px 12px; cursor: pointer;
  font-size: .78rem; font-family: inherit; color: var(--text);
  transition: background .15s, border-color .15s;
}
.month-btn:hover, .month-btn.active {
  background: var(--accent); color: #fff; border-color: var(--accent);
}

/* ── Empty ── */
.empty { color: var(--muted); font-size: .85rem; padding: 18px 0; }
"""

TAB_JS = """
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
  });
});
"""


def generate_html(eu, swiss, global_news, papers, markets):
    all_news = eu + swiss + global_news
    flagged  = sum(1 for i in all_news if i.get('designs'))

    seen, unique_papers = set(), []
    for p in papers:
        t = p.get('title','').lower().strip()
        if t and t not in seen:
            seen.add(t)
            unique_papers.append(p)

    total = len(all_news) + len(unique_papers) + len(markets)
    stats = f'{total} items · {flagged} research design flag{"s" if flagged!=1 else ""}'

    eu_html      = render_section(eu, 'EU migration')
    swiss_html   = render_section(swiss, 'Swiss migration')
    global_html  = render_section(global_news, 'global migration')
    papers_html  = ('\n'.join(render_paper(p) for p in unique_papers)
                    if unique_papers else
                    '<p class="empty">No recent papers found in target journals.</p>')
    rd_html      = render_rd_tab(all_news)
    markets_html = ('\n'.join(render_market(m) for m in markets)
                    if markets else
                    '<p class="empty">No migration-related prediction markets found today.</p>')
    archive_html = render_archive_tab()

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
  <button class="tab" data-tab="archive">Monthly Archive</button>
</nav>
<main>
  <section id="eu"      class="tab-content active">{eu_html}</section>
  <section id="swiss"   class="tab-content">{swiss_html}</section>
  <section id="global"  class="tab-content">{global_html}</section>
  <section id="papers"  class="tab-content">{papers_html}</section>
  <section id="rd"      class="tab-content">{rd_html}</section>
  <section id="markets" class="tab-content">{markets_html}</section>
  <section id="archive" class="tab-content">{archive_html}</section>
</main>
<script>{TAB_JS}</script>
<script>{ARCHIVE_JS}</script>
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
        print(f'  {name}…')
        eu.extend(parse_rss(name, url, filter_kw=True, swiss=False))
    if len(eu) < 3:
        print('  GDELT (EU supplement)…')
        eu.extend(fetch_gdelt('Europe'))

    print('\n[2/5] Swiss migration news')
    swiss = []
    for name, url in SWISS_FEEDS:
        print(f'  {name}…')
        swiss.extend(parse_rss(name, url, filter_kw=True, swiss=True))
    if len(swiss) < 3:
        print('  GDELT (Swiss supplement)…')
        swiss.extend(fetch_gdelt('Switzerland OR Swiss'))

    print('\n[3/5] Global migration news')
    global_news = []
    for name, url in GLOBAL_FEEDS:
        print(f'  {name}…')
        global_news.extend(parse_rss(name, url, filter_kw=False))
    print('  GDELT (global)…')
    global_news.extend(fetch_gdelt())

    print('\n[4/5] Academic papers')
    papers = fetch_semantic_scholar() + fetch_crossref()
    print(f'  {len(papers)} papers in target journals')

    print('\n[5/5] Prediction markets')
    markets = fetch_polymarket() + fetch_kalshi()
    print(f'  {len(markets)} migration markets found')

    print('\nGenerating index.html…')
    page = generate_html(eu, swiss, global_news, papers, markets)
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(page)
    print(f'Done → index.html ({len(page):,} bytes)')
