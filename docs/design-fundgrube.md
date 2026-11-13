# Design-Notiz: Fundgrube

Grundlage ist die wiederverwendbare [Design-Philosophie](../DESIGN.md).

## Aufgabe und Leitidee

Fundgrube ist eine **Arbeitsoberfläche zum Sichten von Auktionen**, keine
Landingpage und keine Scroll-Erzählung. Die erste Ansicht soll beantworten:
Was ist interessant, warum, wie lange läuft es noch, und was muss ich prüfen?
Eine kompakte Kopfzeile, nachvollziehbare Filter und bildgeführte Angebotskarten
tragen diese Aufgabe. `info.html` erklärt nur Scores, Quellenstand und Bildhilfe.
Betriebs- und API-Dokumentation bleibt in den Repository-Dokumenten.

Visuelle Richtung: dunkle neutrale Grundflächen, präzise Typografie,
zurückhaltende räumliche Ebenen, ein warmer Kupferakzent und kühler Fokus.
Systemschrift, tabellarische Zahlen und wiederkehrende Abstände halten den
Arbeitsbereich ruhig. Kein übergroßer Hero, kein dekoratives Dauerleuchten,
keine zusätzliche Abhängigkeit von externen Schrift- oder Grafikdiensten.

Die Marke zeigt ein facettiertes Fundstück über einer offenen, winkligen Grube.
Die eigenständige Silhouette ohne Buchstaben oder Hintergrundkachel verbindet
Suchen und Finden mit dem Kupferakzent. Beide Kopfzeilen und das Browser-Favicon
nutzen dieselbe lokale SVG-Datei `site/favicon.svg`; die Wortmarke bleibt als
Text erhalten. Das Signet ist neben dem bereits benannten Startseitenlink für
assistive Technik dekorativ und hat feste Abmessungen gegen Layoutsprünge.

## Hierarchie und Interaktion

1. Fundgrube, Navigation zur Info-Seite und die Aktion „Neu laden“.
2. Suche, Mindest-Interest und die wenigen häufig benötigten Filter.
3. Angebotsbild, Titel und Interest als zentrale Vergleichsgröße.
4. Scalp/Useability, semantische Begründung, Preis-/Transportangaben und Restzeit.
5. Quellenalter, Unsicherheiten, aufklappbare Details und Originalangebot.

Unterhalb der primären Treffer folgen zwei kompakte Top-10-Stöberlisten für
vollständig bewertete Angebote unter der aktuellen Interest-Schwelle: einmal
nach Scalp, einmal nach Useability. Sie ergänzen die Hauptpriorität, ersetzen
sie nicht. Gleiche Sachfilter und das aktive Zeitfenster gelten weiter;
unbekannte Bewertungen sind keine Nullwerte. Einträge dürfen in beiden Listen
vorkommen. Rang, Titel, alle drei Scores, Herkunft und wesentliche Kosten/Risiken
bleiben ohne ein zweites Raster aus zwanzig großen Bildkarten erkennbar.

Ein separates persönliches Profil blendet allein angebotene Prospekte,
Broschüren, Kataloge, Anleitungen und Zeitschriften aus Haupt- und Stöberlisten
aus, auch bei Interest 0 und Diagnosefiltern. Die Anzeige verändert dafür keine
gespeicherten Scalp-/Useability-Werte und keine Bewertungsherkunft. Technische
Zähler und Profilregeln werden nicht als zusätzliche Informationsblöcke auf der
Website ausgegeben. Maschinen mit beiliegenden Unterlagen und echte Gerätekomponenten
werden nicht allein wegen ähnlicher Titelwörter ausgeschlossen.

Auf schmalen Bildschirmen bilden Karten und Filter eine gut bedienbare Spalte.
Kein horizontales Scrollen der gesamten Seite. Wichtige Handlungen funktionieren
ohne Hover; Fokus, Touch-Ziele und Dialogbedienung bleiben sichtbar.

## Datenwahrheit und Zeit

Aktiv ist ein bekanntes Angebot bei `0 < Restlaufzeit < 72 Stunden`, sofern es
die eingestellte Interest-Schwelle und die übrigen Filter erfüllt. Das Alter
der Quellenbeobachtung ist **kein Ablaufdatum des Angebots**. Der gespeicherte
Bestand bleibt höchstens bis zum Auktionsende erhalten, begrenzt durch die dokumentierte
Kapazität von 5.000 Datensätzen und die tatsächlich erreichte Quellenabdeckung.
Nach einem erfolgreichen vollständigen AOG-Crawl werden Angebote entfernt,
die in keiner aktivierten Suche mehr vorkommen. Teilabrufe und Fehler bewahren
ältere Beobachtungen; ein bestätigt leerer Vollscan darf den Bestand leeren.
Neuere bekannte Beobachtungen werden nicht durch ältere Cache-Antworten gelöscht.
Die GUI berechnet den Zeithorizont auch bei einer länger geöffneten Seite neu.

Ältere Quellenstände bleiben als solche markiert. Ein Fund wird durch
Wiederanzeige oder Neubau nicht erneut verifiziert. „0 Gebote“ und Preise
bezeichnen den Quellenstand; die 50-km-Abholsuche ist kein bestätigter Standort.
Die Abholsuche hat einen eigenen Beobachtungszeitpunkt: Ein neuer allgemeiner
Preis-/Gebotsstand darf alte Radius-Evidenz nicht verjüngen. Details zeigen beide
Zeitbezüge getrennt; ein über zwei Stunden alter Umkreissuchstand wird auch in
der Transportzeile der Karte ausdrücklich markiert.
Scores, LLM-/Heuristik-Herkunft und tatsächliche Eignungsfilter bleiben getrennt.
Scalp und Useability sind am festen Rubrikmaßstab orientierte Einschätzungen,
keine relativen Rangplätze: Neue Angebote verändern bestehende Scores nicht.
Jeder Artikel behält den Zeitpunkt seiner tatsächlichen Bewertung. Die Anzeige
eines neuen Snapshots ist weder eine neue Bewertung noch eine Quellenprüfung.

Der geplante Quellenabgleich versucht, die allgemeine und die Abholsuche
jedes Mal vollständig zu durchlaufen. Vollständige, unvollständige und
unbestätigte Quellenantworten bleiben in den Exportmetadaten unterscheidbar.
Ein erfolgreiches Deployment ersetzt keinen erfolgreichen Quellenabruf.
Die Website suggeriert keine direkte eBay-Verifikation.

Ein normal geladener Bestand benötigt keine „Snapshot geladen“-Box, Scan-Zähler,
Cache-Erklärungen oder Speicherstatistik. Nur Fehler, unvollständige oder alte
Daten erhalten einen kurzen Hinweis mit der Auswirkung für die Nutzung.
Quellenalter und Risiken des einzelnen Angebots bleiben an den Karten und in
Details sichtbar. „Neu laden“ holt die zuletzt veröffentlichten Daten; es startet
keinen Quellenlauf. Die Ergebniszeile nennt nur Trefferzahl und Zeitfenster.

Die private Such-PLZ und Suchstadt werden weder auf den Seiten angezeigt noch
als Suchprofil in Website-/API-Exporten ausgeliefert. Der Export berechnet vor
der Redaktion `pickup_search_profile_match`; damit bleibt der Abholfilter ohne
Offenlegung der Suchregion möglich. Vorhandene Legacy-Snapshots bleiben lesbar,
aber ihre Suchregion wird ebenfalls nicht dargestellt. Tatsächliche
Verkäuferstandorte sind Angebotsdaten und bleiben davon getrennt.

Die Info-Seite enthält drei kurze Abschnitte: Scores, Prüfung des Originalangebots
und Bildhilfe. Keine Betriebsdiagnostik, Review-Zähler, Rubriktabellen oder
Agentenanweisungen. Solche Details gehören in `docs/` und die API-Dokumentation.

## Bilder, Bewegung und Fehler

Echte eBay-Fotos werden aus zulässigen Bildverweisen geladen, nicht erfunden
oder über einen neuen Proxy kopiert. Der vollständige Gegenstand ist wichtiger
als ein spektakulärer Ausschnitt. Stabile Bildrahmen, frühes Laden der ersten
Karten und verzögertes Laden weiterer Bilder reduzieren Sprünge und Wartezeit.
Bei Ladefehlern gibt es einen erkennbaren Ersatz samt Originalbild-Verweis.
Eine verlinkte Bildhilfe unterscheidet das Öffnen der Originaladresse von der
Einbettung und erklärt den Firefox-eigenen Schutz unabhängig von Erweiterungen.
Der Fehlerzustand behauptet keine automatisch erkannte Ursache. Eine externe
Browsersperre lässt sich damit nicht als behoben versprechen.

Bewegung beschränkt sich auf kleine Interaktionsrückmeldungen. Bei reduzierter
Bewegung bleiben alle Informationen und Funktionen erhalten. Leerer Bestand,
zu strenge Filter, Quellenfehler und veraltete Beobachtungen sind eigenständige,
verständlich beschriftete Zustände.

## Abnahme

Score-Logik und Faktenintegrität unverändert testen; strikte 72-Stunden-Grenze,
ältere Beobachtungen, dynamischen Fenstereintritt und Bildfehler regressionsprüfen.
Stöberlisten bei veränderter Interest-Schwelle, Sachfiltern und Zeitgrenzen
mitprüfen; abgelaufene oder unvollständig bewertete Angebote nicht einschleusen.
Profilausschlüsse auch bei hohen LLM-Scores und Interesse 0 in allen Listen
prüfen; echte Geräte mit Anleitung dürfen dabei nicht verschwinden. Fehler, Teilabrufe
und alte Quellenstände müssen trotz reduzierter Oberfläche erkennbar bleiben.
Anonymer Abholabgleich und Legacy-Fallback werden ohne Anzeige der Suchregion geprüft.
HTML/CSS/JS-Versionen in beiden Seiten gemeinsam anheben. Nach Deployment
Dashboard, Info-Navigation, Filter, Detailabruf, Bilder und Daten-Snapshot prüfen.
Nicht verfügbare Browser-/Viewport-Prüfungen ausdrücklich offenlassen.
