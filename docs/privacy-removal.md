# Private Suchdaten aus Git entfernen

Das bestehende Repository und die Pages-Adresse können erhalten bleiben. Ein
normaler Lösch-Commit entfernt jedoch keine älteren Git-Objekte, PR-Referenzen,
Actions-Ausgaben oder bereits heruntergeladenen Kopien. Die folgende Wartung
ist deshalb von der normalen Veröffentlichung getrennt.

## Zuerst weitere Veröffentlichungen verhindern

1. **Vor der ersten neuen produktiven Veröffentlichung** reguläre
   GitHub-Actions-Starts und die externe Review-Aufgabe pausieren. Alle
   bereits aktiven und wartenden schreibenden Läufe regulär abbrechen und
   deren Endstatus prüfen. Ein alter Checkout darf den bereinigten Stand
   nicht nachträglich überschreiben. Ohne bestätigten Stopp hier nicht fortfahren.
2. Das Repository-Secret `HUNTER_PRIVATE_SEARCH` mit
   `python scripts/configure_private_search.py` einrichten. Der Helfer setzt
   eine angemeldete GitHub CLI mit Berechtigung zum Verwalten von Secrets
   voraus. Er fragt die PLZ verdeckt ab, erzeugt einen zufälligen Schlüssel
   und übergibt das JSON über Standardeingabe; es entsteht keine Profildatei.
   Einen vorhandenen Schlüssel nicht beiläufig rotieren. Das Einrichten des
   Secrets allein startet keinen Quellenlauf und hält bestehende Läufe nicht an.
3. Die Codeänderung für den privaten Suchkontext erst anschließend auf `main`
   übernehmen. Ohne gültiges Secret blockiert der neue Code Live-Abrufe.
   PR-Tests prüfen den Offlinebetrieb ohne Secret. Die automatische LLM-API
   und deren Freigabe bleiben davon unberührt.
4. Genau einen kontrollierten Lauf des geprüften neuen Quellstands zulassen;
   die regulären und externen Starts bleiben für die Wartung pausiert.
   Den ersten neuen Datenstand prüfen: öffentliche Konfiguration ohne
   Such-PLZ/Suchstadt, `state.json` ohne Klartext-Suchbelege und Website/API
   ohne Klartext oder internen HMAC. Der Quellenlauf muss mit unveränderten
   Budgets und beiden freigegebenen Suchen funktionieren. Bewertungen und
   ihre ursprünglichen Sichtungszeitpunkte müssen erhalten bleiben.

## Wartungsfenster und Inventar

Vor der Historienänderung den bereits eingerichteten Schreibstopp erneut
bestätigen. Auch der kontrollierte Prüf-Lauf muss beendet sein. Keine
schreibenden Läufe dürfen mehr aktiv oder wartend sein; die regulären und
externen Starts bleiben bis zur abschließenden Nachkontrolle pausiert.

Beim Audit am 13. November 2026 war insbesondere der alte Lauf
`43095851011` noch ohne gestartete Jobs in der Warteschlange. Diesen Zustand
unmittelbar vor dem Eingriff erneut prüfen. Als betroffenes älteres
Pages-Artefakt wurde `14052993113` aus Lauf `43452753853` gefunden; eine kurze
Artefakt-Aufbewahrung ersetzt keine Prüfung von Logs und Caches.

Alle Remote-Refs und ihre aktuellen SHAs festhalten. Das vollständige Ref-/Commit-Inventar
vom 13. November 2026, 22:39 UTC, umfasst 219 erreichbare Commits, sieben
Zweige (`main`, `data`, vier bereits integrierte Arbeitszweige und den Zweig
dieser Codekorrektur), keine Tags sowie PR-Referenzen der PRs 1 bis 5.
Commit-Nachrichten enthalten keine bekannten privaten Suchwerte; sämtliche
Autor-/Committer-Adressen sind noreply-Adressen. Neue Arbeit kann diese
Bestände verändern. Vor dem eigentlichen Rewrite muss auch dieser PR
integriert sein. Alte PR-Head-Refs werden von GitHub separat verwaltet und
verschwinden nicht durch normales Löschen eines Zweigs.

Die Suche nach den bekannten privaten Werten deckt die Quellhistorie und alle
historischen Daten-Manifeste ab. Der weitergehende State-/Dashboard-Lauf wurde
durch eine Host-Unterbrechung beendet; eine vollständige Prüfung der zu
erhaltenden Inhalte bleibt vor dem Rewrite erforderlich.

Den letzten konsistenten Datenstand und den vollständigen Review-Store
verschlüsselt bzw. in einem privaten Arbeitsverzeichnis sichern. Die Sicherung
ist selbst vertraulich. Besonders Tageszähler, gestartete Scanzeitpunkte,
Cooldowns und längere Sicherheitspausen müssen bei der Migration erhalten
bleiben. Eine leere `state.json` wäre kein zulässiger Ersatz dafür.

## Lokale Bereinigung vorbereiten und prüfen

Die offizielle GitHub-Anleitung verwendet `git-filter-repo` mit
`--sensitive-data-removal` (Version mindestens 2.47). In einem frischen Klon
mit allen relevanten Zweigen arbeiten und einen Wiederherstellungspunkt
außerhalb öffentlicher Systeme behalten. Die Filterregeln müssen die
festgestellten privaten Suchfelder, Konfigurationswerte, Beispiele und
gegebenenfalls Commit-Nachrichten abdecken.

Keine globale Ersetzung einer Stadt oder Ziffernfolge über alle Dateien:
Hersteller-/Verkäuferangaben, Artikel-IDs und andere legitime Quellenfakten
können dieselben Zeichen enthalten. Auch generierte Snapshots sind keine
beliebig austauschbaren Textdateien; sie enthalten prüfbare Inhalts- und
Datei-Hashes. Historische generierte Snapshots und Zustände bei Bedarf
vollständig aus der erhaltenen Historie entfernen und den aktuellen
Datenzweig aus einem konsistent neu erzeugten, bereinigten Snapshot aufbauen.
Dabei den gesicherten Zustand einschließlich Anfragegrenzen fortführen.

Der bestehende Datenzweig ist nicht unabhängig von der Quellhistorie: Seine
103 eigenen Snapshot-Commits hängen an zwei frühen Quellvorfahren, von denen
der spätere bereits private Konfiguration enthält. Nur generierte Dateien aus
`data` zu entfernen würde diesen Vorfahren weiter erreichbar lassen. Auch
diese Elternkette muss bereinigt werden. Ein verifiziert sauberer, parentloser
Ursprungscommit mit ausschließlich `.gitignore` ist vorhanden; ein neuer
bereinigter Verlauf kann technisch auf ihm aufbauen, ohne ein neues
Repository anzulegen. Ein solcher Neuaufbau würde zusätzliche Historie
verwerfen und ist vor der Veröffentlichung gesondert im Wartungsplan zu
entscheiden. Ein gezielter Filter muss entsprechend alle betroffenen
Quellvorfahren erfassen.

Den Review-Store inhaltlich erhalten. Historische `source_snapshot_id`-Werte
und ursprüngliche `reviewed_at`-Zeitpunkte sind Provenienz, keine Angaben,
die durch erfundene neue Zeitpunkte oder neue Snapshot-IDs ersetzt werden
dürfen. Bestehende Bewertungen bleiben an Titel/Beschreibung/Zustand gebunden.

Vor jeder Remote-Änderung lokal prüfen:

- Alle zu erhaltenden Branches/Tags sind bereinigt; veraltete Arbeitszweige
  sind ausdrücklich zum Löschen vorgesehen oder ebenfalls bereinigt.
- Keine privaten Suchwerte verbleiben in erreichbaren Dateien oder
  Commit-Nachrichten. Quellstand, Offline-Tests, Exportvalidierung und
  Review-Store-Prüfung sind erfolgreich.
- Die aktualisierten Exportdateien passen untereinander: Manifest,
  Seiten, Details, Dashboard, Prüfsummen und Snapshot-ID.
- Eine Liste alter/neuer Heads und der tatsächlich betroffenen historischen
  Commits liegt für die kontrollierte Veröffentlichung und GitHub Support vor.

## Remote-Bereinigung und Nachkontrolle

Die geprüften neuen Branch-Heads in einem abgestimmten Wartungsfenster
veröffentlichen. Bei erzwungenen Aktualisierungen genaue alte SHAs als
`--force-with-lease`-Bedingung verwenden. Ein unerwartet veränderter Remote-Head
ist ein Abbruchgrund; keine fremden Commits überschreiben und keinen
ungeprüften `--mirror`-Push verwenden. Historienänderungen verändern
Commit-IDs und können Signaturen sowie alte Links ungültig machen.

Betroffene Actions-Artefakte, Logs und Caches über die normalen
Administrationsfunktionen entfernen. Danach GitHub Support um Behandlung
der betroffenen internen PR-Referenzen, zwischengespeicherten Ansichten und
verwaisten Objekte bitten. Die tatsächliche Bereinigung und Unterstützung
richtet sich nach GitHubs Verfahren; ein erfolgreiches Force-Push allein
bestätigt diese Schritte nicht.

Für die Support-Anfrage sind Repository, betroffene PRs, relevante alte
Commit-IDs, Ergebnisse des Filter-Reports und die erledigten Schritte
nützlich. Private Werte selbst nur über einen dafür vorgesehenen privaten
Support-Kanal mitteilen, nicht in einem öffentlichen Issue.

Abschließend frisch klonen, alle sichtbaren Refs und die aktuelle Pages-API
prüfen. Andere eigene Klone neu erstellen oder sicher bereinigen; einen alten
Zweig nicht zurückmischen. Erst dann Zeitpläne wieder aktivieren und einen
regulären vollständigen Quellenlauf samt Review-Anwendung und öffentlichem
Export kontrollieren. Bereits fremd gespeicherte Kopien lassen sich dadurch
nicht zurückholen.

## Verfügbare Werkzeuge

Die normale GitHub-Anbindung kann Code und Review-Dateien über Commits/PRs
veröffentlichen und Branch-Refs mit einem Lease aktualisieren. Sie bietet in
dieser Sitzung jedoch keine Funktionen zum Setzen von Secrets, Abbrechen
oder Abschalten von Workflows, Löschen von Artefakten/Logs/Caches oder
Bereinigen GitHub-interner PR-Refs. Diese Schritte benötigen einen regulären
Owner-/Admin-Zugang. Ein vorbereitetes Dokument oder ein erfolgreicher
Code-PR ist noch keine ausgeführte Historienbereinigung.

## Offizielle Referenzen

- [GitHub: Sensitive Daten aus einem Repository entfernen](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
- [GitHub: Actions-Secrets verwenden](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)
- [GitHub CLI: gh secret set](https://cli.github.com/manual/gh_secret_set)
- [git-filter-repo: Dokumentation](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt)
