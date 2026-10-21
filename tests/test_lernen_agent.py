"""Ende-zu-Ende-Test der Lern-Suche im Agenten (Vinted, Telegram und Claude werden simuliert).
Die Anzeigenseiten sind wie echte Vinted-Seiten aufgebaut (Stand Okt. 2026, siehe debug/summary.txt).
"""
import json, os, shutil, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
for f in ('agent.py', 'learn.py'):
    shutil.copy(os.path.join(ROOT, f), tmp)
os.environ.update(TELEGRAM_BOT_TOKEN='TEST', TELEGRAM_CHAT_ID='42', ANTHROPIC_API_KEY='')
sys.path.insert(0, tmp)
import agent, learn
agent.time.sleep = lambda s: None

ok = True
def check(c, label):
    global ok
    print(('PASS ' if c else 'FAIL ') + label); ok = ok and bool(c)

# ------------------------------------------------------------------ Vinted-Seiten
ITEMS = {}   # id → dict
def item(iid, title, brand, size, cond, price, color, cat, desc):
    ITEMS[iid] = dict(id=iid, title=title, brand=brand, size=size, cond=cond, price=price, color=color, cat=cat, desc=desc)
# Beispiele (teurer, Herren)
EX = [
    ('9001', 'Polo Ralph Lauren Cable Knit Sweater navy', 'M', 55, 'Marineblau', 'Zopfstrick Baumwolle #cableknit'),
    ('9002', 'Ralph Lauren Zopfstrick Pullover creme', 'L', 60, 'Creme', 'cable knit, kaum getragen'),
    ('9003', 'Polo Ralph Lauren cable knit jumper grün', 'M', 48, 'Dunkelgrün', 'classic cable knit cotton'),
    ('9004', 'Ralph Lauren Cable Knit Pullover grau', 'L', 52, 'Grau', 'Zopfmuster, Pony Logo'),
    ('9005', 'Polo Ralph Lauren Cable-Knit Cotton Sweater', 'M', 65, 'Blau', 'cable knit #preppy'),
    ('9006', 'Ralph Lauren cable knit sweater rot', 'M', 45, 'Rot', 'Zopf Pullover'),
]
for iid, t, sz, pr, col, d in EX:
    item(iid, t, 'Ralph Lauren', sz, 'Sehr gut', pr, col, 'Herren Strickpullover', d)
# neue Anzeigen im Katalog
item('101', 'Ralph Lauren Cable Knit Pullover Herren', 'Ralph Lauren', 'L', 'Sehr gut', 18, 'Marineblau', 'Herren Strickpullover', 'Zopfstrick Baumwolle')
item('102', 'Ralph Lauren Poloshirt weiß', 'Ralph Lauren', 'M', 'Sehr gut', 12, 'Weiß', 'Herren Poloshirts', 'slim fit')
item('103', 'Ralph Lauren cable knit Pullover Damen rosa', 'Ralph Lauren', 'M', 'Sehr gut', 15, 'Rosa', 'Damen Strickpullover', 'pull torsadé femme')
item('104', 'Ralph Lauren Cable Knit Cardigan', 'Ralph Lauren', 'M', 'Sehr gut', 17, 'Marineblau', 'Herren Strickjacken', 'cable knit cardigan Strickjacke')
item('105', 'Ralph Lauren cable knit sweater', 'Ralph Lauren', 'M', 'Gut', 45, 'Grau', 'Herren Strickpullover', 'zu teuer')
item('106', 'Ralph Lauren Cable Knit Jumper beige', 'Ralph Lauren', 'M', 'Sehr gut', 19, 'Beige', 'Herren Strickpullover', 'cable knit cotton')
CATALOG_IDS = ['101', '102', '103', '104', '105']

def a(code, title, value, url=False):
    extra = r'\"url\":\"/catalog?x=1\",' if url else ''
    return r'{\"code\":\"%s\",\"data\":{\"id\":1,\"title\":\"%s\",%s\"value\":\"%s\"},\"type\":\"faq\"}' % (code, title, extra, value)

def item_page(i):
    attrs = ','.join([a('size', 'Größe', i['size'], url=True), a('status', 'Zustand', i['cond']), a('color', 'Farbe', i['color']),
                      a('upload_date', 'Hochgeladen', '3 Min.')])
    return (f'<html><head><title>{i["title"]} | Vinted</title><meta property="og:title" content="{i["title"]} | Vinted"/>'
            f'<meta property="og:image" content="https://img/{i["id"]}/f800/x.webp?s=1"/></head><body>'
            f'<div itemProp="description"><span>{i["desc"]}</span></div>'
            '<script>self.__next_f.push([1,"' + r'{\"item\":{\"brand_dto\":{\"id\":\"88\",\"slug\":\"x\",\"title\":\"' + i['brand'] + r'\"},\"x\":1}' + attrs + '"])</script>'
            '<script type="application/ld+json">{"offers":{"priceCurrency":"EUR","price":' + str(i['price']) +
            ',"availability":"InStock","itemCondition":"UsedCondition"},"category":"' + i['cat'] + '","color":"' + i['color'] + '"}</script></body></html>')

def card(i):
    t = f"{i['title']}, Marke: {i['brand']}, Zustand: {i['cond']}, Größe: {i['size']}, {i['price']:.2f} €, {i['price'] * 1.05 + 0.7:.2f} €"
    return (f'<div data-testid="grid-item"><img src="https://img/{i["id"]}/310x430/t.webp?s=2" data-testid="product-item-id-{i["id"]}--image--img">'
            f'<a href="/items/{i["id"]}-x" title="{t}" data-testid="product-item-id-{i["id"]}--overlay-link"></a></div>')

requests = []
def fake_vinted_get(path):
    requests.append(path)
    if path.startswith('/catalog'):
        return '<html>' + ''.join(card(ITEMS[i]) for i in CATALOG_IDS) + '</html>'
    iid = path.split('/items/')[1].split('-')[0]
    if iid not in ITEMS:
        raise RuntimeError('HTTP 404 für ' + path)
    return item_page(ITEMS[iid])
agent.vinted_get = fake_vinted_get

sent, updates = [], []
def fake_tg(method, params):
    if method == 'getUpdates':
        return {'ok': True, 'result': [u for u in updates if u['update_id'] >= params['offset']]}
    sent.append((method, params)); return {'ok': True}
agent.tg = fake_tg

def msg(uid, text):
    return {'update_id': uid, 'message': {'chat': {'id': 42}, 'text': text}}
def cb(uid, data, mid=500):
    return {'update_id': uid, 'callback_query': {'id': f'cb{uid}', 'data': data, 'message': {'chat': {'id': 42}, 'message_id': mid}}}

NOW = [1792600000.0]
agent.time.time = lambda: NOW[0]
def run():
    sent.clear(); requests.clear(); agent.main(); NOW[0] += 360
    return json.load(open(agent.STATE_FILE))
def texts():
    return [p.get('text', '') for m, p in sent if m == 'sendMessage']
def photos():
    return [p for m, p in sent if m == 'sendPhoto']
json.dump([], open(agent.WATCH_FILE, 'w'))
json.dump({'commands_set': True, 'commands_version': 3}, open(agent.STATE_FILE, 'w'))

# 1) Lern-Suche anlegen + Links schicken
updates += [msg(1, '/lernen RL Zopf 20'),
            msg(2, 'Schau mal https://www.vinted.at/items/9001-polo-ralph https://www.vinted.de/items/9002?utm_source=share\nhttps://www.vinted.at/items/9003-x'),
            msg(3, 'https://www.vinted.at/items/9004-a https://www.vinted.at/items/9005-b https://www.vinted.at/items/9006-c https://www.vinted.at/items/9999-geloescht')]
s = run()
ph1 = photos()
wl = json.load(open(agent.WATCH_FILE))
check(wl and wl[0]['type'] == 'lernen' and wl[0]['maxPrice'] == 20 and wl[0]['schedule'] == {'mode': 'live'}, 'Lern-Suche per /lernen angelegt (bis 20 €, laufend)')
profs = json.load(open(agent.PROFILES_FILE))
p = profs['rl-zopf']['profile']; ex = profs['rl-zopf']['examples']
check(len(ex) == 6, f'6 Beispiele gelernt, gelöschte Anzeige übersprungen: {len(ex)}')
check(ex[0]['brand'] == 'ralph lauren' and ex[0]['size'] == 'M' and ex[0]['price'] == 55 and ex[0]['category'] == 'Herren Strickpullover', 'Felder aus Anzeigenseite: Marke, Größe, Preis, Kategorie')
check(ex[1]['colors'] == ['creme'] and 'cable knit' in ex[1]['desc'] and ex[0]['photo'].startswith('https://img/9001/f800'), 'Farbe, Beschreibung, Bild')
t = '\n'.join(texts())
check('6 Anzeige(n) gelernt' in t and '1 nicht lesbar' in t, 'Telegram: Rückmeldung mit Zählung')
check('Sucht nach' in t and 'cable knit' in t, 'Telegram: Zusammenfassung mit selbst gefundenen Suchbegriffen')
check(any('cable knit' in q for q in p['queries']), 'Suchbegriffe gelernt: ' + str(p['queries']))

# 2) Sucht direkt nach dem Lernen selbst (gleicher und nächster Lauf), meldet nur Passendes unter 20 €
print('Profil:', {k: p[k] for k in ('threshold', 'categories', 'sizes')})
s = run()
ph = ph1 + photos()
caps = ' '.join(x['caption'] for x in ph)
check(any('cable' in x['caption'] or True for x in ph) and s['watch_last'].get('rl-zopf'), 'Sucht mit gelernten Begriffen')
check('/items/101' in caps, 'Passender Herren-Zopfpulli (18 €) gemeldet')
check('/items/102' not in caps, 'Poloshirt nicht gemeldet')
check('/items/103' not in caps, 'Damen-Pullover nicht gemeldet (Kategorie gelernt)')
check('/items/105' not in caps, 'Über Höchstpreis nicht gemeldet')
check(all(x.get('reply_markup', {}).get('inline_keyboard') for x in ph), '👍/👎-Knöpfe unter jedem Treffer')
check(any('passt zu' in x['caption'] for x in ph), 'Ähnlichkeit steht in der Nachricht')
first_ids = [x['caption'] for x in ph]
cardigan_sent = '/items/104' in caps

# 3) Gleicher Katalog: nichts doppelt
s = run()
check(not photos(), 'Kein Treffer doppelt')

# 4) 👎 auf Cardigan (falls gemeldet) bzw. 👍 auf 101 → Profil lernt
updates += [cb(10, 'fb:u:rl-zopf:101', 501)]
if cardigan_sent:
    updates += [cb(11, 'fb:d:rl-zopf:104', 502)]
s = run()
profs = json.load(open(agent.PROFILES_FILE))
check(any(x['id'] == '101' and x['source'] == 'telegram' for x in profs['rl-zopf']['examples']), '👍 → Anzeige wird zum Beispiel')
check(any(m == 'editMessageReplyMarkup' and '👍 gemerkt' in json.dumps(p_, ensure_ascii=False) for m, p_ in sent), 'Knopf zeigt „👍 gemerkt“')
check(any(m == 'answerCallbackQuery' for m, _ in sent), 'Telegram-Antwort auf Knopfdruck')
if cardigan_sent:
    check(any(x['id'] == '104' for x in profs['rl-zopf']['negatives']), '👎 → Gegenbeispiel')

# 5) Bildprüfung: mit Schlüssel, niedriger Wert blockiert, Tageslimit zählt
agent.ANTHROPIC_KEY = 'sk-test'
calls = []
def fake_vision(key, ex_urls, cand, hint='', fetch=None, timeout=40, avoid=()):
    calls.append((ex_urls, cand)); return (30 if '106' in cand else 85), 'Zopfstrick passt', 0.0002
learn.vision_score = fake_vision
CATALOG_IDS.append('106')
item('108', 'Ralph Lauren Pullover', 'Ralph Lauren', 'M', 'Sehr gut', 16, 'Grau', 'Herren Strickpullover', 'schöner Pulli')
item('109', 'Ralph Lauren Pullover Herren', 'Ralph Lauren', 'L', 'Sehr gut', 14, 'Grau', 'Herren Strickpullover', 'warm')
CATALOG_IDS += ['108']
s = run()
check(any('/items/108' in x['caption'] for x in photos()), 'Allgemeiner Titel, Bild passt (85) → über Bildprüfung gemeldet')
check(calls and len(calls[0][0]) >= 1 and calls[0][1].startswith('https://img/106/310x430'), 'Bildprüfung: Beispielbilder + kleines Vorschaubild')
check(not any('/items/106' in x['caption'] for x in photos()), 'Bildprüfung 30 % → nicht gemeldet')
check(s['vision']['n'] >= 1 and s['vision']['usd'] > 0, 'Bildprüfung wird gezählt (Kosten)')
settings = json.load(open(agent.SETTINGS_FILE)) if os.path.exists(agent.SETTINGS_FILE) else {}
settings['vision_daily_max'] = 1; json.dump(settings, open(agent.SETTINGS_FILE, 'w'))
ITEMS['107'] = dict(ITEMS['101'], id='107'); CATALOG_IDS.append('107')
calls.clear(); s = run()
check(not calls and any('/items/107' in x['caption'] for x in photos()), 'Tageslimit erreicht → ohne Bildprüfung weiter')
CATALOG_IDS.append('109'); s = run()
check(not any('/items/109' in x['caption'] for x in photos()), 'Allgemeiner Titel ohne Bildprüfung → nicht gemeldet')

# 6) /profil, /fertig, Liste
updates += [msg(20, '/profil'), msg(21, '/fertig'), msg(22, '/liste')]
s = run()
t = '\n'.join(texts())
check('Beispiele' in t and 'Meldet ab' in t, '/profil zeigt Gelerntes')
check('🧠' in t and 'laufend' in t, '/liste zeigt Lern-Suche')
check(s['learn_target'] == '', '/fertig beendet Lern-Modus')

# 7) Links ohne aktive Lern-Suche bei nur einer Lern-Suche → landen dort
updates += [msg(30, 'https://www.vinted.at/items/101-x')]
s = run()
check(any('schon bekannt' in x for x in texts()), 'Bekannte Anzeige wird erkannt')

# 8) Eigene Ausschlusskriterien (aus der Web-Oberfläche)
wl8 = json.load(open(agent.WATCH_FILE))
wl8[0]['excludeRules'] = ['V-Ausschnitt', 'Reißverschluss / Zip', 'loch']
json.dump(wl8, open(agent.WATCH_FILE, 'w'))
item('120', 'Ralph Lauren Cable Knit V-Neck Pullover', 'Ralph Lauren', 'M', 'Sehr gut', 15, 'Grau', 'Herren Strickpullover', 'cable knit')
item('121', 'Ralph Lauren Cable Knit Pullover', 'Ralph Lauren', 'M', 'Sehr gut', 15, 'Grau', 'Herren Pullover mit V-Ausschnitt', 'cable knit')
item('122', 'Ralph Lauren Cable Knit Pullover navy', 'Ralph Lauren', 'M', 'Sehr gut', 15, 'Marineblau', 'Herren Strickpullover', 'cable knit, kleines Loch am Ärmel')
item('123', 'Ralph Lauren Cable Knit Half Zip', 'Ralph Lauren', 'M', 'Sehr gut', 15, 'Grau', 'Herren Strickpullover', 'cable knit')
item('124', 'Ralph Lauren Cable Knit Pullover creme', 'Ralph Lauren', 'L', 'Sehr gut', 15, 'Creme', 'Herren Strickpullover', 'cable knit Zopfstrick')
CATALOG_IDS[:] = ['120', '121', '122', '123', '124']
avoid_seen = []
def fake_vision2(key, ex_urls, cand, hint='', fetch=None, timeout=40, avoid=()):
    avoid_seen.append(list(avoid)); return 90, 'Zopfstrick', 0.0002
learn.vision_score = fake_vision2
settings = json.load(open(agent.SETTINGS_FILE)); settings['vision_daily_max'] = 150; json.dump(settings, open(agent.SETTINGS_FILE, 'w'))
s = run()
caps = ' '.join(x['caption'] for x in photos())
check('/items/120' not in caps, 'Ausschluss V-Ausschnitt (Titel „V-Neck“)')
check('/items/121' not in caps, 'Ausschluss V-Ausschnitt (Vinted-Kategorie)')
check('/items/122' not in caps, 'Ausschluss freies Wort „loch“ (Beschreibung)')
check('/items/123' not in caps, 'Ausschluss Zip')
check('/items/124' in caps, 'Passender Pulli ohne Ausschluss gemeldet')
check(avoid_seen and 'V-Ausschnitt' in avoid_seen[-1], 'Bildprüfung bekommt die Ausschlüsse mit')

print('\nALLE LERN-AGENT-TESTS BESTANDEN' if ok else '\nES GIBT FEHLER'); sys.exit(0 if ok else 1)
