"""Lern-Suchen für den Vinted Deal-Agent.

Aus Beispiel-Anzeigen (Links, 👍/👎 in Telegram) entsteht ein Profil:
  • Marken, Größen, Farben, Zustände und Preise der Beispiele
  • typische Wörter und Hashtags (gewichtet gegen „normale“ Anzeigen aus denselben Suchen)
  • selbst erzeugte Suchbegriffe
  • eine Schwelle, ab der eine neue Anzeige gemeldet wird
Optional prüft Claude (Haiku) das Titelbild gegen Beispielbilder.

Nur Python-Standardbibliothek, keine Netzwerkzugriffe außer in vision_score().
"""
import base64, json, math, re, unicodedata, urllib.error, urllib.request
from collections import Counter

STOP = set('''
der die das und oder mit ohne für von vom zum zur im in am an auf aus bei bis ist sind ein eine einer eines einem einen
nur noch sehr gut neu top wie also auch kaum mal getragen tragen größe grosse groesse farbe zustand marke original
the and with for from size new good very worn used only like perfect condition great vintage item
le la les des du de et avec pour taille très tres bon etat état neuf porté porte un une
il lo gli della con per taglia molto buono nuovo usato e di da
st cm eur euro preis versand vb fest bitte gerne frage fragen keine tausch
verkaufe verkauf schnell schneller sofort abholung privatverkauf rücknahme ruecknahme garantie tierfreier rauchfreier haushalt
envoi rapide soigne soigné couleur porter sur mes autres articles annonce annonces achat lot prix negociable négociable dispo
pieces pièces voir photos photo bonne qualite qualité vends vend super belle beau
spedizione veloce vendo foto prezzo trattabile ottime ottimo condizioni
shipping fast sale selling price photos please check other items bundle
casual style tenue look outfit parfait parfaite idéal ideal
pas avant hesitez hésitez largeur longueur mesure mesures epaule épaule aisselle aisselles dos manches manche sous plus tres très etat état bon bonne neuf jamais peu
nicht noch kaum mal gerne einfach ganz siehe bild bilder länge breite achsel schulter
'''.split())
# Allgemeine Kleidungswörter: helfen nicht beim Unterscheiden (Zopfpulli vs. glatter Pulli)
GENERIC = set('''
logo brode brodee embroidered bestickt stick col rond neck crew ausschnitt rundhals collo
pullover pulli pull sweater sweatshirt jumper maglione maglia jersey strickpullover herren damen homme femme men women man woman
kinder kids enfant uomo donna size taille grosse groesse original originale
'''.split())
SIZE_ALIASES = {'xxs': 'xxs', 'xs': 'xs', 's': 's', 'm': 'm', 'l': 'l', 'xl': 'xl', 'xxl': 'xxl', '2xl': 'xxl', 'xxxl': 'xxxl', '3xl': 'xxxl'}
CONDITION_RANK = {'neu mit etikett': 5, 'neu, mit etikett': 5, 'neu': 4, 'sehr gut': 3, 'gut': 2, 'zufriedenstellend': 1}

# ------------------------------------------------------------------ Text
def fold(s):
    s = unicodedata.normalize('NFKD', str(s or '').lower())
    s = s.replace('ß', 'ss')
    return ''.join(c for c in s if not unicodedata.combining(c))

def tokens(text):
    """Wörter (≥3 Zeichen, ohne Füllwörter), Hashtags als Wort, plus Wortpaare."""
    t = fold(text)
    words = [w for w in re.findall(r'[a-z0-9]+', t) if len(w) >= 3 and w not in STOP and not w.isdigit()]
    pairs = [f'{a} {b}' for a, b in zip(words, words[1:])]
    return set(words) | set(pairs)

def size_keys(size):
    parts = [fold(p).strip() for p in re.split(r'[/|,]', str(size or '')) if p.strip()]
    out = set()
    for p in parts:
        p = p.replace(',', '.').replace('eu ', '').strip()
        out.add(SIZE_ALIASES.get(p, p))
    return out

def cond_rank(c):
    return CONDITION_RANK.get(fold(c).strip(), 0)

def item_features(it):
    """Merkmale einer Anzeige (aus Katalogkarte und optional Detailseite)."""
    colors = [fold(c).strip() for c in re.split(r'[,/]', it.get('color') or '') if c.strip()]
    return {
        'id': str(it.get('id', '')), 'url': it.get('url', ''), 'title': it.get('title', ''),
        'brand': fold(it.get('brand', '')).strip(), 'size': it.get('size', ''), 'condition': it.get('condition', ''),
        'price': float(it.get('price') or 0), 'colors': colors, 'desc': (it.get('desc') or '')[:600],
        'category': (it.get('category') or '').strip(),
        'photo': it.get('thumb') or it.get('image') or '',
    }

# ------------------------------------------------------------------ Profil
def _share(counter, n):
    return {k: round(v / n, 3) for k, v in counter.most_common() if v}

def build_profile(examples, negatives=(), background=None, strictness=1.0):
    """examples/negatives: Listen von item_features(). background: Counter mit Wortzahlen normaler Anzeigen."""
    pos = [e for e in examples if e.get('title')]
    n = len(pos)
    if not n:
        return None
    bg = Counter(background or {})
    n_bg = max(1, bg.pop('__docs__', 0))
    brands, sizes, colors, conds, cats = Counter(), Counter(), Counter(), Counter(), Counter()
    tok_df, title_ngrams = Counter(), Counter()
    prices = sorted(e['price'] for e in pos if e['price'])
    for e in pos:
        if e['brand']:
            brands[e['brand']] += 1
        for s in size_keys(e['size']):
            sizes[s] += 1
        for c in e['colors']:
            colors[c] += 1
        if e['condition']:
            conds[fold(e['condition'])] += 1
        if e.get('category'):
            cats[e['category']] += 1
        tok_df.update(tokens(f"{e['title']} {e['desc']}"))
        title_ngrams.update(_ngrams(e['title']))
    brand_words = set()
    for b in brands:
        brand_words |= set(re.findall(r'[a-z0-9]+', b))
    color_words = {w for c in colors for w in re.findall(r'[a-z]+', c)} | set(
        'schwarz weiss grau blau rot grun beige braun creme rosa pink lila gelb orange navy black white grey gray blue red green brown '
        'noir blanc gris bleu rouge vert marron nero bianco grigio blu rosso verde ecru'.split())
    neg_df = Counter()
    for e in negatives:
        neg_df.update(tokens(f"{e.get('title', '')} {e.get('desc', '')}"))
    # Wortgewichte: häufig in Beispielen, selten in normalen Anzeigen
    weights = {}
    for t, df in tok_df.items():
        if df < 2 and n >= 4:
            continue
        if set(t.split()) <= brand_words | GENERIC | color_words:
            continue                               # Marke und allgemeine Wörter („Pullover“) zählen nicht als „typisch“
        p = (df + 0.5) / (n + 1)
        q = (bg.get(t, 0) + 0.5) / (n_bg + 1)
        # Grundgewicht = wie oft das Wort in den Beispielen vorkommt; Bonus (bis 4×), wenn es in normalen Anzeigen selten ist.
        # So bleibt „cable knit“ wichtig, auch wenn deine anderen Suchen ebenfalls viele Zopfpullis finden.
        boost = max(0.0, min(3.0, math.log(p / q))) if bg else 1.0
        base = 0.5 + 0.5 * boost
        nr = neg_df.get(t, 0) / max(1, len(negatives))
        if nr > p:                                 # nur Wörter, die bei 👎 häufiger sind als bei den Beispielen
            base -= 3 * (nr - p)
        if base > 0.05:
            weights[t] = round(base * p, 4)
    weights = dict(sorted(weights.items(), key=lambda kv: -kv[1])[:80])
    # Wörter, die nur bei 👎 vorkommen (mind. 2×) und nie in den Beispielen → sprechen gegen eine Anzeige
    avoid = sorted(t for t, c in neg_df.items() if c >= 2 and ' ' not in t and not tok_df.get(t)
                   and t not in brand_words and t not in color_words and t not in GENERIC)[:40]
    prof = {
        'n': n, 'n_neg': len(negatives),
        'brands': _share(brands, n), 'categories': _share(cats, n), 'sizes': _share(sizes, n), 'colors': _share(colors, n), 'conditions': _share(conds, n),
        'min_condition': min((cond_rank(e['condition']) for e in pos if e['condition']), default=0),
        'price': {'min': prices[0] if prices else 0, 'median': prices[len(prices) // 2] if prices else 0,
                  'max': prices[-1] if prices else 0},
        'weights': weights, 'brand_words': sorted(brand_words), 'avoid': avoid,
        'queries': make_queries(title_ngrams, brands, n),
        'broad_queries': make_queries(title_ngrams, brands, n, distinctive=False, limit=2),
        'strictness': strictness,
    }
    # Selbstkalibrierung: Text-Rohwerte der Beispiele (ohne sich selbst wäre genauer, reicht so)
    raws = sorted(_text_raw(prof, f"{e['title']} {e['desc']}") for e in pos)
    prof['text_ref'] = raws[len(raws) // 2] or 1.0
    scores = sorted(score(prof, e, detail=True)['score'] for e in pos)
    p20 = scores[int(len(scores) * 0.25)] if scores else 0.6     # robust gegen einzelne Ausreißer-Beispiele
    thr = 0.8 * p20 * strictness
    for e in negatives:                        # 👎 hebt die Schwelle knapp über abgelehnte Anzeigen,
        s = score(prof, e, detail=True)['score']   # aber nie über die meisten Beispiele
        if s < p20:
            thr = max(thr, s + 0.02)
    prof['threshold'] = round(min(max(thr, 0.5), 0.9), 3)
    return prof

def _ngrams(title):
    words = [w for w in re.findall(r'[a-z0-9]+', fold(title)) if w not in STOP or w in ('knit',)]
    out = set()
    for k in (2, 3, 4):
        for i in range(len(words) - k + 1):
            g = words[i:i + k]
            if not any(w.isdigit() for w in g):
                out.add(' '.join(g))
    return out

def make_queries(title_ngrams, brands, n, limit=4, distinctive=True):
    """Suchbegriffe aus den häufigsten Wortgruppen der Titel, bei Bedarf mit der Hauptmarke davor.
    distinctive=True: nur Wortgruppen mit einem besonderen Wort (z. B. „cable knit“, „torsade“) – nicht nur „ralph lauren pullover“."""
    top_brand = brands.most_common(1)[0][0] if brands else ''
    brand_words = {w for b in brands for w in re.findall(r'[a-z0-9]+', b)}
    min_df = max(2, math.ceil(n * 0.1)) if n >= 4 else 1
    cands = [(g, c) for g, c in title_ngrams.items() if c >= min_df]
    cands.sort(key=lambda gc: (-gc[1], -len(gc[0])))
    out = []
    for g, _ in cands:
        words = set(g.split())
        if words <= brand_words:
            continue                               # nur die Marke allein ist zu breit
        special = words - brand_words - GENERIC - STOP
        if distinctive and not special:
            continue
        if not distinctive and special:
            continue
        q = g if words & brand_words or not top_brand else f'{_short_brand(top_brand)} {g}'
        if any(q in o or o in q for o in out):
            continue
        out.append(q)
        if len(out) >= limit:
            break
    if not out and top_brand and not distinctive:
        out = [_short_brand(top_brand)]
    return out

def _short_brand(b):
    return re.sub(r'^polo ', '', b)                # „polo ralph lauren“ → „ralph lauren“ findet mehr

# ------------------------------------------------------------------ Punktzahl
def _text_raw(prof, text):
    toks = tokens(text)
    return sum(w for t, w in prof['weights'].items() if t in toks)

def score(prof, it, detail=False):
    """0…1. Ohne Detailseite (detail=False) zählen Titel, Marke, Größe, Zustand."""
    parts = {}
    b = it.get('brand', '')
    if prof['brands']:
        parts['marke'] = (1.0 if b in prof['brands'] else 0.5 * max((v for k, v in prof['brands'].items() if k and (k in b or b in k) and b), default=0)) if b else 0.3
    if prof['sizes']:
        ks = size_keys(it.get('size'))
        parts['größe'] = 1.0 if ks & set(prof['sizes']) else (0.4 if not ks else 0.0)
    if prof['conditions']:
        r = cond_rank(it.get('condition'))
        parts['zustand'] = 1.0 if r >= prof['min_condition'] else 0.3
    text = f"{it.get('title', '')} {it.get('desc', '') if detail else ''}"
    ref = prof.get('text_ref') or 1.0
    if not detail:
        ref *= 0.6                                 # nur Titel: weniger Wörter erwartet
    parts['text'] = min(1.0, _text_raw(prof, text) / ref) if prof['weights'] else 0.5
    if detail and prof['colors']:
        cs = it.get('colors') or []
        parts['farbe'] = (1.0 if any(c in prof['colors'] for c in cs) else 0.3) if cs else 0.6
    if detail and prof.get('categories') and it.get('category'):
        parts['kategorie'] = 1.0 if it['category'] in prof['categories'] else 0.0
    W = {'marke': 0.35, 'größe': 0.3, 'zustand': 0.15, 'farbe': 0.2, 'kategorie': 0.35}
    meta_w = sum(W[k] for k in parts if k in W)
    meta = sum(W[k] * v for k, v in parts.items() if k in W) / meta_w if meta_w else 0.5
    s = 0.6 * parts['text'] + 0.4 * meta
    # Wenn fast alle Beispiele dieselbe Marke haben, ist eine fremde Marke ein starkes Gegenargument
    if 'marke' in parts and parts['marke'] < 0.5 and max(prof['brands'].values()) >= 0.8:
        s *= 0.55
    hits = [a for a in prof.get('avoid', []) if a in tokens(text)]
    if hits:
        s *= 0.5
        parts['meidet'] = hits[:3]
    # Kategorie, die bei den Beispielen nie vorkommt (z. B. Damen statt Herren), obwohl die Beispiele einheitlich sind
    if parts.get('kategorie') == 0.0 and (max(prof['categories'].values()) >= 0.6 or prof['n'] >= 5):
        s *= 0.55                                   # Kategorie kommt bei keinem Beispiel vor (z. B. T-Shirt statt Pullover)
    # Größe außerhalb dessen, was die Beispiele haben (nur wenn die Beispiele wenige Größen abdecken)
    if parts.get('größe') == 0.0 and len(prof['sizes']) <= 6:
        s *= 0.6
    return {'score': round(s, 3), 'parts': {k: (round(v, 2) if isinstance(v, float) else v) for k, v in parts.items()}}

def summary(prof, name=''):
    """Kurzer Text für Telegram: was gelernt wurde."""
    if not prof:
        return 'Noch keine Beispiele.'
    pct = lambda d, k=4: ', '.join(f'{k_} {round(v * 100)} %' for k_, v in list(d.items())[:k]) or '–'
    top = [t for t in prof['weights'] if ' ' not in t][:10]
    pr = prof['price']
    return (f"🧠 <b>{name}</b>: {prof['n']} Beispiele" + (f", {prof['n_neg']} abgelehnt" if prof['n_neg'] else '') + '\n'
            f"Marken: {pct(prof['brands'], 3)}\n"
            + (f"Kategorien: {pct(prof.get('categories', {}), 3)}\n" if prof.get('categories') else '') +
            f"Größen: {pct(prof['sizes'], 5)}\nFarben: {pct(prof['colors'], 5)}\n"
            f"Preise der Beispiele: {pr['min']:.0f}–{pr['max']:.0f} € (Mitte {pr['median']:.0f} €)\n"
            f"Typische Wörter: {', '.join(top) or '–'}\n"
            f"Sucht nach: {' · '.join(prof['queries']) or '–'}\n"
            + (f"Meidet: {', '.join(prof.get('avoid', [])[:8])}\n" if prof.get('avoid') else '') +
            f"Meldet ab Ähnlichkeit {round(prof['threshold'] * 100)} %")

# ------------------------------------------------------------------ Eigene Ausschlusskriterien
# Schnellauswahl in der Web-Oberfläche → Wörter (alle Sprachen, die auf Vinted.at vorkommen) und Vinted-Kategorien
EXCLUDE_PRESETS = {
    'V-Ausschnitt': {'words': ['v-ausschnitt', 'v ausschnitt', 'v-neck', 'v neck', 'vneck', 'col v', 'col en v', 'scollo a v', 'cuello en v', 'cuello de pico'],
                     'cats': ['v-ausschnitt']},
    'Damen': {'words': ['femme', 'women', "women's", 'womens', 'donna', 'damen', 'mujer', 'ladies'], 'cats': ['damen']},
    'Herren': {'words': ['homme', "men's", 'mens', 'uomo', 'herren', 'hombre'], 'cats': ['herren']},
    'Kinder': {'words': ['kinder', 'enfant', 'kids', 'bambino', 'bambina', 'junior', 'boys', 'girls', 'jahre', 'ans'], 'cats': ['kinder', 'baby']},
    'Cardigan / Strickjacke': {'words': ['cardigan', 'strickjacke', 'gilet', 'veste en maille'], 'cats': ['strickjacke', 'cardigan']},
    'Reißverschluss / Zip': {'words': ['zip', 'reißverschluss', 'reissverschluss', 'zipper', 'troyer', 'quarter zip', 'half zip', 'demi-zip', 'zippe'], 'cats': []},
    'Flecken / Löcher': {'words': ['fleck', 'flecken', 'loch', 'löcher', 'tache', 'taches', 'trou', 'trous', 'macchia', 'macchie', 'buco', 'stain', 'hole'], 'cats': []},
    'Ohne Ärmel / Weste': {'words': ['ärmellos', 'armellos', 'weste', 'pullunder', 'sans manches', 'smanicato', 'sleeveless', 'vest'], 'cats': ['weste', 'pullunder']},
}

def exclusion_hit(rules, title='', desc='', category=''):
    """Erste passende Ausschlussregel (Text) oder None. Freie Regeln sind einfache Wörter."""
    text = ' ' + re.sub(r'[^a-z0-9äöüß\'\- ]+', ' ', fold(f'{title} {desc}')) + ' '
    cat = fold(category)
    for r in rules or []:
        pre = EXCLUDE_PRESETS.get(r)
        words = pre['words'] if pre else [r]
        for w in words:
            w = fold(w).strip()
            if w and re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z0-9])', text):
                return r
        if pre and cat and any(fold(c) in cat for c in pre['cats']):
            return r
    return None

# ------------------------------------------------------------------ Bildprüfung (Claude)
VISION_MODEL = 'claude-haiku-5-5'
PRICE_IN, PRICE_OUT = 0.10, 0.50       # US-$ pro Mio. Tokens (Haiku 5.5, Stand Okt. 2026)

def small_image(url):
    """Vinted-Bild-URLs sind signiert (?s=…) – die Größe im Pfad darf man nicht ändern. Katalog-Vorschaubilder sind ohnehin klein."""
    return url or ''

def _media_type(b):
    if b[:3] == b'\xff\xd8\xff': return 'image/jpeg'
    if b[:8] == b'\x89PNG\r\n\x1a\n': return 'image/png'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP': return 'image/webp'
    if b[:6] in (b'GIF87a', b'GIF89a'): return 'image/gif'
    return 'image/jpeg'

def _img_block(url, fetch):
    """Bild selbst laden und mitschicken (zuverlässiger, als Claude die Vinted-URL abrufen zu lassen)."""
    if fetch:
        b = fetch(small_image(url))
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': _media_type(b), 'data': base64.b64encode(b).decode()}}
    return {'type': 'image', 'source': {'type': 'url', 'url': small_image(url)}}

def vision_score(api_key, example_urls, cand_url, desc_hint='', fetch=None, timeout=40, avoid=()):
    """Bewertet, wie gut das Kandidatenbild zu den Beispielbildern passt. → (score 0–100, grund, kosten_usd)"""
    content = [{'type': 'text', 'text': 'Beispielbilder von Artikeln, die der Nutzer sucht:'}]
    for u in example_urls[:4]:
        try:
            content.append(_img_block(u, fetch))
        except Exception:
            pass
    content.append({'type': 'text', 'text': 'Neue Anzeige:'})
    content.append(_img_block(cand_url, fetch))
    content.append({'type': 'text', 'text': (
        'Wie gut passt die neue Anzeige zu den Beispielen? Achte auf Art des Artikels, Muster/Strick, Schnitt, Logo, '
        'Material und Stil – nicht auf Farbe, Hintergrund oder Fotoqualität. ' + (f'Gesucht wird: {desc_hint}. ' if desc_hint else '') +
        (f'Der Nutzer will ausdrücklich NICHT: {", ".join(avoid)} – zeigt das Bild so etwas, gib höchstens 20 Punkte. ' if avoid else '') +
        'Antworte nur mit JSON: {"score": 0-100, "grund": "max. 8 Wörter"}')})
    body = {'model': VISION_MODEL, 'max_tokens': 400, 'messages': [{'role': 'user', 'content': content}]}
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps(body).encode(), headers={
        'x-api-key': api_key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Claude-API {e.code}: {e.read().decode("utf-8", "replace")[:200]}')
    u = res.get('usage', {})
    cost = (u.get('input_tokens', 0) * PRICE_IN + u.get('output_tokens', 0) * PRICE_OUT) / 1e6
    text = ''.join(c.get('text', '') for c in res.get('content', []) if c.get('type') == 'text')
    # Antwort lesen – robust: JSON, sonst erste Zahl. Nicht lesbar → None (dann entscheidet das Bild nicht).
    score, why = None, ''
    m = re.search(r'\{.*?\}', text, re.S)
    if m:
        try:
            data = {k.lower(): v for k, v in json.loads(m.group(0)).items()}
            score = int(float(data.get('score', data.get('punkte', data.get('wert')))))
            why = str(data.get('grund', data.get('reason', '')))
        except (ValueError, TypeError, json.JSONDecodeError):
            score = None
    if score is None:
        n = re.search(r'\b(\d{1,3})\b', text)
        if n:
            score = int(n.group(1))
    if score is None:
        why = 'unlesbare Antwort: ' + re.sub(r'\s+', ' ', text)[:150] + f" (stop={res.get('stop_reason')})"
        return None, why, cost
    return max(0, min(100, score)), why[:80], cost
