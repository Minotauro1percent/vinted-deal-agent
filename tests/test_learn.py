"""Test der Lern-Logik mit realistischen Beispielen (ohne Netz)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import learn
from collections import Counter

def it(title, brand, size, cond, price, color='', desc=''):
    return learn.item_features({'id': str(abs(hash(title)) % 10**9), 'title': title, 'brand': brand, 'size': size,
                                'condition': cond, 'price': price, 'color': color, 'desc': desc})

POS = [
    it('Polo Ralph Lauren Cable Knit Sweater', 'Polo Ralph Lauren', 'M', 'Sehr gut', 45, 'Marineblau', 'Zopfstrick Pullover 100% Baumwolle #ralphlauren #cableknit'),
    it('Ralph Lauren Zopfstrick Pullover creme', 'Polo Ralph Lauren', 'L', 'Sehr gut', 55, 'Creme', 'Cable knit, kaum getragen'),
    it('Polo Ralph Lauren cable knit jumper navy', 'Polo Ralph Lauren', 'L / 40 / 12', 'Gut', 38, 'Marineblau', 'Classic cable knit cotton jumper'),
    it('Ralph Lauren Cable Knit Pullover grün', 'Polo Ralph Lauren', 'M', 'Neu', 60, 'Dunkelgrün', 'Zopfmuster, Pony Logo'),
    it('Pull torsadé Ralph Lauren', 'Polo Ralph Lauren', 'M', 'Très bon état', 40, 'Grau', 'Pull en maille torsadée coton'),
    it('Polo Ralph Lauren Strickpullover Zopfmuster', 'Polo Ralph Lauren', 'L', 'Sehr gut', 50, 'Beige', 'Cable knit sweater Baumwolle'),
    it('Polo Ralph Lauren Cable-Knit Cotton Sweater', 'Polo Ralph Lauren', 'M', 'Sehr gut', 48, 'Blau', 'cable knit #vintage #preppy'),
    it('Ralph Lauren cable knit sweater red', 'Polo Ralph Lauren', 'M', 'Gut', 35, 'Rot', 'Zopf Pullover rot'),
]
# „normale“ Anzeigen aus denselben Suchen → Hintergrund
BG_TITLES = ['Ralph Lauren Poloshirt', 'Polo Ralph Lauren Hemd', 'Ralph Lauren Hoodie', 'Ralph Lauren Pullover V-Ausschnitt',
             'Ralph Lauren Strickjacke', 'Ralph Lauren Kinder Pullover', 'Lauren Ralph Lauren Kleid', 'Polo Ralph Lauren Cap',
             'Ralph Lauren Wollpullover', 'Ralph Lauren Sweatshirt', 'Ralph Lauren Cardigan Zopf', 'Tommy Hilfiger Pullover'] * 3
bg = Counter()
for t in BG_TITLES:
    bg.update(learn.tokens(t))
bg['__docs__'] = len(BG_TITLES)

ok = True
def check(c, label):
    global ok
    print(('PASS ' if c else 'FAIL ') + label); ok = ok and bool(c)

prof = learn.build_profile(POS, background=bg)
print(learn.summary(prof, 'RL Zopf').replace('<b>', '').replace('</b>', ''))
print('Schwelle', prof['threshold'], 'Queries', prof['queries'])
check(list(prof['brands'])[0] == 'polo ralph lauren', 'Hauptmarke erkannt')
check(set(prof['sizes']) >= {'m', 'l'}, 'Größen M und L erkannt')
check(any('cable knit' in q for q in prof['queries']), 'Suchbegriff „cable knit“ selbst gefunden: ' + str(prof['queries']))
check('cable knit' in prof['weights'] or 'cable' in prof['weights'], '„cable knit“ ist typisches Wort')
check('pullover' not in list(prof['weights'])[:3], '„pullover“ allein ist nicht das wichtigste Wort (zu allgemein)')

def s(x, detail=True):
    return learn.score(prof, x, detail)['score']
good = it('Polo Ralph Lauren Cable Knit Pullover Herren', 'Polo Ralph Lauren', 'L', 'Sehr gut', 18, 'Blau', 'Zopfstrick Baumwolle')
good_title = it('Ralph Lauren cable knit sweater', 'Polo Ralph Lauren', 'M', 'Gut', 15)
bad = [
    it('Ralph Lauren Poloshirt weiß', 'Polo Ralph Lauren', 'M', 'Sehr gut', 12, 'Weiß', 'Slim fit polo'),
    it('Polo Ralph Lauren Hemd Oxford', 'Polo Ralph Lauren', 'L', 'Sehr gut', 15, 'Hellblau', 'Oxford shirt'),
    it('Tommy Hilfiger Cable Knit Pullover', 'Tommy Hilfiger', 'M', 'Sehr gut', 14, 'Marineblau', 'Zopfstrick'),
    it('Ralph Lauren Cable Knit Pullover Kinder', 'Polo Ralph Lauren', '10 Jahre / 140 cm', 'Gut', 12, 'Rot', 'Kinderpullover'),
    it('Ralph Lauren Pullover V-Ausschnitt', 'Polo Ralph Lauren', 'XXL', 'Gut', 16, 'Grau', 'Merino'),
]
thr = prof['threshold']
check(s(good) >= thr, f'Passende Anzeige über Schwelle ({s(good)} ≥ {thr})')
check(s(good_title, False) >= thr * 0.7, f'Passender Titel besteht Vorprüfung ({s(good_title, False)})')
for b in bad:
    check(s(b) < thr, f'Unpassend unter Schwelle: {b["title"]} ({s(b)})')

# 👎 auf Tommy-Pullover → Schwelle geht nicht über die meisten Beispiele, Tommy bleibt drunter
prof2 = learn.build_profile(POS, negatives=[bad[2]], background=bg)
check(learn.score(prof2, bad[2], True)['score'] < prof2['threshold'] and learn.score(prof2, good, True)['score'] >= prof2['threshold'], '👎 Tommy: bleibt draußen, Passendes kommt weiter durch')
card = it('Polo Ralph Lauren Cable Knit Cardigan', 'Polo Ralph Lauren', 'M', 'Sehr gut', 17, 'Marineblau', 'Cable knit Strickjacke Baumwolle')
print('Cardigan vorher', s(card))
prof3 = learn.build_profile(POS, negatives=[card, it('Ralph Lauren Zopf Strickjacke cardigan', 'Polo Ralph Lauren', 'L', 'Gut', 15, 'Creme', 'cable knit cardigan')], background=bg)
check(learn.score(prof3, card, True)['score'] < prof3['threshold'], f"2× 👎 Cardigan: danach unter Schwelle ({learn.score(prof3, card, True)['score']} < {prof3['threshold']})")
check(learn.score(prof3, good, True)['score'] >= prof3['threshold'], 'Pullover kommt nach 👎 Cardigan weiter durch')
# Wenige Beispiele: trotzdem brauchbar
small = learn.build_profile(POS[:3], background=bg)
check(small and small['queries'] and learn.score(small, good, True)['score'] >= small['threshold'], 'Schon mit 3 Beispielen brauchbar')
u = 'https://images1.vinted.net/t/01_abc/f800/1700000.webp?s=x'
check(learn.small_image(u) == u, 'Signierte Bild-URL bleibt unverändert')

# Bildprüfung: Antwort robust auslesen (ohne Netz)
import io, json as _json
class _R:
    def __init__(self, text): self.b = _json.dumps({'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 2000, 'output_tokens': 30}, 'stop_reason': 'end_turn'}).encode()
    def read(self): return self.b
    def __enter__(self): return self
    def __exit__(self, *a): pass
for text, want in [('{"score": 82, "grund": "Zopfstrick"}', 82), ('```json\n{"Score": 40, "Grund": "glatt"}\n```', 40),
                   ('Bewertung: 15 – kein Zopfmuster', 15), ('Ich kann das nicht beurteilen.', None)]:
    learn.urllib.request.urlopen = lambda req, timeout=40, _t=text: _R(_t)
    sc, why, cost = learn.vision_score('k', ['https://x/a.jpg'], 'https://x/b.jpg', fetch=lambda u: b'\xff\xd8\xff' + b'0' * 10)
    check(sc == want and cost > 0, f'Bildprüfung liest „{text[:25]}…“ → {sc}')
print('\nALLE LERN-TESTS BESTANDEN' if ok else '\nES GIBT FEHLER'); sys.exit(0 if ok else 1)
