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

Auf schmalen Bildschirmen bilden Karten und Filter eine gut bedienbare Spalte.
Kein horizontales Scrollen der gesamten Seite. Wichtige Handlungen funktionieren
ohne Hover; Fokus, Touch-Ziele und Dialogbedienung bleiben sichtbar.

## Datenwahrheit und Zeit

Aktiv ist ein bekanntes Angebot bei `0 < Restlaufzeit < 72 Stunden`, sofern es
die eingestellte Interest-Schwelle und die übrigen Filter erfüllt. Das Alter
der Quellenbeobachtung ist **kein Ablaufdatum des Angebots**. Der gespeicherte
Bestand bleibt bis zum Auktionsende erhalten, begrenzt durch die dokumentierte
Kapazität von 800 Datensätzen und die tatsächlich erreichte Quellenabdeckung.
Die GUI berechnet den Zeithorizont auch bei einer länger geöffneten Seite neu.

Ältere Quellenstände bleiben als solche markiert. Ein Fund wird durch
Wiederanzeige oder Neubau nicht erneut verifiziert. „0 Gebote“ und Preise
bezeichnen den Quellenstand; die 50-km-Abholsuche ist kein bestätigter Standort.
Scores, LLM-/Heuristik-Herkunft und tatsächliche Eignungsfilter bleiben getrennt.

## Bilder, Bewegung und Fehler

Echte eBay-Fotos werden aus zulässigen Bildverweisen geladen, nicht erfunden
oder über einen neuen Proxy kopiert. Der vollständige Gegenstand ist wichtiger
als ein spektakulärer Ausschnitt. Stabile Bildrahmen, frühes Laden der ersten
Karten und verzögertes Laden weiterer Bilder reduzieren Sprünge und Wartezeit.
Bei Ladefehlern gibt es einen erkennbaren Ersatz samt Originalbild-Verweis.
Eine externe Browsersperre lässt sich damit nicht als behoben versprechen.

Bewegung beschränkt sich auf kleine Interaktionsrückmeldungen. Bei reduzierter
Bewegung bleiben alle Informationen und Funktionen erhalten. Leerer Bestand,
zu strenge Filter, Quellenfehler und veraltete Beobachtungen sind eigenständige,
verständlich beschriftete Zustände.

## Abnahme

Score-Logik und Faktenintegrität unverändert testen; strikte 72-Stunden-Grenze,
ältere Beobachtungen, dynamischen Fenstereintritt und Bildfehler regressionsprüfen.
HTML/CSS/JS-Versionen in beiden Seiten gemeinsam anheben. Nach Deployment
Dashboard, Info-Navigation, Filter, Detailabruf, Bilder und Daten-Snapshot prüfen.
Nicht verfügbare Browser-/Viewport-Prüfungen ausdrücklich offenlassen.
