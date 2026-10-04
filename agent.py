#!/usr/bin/env python3
# Version 1.1.0
"""Vinted Deal-Agent für GitHub Actions.

Läuft alle 15 Minuten (siehe .github/workflows/agent.yml):
  1. liest neue Telegram-Befehle (/start, /stop, /status, /suche, /liste, /limit, /an, /aus, /hilfe)
  2. sucht – wenn aktiv und die letzte Suche >= SEARCH_EVERY_MIN her ist – auf Vinted nach der Watchlist
  3. schickt neue Deals mit Titelbild an Telegram
  4. speichert den Zustand in state.json (wird vom Workflow ins Repo zurückgeschrieben)

Nur Python-Standardbibliothek, keine Installation nötig.
Benötigte Umgebungsvariablen (GitHub-Secrets): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import gzip, html, json, os, random, re, sys, time, urllib.error, urllib.parse, urllib.request, zlib
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

BASE = os.environ.get('VINTED_DOMAIN', 'https://www.vinted.at')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = str(os.environ.get('TELEGRAM_CHAT_ID', '')).strip()
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'state.json')
WATCH_FILE = os.path.join(HERE, 'watchlist.json')
DRY_RUN = os.environ.get('DRY_RUN') == '1'          # zum Testen: nichts senden

SEARCH_EVERY_MIN = int(os.environ.get('SEARCH_EVERY_MIN', '60'))
FIRST_RUN_MAX_PER_ITEM = 3
BUYER_FEE_FIXED, BUYER_FEE_PCT, SHIPPING_EST = 0.70, 0.05, 5.0
CONDITION_RANK = {'Neu, mit Etikett': 5, 'Neu mit Etikett': 5, 'Neu': 4, 'Sehr gut': 3, 'Gut': 2, 'Zufriedenstellend': 1}
TZ = timezone(timedelta(hours=2))  # Wien (Sommerzeit); nur für Anzeige

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/129.0.0.0 Safari/537.36')

RUN_LOG = []

def log(*a):
    line = ' '.join(str(x) for x in a)
    RUN_LOG.append(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC ') + line)
    print('[agent]', line, flush=True)

# ---------------------------------------------------------------- Zustand
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
    return {'enabled': True, 'tg_offset': 0, 'seen': [], 'first_run_done': [], 'last_search': 0,
            'force_search': False, 'blocked_notified': False, 'stats': {'searches': 0, 'deals': 0, 'last_deals': 0}}

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

UPLOAD_RE = re.compile(r'\\?"code\\?":\\?"upload_date\\?",\\?"data\\?":\{[^}]*?\\?"value\\?":\\?"([^"\\]*)')

def fetch_details(item):
    page = vinted_get(urllib.parse.urlparse(item['url']).path)
    p = _DescParser(); p.feed(page)
    up = UPLOAD_RE.search(page)
    return {'desc': ''.join(p.buf).strip(), 'image': p.og or item.get('thumb', ''), 'uploaded': up.group(1) if up else ''}

def search_url(query, w):
    q = {'search_text': query, 'order': 'newest_first', 'currency': 'EUR'}
    if w['maxPrice']:
        q['price_to'] = f"{w['maxPrice']:g}"
    return '/catalog?' + urllib.parse.urlencode(q)

# ---------------------------------------------------------------- Filter
def lst(s):
    return [x.strip() for x in str(s or '').split(',') if x.strip()]

def norm(s):
    return re.sub(r'\s+', ' ', re.sub(r'[’`´]', "'", str(s or '').lower())).strip()

def compile_watch(w):
    queries = lst(w.get('queries')) or [w.get('name', '')]
    rmin = float(w.get('resaleMin') or 0)
    return {'name': w.get('name') or 'Ohne Namen', 'active': w.get('active', True) is not False,
            'queries': queries, 'brands': [norm(b) for b in lst(w.get('brands'))],
            'require': [norm(x) for x in lst(w.get('require'))], 'exclude': [norm(x) for x in lst(w.get('exclude'))],
            'maxPrice': float(w.get('maxPrice') or 0), 'resale': [rmin, float(w.get('resaleMax') or rmin)],
            'sizes': [norm(x) for x in lst(w.get('sizes'))], 'minCondition': w.get('minCondition') or 'Gut'}

FAKE_RE = re.compile(r'\b(fake|replica|rep|dupe|imitat|nachgemacht|inspired)\b', re.I)
REWORK_RE = re.compile(r'\b(rework|reworked|patchwork|upcycl\w*|custom\w*)\b', re.I)
KIDSIZE_RE = re.compile(r'jahre|monate|\bans\b|\banni\b|years|months|\d+\s*cm\b', re.I)

def matches(w, it, desc=''):
    """True = Treffer, False = verwerfen, None = Beschreibung prüfen."""
    text = norm(f"{it['title']} {desc}")
    brand = norm(it['brand'])
    if w['maxPrice'] and it['price'] > w['maxPrice']:
        return False
    if w['brands']:
        if brand:
            if brand not in w['brands']:
                return False
        elif not any(b in text for b in w['brands']):
            return False
    if any(x in text for x in w['exclude']):
        return False
    if FAKE_RE.search(text) or REWORK_RE.search(text) or KIDSIZE_RE.search(it['size']):
        return False
    if CONDITION_RANK.get(it['condition'], 0) < CONDITION_RANK.get(w['minCondition'], 0):
        return False
    if w['sizes'] and norm(it['size'].split('/')[0]) not in w['sizes']:
        return False
    if w['require'] and not any(x in text for x in w['require']):
        return None
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

def build_message(w, it):
    cost = it['price'] + BUYER_FEE_FIXED + it['price'] * BUYER_FEE_PCT + SHIPPING_EST
    m1, m2 = w['resale'][0] - cost, w['resale'][1] - cost
    lines = [f"🔥 <b>Deal: {esc(w['name'])}</b>", f"<b>{esc(it['title'])}</b>",
             f"💶 <b>{euro(it['price'])}</b>" + (f" ({euro(it['total'])} inkl. Käuferschutz)" if it.get('total') else '')
             + (f" · dein Limit {euro(w['maxPrice'])}" if w['maxPrice'] else ''),
             '📏 ' + esc(' · '.join(x for x in (it['size'], it['condition'], it['brand']) if x))]
    if w['resale'][0]:
        lines.append(f"📈 Wiederverkauf ca. {w['resale'][0]:.0f}–{w['resale'][1]:.0f} € → Spanne ca. {m1:.0f} bis {m2:.0f} €")
    if it.get('uploaded'):
        lines.append(f"🕒 hochgeladen vor {esc(it['uploaded'])}")
    if it.get('desc'):
        lines.append('📝 ' + esc(short_desc(it['desc'])))
    lines.append(f"🔗 {it['url']}")
    return '\n'.join(lines)

def send_deal(w, it):
    text = build_message(w, it)
    img = it.get('image') or it.get('thumb')
    if img:
        r = tg('sendPhoto', {'chat_id': CHAT_ID, 'photo': img, 'caption': text[:1024], 'parse_mode': 'HTML'})
        if r.get('ok'):
            return True
    return send(text).get('ok', False)

# ---------------------------------------------------------------- Befehle
HELP = ("<b>Vinted Deal-Agent – Befehle</b>\n"
        "/start – Suche einschalten (sucht sofort)\n"
        "/stop – Suche pausieren\n"
        "/status – läuft er gerade? letzte Suche, Treffer\n"
        "/suche – beim nächsten Lauf sofort suchen\n"
        "/liste – alle Suchen mit Nummer und Max-Preis\n"
        "/limit 2 90 – Max-Preis von Suche 2 auf 90 € setzen\n"
        "/aus 3 · /an 3 – Suche 3 aus- oder einschalten\n"
        "/hilfe – diese Übersicht\n\n"
        "Befehle werden beim nächsten Lauf verarbeitet (spätestens nach ca. 15 Min.).")

BOT_COMMANDS = [('start', 'Suche einschalten'), ('stop', 'Suche pausieren'), ('status', 'Status anzeigen'),
                ('suche', 'jetzt suchen'), ('liste', 'Suchen anzeigen'), ('limit', 'Max-Preis ändern: /limit 2 90'),
                ('an', 'Suche einschalten: /an 3'), ('aus', 'Suche ausschalten: /aus 3'), ('hilfe', 'Hilfe')]

def fmt_time(ts):
    if not ts:
        return 'noch nie'
    return datetime.fromtimestamp(ts, TZ).strftime('%d.%m. %H:%M')

def watch_list_text(raw):
    rows = []
    for i, w in enumerate(raw, 1):
        flag = '✅' if w.get('active', True) is not False else '⏸'
        rows.append(f"{flag} <b>{i}</b> {esc(w.get('name'))} – bis {euro(float(w.get('maxPrice') or 0))}")
    return '<b>Deine Suchen</b>\n' + '\n'.join(rows) + '\n\nÄndern: /limit NR PREIS · /aus NR · /an NR'

def handle_commands(state, raw_watch):
    """Liest neue Telegram-Nachrichten und führt Befehle aus. Gibt True zurück, wenn watchlist.json geändert wurde."""
    res = tg('getUpdates', {'offset': state['tg_offset'] + 1, 'timeout': 0, 'allowed_updates': ['message']})
    if not res.get('ok', True):
        log('getUpdates fehlgeschlagen:', str(res.get('description'))[:200])
    log(f"Telegram: {len(res.get('result', []))} neue Nachricht(en)")
    changed = False
    for u in res.get('result', []):
        state['tg_offset'] = max(state['tg_offset'], u['update_id'])
        msg = u.get('message') or {}
        if str(msg.get('chat', {}).get('id')) != CHAT_ID:
            continue  # fremde Chats ignorieren
        text = (msg.get('text') or '').strip()
        if not text.startswith('/'):
            continue
        cmd, *args = text.split()
        cmd = cmd.split('@')[0].lower()
        log('Befehl', cmd, args)
        if cmd == '/stop':
            state['enabled'] = False
            send('⏸ Suche pausiert. Mit /start geht es weiter.')
        elif cmd == '/start':
            state['enabled'] = True; state['force_search'] = True
            send('▶️ Suche läuft. Die erste Suche startet jetzt.')
        elif cmd == '/suche':
            state['force_search'] = True
            send('🔎 Suche wird gestartet …' if state['enabled'] else '🔎 Einmalige Suche wird gestartet (Agent bleibt pausiert).')
        elif cmd == '/status':
            active = sum(1 for w in raw_watch if w.get('active', True) is not False)
            nxt = '–' if not state['enabled'] else fmt_time(max(state['last_search'] + SEARCH_EVERY_MIN * 60, time.time()))
            send(f"{'▶️ Läuft' if state['enabled'] else '⏸ Pausiert'}\n"
                 f"Letzte Suche: {fmt_time(state['last_search'])} · nächste ca. {nxt}\n"
                 f"Aktive Suchen: {active} von {len(raw_watch)}\n"
                 f"Deals letzte Suche: {state['stats']['last_deals']} · gesamt: {state['stats']['deals']}")
        elif cmd == '/liste':
            send(watch_list_text(raw_watch))
        elif cmd in ('/limit', '/an', '/aus'):
            try:
                idx = int(args[0]) - 1
                w = raw_watch[idx]
                if idx < 0: raise IndexError
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
            changed = True
        elif cmd in ('/hilfe', '/help'):
            send(HELP)
        else:
            send('Unbekannter Befehl. /hilfe zeigt alle Befehle.')
    return changed

# ---------------------------------------------------------------- Suche
def run_search(state, raw_watch):
    seen = set(state['seen'])
    first_done = set(state['first_run_done'])
    found = 0
    for w in map(compile_watch, raw_watch):
        if not w['active']:
            continue
        first = w['name'] not in first_done
        first_count = 0
        cands = {}
        key = lambda iid: f"{w['name']}|{iid}"          # „gesehen“ gilt pro Suche
        for q in w['queries']:
            try:
                page = vinted_get(search_url(q, w))
            except Blocked:
                raise
            except Exception as e:                         # einzelne Anfrage kaputt -> überspringen
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
            if first and first_count >= FIRST_RUN_MAX_PER_ITEM:
                seen.add(key(it['id'])); continue
            try:
                it.update(fetch_details(it))
            except Blocked:
                raise
            except Exception as e:
                log(f"Details übersprungen ({it['id']}): {e}")
                it.update({'desc': '', 'image': it.get('thumb', ''), 'uploaded': ''})
            time.sleep(random.uniform(2, 4))
            seen.add(key(it['id']))
            if not matches(w, it, it['desc']):
                continue
            if send_deal(w, it):
                seen.add(it['id'])                          # gemeldet: nie wieder, auch nicht von anderen Suchen
                found += 1; first_count += 1
        first_done.add(w['name'])
        state['seen'] = list(seen)[-20000:]                 # nach jeder Suche sichern
        state['first_run_done'] = sorted(first_done)
    state['seen'] = list(seen)[-20000:]
    state['first_run_done'] = sorted(first_done)
    return found

# ---------------------------------------------------------------- Hauptablauf
def main():
    if not TOKEN or not CHAT_ID:
        if not DRY_RUN:
            log('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID fehlen (GitHub-Secrets).'); sys.exit(1)
    state = {**default_state(), **load_json(STATE_FILE, {})}
    raw_watch = load_json(WATCH_FILE, [])
    if not state.get('commands_set') and not DRY_RUN:
        tg('setMyCommands', {'commands': [{'command': c, 'description': d} for c, d in BOT_COMMANDS]})
        state['commands_set'] = True
        send('🤖 Deal-Agent läuft jetzt in der Cloud (GitHub). Er sucht stündlich, auch wenn dein PC aus ist.\n\n' + HELP)

    if handle_commands(state, raw_watch):
        save_json(WATCH_FILE, raw_watch)

    due = time.time() - state['last_search'] >= (SEARCH_EVERY_MIN - 5) * 60
    if state['force_search'] or (state['enabled'] and due):
        state['force_search'] = False
        try:
            n = run_search(state, raw_watch)
            state['blocked_notified'] = False
            state['stats']['searches'] += 1; state['stats']['deals'] += n; state['stats']['last_deals'] = n
            log(f'Suche fertig: {n} neue Deals')
        except Blocked as e:
            log('Vinted blockiert:', e)
            if not state['blocked_notified']:
                send('⚠️ Vinted hat die Cloud-Suche gerade blockiert. Ich versuche es beim nächsten Lauf erneut.')
                state['blocked_notified'] = True
        except Exception as e:  # nie den ganzen Lauf abbrechen
            log('Fehler bei der Suche:', repr(e))
        state['last_search'] = int(time.time())
    else:
        log('Keine Suche fällig' if state['enabled'] else 'Pausiert')

    save_json(STATE_FILE, state)
    with open(os.path.join(HERE, 'last_run.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(RUN_LOG[-200:]) + '\n')

if __name__ == '__main__':
    main()
