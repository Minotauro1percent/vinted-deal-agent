"""Einmaliger Test: lädt eine Katalogseite und einige Anzeigen und speichert sie unter debug/."""
import os, re, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent
os.makedirs('debug', exist_ok=True)
page = agent.vinted_get('/catalog?search_text=ralph+lauren+cable+knit&order=newest_first&currency=EUR')
open('debug/catalog.html', 'w').write(page)
items = agent.parse_catalog(page)[:4]
for it in items:
    time.sleep(4)
    path = urllib.parse.urlparse(it['url']).path
    try:
        html = agent.vinted_get(path)
        open(f"debug/item_{it['id']}.html", 'w').write(html)
        print('ok', it['id'], len(html))
    except Exception as e:
        print('fail', it['id'], e)
# Kurz-URL / Teilen-Link testen
for u in ['https://www.vinted.at/items/' + items[0]['id'], 'https://www.vinted.de/items/' + items[0]['id'] + '?utm_source=share']:
    try:
        r = agent.urllib.request.urlopen(agent.urllib.request.Request(u, headers={'User-Agent': agent.UA}), timeout=20)
        print('redirect', u, '->', r.geturl(), r.status)
    except Exception as e:
        print('redirect-fail', u, e)
