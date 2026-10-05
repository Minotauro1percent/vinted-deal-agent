"""Lokaler Test für agent.py v2: Vinted-Seiten und Telegram werden simuliert.
Start:  python3 tests/test_agent.py
"""
import json, os, shutil, sys, tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
shutil.copy(os.path.join(ROOT, 'agent.py'), tmp)
os.environ.update(TELEGRAM_BOT_TOKEN='TEST', TELEGRAM_CHAT_ID='42')
sys.path.insert(0, tmp)
import agent
agent.time.sleep = lambda s: None
VIE = ZoneInfo('Europe/Vienna')
def ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=VIE).timestamp()

ok = True
def check(cond, label):
    global ok
    print(('PASS ' if cond else 'FAIL ') + label); ok = ok and bool(cond)

# ------------------------------------------------------------------ Fixtures
def card(iid, title, href):
    return (f'<div data-testid="grid-item"><div data-testid="product-item-id-{iid}">'
            f'<img src="https://img/{iid}.webp" data-testid="product-item-id-{iid}--image--img">'
            f'<a href="{href}?referrer=catalog" title="{title}" data-testid="product-item-id-{iid}--overlay-link"></a></div></div>')

CARDS = {
    'ralph': [
        card('101', 'Polo Ralph Lauren Cable Knit Sweater, Marke: Polo Ralph Lauren, Zustand: Sehr gut, Größe: M, 15.00 €, 16.45 €', '/items/101-rl'),
        card('102', 'Pull torsadé Chaps, Marke: Chaps Ralph Lauren, Zustand: Sehr gut, Größe: M, 10.00 €, 11.20 €', '/items/102-chaps'),
        card('103', 'Polo Ralph Lauren Pullover, Marke: Polo Ralph Lauren, Zustand: Sehr gut, Größe: L, 17.00 €, 18.55 €', '/items/103-rl'),
        card('104', 'Ralph Lauren Pullover Zopf, Marke: Polo Ralph Lauren, Zustand: Sehr gut, Größe: XL, 12.00 €, 13.30 €', '/items/104-rl'),
        card('105', 'Polo Ralph Lauren Zopfstrick, Marke: Polo Ralph Lauren, Zustand: Sehr gut, Größe: L, 16.00 €, 17.10 €', '/items/105-rl'),
    ],
    'barbour': [card('201', 'Barbour Bedale, Marke: Barbour, Zustand: Gut, Größe: L, 75.00 €, 79.45 €', '/items/201-bb')],
    'xiaomi': [
        card('301', 'Xiaomi 17 256GB schwarz, Marke: Xiaomi, Zustand: Neu, 450.00 €, 473.20 €', '/items/301-x'),
        card('302', 'Xiaomi 17 128 GB, Marke: Xiaomi, Zustand: Neu, 400.00 €, 420.70 €', '/items/302-x'),
        card('303', 'Xiaomi 17 256 GB, Marke: Xiaomi, Zustand: Neu, 430.00 €, 452.20 €', '/items/303-x'),
        card('304', 'Xiaomi 17 Hülle 256GB, Marke: Xiaomi, Zustand: Neu, 10.00 €, 11.20 €', '/items/304-x'),
        card('305', 'Xiaomi 17 256GB, Marke: Xiaomi, Zustand: Sehr gut, 380.00 €, 399.70 €', '/items/305-x'),
    ],
    'moon': [card('401', 'Moon Boot Icon, Marke: Moon Boot, Zustand: Gut, Größe: 39, 25.00 €, 26.95 €', '/items/401-mb')],
}
# Pfad → (Beschreibung, Farbe, SIM-Lock)
DETAIL = {
    '/items/101-rl': ('Zopfstrick, 100% Baumwolle', 'Blau', ''),
    '/items/103-rl': ('Schöner <b>Zopfstrick</b>-Pulli<br>kaum getragen #ralph', 'Marineblau, Weiß', ''),
    '/items/105-rl': ('Zopfstrick', 'Rot', ''),
    '/items/201-bb': ('Klassische Wachsjacke', 'Grün', ''),
    '/items/301-x': ('Originalverpackt', 'Schwarz', 'Freigegeben'),
    '/items/302-x': ('OVP', 'Schwarz', 'Freigegeben'),
    '/items/303-x': ('OVP', 'Blau', 'Gesperrt'),
}
def attr(code, title, value):
    return r'\"code\":\"%s\",\"data\":{\"title\":\"%s\",\"value\":\"%s\"}' % (code, title, value)

requests = []
def fake_vinted_get(path):
    requests.append(path)
    if path.startswith('/catalog'):
        q = path.lower()
        key = next((k for k in CARDS if k in q), None)
        return '<html><body>' + ''.join(CARDS.get(key, [])) + '</body></html>'
    desc, color, sim = DETAIL.get(path, ('ohne Angabe', '', ''))
    parts = [attr('upload_date', 'Hochgeladen', '7 Min.')]
    if color: parts.append(attr('color', 'Farbe', color))
    if sim: parts.append(attr('sim_lock', 'SIM-Lock', sim))
    return (f'<html><head><meta property="og:image" content="https://img/big{path[-6:]}.webp"></head><body>'
            f'<div itemprop="description"><span>{desc}</span></div>'
            '<script>x=[' + ','.join('{' + p + '}' for p in parts) + ']</script></body></html>')
agent.vinted_get = fake_vinted_get

sent, updates = [], []
def fake_tg(method, params):
    if method == 'getUpdates':
        return {'ok': True, 'result': [u for u in updates if u['update_id'] >= params['offset']]}
    sent.append((method, params))
    return {'ok': True}
agent.tg = fake_tg

def msg(uid, text, chat='42'):
    return {'update_id': uid, 'message': {'chat': {'id': int(chat)}, 'text': text}}

NOW = [ts(2026, 10, 5, 7, 30)]          # Montag 07:30 Wien
agent.time.time = lambda: NOW[0]
def run():
    sent.clear(); requests.clear(); agent.main()
    return json.load(open(agent.STATE_FILE))
def photos():
    return [p for m, p in sent if m == 'sendPhoto']
def texts():
    return [p['text'] for m, p in sent if m == 'sendMessage']
def wl():
    return json.load(open(agent.WATCH_FILE))
def st():
    return json.load(open(agent.SETTINGS_FILE))
def write(path, data):
    json.dump(data, open(path, 'w'))

# ------------------------------------------------------------------ 1) Migration von v1
V1_WATCH = [
    {'name': 'RL Zopfstrick', 'queries': 'ralph lauren zopf', 'brands': 'Polo Ralph Lauren, Ralph Lauren',
     'require': 'zopf, cable', 'exclude': '', 'maxPrice': 20, 'resaleMin': 35, 'resaleMax': 45,
     'sizes': 'M, L', 'minCondition': 'Sehr gut'},
    {'name': 'Barbour', 'queries': 'barbour', 'brands': 'Barbour', 'maxPrice': 80, 'resaleMin': 145, 'resaleMax': 210},
]
write(agent.WATCH_FILE, V1_WATCH)
write(agent.STATE_FILE, {'enabled': True, 'tg_offset': 0, 'commands_set': True, 'last_search': NOW[0] - 3700,
                         'seen': ['RL Zopfstrick|102', 'Barbour|999', '555'], 'first_run_done': ['RL Zopfstrick']})
s = run()
w = wl()
check(os.path.exists(agent.SETTINGS_FILE) and st()['enabled'] is True, 'v1: settings.json angelegt, enabled übernommen')
check('enabled' not in s, 'v1: enabled nicht mehr im Zustand')
check([x['id'] for x in w] == ['rl-zopfstrick', 'barbour'], 'v1: Suchen bekommen IDs')
check(all(x['schedule'] == {'mode': 'default'} and x['category'] == 'kleidung' for x in w), 'v1: Kategorie + Standard-Zeitplan')
check('rl-zopfstrick|102' in s['seen'] and '555' in s['seen'], 'v1: gesehene Artikel umgeschlüsselt')
check('/items/102-chaps' not in requests, 'v1: bereits verworfener Artikel nicht erneut geprüft')
check('rl-zopfstrick' in s['first_run_done'], 'v1: first_run_done umgeschlüsselt')
ph = photos()
check(len(ph) == 4, f'4 Deals: RL 101, 103 (über Beschreibung), 105 + Barbour 201: {len(ph)}')
check(not any('/items/102' in p['caption'] or '/items/104' in p['caption'] for p in ph), 'Chaps und Größe XL nicht gemeldet')

# ------------------------------------------------------------------ 2) Keine Suche vor Ablauf
NOW[0] += 600
s = run()
check(not photos() and not any(r.startswith('/catalog') for r in requests), 'Nichts fällig nach 10 Min.')

# ------------------------------------------------------------------ 3) Neue v2-Suchen (Web-Oberfläche)
w = wl()
w[0]['colors'] = ['Blau', 'Hellblau', 'Marineblau']
w.append({'id': 'xiaomi-17', 'name': 'Xiaomi 17', 'category': 'technik', 'active': True,
          'queries': ['xiaomi 17'], 'brands': ['Xiaomi'], 'exclude': ['hülle', 'case'], 'maxPrice': 500,
          'storage': ['256 GB'], 'simFree': True, 'minCondition': 'Neu',
          'schedule': {'mode': 'interval', 'everyMin': 30}})
w.append({'id': 'moon-boot', 'name': 'Moon Boot', 'category': 'schuhe', 'active': True, 'queries': ['moon boot'],
          'maxPrice': 28, 'sizes': ['39', '40'], 'schedule': {'mode': 'times', 'days': [1], 'times': ['08:00']}})
write(agent.WATCH_FILE, w)
s = run()                              # Mo 07:40
check(s['watch_last'].get('moon-boot'), 'Feste Zeiten: neue Suche merkt sich Startpunkt')
check(not any('moon' in r for r in requests), 'Feste Zeiten: vor 08:00 keine Suche')
ph = photos()
check([p for p in ph if '/items/301' in p['caption']], 'Technik: 256 GB + SIM-frei + Neu gemeldet')
check(not [p for p in ph if any(f'/items/{i}' in p['caption'] for i in ('302', '303', '304', '305'))],
      'Technik: 128 GB, gesperrt, Hülle, gebraucht verworfen')
check(any('SIM-Lock: Freigegeben' in p['caption'] for p in ph), 'SIM-Lock steht in der Nachricht')

NOW[0] = ts(2026, 10, 5, 8, 7)        # Mo 08:07
s = run()
check(any('moon' in r for r in requests), 'Feste Zeiten: Mo 08:07 wird gesucht')
check(any('xiaomi' in r for r in requests), 'Intervall 30 Min.: nach 27 Min. fällig (Toleranz 5 Min.)')
NOW[0] = ts(2026, 10, 5, 8, 22)
s = run()
check(not any('moon' in r for r in requests), 'Feste Zeiten: nur einmal pro Termin')
check(not any('xiaomi' in r for r in requests), 'Intervall 30 Min.: nach 15 Min. noch nicht')

# ------------------------------------------------------------------ 4) Farben (Unit)
c = agent.compile_watch(wl()[0])
base = {'title': 'Polo Ralph Lauren Zopf', 'brand': 'Polo Ralph Lauren', 'condition': 'Sehr gut', 'size': 'M', 'price': 15}
check(agent.matches(c, base) is None, 'Farbfilter → Detailseite nötig')
check(agent.matches(c, base, {'desc': '', 'color': 'Marineblau, Weiß'}) is True, 'Farbe Marineblau passt')
check(agent.matches(c, base, {'desc': '', 'color': 'Rot'}) is False, 'Farbe Rot passt nicht')
check(agent.matches(c, {**base, 'size': '12 Jahre / 152 cm'}) is False, 'Kindergröße verworfen')
cs = agent.compile_watch({'name': 's', 'sizes': ['36', '42,5']})
check(agent.matches(cs, {**base, 'size': 'S / 36 / 8'}) is True, 'Größe 36 passt zu „S / 36 / 8“')
check(agent.matches(cs, {**base, 'size': '42.5'}) is True, 'Größe 42,5 = 42.5')
check(agent.matches(cs, {**base, 'size': 'M / 38 / 10'}) is False, 'Größe 38 passt nicht')
cb = agent.compile_watch({'name': 'b', 'colors': ['Blau', 'Grün']})
check(agent.matches(cb, base, {'desc': '', 'color': 'Hellblau'}) is True, 'Farbfamilie: Blau passt zu Hellblau')
check(agent.matches(cb, base, {'desc': '', 'color': 'Dunkelgrün'}) is True, 'Farbfamilie: Grün passt zu Dunkelgrün')
check(agent.matches(cb, {**base, 'title': 'Navy cable knit'}, {'desc': '', 'color': ''}) is True, 'Ohne Farbangabe: „navy“ im Titel zählt als Blau')
check(agent.matches(cb, {**base, 'title': 'Pulli rot'}, {'desc': '', 'color': ''}) is False, 'Ohne Farbangabe: rot ≠ Blau/Grün')

# ------------------------------------------------------------------ 5) Zeitplan-Rechnung (Unit)
mon8 = {'id': 'x', 'schedule': {'mode': 'times', 'days': [1], 'times': ['08:00']}}
stt = {'watch_last': {'x': ts(2026, 10, 5, 8, 7)}}
check(not agent.is_due(mon8, {}, stt, ts(2026, 10, 9, 12)), 'Mo 08:00: Freitag nicht fällig')
check(agent.is_due(mon8, {}, stt, ts(2026, 10, 12, 8, 1)), 'Mo 08:00: nächster Montag fällig')
check(agent.next_run_ts(mon8, {}, stt, ts(2026, 10, 6, 9)) == ts(2026, 10, 12, 8), 'Nächster Termin = Mo 12.10. 08:00')
check(agent.schedule_text(mon8, {}) == 'Mo 08:00', 'Text „Mo 08:00“')
wk = {'id': 'y', 'schedule': {'mode': 'times', 'days': [1, 2, 3, 4, 5], 'times': ['07:30', '18:00']}}
check(agent.schedule_text(wk, {}) == 'Mo–Fr 07:30, 18:00', 'Text „Mo–Fr 07:30, 18:00“')
# Zeitumstellung: So 25.10.2026 03:00 → 02:00 (Sommer→Winter)
sun = {'id': 'z', 'schedule': {'mode': 'times', 'days': [7], 'times': ['09:00']}}
check(agent.next_run_ts(sun, {}, {'watch_last': {}}, ts(2026, 10, 24, 12)) == ts(2026, 10, 25, 9), 'Zeitumstellung: So 09:00 Ortszeit')
dflt = agent.compile_watch({'name': 'a'})
check(agent.schedule_text(dflt, {'default_interval_min': 60}) == 'stündlich (Standard)', 'Text „stündlich (Standard)“')
check(agent.schedule_text(dflt, {'default_interval_min': 180}) == 'alle 3 Std. (Standard)', 'Text „alle 3 Std.“')
check(agent.interval_min({'schedule': {'mode': 'interval', 'everyMin': 5}}, {}) == 15, 'Intervall mindestens 15 Min.')

# ------------------------------------------------------------------ 6) Befehle
updates += [msg(1, '/status'), msg(2, '/liste'), msg(3, '/limit 2 95'), msg(4, '/aus 2'), msg(5, '/stop'), msg(6, '/stop', chat='999')]
NOW[0] += 60
s = run()
t = texts()
check(any('Läuft' in x and 'Als Nächstes' in x for x in t), '/status mit nächsten Terminen')
check(any('Deine Suchen' in x and 'Mo 08:00' in x for x in t), '/liste zeigt Zeitplan')
check(wl()[1]['maxPrice'] == 95 and wl()[1]['active'] is False, '/limit und /aus gespeichert')
check(st()['enabled'] is False, '/stop → settings.enabled = false')
check(s['tg_offset'] == 6 and sum('pausiert' in x for x in t) == 1, 'Offset weiter, fremder Chat ignoriert')

NOW[0] += 7200
s = run()
check(not any(r.startswith('/catalog') for r in requests), 'Pausiert: keine Suche')

# „Jetzt suchen“ für eine einzelne Suche aus der Web-Oberfläche (auch wenn pausiert)
x = st(); x['force'] = {'barbour': int(NOW[0])}; write(agent.SETTINGS_FILE, x)
s = run()
check(any('barbour' in r for r in requests) and not any('xiaomi' in r for r in requests), 'Einzel-Suche sofort (Web)')
s = run()
check(not any(r.startswith('/catalog') for r in requests), 'Einzel-Suche nur einmal')

updates.append(msg(7, '/start'))
s = run()
check(st()['enabled'] is True and any('Agent läuft' in x for x in texts()), '/start schaltet ein')
check(any('xiaomi' in r for r in requests) and not any('barbour' in r for r in requests), '/start sucht alle aktiven, nicht die ausgeschalteten')
check(not photos(), 'Bereits gemeldete Deals nicht doppelt')

# ------------------------------------------------------------------ 7) Gelöschte Suche wird aufgeräumt
write(agent.WATCH_FILE, [x for x in wl() if x['id'] != 'moon-boot'])
s = run()
check('moon-boot' not in s['watch_last'], 'Gelöschte Suche aus Zustand entfernt')

print('\nALLE TESTS BESTANDEN' if ok else '\nES GIBT FEHLER')
sys.exit(0 if ok else 1)
