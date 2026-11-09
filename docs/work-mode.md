# Fundgrube im Work-Modus aktualisieren

Die reguläre GitHub-Anbindung kann autorisierte Aktualisierungen und echte
interaktive Bewertungen über Repository-Dateien anstoßen. Dafür braucht der
Assistent weder `gh` noch eine angemeldete Browseroberfläche oder einen eigenen
`workflow_dispatch`-Aufruf. Der Produktionsworkflow verarbeitet Pushes auf
`main`; seine bestehenden Berechtigungen und Schutzgrenzen bleiben maßgeblich.
Ein reiner Lese- oder Shortlist-Auftrag erlaubt weiterhin keinen Commit.

## Einen ausdrücklich beauftragten Quellenlauf starten

1. Prüfe die jüngsten Actions-Läufe und den letzten Quellenlauf im Manifest.
   Ein bereits laufender passender Lauf braucht keinen weiteren Start.
2. Lies `.github/fundgrube-refresh.json` auf `main` samt aktuellem Datei-SHA.
   Erhalte `schema_version: 1`, setze eine neue eindeutige `request_id`
   (beispielsweise UTC-Zeit plus eindeutige Kennung) und `requested_at` auf die
   tatsächliche aktuelle UTC-Zeit im ISO-Format mit `Z`.
3. Speichere genau diese Datei über die normale GitHub-Anbindung auf `main`,
   unter Verwendung des gerade gelesenen Datei-SHAs. Bei einem Konflikt neu
   lesen, keine fremde Änderung überschreiben und keinen Force-Push verwenden.
4. Ordne den Actions-Lauf dem resultierenden Commit zu. Prüfe anschließend
   dessen Jobs, die Quellenstatus-Zusammenfassung und den veröffentlichten
   Datenstand: `data` erneut in einen SHA auflösen und `api/manifest.json`
   unter diesem SHA lesen. Ein Commit allein belegt weder einen gestarteten
   noch einen abgeschlossenen Quellenabruf.

Die Datei ist ein nachvollziehbarer Startvermerk. Ihre Inhalte werden nicht als
Befehl ausgeführt und besitzen keinen eigenen Dispatcher oder Validator. Der
normale Push auf `main` startet den Workflow. Deshalb keine leeren Reviews,
erfundenen Bewertungen oder sachfremden Konfigurationsänderungen als Startsignal
verwenden. Auch ein Startvermerk darf nur bei einem autorisierten Auftrag
geschrieben werden; fehlende Rechte oder Branch-Freigaben bleiben Blocker.

Der 29-Minuten-Doppelstartschutz kann einen neuen Abruf aufschieben. Mindestens
zehn Sekunden Anfrageabstand, höchstens 160 Datenanfragen pro Lauf, 25 Minuten
Scanzeit und 8.000 HTTP-Anfragen pro UTC-Tag gelten auch für manuelle Starts.
`Retry-After`, Sperren, Abschaltschalter und der Zeitplan zu Minute 07/37 bleiben
erhalten. Wiederholte Startvermerke dürfen diese Grenzen nicht umgehen.

## Echte Bewertungen ohne lokalen Pack-Lauf speichern

Dieser Weg benötigt einen ausdrücklichen Auftrag zum Speichern der tatsächlich
erarbeiteten semantischen Bewertungen. Er erzeugt selbst keine Bewertungen und
aktiviert keine automatische LLM-API. Zuerst alle Seiten eines konsistenten
Daten-Snapshots lesen; benötigte Angebotsdetails gehören zum selben Daten-SHA.
Nur tatsächlich gesichtete Angebote in die Bewertung aufnehmen.

Lies `reviews/pending.json` samt aktuellem Datei-SHA. Der freie Eingang lautet:

```json
{
  "schema_version": 1,
  "request": null
}
```

Ein belegter Eingang darf nicht überschrieben werden. Prüfe zuerst den
zugehörigen Lauf bzw. melde den offenen oder fehlgeschlagenen Auftrag. Wenn der
Eingang frei ist, ersetzt ein Commit auf `main` nur `request` durch ein Objekt
mit genau diesen Feldern; zusätzliche Kopf- oder Auftragsfelder sind ungültig:

| Feld | Inhalt |
| --- | --- |
| `id` | Neue eindeutige Kennung, 1–80 Zeichen; ASCII-Buchstaben und Ziffern, nach dem ersten Zeichen zusätzlich `_`, `.` und `-` erlaubt. |
| `source_commit` | Der festgehaltene Daten-Commit: genau 40 kleine hexadezimale Zeichen, kein Branchname oder URL. |
| `ratings` | Das vollständige echte V2-Batch-Objekt aus der Sichtung; kein Dateipfad und kein bereits gepackter V3-Store. |

`ratings` besitzt genau `schema_version: 2`, `source_snapshot_id`, `reviewed_at`,
`reviewer: "ChatGPT"` und `groups`. Jede Gruppe enthält `ids` und `assessment`;
der vollständige [Review-Vertrag](api.md#interaktive-chatgpt-bewertungen-zurück-in-die-gui)
und [AGENTS.md](../AGENTS.md) gelten unverändert. `groups` darf nicht leer sein.
Bewertungszeitpunkte sind die tatsächlichen Sichtungszeiten; ein erneuter
Transport rechtfertigt kein neues Bewertungsdatum. Keine abgeleiteten Scores,
exakten Preisfelder oder als LLM-Ergebnis umbenannten Heuristiken einreichen.

Der Workflow führt vor dem Sammeln `scripts/process_review_request.py` aus:

1. Er liest nur `api/manifest.json` und `data/deals.json` aus diesem Repository
   am angegebenen unveränderlichen `source_commit`. Andere URLs, Pfade oder
   Befehle können nicht durch den Auftrag vorgegeben werden.
2. Er prüft übereinstimmende Snapshot-IDs und Bestandszahlen und führt die
   unveränderte Validierung von `scripts/pack_reviews.py` aus. Der bestehende
   `reviews/current.json`-Store wird inkrementell einbezogen; fehlt er, stoppt
   die Verarbeitung. Doppelte/unbekannte IDs, unvollständige Beschreibungen
   oder widersprüchliche Bewertungen sind Fehler, keine Einladung zum Umgehen
   der Prüfung. Bestehende Werte, Herkunft und Sichtungszeiten bleiben erhalten;
   die bestehenden Regeln für neuere Bewertungen und bekannte Endzeiten gelten.
3. Nur nach erfolgreicher Prüfung entsteht ein gemeinsamer Commit für
   `reviews/current.json`, den wieder freien `reviews/pending.json`-Eingang und
   `reviews/last-request-result.json`. Ändert sich `main` währenddessen, wird
   der konkurrierende Stand nicht überschrieben. Ein neuer Lauf muss vom neuen
   Stand ausgehen; es gibt keinen Force-Push.
4. Erst nach Bestätigung dieses Commits wird der lokale Store übernommen.
   Derselbe Workflow sammelt anschließend und veröffentlicht passende Reviews.
   Der interne Commit mit `GITHUB_TOKEN` löst keinen weiteren Push-Workflow aus.

Vor jedem Quellenlauf liest ein unabhängiger Schritt den aktuellen
`reviews/current.json`-Store von `main` neu: `--sync-current-store` löst `main`
einmalig in einen SHA auf, lädt nur diesen festen Pfad und prüft das vollständige
Review-Schema einschließlich Größenlimit und doppelter JSON-Felder. Danach
ersetzt er die lokale Datei atomar mit dem unveränderten validierten Text;
er schreibt nichts nach GitHub und erneuert keine Scores oder Zeitstempel.
So verwendet auch ein bereits aufgereihter Lauf mit altem Checkout die inzwischen
gespeicherten Bewertungen. Abruf- oder Validierungsfehler dieses Abgleichs stoppen
den Lauf vor dem Crawl; ein veralteter lokaler Store wird dann nicht exportiert.

Die Bestätigung in `reviews/last-request-result.json` enthält `status: packed`,
`request_id`, den Digest des Auftrags, `source_commit`, `source_snapshot_id`,
`request_commit`, `processed_at` sowie `submitted_reviews` und `stored_reviews`.
`packed` bestätigt die geprüfte Speicherung. Es beweist weder einen erfolgreichen
Quellenlauf noch die Anwendung aller Reviews oder ein erfolgreiches Pages-Deployment.
Prüfe diese Ergebnisse separat im Workflow und im neuen Datenmanifest, einschließlich
`interactive_review`. Ein übernommenes Review erneuert keine Quellenfakten.

Ungültige Eingaben oder Validierungsfehler lassen Eingang und bisherigen Store
unverändert. Der Workflow zeigt einen getrennten Review-Fehler und setzt Crawl
und Veröffentlichung nach dem erfolgreichen Store-Abgleich fort; ein fehlerhafter
Auftrag allein blockiert damit keine späteren Quellenläufe. Bei unklarem Ergebnis eines
Schreibaufrufs zuerst `main`, Eingang und Bestätigung prüfen: Ein Fehler nach dem
Remote-Commit bedeutet nicht, dass auf GitHub nichts gespeichert wurde.
Den eigenen autorisierten Auftrag mit aktuellem Datei-SHA korrigieren oder einen
ausdrücklich aufgegebenen eigenen Auftrag auf `request: null` zurücksetzen.
Fremde Aufträge nie überschreiben und keine Prüfungen abschalten. Der lokale
Weg über `pack_reviews.py` bleibt verfügbar.

## Teilweise erfolgreiche Quellenläufe erkennen

Ein grüner Workflow ist kein Vollscan-Nachweis. Die Laufzusammenfassung nennt
Quellenstatus, Erfolgszeitpunkte, Anfragezahlen und Abschlussgründe beider Suchen;
bei `partial`/`error` oder unvollständigem aktivem AOG-Durchlauf erscheint eine Warnung.

`repeated_page` bedeutet, dass die Quelle einen bereits gelesenen Satz von
Angebots-IDs für einen anderen Offset geliefert hat. Die Diagnose nennt nun
die betroffene Suche und beide Offsets. Der sichere Abbruch bleibt bestehen;
die bessere Diagnose ist keine Reparatur der externen Quelle. Erst ein neuer
Lauf zeigt, ob sie wieder korrekt weiterblättert. Auch vollständig durchlaufene
Offsets bleiben eine veränderliche Quellenliste: `complete=false`, keine
garantierte eBay-Vollabdeckung und kein Löschbeweis durch fehlende IDs.

Referenz: [GitHub zu Push-Ereignissen und `GITHUB_TOKEN`](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
