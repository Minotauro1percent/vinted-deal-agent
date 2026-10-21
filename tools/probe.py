"""Einmaliger Test: welche Felder hat eine Vinted-Anzeige? Speichert nur eine kurze Zusammenfassung (keine ganzen Seiten)."""
import os, re, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent
os.makedirs('debug', exist_ok=True)
out = []
def w(*a):
    out.append(' '.join(str(x) for x in a))
def clean(s):
    return re.sub(r'[A-Za-z0-9_\-]{28,}', '<lang>', s)    # lange Zeichenketten (mögliche Schlüssel) entfernen
page = agent.vinted_get('/catalog?search_text=ralph+lauren+cable+knit&order=newest_first&currency=EUR')
items = agent.parse_catalog(page)[:3]
w('katalog', len(items), [(i['id'], i['title'], i['brand'], i['size'], i['condition'], i['price'], i['thumb'][:120]) for i in items])
for it in items:
    time.sleep(4)
    path = urllib.parse.urlparse(it['url']).path
    try:
        h = agent.vinted_get(path)
    except Exception as e:
        w('fail', it['id'], e); continue
    w('\n=== item', it['id'], len(h))
    w('attrs', agent.ATTR_RE.findall(h)[:30])
    for m in re.finditer(r'<meta[^>]+(?:property|name)="(og:[a-z:]+|product:[a-z:]+|description|twitter:[a-z]+)"[^>]*>', h):
        w('meta', clean(m.group(0))[:250])
    w('title-tag', re.findall(r'<title>([^<]{0,200})</title>', h)[:1])
    for pat in [r'\\?"price\\?"', r'"priceCurrency"', r'\\?"brand_dto\\?"', r'\\?"size_title\\?"', r'\\?"status\\?"', r'\\?"photos\\?"', r'itemprop="(\w+)"', r'\\?"catalog_id\\?"', r'\\?"title\\?":\\?"']:
        ms = [clean(h[max(0, m.start() - 60):m.end() + 160]).replace('\n', ' ') for m in list(re.finditer(pat, h))[:3]]
        w('ctx', pat, len(re.findall(pat, h)), ms)
for u in ['https://www.vinted.at/items/' + items[0]['id'], 'https://www.vinted.de/items/' + items[0]['id'] + '?utm_source=share']:
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': agent.UA}), timeout=20)
        w('redirect', u, '->', r.geturl(), r.status)
    except Exception as e:
        w('redirect-fail', u, e)
open('debug/summary.txt', 'w').write('\n'.join(out) + '\n')
print('\n'.join(out)[:3000])
