#!/usr/bin/env python3
# Version 2.0.0
"""Vinted Deal-Agent für GitHub Actions.

Jeder Lauf (ca. alle 15 Minuten, siehe .github/workflows/agent.yml):
  1. liest neue Telegram-Befehle (/start, /stop, /status, /suche, /liste, /limit, /an, /aus, /hilfe)
  2. prüft für jede Suche ihren eigenen Zeitplan (Standard-Intervall, eigenes Intervall oder feste
     Wochentage + Uhrzeiten) und führt fällige Suchen aus
  3. schickt neue Deals mit Titelbild an Telegram
  4. speichert Zustand (state.json) und Einstellungen (settings.json)

Konfiguration:  watchlist.json (Suchen)  ·  settings.json (Agent an/aus, Standard-Intervall, Jetzt-suchen)
Bearbeitet werden beide bequem über die Web-Oberfläche in docs/index.html (GitHub Pages).
Nur Python-Standardbibliothek. GitHub-Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import gzip, html, json, os, random, re, sys, time, uuid, urllib.error, urllib.parse, urllib.request, zlib
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

BASE = os.environ.get('VINTED_DOMAIN', 'https://www.vinted.at')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = str(os.environ.get('TELEGRAM_CHAT_ID', '')).strip()
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'state.json')
WATCH_FILE = os.path.join(HERE, 'watchlist.json')
SETTINGS_FILE = os.path.join(HERE, 'settings.json')
DRY_RUN = os.environ.get('DRY_RUN') == '1'

BUYER_FEE_FIXED, BUYER_FEE_PCT = 0.70, 0.05
CONDITION_RANK = {'Neu, mit Etikett': 5, 'Neu mit Etikett': 5, 'Neu': 4, 'Sehr gut': 3, 'Gut': 2, 'Zufriedenstellend': 1}
TZ = ZoneInfo('Europe/Vienna')
WEEKDAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
TOLERANCE_S = 5 * 60          # Läufe kommen ca. alle 15 Min.; so wird „stündlich“ nicht zu „alle 75 Min.“

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/129.0.0.0 Safari/537.36')

DEFAULT_SETTINGS = {'enabled': True, 'default_interval_min': 60, 'first_run_max': 3, 'shipping_est': 5.0,
                    'max_per_run': 15, 'force_all': 0, 'force': {}}

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
            'stats': {'searches': 0, 'deals': 0, 'last_deals': 0}}

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


ATTR_RE = re.compile(r'\\?"code\\?":\\?"([a-z_]+)\\?",\\?"data\\?":\{[^}]*?\\?"title\\?":\\?"([^"\\]*)\\?",\\?"value\\?":\\?"([^"\\]*)')

def fetch_details(item):
    page = vinted_get(urllib.parse.urlparse(item['url']).path)
    p = _DescParser(); p.feed(page)
    attrs = {}
    for code, title, value in ATTR_RE.findall(page):
        attrs.setdefault(code, value)
        attrs.setdefault('t:' + title.lower(), value)
    return {'desc': ''.join(p.buf).strip(), 'image': p.og or item.get('thumb', ''),
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
            'minCondition': w.get('minCondition') or 'Gut', 'schedule': sched}

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

def send_deal(w, it, shipping):
    text = build_message(w, it, shipping)
    img = it.get('image') or it.get('thumb')
    if img:
        r = tg('sendPhoto', {'chat_id': CHAT_ID, 'photo': img, 'caption': text[:1024], 'parse_mode': 'HTML'})
        if r.get('ok'):
            return True
    return send(text).get('ok', False)

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
        "Suchen, Größen, Farben und Zeitpläne bequem einstellen: in der Web-Oberfläche.\n"
        "Befehle werden beim nächsten Lauf verarbeitet (spätestens nach ca. 15 Min.).")

BOT_COMMANDS = [('start', 'Agent einschalten'), ('stop', 'Agent pausieren'), ('status', 'Status anzeigen'),
                ('suche', 'jetzt suchen'), ('liste', 'Suchen anzeigen'), ('limit', 'Max-Preis ändern: /limit 2 90'),
                ('an', 'Suche einschalten: /an 3'), ('aus', 'Suche ausschalten: /aus 3'), ('hilfe', 'Hilfe')]

def fmt_time(ts):
    if not ts:
        return 'noch nie'
    return datetime.fromtimestamp(ts, TZ).strftime('%a %d.%m. %H:%M').replace('Mon', 'Mo').replace('Tue', 'Di') \
        .replace('Wed', 'Mi').replace('Thu', 'Do').replace('Fri', 'Fr').replace('Sat', 'Sa').replace('Sun', 'So')

def watch_list_text(raw, settings):
    rows = []
    for i, w in enumerate(raw, 1):
        c = compile_watch(w)
        flag = '✅' if c['active'] else '⏸'
        rows.append(f"{flag} <b>{i}</b> {esc(c['name'])} – bis {euro(c['maxPrice'])} · {esc(schedule_text(c, settings))}")
    return '<b>Deine Suchen</b>\n' + '\n'.join(rows) + '\n\nÄndern: /limit NR PREIS · /aus NR · /an NR'

def handle_commands(state, settings, raw_watch, now):
    """Telegram-Befehle ausführen. Gibt (watch_geändert, settings_geändert) zurück."""
    res = tg('getUpdates', {'offset': state['tg_offset'] + 1, 'timeout': 0, 'allowed_updates': ['message']})
    if not res.get('ok', True):
        log('getUpdates fehlgeschlagen:', str(res.get('description'))[:200])
    log(f"Telegram: {len(res.get('result', []))} neue Nachricht(en)")
    wch = sch = False
    for u in res.get('result', []):
        state['tg_offset'] = max(state['tg_offset'], u['update_id'])
        msg = u.get('message') or {}
        if str(msg.get('chat', {}).get('id')) != CHAT_ID:
            continue
        text = (msg.get('text') or '').strip()
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
        elif cmd == '/status':
            ws = [compile_watch(w) for w in raw_watch]
            act = [w for w in ws if w['active']]
            nxt = sorted((next_run_ts(w, settings, state, now) or 0, w['name']) for w in act)
            nxt_txt = '\n'.join(f"• {fmt_time(t)} – {esc(n)}" for t, n in nxt[:5] if t) or '–'
            send(f"{'▶️ Läuft' if settings['enabled'] else '⏸ Pausiert'}\n"
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
        elif cmd in ('/hilfe', '/help'):
            send(HELP)
        else:
            send('Unbekannter Befehl. /hilfe zeigt alle Befehle.')
    return wch, sch

# ---------------------------------------------------------------- Suche
def run_one(w, state, settings, seen, first_done):
    first = w['id'] not in first_done
    cap = int(fnum(settings.get('first_run_max'), 3)) if first else int(fnum(settings.get('max_per_run'), 15))
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
        for it in parse_catalog(page):
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
    if state.get('commands_version') != 2 and state.get('commands_set') and not DRY_RUN:
        tg('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]})
        state['commands_version'] = 2

    wc, sc = handle_commands(state, settings, raw_watch, now)
    watch_changed |= wc; settings_changed |= sc

    todo = due_watches(raw_watch, state, settings, now)
    if todo:
        seen, first_done = set(state['seen']), set(state['first_run_done'])
        total = 0
        try:
            for w, forced in todo:
                log(f"Suche „{w['name']}“" + (' (sofort)' if forced else ''))
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

    # aufräumen: Zustand gelöschter Suchen entfernen
    ids = {compile_watch(w)['id'] for w in raw_watch}
    state['watch_last'] = {k: v for k, v in state['watch_last'].items() if k in ids}
    if watch_changed:
        save_json(WATCH_FILE, raw_watch)
    if settings_changed:
        save_json(SETTINGS_FILE, settings)
    save_json(STATE_FILE, state)
    with open(os.path.join(HERE, 'last_run.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(RUN_LOG[-200:]) + '\n')

if __name__ == '__main__':
    main()
