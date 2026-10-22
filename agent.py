#!/usr/bin/env python3
# Version 3.0.0
"""Vinted Deal-Agent für GitHub Actions.

Jeder Lauf (ca. alle 5–8 Minuten, siehe .github/workflows/agent.yml):
  1. liest neue Telegram-Befehle (/start, /stop, /status, /suche, /liste, /limit, /an, /aus, /hilfe)
  2. prüft für jede Suche ihren eigenen Zeitplan (Standard-Intervall, eigenes Intervall oder feste
     Wochentage + Uhrzeiten) und führt fällige Suchen aus
  3. schickt neue Deals mit Titelbild an Telegram
  4. speichert Zustand (state.json) und Einstellungen (settings.json)

Konfiguration:  watchlist.json (Suchen)  ·  settings.json (Agent an/aus, Standard-Intervall, Jetzt-suchen)
Lern-Suchen:    profiles.json (Beispiel-Anzeigen + gelerntes Profil, siehe learn.py)
Bearbeitet werden beide bequem über die Web-Oberfläche in docs/index.html (GitHub Pages).
Nur Python-Standardbibliothek. GitHub-Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, optional ANTHROPIC_API_KEY (Bildprüfung)
"""
import gzip, html, json, os, random, re, sys, time, uuid, urllib.error, urllib.parse, urllib.request, zlib
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import learn

BASE = os.environ.get('VINTED_DOMAIN', 'https://www.vinted.at')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = str(os.environ.get('TELEGRAM_CHAT_ID', '')).strip()
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'state.json')
WATCH_FILE = os.path.join(HERE, 'watchlist.json')
SETTINGS_FILE = os.path.join(HERE, 'settings.json')
PROFILES_FILE = os.path.join(HERE, 'profiles.json')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '').strip()
DRY_RUN = os.environ.get('DRY_RUN') == '1'

BUYER_FEE_FIXED, BUYER_FEE_PCT = 0.70, 0.05
CONDITION_RANK = {'Neu, mit Etikett': 5, 'Neu mit Etikett': 5, 'Neu': 4, 'Sehr gut': 3, 'Gut': 2, 'Zufriedenstellend': 1}
TZ = ZoneInfo('Europe/Vienna')
WEEKDAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
TOLERANCE_S = 3 * 60          # Läufe kommen ca. alle 5–8 Min.; so wird „stündlich“ nicht zu „alle 65 Min.“

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/129.0.0.0 Safari/537.36')

DEFAULT_SETTINGS = {'enabled': True, 'default_interval_min': 60, 'first_run_max': 3, 'shipping_est': 5.0,
                    'max_per_run': 15, 'force_all': 0, 'force': {}, 'vision_daily_max': 150, 'vision_min': 60,
                    'night_mode': False, 'night_max': 3, 'night_from': '', 'night_to': ''}

RUN_LOG = []

def log(*a):
    line = ' '.join(str(x) for x in a)
    RUN_LOG.append(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC ') + line)
    print('[agent]', line, flush=True)

# ---------------------------------------------------------------- Dateien
def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')

def default_state():
    return {'tg_offset': 0, 'seen': [], 'first_run_done': [], 'watch_last': {}, 'last_search': 0,
            'force_all_done': 0, 'force_done': {}, 'blocked_notified': False,
            'stats': {'searches': 0, 'deals': 0, 'last_deals': 0},
            'learn_target': '', 'learn_queue': [], 'bg': {}, 'sent_feat': {}, 'pending_cb': [],
            'vision': {'day': '', 'n': 0, 'usd': 0.0, 'total_usd': 0.0}}

def slug(s):
    s = re.sub(r'[^a-z0-9]+', '-', str(s).lower()).strip('-')
    return s[:40] or uuid.uuid4().hex[:8]

def migrate(state, settings, watch):
    """Alte Formate (v1) in v2 überführen. Gibt True zurück, wenn watchlist.json geändert wurde."""
    changed = False
    ids = set()
    for w in watch:
        if not w.get('id'):
            w['id'] = slug(w.get('name', ''))
            changed = True
        while w['id'] in ids:
            w['id'] += '-2'; changed = True
        ids.add(w['id'])
        if 'category' not in w:
            w['category'] = 'technik' if w.get('simFree') or w.get('storage') else 'kleidung'; changed = True
        if 'schedule' not in w:
            w['schedule'] = {'mode': 'default'}; changed = True
    # v1 hatte enabled/force_search im Zustand
    if 'enabled' in state:
        settings['enabled'] = bool(state.pop('enabled'))
    if state.pop('force_search', False):
        settings['force_all'] = int(time.time())
    # v1: first_run_done enthielt Namen
    names = {w.get('name'): w['id'] for w in watch}
    state['first_run_done'] = sorted({names.get(x, x) for x in state.get('first_run_done', [])})
    # v1: gesehene Artikel als „Name|ID“ → „SuchID|ID“
    seen = []
    for k in state.get('seen', []):
        k = str(k)
        if '|' in k:
            n, iid = k.rsplit('|', 1)
            k = f"{names.get(n, n)}|{iid}"
        seen.append(k)
    state['seen'] = seen
    # v1 kannte nur einen gemeinsamen Zeitpunkt
    if not state.get('watch_last') and state.get('last_search'):
        state['watch_last'] = {w['id']: int(state['last_search']) for w in watch}
    return changed

# ---------------------------------------------------------------- HTTP
class Blocked(Exception): pass

_cookies = {}

def http_get(url, headers=None, timeout=25):
    h = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
         'Accept-Language': 'de-AT,de;q=0.9,en;q=0.8', 'Accept-Encoding': 'gzip, deflate'}
    if _cookies:
        h['Cookie'] = '; '.join(f'{k}={v}' for k, v in _cookies.items())
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw, hdrs = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), e.headers
    for c in hdrs.get_all('Set-Cookie') or []:
        k, _, v = c.split(';', 1)[0].partition('=')
        if k:
            _cookies[k.strip()] = v.strip()
    enc = (hdrs.get('Content-Encoding') or '').lower()
    if enc == 'gzip':
        raw = gzip.decompress(raw)
    elif enc == 'deflate':
        raw = zlib.decompress(raw)
    return status, raw.decode('utf-8', 'replace')

def vinted_get(path):
    url = path if path.startswith('http') else BASE + path
    for attempt in range(3):
        status, text = http_get(url)
        if status == 429:
            log('429 – warte 60 s'); time.sleep(60); continue
        if '<title>Session refresh' in text[:5000] or 'session-refresh' in text[:3000]:
            log('Session refresh – Startseite laden und erneut'); http_get(BASE + '/'); time.sleep(3); continue
        if status == 403 or ('captcha-delivery' in text[:5000] and 'grid-item' not in text):
            raise Blocked(f'HTTP {status}')
        if status >= 400:
            raise RuntimeError(f'HTTP {status} für {url}')
        return text
    raise RuntimeError(f'Vinted antwortet nicht sauber: {url}')

# ---------------------------------------------------------------- Vinted parsen
CARD_TITLE_RE = re.compile(r'^(.*?), (?:Marke: (.*?), )?(?:Modell: .*?, )?Zustand: (.*?)(?:, Größe: (.*?))?, ([\d.,]+) €(?:, ([\d.,]+) €)?')

def num(s):
    if not s:
        return None
    s = re.sub(r',(?=\d{3}\b)', '', s).replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None

def parse_card_title(title):
    m = CARD_TITLE_RE.match(title or '')
    if not m:
        return None
    t, brand, cond, size, price, total = m.groups()
    return {'title': t, 'brand': brand or '', 'condition': cond, 'size': size or '', 'price': num(price), 'total': num(total)}

class _CatalogParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.imgs = {}, {}
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        tid = a.get('data-testid') or ''
        m = re.match(r'product-item-id-(\d+)--(overlay-link|image--img)$', tid)
        if not m:
            return
        iid, kind = m.groups()
        if kind == 'overlay-link' and tag == 'a':
            self.items[iid] = {'href': a.get('href', ''), 'title': a.get('title', '')}
        elif kind == 'image--img' and tag == 'img':
            self.imgs[iid] = a.get('src', '')

def parse_catalog(page):
    p = _CatalogParser(); p.feed(page)
    out = []
    for iid, d in p.items.items():
        info = parse_card_title(d['title'])
        if not info or info['price'] is None:
            continue
        href = d['href'].split('?')[0]
        out.append({'id': iid, 'url': urllib.parse.urljoin(BASE, href), 'thumb': p.imgs.get(iid, ''), **info})
    return out

VOID_TAGS = {'br', 'img', 'meta', 'link', 'input', 'hr', 'source', 'wbr'}

class _DescParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth, self.buf, self.og, self.done = 0, [], '', False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta' and a.get('property') == 'og:image' and not self.og:
            self.og = a.get('content', '')
        if self.depth:
            if tag == 'br':
                self.buf.append('\n')
            elif tag not in VOID_TAGS:
                self.depth += 1
        elif not self.done and a.get('itemprop') == 'description' and tag not in VOID_TAGS:
            self.depth = 1
    def handle_startendtag(self, tag, attrs):
        if self.depth and tag == 'br':
            self.buf.append('\n')
        elif tag == 'meta':
            self.handle_starttag(tag, attrs)
    def handle_endtag(self, tag):
        if self.depth and tag not in VOID_TAGS:
            self.depth -= 1
            if self.depth == 0:
                self.done = True
    def handle_data(self, data):
        if self.depth:
            self.buf.append(data)


ATTR_RE = re.compile(r'\\?"code\\?":\\?"([a-z_]+)\\?",\\?"data\\?":\{[^}]*?\\?"title\\?":\\?"([^"\\]*)\\?"[^}]*?\\?"value\\?":\\?"([^"\\]*)')

def fetch_details(item):
    page = vinted_get(urllib.parse.urlparse(item['url']).path)
    p = _DescParser(); p.feed(page)
    attrs = {}
    for code, title, value in ATTR_RE.findall(page):
        attrs.setdefault(code, value)
        attrs.setdefault('t:' + title.lower(), value)
    cm = CATEGORY_RE.search(page)
    return {'desc': ''.join(p.buf).strip(), 'image': p.og or item.get('thumb', ''), 'category': html.unescape(cm.group(1)) if cm else '',
            'uploaded': attrs.get('upload_date', ''), 'color': attrs.get('color', ''),
            'simlock': attrs.get('t:sim-lock', '')}

def search_url(query, w):
    q = {'search_text': query, 'order': 'newest_first', 'currency': 'EUR'}
    if w['maxPrice']:
        q['price_to'] = f"{w['maxPrice']:g}"
    if w['minPrice']:
        q['price_from'] = f"{w['minPrice']:g}"
    return '/catalog?' + urllib.parse.urlencode(q)

# ---------------------------------------------------------------- Suchen vorbereiten
def lst(s):
    if isinstance(s, list):
        return [str(x).strip() for x in s if str(x).strip()]
    return [x.strip() for x in str(s or '').split(',') if x.strip()]

def norm(s):
    return re.sub(r'\s+', ' ', re.sub(r'[’`´]', "'", str(s or '').lower())).strip()

def size_key(s):
    return norm(s).replace(',', '.').replace('eu ', '').strip()

def fnum(x, default=0.0):
    try:
        return float(str(x).replace(',', '.')) if str(x).strip() != '' else default
    except ValueError:
        return default

def storage_re(entry):
    m = re.match(r'\s*(\d+)\s*(gb|tb|go|to|g|t)?', norm(entry))
    if not m:
        return None
    n, unit = m.group(1), (m.group(2) or 'gb')
    u = r'(tb|to|t)' if unit.startswith('t') else r'(gb|go|g)'
    return re.compile(rf'(?<!\d){n}\s*{u}\b')

def compile_watch(w):
    rmin = fnum(w.get('resaleMin'))
    sched = w.get('schedule') or {'mode': 'default'}
    return {'id': w.get('id') or slug(w.get('name', '')), 'name': w.get('name') or 'Ohne Namen',
            'active': w.get('active', True) is not False, 'category': w.get('category') or 'kleidung',
            'queries': lst(w.get('queries')) or [w.get('name', '')],
            'brands': [norm(b) for b in lst(w.get('brands'))],
            'require': [norm(x) for x in lst(w.get('require'))], 'exclude': [norm(x) for x in lst(w.get('exclude'))],
            'minPrice': fnum(w.get('minPrice')), 'maxPrice': fnum(w.get('maxPrice')),
            'resale': [rmin, fnum(w.get('resaleMax'), rmin) or rmin],
            'sizes': [size_key(x) for x in lst(w.get('sizes'))], 'colors': [norm(x) for x in lst(w.get('colors'))],
            'storage': [r for r in (storage_re(x) for x in lst(w.get('storage'))) if r],
            'storage_txt': lst(w.get('storage')), 'simFree': bool(w.get('simFree')),
            'minCondition': w.get('minCondition') or 'Gut', 'schedule': sched,
            'type': w.get('type') or 'filter', 'vision': w.get('vision', True) is not False,
            'strictness': fnum(w.get('strictness'), 1.0) or 1.0, 'queriesManual': bool(w.get('queriesManual')),
            'excludeRules': lst(w.get('excludeRules'))}

# ---------------------------------------------------------------- Zeitpläne
def parse_hhmm(t):
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', str(t))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))

def _slots(sched, start_date, days_range):
    days = {int(d) for d in sched.get('days', []) if str(d).isdigit()}  # 1=Mo … 7=So
    times = [t for t in (parse_hhmm(x) for x in sched.get('times', [])) if t]
    for off in days_range:
        d = start_date + timedelta(days=off)
        if d.isoweekday() in days:
            for hh, mm in times:
                yield datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)

def latest_slot(sched, now):
    now_l = datetime.fromtimestamp(now, TZ)
    past = [s for s in _slots(sched, now_l.date(), range(-7, 1)) if s.timestamp() <= now]
    return max(past).timestamp() if past else None

def next_slot(sched, after):
    a = datetime.fromtimestamp(after, TZ)
    fut = [s for s in _slots(sched, a.date(), range(0, 9)) if s.timestamp() > after]
    return min(fut).timestamp() if fut else None

def interval_min(w, settings):
    s = w['schedule']
    if s.get('mode') == 'live':
        return 0
    if s.get('mode') == 'interval':
        return max(15, int(fnum(s.get('everyMin'), 60)))
    return max(15, int(fnum(settings.get('default_interval_min'), 60)))

def is_due(w, settings, state, now):
    last = state['watch_last'].get(w['id'], 0)
    s = w['schedule']
    if s.get('mode') == 'times':
        if not last:                       # neue Suche mit festen Zeiten: erst ab dem nächsten Termin
            state['watch_last'][w['id']] = now
            return False
        slot = latest_slot(s, now)
        return slot is not None and slot > last
    return now - last >= interval_min(w, settings) * 60 - TOLERANCE_S

def next_run_ts(w, settings, state, now):
    last = state['watch_last'].get(w['id'], 0)
    if w['schedule'].get('mode') == 'times':
        return next_slot(w['schedule'], max(last, now))
    return max(now, last + interval_min(w, settings) * 60)

def schedule_text(w, settings):
    s = w['schedule']
    if s.get('mode') == 'times':
        days = sorted({int(d) for d in s.get('days', []) if str(d).isdigit()})
        dtxt = 'täglich' if len(days) == 7 else ('Mo–Fr' if days == [1, 2, 3, 4, 5] else ', '.join(WEEKDAYS[d - 1] for d in days))
        times = ', '.join(t for t in s.get('times', []) if parse_hhmm(t))
        return f'{dtxt} {times}'.strip() or 'kein Termin'
    if s.get('mode') == 'live':
        return 'laufend (alle ~5 Min.)'
    m = interval_min(w, settings)
    txt = f'alle {m} Min.' if m < 60 or m % 60 else ('stündlich' if m == 60 else f'alle {m // 60} Std.')
    return txt + (' (Standard)' if s.get('mode') != 'interval' else '')

# ---------------------------------------------------------------- Filter
FAKE_RE = re.compile(r'\b(fake|replica|rep|dupe|imitat|nachgemacht|inspired)\b', re.I)
REWORK_RE = re.compile(r'\b(rework|reworked|patchwork|upcycl\w*|custom\w*)\b', re.I)
KIDSIZE_RE = re.compile(r'jahre|monate|\bans\b|\banni\b|years|months|\d+\s*cm\b', re.I)
SIMFREE_TXT = re.compile(r'sim.?lock.?frei|ohne sim.?lock|simlock free|unlocked|entsperrt|désimlock|desimlock|sbloccat|libre', re.I)
CLOTHING = {'kleidung', 'schuhe', 'taschen'}
COLOR_WORDS = {'schwarz': 'black noir nero negro', 'weiß': 'weiss white blanc bianco blanco', 'grau': 'grey gray gris grigio',
               'blau': 'blue bleu blu azul navy', 'rot': 'red rouge rosso rojo', 'grün': 'gruen green vert verde',
               'braun': 'brown marron brun marrone', 'beige': 'sand camel', 'creme': 'cream ecru offwhite',
               'gelb': 'yellow jaune giallo', 'orange': 'arancione', 'rosa': 'pink rose', 'pink': 'rosa',
               'lila': 'purple violet viola flieder', 'khaki': 'olive oliv', 'mehrfarbig': 'bunt multicolor multicolore',
               'silber': 'silver argent', 'gold': 'golden doré oro', 'türkis': 'turquoise teal petrol',
               'burgunderrot': 'bordeaux burgundy weinrot', 'marineblau': 'navy dunkelblau', 'hellblau': 'light blue babyblau'}

def color_ok(wanted, item_color, text):
    """„Blau“ passt auch zu Hellblau/Marineblau. Ohne Farbangabe am Artikel wird Titel + Beschreibung geprüft."""
    have = [norm(c) for c in lst(item_color)]
    if have:
        return any(c in h for c in wanted for h in have)
    words = set()
    for c in wanted:
        words.add(c); words.update(COLOR_WORDS.get(c, '').split())
    return any(re.search(r'(?<![a-zäöüß])' + re.escape(x) + r'\w*', text) for x in words if x)

def matches(w, it, details=None):
    """True = Treffer · False = verwerfen · None = erst Detailseite laden."""
    desc = (details or {}).get('desc', '')
    text = norm(f"{it['title']} {desc}")
    brand = norm(it['brand'])
    if w['maxPrice'] and it['price'] > w['maxPrice']:
        return False
    if w['minPrice'] and it['price'] < w['minPrice']:
        return False
    if w['brands']:
        if brand:
            if brand not in w['brands']:
                return False
        elif not any(b in text for b in w['brands']):
            return False
    if any(x in text for x in w['exclude']):
        return False
    if FAKE_RE.search(text) or REWORK_RE.search(text):
        return False
    if w['category'] in CLOTHING and KIDSIZE_RE.search(it['size']):
        return False
    if CONDITION_RANK.get(it['condition'], 0) < CONDITION_RANK.get(w['minCondition'], 0):
        return False
    if w['sizes']:
        parts = [size_key(x) for x in [it['size']] + it['size'].split('/')]
        if not any(p in w['sizes'] for p in parts):
            return False
    if details is None:
        if w['require'] or w['colors'] or w['storage'] or w['simFree']:
            return None
        return True
    if w['require'] and not any(x in text for x in w['require']):
        return False
    if w['storage'] and not any(r.search(text) for r in w['storage']):
        return False
    if w['colors'] and not color_ok(w['colors'], details.get('color'), text):
        return False
    if w['simFree']:
        sl = norm(details.get('simlock'))
        if sl != 'freigegeben' and not (not sl and SIMFREE_TXT.search(text)):
            return False
    return True

# ---------------------------------------------------------------- Telegram
def tg(method, params):
    if DRY_RUN:
        log('DRY tg', method, json.dumps(params, ensure_ascii=False)[:300]); return {'ok': True, 'result': []}
    data = json.dumps(params).encode()
    req = urllib.request.Request(f'https://api.telegram.org/bot{TOKEN}/{method}', data=data,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        log('Telegram-Fehler', method, e.code, body[:300])
        return {'ok': False, 'description': body}

def send(text):
    return tg('sendMessage', {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True})

def esc(s):
    return html.escape(str(s), quote=False)

def euro(n):
    return f'{n:.2f}'.replace('.', ',') + ' €'

def short_desc(d, n=220):
    d = re.sub(r'#\S+', '', re.sub(r'\s+', ' ', d)).strip()
    return d if len(d) <= n else d[:n - 1] + '…'

def build_message(w, it, shipping):
    cost = it['price'] + BUYER_FEE_FIXED + it['price'] * BUYER_FEE_PCT + shipping
    m1, m2 = w['resale'][0] - cost, w['resale'][1] - cost
    lines = [f"🔥 <b>Deal: {esc(w['name'])}</b>", f"<b>{esc(it['title'])}</b>",
             f"💶 <b>{euro(it['price'])}</b>" + (f" ({euro(it['total'])} inkl. Käuferschutz)" if it.get('total') else '')
             + (f" · dein Limit {euro(w['maxPrice'])}" if w['maxPrice'] else ''),
             '📏 ' + esc(' · '.join(x for x in (it['size'], it.get('color', ''), it['condition'], it['brand']) if x))]
    if it.get('simlock'):
        lines.append(f"📶 SIM-Lock: {esc(it['simlock'])}")
    if w['resale'][0]:
        lines.append(f"📈 Wiederverkauf ca. {w['resale'][0]:.0f}–{w['resale'][1]:.0f} € → Spanne ca. {m1:.0f} bis {m2:.0f} €")
    if it.get('uploaded'):
        lines.append(f"🕒 hochgeladen vor {esc(it['uploaded'])}")
    if it.get('desc'):
        lines.append('📝 ' + esc(short_desc(it['desc'])))
    lines.append(f"🔗 {it['url']}")
    return '\n'.join(lines)

def send_deal(w, it, shipping, extra=None, buttons=None):
    text = build_message(w, it, shipping)
    if extra:
        head, _, tail = text.partition('\n')
        text = head + '\n' + extra + '\n' + tail
    markup = {'reply_markup': {'inline_keyboard': buttons}} if buttons else {}
    img = it.get('image') or it.get('thumb')
    if img:
        r = tg('sendPhoto', {'chat_id': CHAT_ID, 'photo': img, 'caption': text[:1024], 'parse_mode': 'HTML', **markup})
        if r.get('ok'):
            return True
    return tg('sendMessage', {'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True, **markup}).get('ok', False)

# ---------------------------------------------------------------- Befehle
HELP = ("<b>Vinted Deal-Agent – Befehle</b>\n"
        "/start – Agent einschalten (sucht sofort)\n"
        "/stop – Agent pausieren\n"
        "/status – läuft er? letzte und nächste Suchen\n"
        "/suche – alle Suchen beim nächsten Lauf sofort ausführen\n"
        "/liste – alle Suchen mit Nummer, Max-Preis und Zeitplan\n"
        "/limit 2 90 – Max-Preis von Suche 2 auf 90 € setzen\n"
        "/aus 3 · /an 3 – Suche 3 aus- oder einschalten\n"
        "/hilfe – diese Übersicht\n\n"
        "<b>Lern-Suchen</b>\n"
        "/lernen Name 20 – Lern-Suche „Name“ (bis 20 €) anlegen oder wählen, danach einfach Vinted-Links schicken\n"
        "/fertig – Lernen beenden, Zusammenfassung anzeigen\n"
        "/profil – was die Lern-Suchen gelernt haben\n"
        "/nightmode – Nachtmodus: alle Suchen nur stündlich, höchstens 3 Artikel pro Stunde (/nightmode 5 für 5)\n"
        "/nightmode 23-7 – Nachtmodus jede Nacht automatisch 23–7 Uhr · /nightmode aus\n"
        "👍 / 👎 unter einem Treffer – er lernt daraus\n\n"
        "Suchen, Größen, Farben und Zeitpläne bequem einstellen: in der Web-Oberfläche.\n"
        "Befehle werden beim nächsten Lauf verarbeitet (meist nach wenigen Minuten).")

BOT_COMMANDS = [('start', 'Agent einschalten'), ('stop', 'Agent pausieren'), ('status', 'Status anzeigen'),
                ('suche', 'jetzt suchen'), ('liste', 'Suchen anzeigen'), ('limit', 'Max-Preis ändern: /limit 2 90'),
                ('an', 'Suche einschalten: /an 3'), ('aus', 'Suche ausschalten: /aus 3'),
                ('lernen', 'Lern-Suche: /lernen Name 20, dann Links schicken'), ('fertig', 'Lernen beenden'),
                ('profil', 'Gelerntes anzeigen'), ('nightmode', 'Nachtmodus: /nightmode, /nightmode 23-7, /nightmode aus'), ('hilfe', 'Hilfe')]

def fmt_time(ts):
    if not ts:
        return 'noch nie'
    return datetime.fromtimestamp(ts, TZ).strftime('%a %d.%m. %H:%M').replace('Mon', 'Mo').replace('Tue', 'Di') \
        .replace('Wed', 'Mi').replace('Thu', 'Do').replace('Fri', 'Fr').replace('Sat', 'Sa').replace('Sun', 'So')

def watch_list_text(raw, settings):
    rows = []
    for i, w in enumerate(raw, 1):
        c = compile_watch(w)
        flag = ('🧠' if c['type'] == 'lernen' else '✅') if c['active'] else '⏸'
        rows.append(f"{flag} <b>{i}</b> {esc(c['name'])} – bis {euro(c['maxPrice'])} · {esc(schedule_text(c, settings))}")
    return '<b>Deine Suchen</b>\n' + '\n'.join(rows) + '\n\nÄndern: /limit NR PREIS · /aus NR · /an NR'

def handle_commands(state, settings, raw_watch, now):
    """Telegram-Befehle ausführen. Gibt (watch_geändert, settings_geändert) zurück."""
    res = tg('getUpdates', {'offset': state['tg_offset'] + 1, 'timeout': 0, 'allowed_updates': ['message', 'callback_query']})
    if not res.get('ok', True):
        log('getUpdates fehlgeschlagen:', str(res.get('description'))[:200])
    log(f"Telegram: {len(res.get('result', []))} neue Nachricht(en)")
    wch = sch = False
    for u in res.get('result', []):
        state['tg_offset'] = max(state['tg_offset'], u['update_id'])
        cb = u.get('callback_query')
        if cb:
            if str((cb.get('message') or {}).get('chat', {}).get('id')) == CHAT_ID:
                state['pending_cb'].append({'id': cb.get('id'), 'data': cb.get('data', ''),
                                            'msg': (cb.get('message') or {}).get('message_id')})
            continue
        msg = u.get('message') or {}
        if str(msg.get('chat', {}).get('id')) != CHAT_ID:
            continue
        text = (msg.get('text') or msg.get('caption') or '').strip()
        links = find_links(text, msg)
        if links and not text.startswith('/'):
            wch |= queue_links(links, state, raw_watch)
            continue
        if not text.startswith('/'):
            continue
        cmd, *args = text.split()
        cmd = cmd.split('@')[0].lower()
        log('Befehl', cmd, args)
        if cmd == '/stop':
            settings['enabled'] = False; sch = True
            send('⏸ Agent pausiert. Mit /start geht es weiter.')
        elif cmd == '/start':
            settings['enabled'] = True; settings['force_all'] = int(now); sch = True
            send('▶️ Agent läuft. Alle Suchen starten jetzt.')
        elif cmd == '/suche':
            settings['force_all'] = int(now); sch = True
            send('🔎 Alle Suchen werden jetzt ausgeführt' + ('.' if settings['enabled'] else ' (Agent bleibt pausiert).'))
        elif cmd in ('/nightmode', '/nachtmodus'):
            send(night_command(args, settings, now)); sch = True
        elif cmd == '/status':
            ws = [compile_watch(w) for w in raw_watch]
            act = [w for w in ws if w['active']]
            nxt = sorted((next_run_ts(w, settings, state, now) or 0, w['name']) for w in act)
            nxt_txt = '\n'.join(f"• {fmt_time(t)} – {esc(n)}" for t, n in nxt[:5] if t) or '–'
            send(f"{'▶️ Läuft' if settings['enabled'] else '⏸ Pausiert'}"
                 + (' · ' + night_text(settings) + (' – gerade aktiv' if night_active(settings, now) else ' – gerade nicht aktiv') if night_text(settings) else '') + "\n"
                 f"Letzte Suche: {fmt_time(state['last_search'])}\n"
                 f"Aktive Suchen: {len(act)} von {len(ws)}\n"
                 f"Deals letzte Suche: {state['stats']['last_deals']} · gesamt: {state['stats']['deals']}\n\n"
                 f"<b>Als Nächstes</b>\n{nxt_txt if settings['enabled'] else 'pausiert'}")
        elif cmd == '/liste':
            send(watch_list_text(raw_watch, settings))
        elif cmd in ('/limit', '/an', '/aus'):
            try:
                idx = int(args[0]) - 1
                if idx < 0: raise IndexError
                w = raw_watch[idx]
            except (IndexError, ValueError):
                send('Bitte mit Nummer aus /liste, z. B. /limit 2 90 oder /aus 3'); continue
            if cmd == '/limit':
                try:
                    price = float(args[1].replace(',', '.').replace('€', ''))
                except (IndexError, ValueError):
                    send('Bitte so: /limit 2 90'); continue
                w['maxPrice'] = price
                send(f"✅ {esc(w['name'])}: Max-Preis jetzt {euro(price)}")
            else:
                w['active'] = cmd == '/an'
                send(f"{'✅' if w['active'] else '⏸'} {esc(w['name'])} ist jetzt {'an' if w['active'] else 'aus'}.")
            wch = True
        elif cmd == '/lernen':
            wch |= cmd_lernen(args, state, raw_watch)
        elif cmd == '/fertig':
            sid = state.get('learn_target')
            state['learn_target'] = ''
            w = next((x for x in raw_watch if x.get('id') == sid), None)
            send(f"✅ Lernen für „{esc(w['name'])}“ beendet. Die Zusammenfassung kommt nach dem Einlesen der Links." if w else 'Es war keine Lern-Suche aktiv.')
            state['want_summary'] = sid or ''
        elif cmd == '/profil':
            profs = load_json(PROFILES_FILE, {})
            ls = [w for w in raw_watch if w.get('type') == 'lernen']
            if not ls:
                send('Noch keine Lern-Suche. Anlegen mit /lernen Name 20')
            for w in ls:
                send(learn.summary((profs.get(w['id']) or {}).get('profile'), esc(w['name'])))
        elif cmd in ('/hilfe', '/help'):
            send(HELP)
        else:
            send('Unbekannter Befehl. /hilfe zeigt alle Befehle.')
    return wch, sch

# ---------------------------------------------------------------- Suche
def night_window(settings):
    a, b = parse_hhmm(settings.get('night_from') or ''), parse_hhmm(settings.get('night_to') or '')
    return (a, b) if a and b and a != b else None

def night_active(settings, now):
    """Nachtmodus gilt, wenn er von Hand an ist oder die Uhrzeit (Wien) im Zeitplan liegt, z. B. 23:00–07:00."""
    if settings.get('night_mode'):
        return True
    win = night_window(settings)
    if not win:
        return False
    t = datetime.fromtimestamp(now, TZ); m = t.hour * 60 + t.minute
    a, b = win[0][0] * 60 + win[0][1], win[1][0] * 60 + win[1][1]
    return a <= m < b if a < b else (m >= a or m < b)

def night_text(settings):
    win = night_window(settings)
    mx = int(fnum(settings.get('night_max'), 3))
    parts = []
    if settings.get('night_mode'):
        parts.append('an')
    if win:
        parts.append(f"Zeitplan täglich {win[0][0]:02d}:{win[0][1]:02d}–{win[1][0]:02d}:{win[1][1]:02d}")
    return f"🌙 Nachtmodus ({', '.join(parts)}; stündlich, max. {mx} Artikel)" if parts else ''

TIME_ARG_RE = re.compile(r'(\d{1,2})(?:[:.](\d{2}))?')

def night_command(args, settings, now):
    """/nightmode · /nightmode 5 · /nightmode 23-7 · /nightmode 23:00 07:00 5 · /nightmode aus"""
    text = ' '.join(args).lower().strip()
    if text in ('aus', 'off', '0', 'stop', 'ende'):
        settings['night_mode'] = False; settings['night_from'] = settings['night_to'] = ''
        return '☀️ Nachtmodus aus (auch der Zeitplan). Alle Suchen laufen wieder nach ihrem eigenen Zeitplan.'
    if text in ('zeitplan aus', 'plan aus'):
        settings['night_from'] = settings['night_to'] = ''
        return '🕒 Nachtmodus-Zeitplan gelöscht.' + (' Der Nachtmodus ist aber noch von Hand an (/nightmode aus).' if settings.get('night_mode') else '')
    times = [(int(h), int(m or 0)) for h, m in TIME_ARG_RE.findall(re.sub(r'\b(bis|von|uhr|-|–)\b', ' ', text.replace('-', ' ').replace('–', ' ')))]
    # Form „23-7 5“: zwei Uhrzeiten (+ optional Anzahl) → Zeitplan; nur eine Zahl → Anzahl
    if len(times) >= 2 and all(h < 24 and m < 60 for h, m in times[:2]):
        (h1, m1), (h2, m2) = times[:2]
        settings['night_from'], settings['night_to'] = f'{h1:02d}:{m1:02d}', f'{h2:02d}:{m2:02d}'
        if len(times) >= 3 and times[2][0] > 0:
            settings['night_max'] = times[2][0]
        settings['night_mode'] = False
        now_on = night_active(settings, now)
        return (f"🕒 Nachtmodus-Zeitplan: täglich {settings['night_from']}–{settings['night_to']} Uhr, dann alle Suchen nur stündlich, "
                f"höchstens {int(fnum(settings.get('night_max'), 3))} Artikel pro Stunde." + (' Gerade aktiv 🌙' if now_on else '')
                + '\nZeitplan löschen: /nightmode zeitplan aus · alles aus: /nightmode aus')
    if len(times) == 1 and times[0][0] > 0:
        settings['night_max'] = times[0][0]
    settings['night_mode'] = True
    return (f"🌙 Nachtmodus an: alle Suchen nur noch stündlich, höchstens {int(fnum(settings.get('night_max'), 3))} Artikel pro Stunde "
            '(die besten zuerst). Ausschalten mit /nightmode aus\nTipp: /nightmode 23-7 schaltet ihn jede Nacht automatisch ein.')

def night_cap(cap, state):
    left = state.get('_night_left')
    return cap if left is None else min(cap, left)

def night_used(state, n):
    if state.get('_night_left') is not None:
        state['_night_left'] = max(0, state['_night_left'] - n)
        state['night_sent'] = int(state.get('night_sent', 0)) + n

def run_one(w, state, settings, seen, first_done):
    first = w['id'] not in first_done
    cap = night_cap(int(fnum(settings.get('first_run_max'), 3)) if first else int(fnum(settings.get('max_per_run'), 15)), state)
    shipping = fnum(settings.get('shipping_est'), 5.0)
    sent = 0
    key = lambda iid: f"{w['id']}|{iid}"
    cands = {}
    for q in w['queries']:
        try:
            page = vinted_get(search_url(q, w))
        except Blocked:
            raise
        except Exception as e:
            log(f"Suchanfrage übersprungen ({q}): {e}")
            time.sleep(random.uniform(5, 10)); continue
        found = parse_catalog(page)
        add_background(state, [it['title'] for it in found if key(it['id']) not in seen and it['id'] not in seen])
        for it in found:
            if key(it['id']) not in seen and it['id'] not in seen:
                cands[it['id']] = it
        time.sleep(random.uniform(2.5, 5))
    log(f"{w['name']}: {len(cands)} neue Kandidaten")
    for it in cands.values():
        if matches(w, it) is False:
            seen.add(key(it['id'])); continue
        if sent >= cap:
            if first:
                seen.add(key(it['id']))  # beim ersten Lauf: Rest als bekannt markieren
            continue
        try:
            det = fetch_details(it)
        except Blocked:
            raise
        except Exception as e:
            log(f"Details übersprungen ({it['id']}): {e}"); continue
        time.sleep(random.uniform(2, 4))
        seen.add(key(it['id']))
        if not matches(w, it, det):
            continue
        it.update(det)
        if send_deal(w, it, shipping):
            seen.add(it['id'])
            sent += 1
    first_done.add(w['id'])
    night_used(state, sent)
    return sent

def due_watches(watch, state, settings, now):
    ws = [compile_watch(w) for w in watch]
    force_all = int(fnum(settings.get('force_all'), 0)) > int(state.get('force_all_done', 0))
    out = []
    for w in ws:
        one = int(fnum((settings.get('force') or {}).get(w['id']), 0)) > int(state['force_done'].get(w['id'], 0))
        forced = one or (force_all and w['active'])     # „Jetzt suchen“ bei einer Suche geht auch, wenn sie aus ist
        if forced or (settings['enabled'] and w['active'] and is_due(w, settings, state, now)):
            out.append((w, forced))
    return out


# ---------------------------------------------------------------- Lern-Suchen
LINK_RE = re.compile(r'https?://[^\s<>"]*vinted[^\s<>"]*', re.I)
ITEM_ID_RE = re.compile(r'/items/(\d+)')

def find_links(text, msg=None):
    urls = LINK_RE.findall(text or '')
    for e in (msg or {}).get('entities', []) + (msg or {}).get('caption_entities', []):
        if e.get('type') == 'text_link' and 'vinted' in e.get('url', ''):
            urls.append(e['url'])
    out = []
    for u in urls:
        u = u.rstrip(').,;!')
        if u not in out:
            out.append(u)
    return out

def learn_watch(raw_watch, sid):
    return next((w for w in raw_watch if w.get('id') == sid and w.get('type') == 'lernen'), None)

def cmd_lernen(args, state, raw_watch):
    """/lernen Name [Preis] – Lern-Suche anlegen oder auswählen."""
    ls = [w for w in raw_watch if w.get('type') == 'lernen']
    if not args:
        rows = '\n'.join(f"• {esc(w['name'])} – bis {euro(fnum(w.get('maxPrice')))}" for w in ls) or '(noch keine)'
        send('<b>Lern-Suchen</b>\n' + rows + '\n\nNeue anlegen oder wählen: /lernen Name 20\n'
             'Danach Vinted-Links schicken (auch viele auf einmal). Teurere Beispiele sind okay – der Preis kommt aus dem Limit.')
        return False
    price = None
    if re.fullmatch(r'\d+([.,]\d+)?€?', args[-1]) and len(args) > 1:
        price = fnum(args[-1].rstrip('€')); args = args[:-1]
    name = ' '.join(args).strip()
    w = next((x for x in ls if x['name'].lower() == name.lower() or x['id'] == slug(name)), None)
    changed = False
    if not w:
        ids = {x.get('id') for x in raw_watch}
        sid = slug(name)[:30]
        while sid in ids:
            sid += '-2'
        w = {'id': sid, 'name': name, 'type': 'lernen', 'category': 'kleidung', 'active': True,
             'maxPrice': price or 0, 'minCondition': 'Gut', 'vision': True, 'strictness': 1.0,
             'schedule': {'mode': 'live'}}
        raw_watch.append(w); changed = True
    elif price:
        w['maxPrice'] = price; changed = True
    state['learn_target'] = w['id']
    lim = fnum(w.get('maxPrice'))
    send(f"🧠 Lern-Modus für „{esc(w['name'])}“" + (f" (bis {euro(lim)})" if lim else '') + '.\n'
         'Schick mir jetzt Links zu Anzeigen, die genau so sind, wie du sie suchst – gern 20–40 Stück, auch mehrere pro Nachricht. '
         'Ich lese sie beim nächsten Lauf ein (ca. 5 Min.).\nWenn du fertig bist: /fertig'
         + ('' if lim else '\n\n⚠️ Noch kein Höchstpreis. Setzen mit /lernen ' + esc(w['name']) + ' 20'))
    return changed

def queue_links(links, state, raw_watch):
    sid = state.get('learn_target')
    ls = [w for w in raw_watch if w.get('type') == 'lernen']
    if not learn_watch(raw_watch, sid):
        if len(ls) == 1:
            sid = ls[0]['id']
        else:
            send('Für welche Lern-Suche sind die Links? Erst /lernen Name schicken, dann die Links.'); return False
    known = {q['url'] for q in state['learn_queue']}
    new = [u for u in links if u not in known]
    state['learn_queue'] += [{'sid': sid, 'url': u} for u in new]
    log(f'{len(new)} Link(s) für Lern-Suche {sid} vorgemerkt')
    return False

def item_path(url):
    m = ITEM_ID_RE.search(url)
    if m:
        return '/items/' + m.group(1)
    try:                                              # Kurz-/Teilen-Link → weiterleiten lassen
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            m = ITEM_ID_RE.search(r.geturl())
            return '/items/' + m.group(1) if m else None
    except Exception:
        return None

BRAND_RE = re.compile(r'brand_dto\\?":\{[^}]*?\\?"title\\?":\\?"([^"\\]+)')
CATEGORY_RE = re.compile(r'itemCondition\\?":\\?"[A-Za-z]+\\?"\},\\?"category\\?":\\?"([^"\\]+)')
PRICE_RES = [re.compile(r'\\?"price\\?":\{\\?"amount\\?":\\?"([\d.]+)'), re.compile(r'product:price:amount"\s+content="([\d.]+)'),
             re.compile(r'"priceCurrency"\s*:\s*"[A-Z]{3}"\s*,\s*"price"\s*:\s*"?([\d.]+)'),
             re.compile(r'"price"\s*:\s*"?([\d.]+)"?\s*,\s*"priceCurrency"')]
TITLE_RES = [re.compile(r'<meta property="og:title" content="([^"]+)"'), re.compile(r'<title>([^<]+)</title>')]

def fetch_item_full(path):
    """Alle Merkmale einer einzelnen Anzeige (für Beispiele)."""
    page = vinted_get(path)
    p = _DescParser(); p.feed(page)
    attrs = {}
    for code, title, value in ATTR_RE.findall(page):
        attrs.setdefault(code, value); attrs.setdefault('t:' + title.lower(), value)
    title = ''
    for r in TITLE_RES:
        m = r.search(page)
        if m:
            title = html.unescape(m.group(1)); break
    title = re.sub(r'\s*[|–-]\s*Vinted.*$', '', title).strip()
    price = 0.0
    for r in PRICE_RES:
        m = r.search(page)
        if m:
            price = float(m.group(1)); break
    iid = ITEM_ID_RE.search(path).group(1)
    bm, cm = BRAND_RE.search(page), CATEGORY_RE.search(page)
    if not p.buf:                                     # Beschreibung notfalls aus og:description
        m = re.search(r'<meta property="og:description" content="([^"]*)"', page)
        if m:
            p.buf = [html.unescape(m.group(1)).split(' - ', 1)[-1]]
    return {'id': iid, 'url': BASE + path, 'title': title, 'category': html.unescape(cm.group(1)) if cm else '',
            'brand': html.unescape(bm.group(1)) if bm else (attrs.get('brand') or attrs.get('t:marke', '')),
            'size': attrs.get('size') or attrs.get('t:größe', ''),
            'condition': attrs.get('status') or attrs.get('t:zustand', ''), 'price': price,
            'color': attrs.get('color') or attrs.get('t:farbe', ''), 'desc': ''.join(p.buf).strip(),
            'image': p.og, 'thumb': p.og}

def profile_entry(profs, sid):
    return profs.setdefault(sid, {'examples': [], 'negatives': [], 'profile': None})

def rebuild(profs, sid, w, state):
    e = profile_entry(profs, sid)
    new = learn.build_profile(e['examples'], e['negatives'], (state['bg'].get('__all__') or {}),
                              fnum(w.get('strictness'), 1.0) or 1.0)
    if new != e.get('profile'):
        e['profile'] = new
        e['updated'] = int(time.time())

def process_learn_queue(state, raw_watch, profs, max_links=40):
    """Vorgemerkte Links einlesen → Beispiele. Gibt geänderte Such-IDs zurück."""
    changed, report = set(), {}
    queue, state['learn_queue'] = state['learn_queue'][:max_links], state['learn_queue'][max_links:]
    for q in queue:
        w = learn_watch(raw_watch, q['sid'])
        if not w:
            continue
        r = report.setdefault(q['sid'], {'ok': 0, 'fail': 0, 'dup': 0})
        path = item_path(q['url'])
        if not path:
            r['fail'] += 1; continue
        e = profile_entry(profs, q['sid'])
        iid = path.rsplit('/', 1)[1]
        if any(x['id'] == iid for x in e['examples']):
            r['dup'] += 1; continue
        try:
            it = fetch_item_full(path)
        except Blocked:
            state['learn_queue'].insert(0, q); raise
        except Exception as ex:
            log('Beispiel nicht lesbar', q['url'], ex); r['fail'] += 1; continue
        if not it['title']:
            r['fail'] += 1; continue
        f = learn.item_features(it); f['source'] = 'link'; f['added'] = int(time.time())
        e['examples'].append(f)
        e['negatives'] = [x for x in e['negatives'] if x['id'] != iid]
        r['ok'] += 1; changed.add(q['sid'])
        time.sleep(random.uniform(2.5, 4.5))
    for sid in changed | ({state.get('want_summary')} - {''}):
        w = learn_watch(raw_watch, sid)
        if w:
            rebuild(profs, sid, w, state)
    for sid, r in report.items():
        w = learn_watch(raw_watch, sid)
        more = sum(1 for q in state['learn_queue'] if q['sid'] == sid)
        msg = f"📥 „{esc(w['name'])}“: {r['ok']} Anzeige(n) gelernt"
        if r['dup']: msg += f", {r['dup']} schon bekannt"
        if r['fail']: msg += f", {r['fail']} nicht lesbar (gelöscht oder kein Anzeigen-Link)"
        if more: msg += f'. {more} weitere folgen beim nächsten Lauf'
        send(msg + '.\n\n' + learn.summary(profs[sid]['profile'], esc(w['name'])))
        if state.get('want_summary') == sid:
            state['want_summary'] = ''
    ws = state.get('want_summary')
    if ws and ws not in report and learn_watch(raw_watch, ws):
        send(learn.summary((profs.get(ws) or {}).get('profile'), esc(learn_watch(raw_watch, ws)['name'])))
        state['want_summary'] = ''
    return changed

def process_callbacks(state, raw_watch, profs):
    """👍/👎 unter Treffern → Beispiele bzw. Gegenbeispiele."""
    changed = set()
    for cb in state['pending_cb']:
        parts = (cb.get('data') or '').split(':')
        if len(parts) != 4 or parts[0] != 'fb':
            continue
        _, vote, sid, iid = parts
        f = (state['sent_feat'].get(iid) or {}).get('f')
        w = learn_watch(raw_watch, sid)
        if not f or not w:
            tg('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': 'Diese Anzeige kenne ich nicht mehr.'}); continue
        e = profile_entry(profs, sid)
        e['examples'] = [x for x in e['examples'] if x['id'] != iid]
        e['negatives'] = [x for x in e['negatives'] if x['id'] != iid]
        (e['examples'] if vote == 'u' else e['negatives']).append({**f, 'source': 'telegram', 'added': int(time.time())})
        changed.add(sid)
        label = '👍 gemerkt – mehr davon' if vote == 'u' else '👎 gemerkt – weniger davon'
        tg('answerCallbackQuery', {'callback_query_id': cb['id'], 'text': label})
        if cb.get('msg'):
            tg('editMessageReplyMarkup', {'chat_id': CHAT_ID, 'message_id': cb['msg'],
                                          'reply_markup': {'inline_keyboard': [[{'text': label, 'callback_data': 'x'}]]}})
    state['pending_cb'] = []
    for sid in changed:
        rebuild(profs, sid, learn_watch(raw_watch, sid), state)
    if changed:
        log(f'Feedback verarbeitet: {", ".join(changed)}')
    return changed

_img_cache = {}

def fetch_bytes(url):
    if url not in _img_cache:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'image/*'})
        with urllib.request.urlopen(req, timeout=20) as r:
            _img_cache[url] = r.read()
    return _img_cache[url]

def vision_ok(state, settings):
    v = state['vision']
    today = datetime.now(TZ).strftime('%Y-%m-%d')
    if v.get('day') != today:
        v.update(day=today, n=0, usd=0.0, limit_hit=False)
    ok = bool(ANTHROPIC_KEY) and v['n'] < int(fnum(settings.get('vision_daily_max'), 150))
    if ANTHROPIC_KEY and not ok and not v.get('limit_hit'):
        v['limit_hit'] = True
        log('Bildprüfung: Tageslimit erreicht – bis Mitternacht ohne Bildprüfung')
    return ok

def add_background(state, titles, sid='__all__'):
    """Wörter normaler Anzeigen aus allen Suchen zählen – daran erkennt das Profil, was wirklich „typisch“ ist."""
    bg = state['bg'].setdefault(sid, {})
    for t in titles:
        for tok in learn.tokens(t):
            bg[tok] = bg.get(tok, 0) + 1
        bg['__docs__'] = bg.get('__docs__', 0) + 1
    if len(bg) > 4000:                                  # klein halten: seltene Wörter vergessen
        keep = sorted(bg.items(), key=lambda kv: -kv[1])[:3000]
        state['bg'][sid] = dict(keep) | {'__docs__': bg['__docs__']}

def example_photos(entry, k=4):
    ex = [x for x in entry['examples'] if x.get('photo')]
    ex.sort(key=lambda x: (x.get('source') != 'telegram', -x.get('added', 0)))   # 👍 zuerst, dann neueste
    return [x['photo'] for x in ex[:k]]

def run_learned(w, state, settings, seen, first_done, profs):
    entry = profs.get(w['id']) or {}
    prof = entry.get('profile')
    if not prof:
        log(f"{w['name']}: noch keine Beispiele"); return 0
    if not w['maxPrice']:
        log(f"{w['name']}: kein Höchstpreis gesetzt"); return 0
    first = w['id'] not in first_done
    cap = night_cap(int(fnum(settings.get('first_run_max'), 3)) if first else int(fnum(settings.get('max_per_run'), 15)), state)
    shipping = fnum(settings.get('shipping_est'), 5.0)
    thr = prof['threshold']
    can_see = w['vision'] and vision_ok(state, settings)
    if w['queriesManual']:
        queries = w['queries']
    else:
        # breite Begriffe („ralph lauren pullover“) nur mit Bildprüfung – sonst kämen zu viele unpassende Treffer
        queries = list(prof.get('queries') or []) + (list(prof.get('broad_queries') or []) if can_see else [])
        queries = queries or prof.get('broad_queries') or w['queries']
    key = lambda iid: f"{w['id']}|{iid}"
    cands, titles = {}, []
    for q in queries:
        try:
            page = vinted_get(search_url(q, w))
        except Blocked:
            raise
        except Exception as e:
            log(f'Suchanfrage übersprungen ({q}): {e}'); continue
        for it in parse_catalog(page):
            if key(it['id']) in seen or it['id'] in seen:
                continue
            titles.append(it['title'])
            cands[it['id']] = it
        time.sleep(random.uniform(2.5, 4.5))
    add_background(state, titles)
    scored = []
    for it in cands.values():
        seen.add(key(it['id']))
        text = f"{it['title']} {it['brand']}"
        if FAKE_RE.search(text) or REWORK_RE.search(text) or KIDSIZE_RE.search(it['size']):
            continue
        if it['price'] > w['maxPrice'] or (w['minPrice'] and it['price'] < w['minPrice']):
            continue
        if learn.exclusion_hit(w['excludeRules'], it['title']):
            continue
        sc = learn.score(prof, learn.item_features(it), detail=False)['score']
        if sc >= thr * (0.6 if can_see else 0.75):          # mit Bildprüfung dürfen auch allgemeine Titel in die Vorauswahl
            scored.append((sc, it))
    scored.sort(key=lambda x: -x[0])
    log(f"{w['name']}: {len(cands)} neu, {len(scored)} in der Vorauswahl (Schwelle {thr:.2f})")
    sent, rescues = 0, 0
    for pre, it in scored:
        if sent >= cap:
            break
        if pre < thr * 0.75 and rescues >= 8:              # höchstens 8 „Bild entscheidet“-Prüfungen pro Lauf
            continue
        try:
            det = fetch_details(it)
        except Blocked:
            raise
        except Exception as e:
            log(f'Details übersprungen ({it["id"]}): {e}'); continue
        time.sleep(random.uniform(2, 3.5))
        it.update(det)
        hit = learn.exclusion_hit(w['excludeRules'], it['title'], it.get('desc', ''), it.get('category', ''))
        if hit:
            log(f"Ausgeschlossen {it['id']}: {hit}"); continue
        f = learn.item_features(it)
        res = learn.score(prof, f, detail=True)
        # Unter der Schwelle, aber Marke/Größe/Kategorie passen und nur der Text ist zu allgemein? → das Bild entscheidet
        rescue = res['score'] < thr and res['score'] >= thr * 0.6 and res['parts'].get('marke', 1) >= 0.5 \
            and res['parts'].get('kategorie', 1) > 0 and res['parts'].get('größe', 1) > 0
        if res['score'] < thr and not rescue:
            continue
        if rescue:
            rescues += 1
        vis = ''
        if w['vision'] and vision_ok(state, settings) and it.get('thumb'):
            try:
                vs, why, cost = learn.vision_score(ANTHROPIC_KEY, example_photos(entry), it['thumb'], w['name'], fetch=fetch_bytes,
                                                 avoid=w['excludeRules'])
                v = state['vision']; v['n'] += 1; v['usd'] += cost; v['total_usd'] = v.get('total_usd', 0) + cost
                need = 75 if rescue else int(fnum(settings.get('vision_min'), 60))
                if vs is None:                             # Antwort nicht lesbar → Bild entscheidet nicht
                    log(f"Bildprüfung {it['id']}: {why}")
                    if rescue:
                        continue
                elif vs < need:
                    log(f"Bildprüfung verwirft {it['id']} ({vs}: {why})"); continue
                else:
                    log(f"Bildprüfung ok {it['id']} ({vs}: {why})")
                    vis = f' · Bild {vs} %' + (f' ({esc(why)})' if why else '')
            except Exception as e:
                log('Bildprüfung fehlgeschlagen:', str(e)[:200])
                if rescue:
                    continue
        elif rescue:
            continue                                   # ohne Bildprüfung keine Rettung über das Bild
        extra = f"🧠 passt zu {round(res['score'] * 100)} %{vis}"
        buttons = [[{'text': '👍 passt', 'callback_data': f"fb:u:{w['id']}:{it['id']}"},
                    {'text': '👎 passt nicht', 'callback_data': f"fb:d:{w['id']}:{it['id']}"}]]
        if send_deal(w, it, shipping, extra, buttons):
            seen.add(it['id']); sent += 1
            state['sent_feat'][it['id']] = {'sid': w['id'], 'f': f, 't': int(time.time())}
    if len(state['sent_feat']) > 400:
        state['sent_feat'] = dict(sorted(state['sent_feat'].items(), key=lambda kv: -kv[1]['t'])[:300])
    first_done.add(w['id'])
    night_used(state, sent)
    return sent

# ---------------------------------------------------------------- Hauptablauf
def main():
    if not TOKEN or not CHAT_ID:
        if not DRY_RUN:
            log('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen (GitHub-Secrets).'); sys.exit(1)
    now = time.time()
    state = {**default_state(), **load_json(STATE_FILE, {})}
    state.setdefault('watch_last', {}); state.setdefault('force_done', {})
    settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}
    raw_watch = load_json(WATCH_FILE, [])
    watch_changed = migrate(state, settings, raw_watch)
    settings_changed = not os.path.exists(SETTINGS_FILE)

    if not state.get('commands_set') and not DRY_RUN:
        tg('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]})
        state['commands_set'] = True
        send('🤖 Deal-Agent läuft jetzt in der Cloud (GitHub).\n\n' + HELP)
    if state.get('commands_version') in (3, 4) and not DRY_RUN:     # Befehlsliste aktualisieren (Nachtmodus)
        tg('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]})
        state['commands_version'] = 5
    if state.get('commands_version') not in (3, 4, 5) and state.get('commands_set') and not DRY_RUN:
        tg('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]})
        state['commands_version'] = 5
        send('🧠 Neu: Lern-Suchen. Mit /lernen Name 20 anlegen und dann einfach Vinted-Links schicken – '
             'der Agent lernt daraus selbst Suchbegriffe und Merkmale. Unter Treffern kannst du mit 👍/👎 nachschärfen. /hilfe zeigt alles.')

    wc, sc = handle_commands(state, settings, raw_watch, now)
    watch_changed |= wc; settings_changed |= sc

    profs = load_json(PROFILES_FILE, {})
    profs_before = json.dumps(profs, sort_keys=True)
    try:
        process_callbacks(state, raw_watch, profs)
        if state['learn_queue']:
            process_learn_queue(state, raw_watch, profs)
    except Blocked as e:
        log('Vinted blockiert beim Einlesen der Beispiele:', e)
    except Exception as e:
        log('Fehler beim Lernen:', repr(e))
    # Profile mit dem gewachsenen Hintergrund neu berechnen (billig, kein Netz)
    for w in raw_watch:
        if w.get('type') == 'lernen' and (profs.get(w['id']) or {}).get('examples'):
            rebuild(profs, w['id'], w, state)

    todo = due_watches(raw_watch, state, settings, now)
    if night_active(settings, now) and settings['enabled']:
        forced = [(w, f) for w, f in todo if f]
        if now - state.get('night_last', 0) >= 3600 - TOLERANCE_S:          # neue Stunde: alle aktiven Suchen, Zähler zurück
            ids = {w['id'] for w, _ in forced}
            todo = forced + [(w, False) for w in (compile_watch(x) for x in raw_watch) if w['active'] and w['id'] not in ids]
            state['night_last'] = int(now); state['night_sent'] = 0
        else:
            todo = forced                                                   # dazwischen nur „Jetzt suchen“
        todo.sort(key=lambda wf: wf[0]['type'] != 'lernen')                 # Lern-Suchen zuerst (beste Treffer)
        state['_night_left'] = max(0, int(fnum(settings.get('night_max'), 3)) - int(state.get('night_sent', 0)))
        if todo:
            log(f"Nachtmodus: {len(todo)} Suche(n), noch {state['_night_left']} Artikel in dieser Stunde")
    if todo:
        seen, first_done = set(state['seen']), set(state['first_run_done'])
        total = 0
        try:
            for w, forced in todo:
                if state.get('_night_left') == 0:
                    log('Nachtmodus: Limit für diese Stunde erreicht – restliche Suchen in der nächsten Stunde'); break
                log(f"Suche „{w['name']}“" + (' (sofort)' if forced else ''))
                if w['type'] == 'lernen':
                    total += run_learned(w, state, settings, seen, first_done, profs)
                else:
                    total += run_one(w, state, settings, seen, first_done)
                state['watch_last'][w['id']] = int(now)
                if forced:
                    state['force_done'][w['id']] = int(now)
                state['seen'] = list(seen)[-20000:]
                state['first_run_done'] = sorted(first_done)
            state['blocked_notified'] = False
        except Blocked as e:
            log('Vinted blockiert:', e)
            if not state['blocked_notified']:
                send('⚠️ Vinted hat die Cloud-Suche gerade blockiert. Ich versuche es beim nächsten Lauf erneut.')
                state['blocked_notified'] = True
        except Exception as e:
            log('Fehler bei der Suche:', repr(e))
        state['force_all_done'] = max(int(state.get('force_all_done', 0)), int(fnum(settings.get('force_all'), 0)))
        state['last_search'] = int(now)
        state['stats']['searches'] += 1; state['stats']['deals'] += total; state['stats']['last_deals'] = total
        log(f'Suche fertig: {total} neue Deals ({len(todo)} Suchen)')
    else:
        log('Keine Suche fällig' if settings['enabled'] else 'Pausiert')

    state.pop('_night_left', None)
    # aufräumen: Zustand gelöschter Suchen entfernen
    ids = {compile_watch(w)['id'] for w in raw_watch}
    state['watch_last'] = {k: v for k, v in state['watch_last'].items() if k in ids}
    for k in list(profs):
        if k not in ids:
            profs.pop(k)
    if json.dumps(profs, sort_keys=True, default=str) != profs_before:
        # „updated“ ändert sich bei jedem Neuberechnen – nur speichern, wenn sich inhaltlich etwas tut
        save_json(PROFILES_FILE, profs)
    if watch_changed:
        save_json(WATCH_FILE, raw_watch)
    if settings_changed:
        save_json(SETTINGS_FILE, settings)
    save_json(STATE_FILE, state)
    with open(os.path.join(HERE, 'last_run.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(RUN_LOG[-200:]) + '\n')

if __name__ == '__main__':
    main()
