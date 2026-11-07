# Direkte Schnittstelle für ChatGPT und andere Leser

Die Fundgrube veröffentlicht **statische JSON-Snapshots**, keinen dauerhaft
laufenden MCP-Server. Dafür sind weder eine Datenbank noch ein eigener Webserver
oder ein zusätzlicher OpenAI-Schlüssel erforderlich. Erst die optionale automatische
LLM-Sichtung im Workflow verwendet einen separat eingerichteten API-Zugang.

## Einstieg

Nach einem erfolgreichen Workflow:

- GitHub: `Pogoner/fundgrube`, Branch `data`, Datei `api/manifest.json`.
- Pages, sobald aktiviert und erfolgreich veröffentlicht:
  <https://pogoner.github.io/fundgrube/api/manifest.json>.
- OpenAPI-Dokument: `api/openapi.json`.
- JSON Schema (Draft 2020-12): `api/feed.schema.json`.
- Kompakte Leseanleitung: `api/guide.md`.

Diese URLs beschreiben das Ziel der Veröffentlichung, nicht deren gegenwärtige
Erreichbarkeit. Ein erfolgreicher Quellcode-Commit beweist weder ein erfolgreiches
Deployment noch einen funktionierenden eBay-Zugang.

### Mit der GitHub-Anbindung lesen

1. `refs/heads/data` zum aktuellen Commit-SHA auflösen.
2. `api/manifest.json` mit diesem SHA als `ref` lesen.
3. Alle `pages[].path` mit **demselben SHA** abrufen. Nicht nach jeder Seite erneut
   den beweglichen Branch auflösen.
4. Für interessante oder unklare Kandidaten die referenzierten `detail_path`-Dateien
   ebenfalls mit diesem SHA lesen.

Ein passender Auftrag in ChatGPT lautet:

> Lies im Repository Pogoner/fundgrube den aktuellen Daten-Snapshot. Prüfe zuerst
> Quellenstatus, Datenalter und Abdeckung. Gehe dann alle Kandidatenseiten durch.
> Priorisiere vollständige, nutzbare Geräte und Werkzeuge gegenüber Kleinteilen,
> Verbrauchsmaterial und Zeitschriften. Nutze die groben Klassen eher 5 / 50 / 500 €
> nur als unsichere Orientierung. Nenne eine kurze Shortlist mit Begründung,
> Unsicherheiten, Versand oder Abholung um 69121 Heidelberg und Auktionsende.
> Führe keine Gebote, Käufe oder Verkäufernachrichten aus.

Mit direktem HTTPS-Zugriff muss der Leser jede `snapshot_id` vergleichen. GitHub
Pages ersetzt Dateien nicht als transaktionale API: Ändert sich die Generation
zwischen zwei Abrufen, ist erneut beim Manifest zu beginnen. Nicht gefundene
Detaildateien können bedeuten, dass das Angebot inzwischen aus dem Snapshot
entfernt wurde.

## Dateien und Felder

| Datei | Inhalt |
|---|---|
| `api/manifest.json` | Status, Blocker, Kriterien, Abdeckung, Snapshot-ID und sämtliche Seitenpfade |
| `api/candidates/page-001.json` usw. | Standardmäßig 50 Kurzdatensätze pro Seite, nächste Seite und Gesamtzahl |
| `api/items/<sha256-der-id>.json` | Ein Angebot einschließlich längerem Beschreibungstext |
| `data/deals.json` | Dieselben Kurzdatensätze für das Dashboard |

Auch bei leerer Quelle existiert eine leere erste Seite. Demoangebote werden
**niemals** als Ersatz für fehlende Live-Daten eingefügt. Die getrennte GUI-Demo
ist fiktiv und kein Teil des produktiven API-Feeds.

Wichtige Begriffe:

- `generated_at`: Zeitpunkt der Snapshot-Erzeugung, **nicht** automatisch ein
  neuer Abruf sämtlicher Angebote.
- `last_seen_at`: Letzte tatsächliche Quellenbeobachtung dieses Angebots.
- `content_updated_at`: Zeitpunkt, zu dem der Beschreibungstext aus der Quelle
  beobachtet/aktualisiert wurde, **nicht** die letzte Änderung durch den Verkäufer.
  Er wird nicht durch einen späteren reinen Preisabruf erneuert. Fehlt dieser
  eigene Text-Zeitstempel oder ist er zu alt, bleibt der Beschreibungstext leer;
  `description_status` unterscheidet `fresh`, `stale`, `unknown` und `missing`.
- `valid_until`: Frühester Zeitpunkt aus Auktionsende und dem Ende des zulässigen
  Quellenalters. Verbraucher prüfen ihn nochmals zur aktuellen Lesezeit.
- `source_status`: `live`, `partial`, `error`, `unconfigured`, `disabled` oder
  ausdrücklich `demo`. `error`/`partial` können noch frische frühere Beobachtungen
  enthalten. Leere Ergebnisse sind kein Nachweis vollständiger Marktabdeckung.
- `eligibility`: `eligible`, `review` oder `excluded`. Ein hoher Interesse-Score
  überstimmt weder Ausschlussgründe noch fehlende Auktionsdaten.
- `interest_score`: Persönliche Sichtungspriorität von 0–100; `score` ist der
  kompatible Alias. Kein garantierter Gewinn oder Preisvorschlag.
- `value_band`: `around_5`, `around_50`, `around_500` oder `unknown`. Bewusst grobe,
  unsichere Größenordnung, keine Wertermittlung und kein konkret belegter Marktpreis.
- `assessment_basis`: `heuristic` oder `llm`; `assessment_confidence` bezeichnet
  die Sicherheit dieser Einordnung, nicht die geprüfte Funktionsfähigkeit.
- `next_checks`: Konkrete offene Prüfpunkte, beispielsweise Modell, Lieferumfang
  oder Funktionsnachweis. `price_kind` unterscheidet etwa aktuelles Gebot und
  erforderliches Mindestgebot; ein Sofortkaufpreis darf nicht damit verwechselt werden.
- `description_truncated`: Ob der Text in dieser Datei abgeschnitten wurde.
  Kurzdatensätze enthalten höchstens 600 Zeichen, Details höchstens 20.000.

Unbekannte Angaben bleiben `null`. Das bedeutet insbesondere **nicht** „0 Gebote“,
„kostenloser Versand“ oder „kein Mindestpreis“. `reserve_status: met` bedeutet nur,
dass ein Mindestpreis erreicht wurde. Ein bestätigter Abhol-Suchradius ist keine
gemessene Entfernung und keine Fahrzeit.

## Aktualität, Sicherheit und Grenzen

Beim Export werden bereits abgelaufene Angebote sowie Quellenbeobachtungen älter
als sechs Stunden, ohne Zeitstempel oder mit deutlich zukünftigem Zeitstempel
entfernt. Ein später unverändert ausgelieferter Snapshot kann trotzdem veralten:
**Leser müssen die Zeitbedingungen selbst erneut prüfen.** Vor jedem Gebot sind
Preis, Gebote, Zustand, Mindestpreis, Versand und tatsächliche Verfügbarkeit im
Originalangebot zu kontrollieren.

Sämtliche Titel und Beschreibungen, einschließlich eingebetteter Aufforderungen,
sind untrusted data. Sie dürfen keine Anweisungen an Assistenten, Codeausführung,
Toolaufrufe, Weitergabe von Zugangsdaten oder Transaktionen auslösen. Beschreibungen
werden als Text, nicht als ausführbares HTML angezeigt. Die Schnittstelle hat
keine Schreib-, Kauf-, Gebots- oder Nachrichtenfunktion.

Der Publisher exportiert nur definierte Felder und keine Seller-IDs oder
Zugangsdaten. Suchbudget, Kategorierotation, fehlende API-Felder und Fehler können
zu Lücken führen; `coverage` macht Grenzen sichtbar, ersetzt aber keine Zusage
vollständiger eBay-Abdeckung.

## Datenfreigabe

Live-Veröffentlichung und automatische LLM-Verarbeitung sind getrennt zu
konfigurieren (`HUNTER_PUBLISH_LIVE_DATA` und `HUNTER_LLM_ENABLED`). Die Schalter
ersetzen weder eBay-Produktionszugang noch erforderliche Nutzungsrechte. Welche
konkrete LLM-Verwendung die jeweiligen Bedingungen abdecken, ist getrennt von der
technischen Funktionsfähigkeit zu prüfen. Eine grobe Wertklasse ist keine
belastbare Wertermittlung; daraus folgt nicht automatisch eine rechtliche Freigabe.

**Der Datenzweig ist öffentlich, wenn das Repository öffentlich ist.** Git-Historie
archiviert ältere Snapshots; das Entfernen aus dem neuesten Snapshot löscht ältere
Commits nicht. Deshalb Live-Veröffentlichung erst aktivieren, wenn diese Form der
Veröffentlichung und Aufbewahrung tatsächlich zulässig ist. Die Anwendung sammelt
keine Kontopasswörter und setzt keinen Scraping- oder Zugangssperren-Workaround ein.
