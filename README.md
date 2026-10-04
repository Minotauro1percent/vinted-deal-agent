# Vinted Deal-Agent (Cloud)

Sucht auf Vinted nach deiner Watchlist und schickt neue Deals mit Titelbild an Telegram.
Läuft auf den Servern von GitHub, also auch wenn dein PC aus ist.

- **Suche:** stündlich (`SEARCH_EVERY_MIN` in `agent.py`, Standard 60)
- **Telegram-Befehle:** werden alle 15 Minuten gelesen
- **Kosten:** keine (öffentliches Repo = unbegrenzte GitHub-Actions-Minuten)

## Telegram-Befehle

| Befehl | Wirkung |
|---|---|
| `/start` | Suche einschalten, sucht sofort |
| `/stop` | Suche pausieren |
| `/status` | läuft er gerade, letzte Suche, Treffer |
| `/suche` | einmalig sofort suchen |
| `/liste` | alle Suchen mit Nummer und Max-Preis |
| `/limit 2 90` | Max-Preis von Suche 2 auf 90 € |
| `/aus 3` · `/an 3` | Suche 3 aus- oder einschalten |
| `/hilfe` | Übersicht |

Befehle werden beim nächsten Lauf verarbeitet, also nach höchstens etwa 15 Minuten.
GitHub startet geplante Läufe manchmal ein paar Minuten später.

## Einrichtung von Hand (5 Minuten)

1. Auf github.com anmelden → **New repository** → Name `vinted-deal-agent`, **Public** → Create.
2. **Add file → Upload files**: `agent.py`, `watchlist.json`, `README.md` hochladen.
   Die Datei `agent.yml` muss in den Ordner `.github/workflows/`. Dafür **Add file → Create new file**,
   als Namen `.github/workflows/agent.yml` eintippen und den Inhalt einfügen.
3. **Settings → Secrets and variables → Actions → New repository secret**, zweimal:
   - `TELEGRAM_BOT_TOKEN` = dein Bot-Token
   - `TELEGRAM_CHAT_ID` = deine Chat-ID
4. **Settings → Actions → General → Workflow permissions** → **Read and write permissions** → Save.
5. Tab **Actions** → „Vinted Deal-Agent“ → **Run workflow**. Nach ca. 1–3 Minuten kommt auf Telegram
   die Begrüßung, danach die ersten Deals.

## Watchlist ändern

- schnell per Telegram: `/limit`, `/an`, `/aus`
- komplett: `watchlist.json` im Repo bearbeiten (Stift-Symbol). Felder wie im Browser-Skript:
  `queries`, `brands`, `require`, `exclude` (jeweils mit Komma), `maxPrice`, `resaleMin`, `resaleMax`,
  `sizes`, `minCondition`, `active`.

## Gut zu wissen

- Läuft das Tampermonkey-Skript im Browser gleichzeitig, bekommst du manche Deals doppelt.
  Dann das Skript in Tampermonkey ausschalten.
- Blockiert Vinted die Cloud-Abfrage, kommt eine Telegram-Warnung. Der nächste Lauf versucht es erneut.
- GitHub pausiert geplante Abläufe in Repos, an denen 60 Tage lang nichts passiert. Der Agent speichert
  bei jeder Suche seinen Zustand im Repo, das zählt als Aktivität.
