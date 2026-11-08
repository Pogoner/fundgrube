# Design-Notiz: Fundgrube

Grundlage ist die wiederverwendbare [Design-Philosophie](../DESIGN.md).

## Aufgabe und Leitidee

Fundgrube ist eine **Arbeitsoberfläche zum Sichten von Auktionen**, keine
Landingpage und keine Scroll-Erzählung. Die erste Ansicht soll beantworten:
Was ist interessant, warum, wie lange läuft es noch, und was muss ich prüfen?
Eine kompakte Kopfzeile, nachvollziehbare Filter und bildgeführte Angebotskarten
tragen diese Aufgabe. Methoden- und Betriebsdokumentation bleiben auf `info.html`.

Visuelle Richtung: dunkle neutrale Grundflächen, präzise Typografie,
zurückhaltende räumliche Ebenen, ein warmer Kupferakzent und kühler Fokus.
Systemschrift, tabellarische Zahlen und wiederkehrende Abstände halten den
Arbeitsbereich ruhig. Kein übergroßer Hero, kein dekoratives Dauerleuchten,
keine zusätzliche Abhängigkeit von externen Schrift- oder Grafikdiensten.

## Hierarchie und Interaktion

1. Fundgrube, Navigation zur Info-Seite und tatsächlicher Datenstatus.
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
gespeicherten Scalp-/Useability-Werte und keine Bewertungsherkunft. Zahl und
Begründung der Profilausschlüsse stehen auf der Info-Seite, nicht in einer neuen
Filterreihe. Maschinen mit beiliegenden Unterlagen und echte Gerätekomponenten
werden nicht allein wegen ähnlicher Titelwörter ausgeschlossen.

Auf schmalen Bildschirmen bilden Karten und Filter eine gut bedienbare Spalte.
Kein horizontales Scrollen der gesamten Seite. Wichtige Handlungen funktionieren
ohne Hover; Fokus, Touch-Ziele und Dialogbedienung bleiben sichtbar.

## Datenwahrheit und Zeit

Aktiv ist ein bekanntes Angebot bei `0 < Restlaufzeit < 72 Stunden`, sofern es
die eingestellte Interest-Schwelle und die übrigen Filter erfüllt. Das Alter
der Quellenbeobachtung ist **kein Ablaufdatum des Angebots**. Der gespeicherte
Bestand bleibt bis zum Auktionsende erhalten, begrenzt durch die dokumentierte
Kapazität von 5.000 Datensätzen und die tatsächlich erreichte Quellenabdeckung.
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

Der geplante 30-Minuten-Abgleich versucht, die allgemeine und die Abholsuche
jedes Mal vollständig zu durchlaufen. Die Info-Seite zeigt den tatsächlichen
Durchlaufstatus, Seitenfortschritt, Abbrüche und den letzten erfolgreichen
vollständigen Quellenlauf. Das Erreichen aller gemeldeten Seiten ist weder ein
atomarer Snapshot der veränderlichen Quelle noch eine eBay-Vollabdeckungszusage.
Ein erfolgreiches Deployment ersetzt diesen Status nicht. Die Hauptseite bleibt
kompakt; ein abgeschlossener Quellenlauf darf dort nicht als bloßer Teilscan
bezeichnet werden, aber auch keine direkte eBay-Verifikation suggerieren.

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
prüfen; echte Geräte mit Anleitung dürfen dabei nicht verschwinden. Vollständige,
unvollständige und ältere Quellenlauf-Metadaten müssen unterscheidbar bleiben.
HTML/CSS/JS-Versionen in beiden Seiten gemeinsam anheben. Nach Deployment
Dashboard, Info-Navigation, Filter, Detailabruf, Bilder und Daten-Snapshot prüfen.
Nicht verfügbare Browser-/Viewport-Prüfungen ausdrücklich offenlassen.
