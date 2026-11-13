# Design-Philosophie für Websites

## Leitidee: ruhige Präzision

Eine gute Website fühlt sich wie ein zusammenhängendes Produkt an: ruhig,
räumlich, präzise und vom Inhalt geführt. Sie macht Bedeutung sichtbar und die
nächste Handlung verständlich. Wertigkeit entsteht durch Komposition,
Typografie, Rhythmus und verlässliches Verhalten — nicht durch möglichst viele
Effekte. Inspiration ist erlaubt, das Kopieren fremder Marken oder Assets nicht.

Diese Leitlinien sind unabhängig von Branche, Produkt und Technik. Sie
beschreiben eine Haltung und überprüfbare Entscheidungen, keinen festen
Seitentyp und keine Pflicht zu dunklen Oberflächen. Produktspezifische Tokens,
Ausnahmen und Anwendungen gehören in eine gesonderte Design-Notiz.

## 1. Die Aufgabe bestimmt die Inszenierung

- **Erklären und erzählen:** Eine klare Dramaturgie, ein Hauptgedanke je Abschnitt
  und bei Bedarf eine zentrale visuelle Idee führen durch den Inhalt.
- **Arbeiten und entscheiden:** Suche, Eingaben, Vergleiche und Ergebnisse stehen
  direkt bereit. Die Oberfläche bleibt kompakt, stabil und wiederholbar bedienbar.
- **Nachschlagen und prüfen:** Quellen, Methoden, Zahlen und Details werden präzise
  gegliedert. Tabellen und Diagramme dienen dem Verständnis, nicht der Dekoration.

Alle drei Formen dürfen dieselbe Designsprache nutzen. Ein Arbeitswerkzeug
braucht aber keine vorgeschaltete Scrollgeschichte; eine Erzählseite nicht
für jeden Satz eine Karte. Vor einer Überarbeitung kurz festhalten: Wer will
hier was tun, welche Information ist zuerst nötig, welche Handlung ist zentral?

## 2. Hierarchie vor Dekoration

Jede Ansicht hat einen erkennbaren Schwerpunkt. Überschrift, Ergebnis oder
wichtigste Handlung führen; Erläuterungen und Metadaten ordnen sich unter.
Visuelle Lautstärke folgt der Bedeutung: Größe, Gewicht, Position, Abstand und
erst dann Farbe einsetzen. Nicht alles gleichzeitig hervorheben.

Verwandte Inhalte werden durch Nähe und Ausrichtung verbunden. Eine zusätzliche
Fläche, Linie oder Karte ist nur sinnvoll, wenn sie eine echte Gruppe oder
Interaktion verständlich macht. Aufeinander gestapelte Boxen ohne Hierarchie
ersetzen keine Komposition. Details dürfen nachgelagert sein, aber relevante
Risiken, Kosten, Fehler und Entscheidungsgrundlagen nicht versteckt werden.

## 3. Ein kleines, bewusstes Gestaltungssystem

Farben, Abstände, Schriften, Radien und Bewegungsdauern werden als benannte Tokens
definiert. Rollen wie `background`, `surface`, `text`, `muted`, `accent`, `border`,
`focus` und `danger` sind dauerhafter als verstreute Farbwerte.

- Neutrale Grundflächen und klar abgestufte Textfarben schaffen Ruhe.
- Eine dominante Akzentfamilie setzt Prioritäten; weitere Farben haben eine
  eindeutige Rolle. Warm und kühl dürfen sich ergänzen, nicht um Aufmerksamkeit
  konkurrieren.
- Dezente Lichtverläufe oder Ebenen können räumliche Tiefe geben. Schatten,
  Transparenz und Unschärfe bleiben sparsam; Kontrast muss auch ohne sie bestehen.
- Radien sind konsistent und zur Größe des Elements passend. Pillen eignen sich
  für kurze Zustände und kompakte Schalter, nicht als universelle Form.
- Erfolg, Warnung und Fehler sind zusätzlich durch Text oder Symbol erkennbar.

Helle und dunkle Themen sind mögliche Ausprägungen derselben Regeln. Bestehende
Theme- oder Kontrastfunktionen nicht stillschweigend entfernen. Normale Texte
erhalten mindestens 4,5:1 Kontrast, große Texte mindestens 3:1; Fokus und wichtige
Bedienelemente müssen sich ebenfalls klar abheben. Kontraste messen, nicht schätzen.

## 4. Typografie und Rhythmus tragen den Entwurf

Eine gut abgestimmte Systemschrift ist ein vollwertiger Ausgangspunkt, etwa
`-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif`.
Externe Schriftdateien nur einsetzen, wenn sie einen erkennbaren Mehrwert bieten;
Lesbarkeit und Ladeverhalten bleiben wichtiger als Originalität um jeden Preis.

Wenige konsequente Schriftstufen genügen. Fließtext hat angenehme Zeilenhöhe und
meist etwa 60–75 Zeichen pro Zeile. Arbeitsoberflächen dürfen dichter sein,
ohne Beschriftungen und Hilfstexte zu unleserlichen Kleinteilen zu machen.
Zahlen verwenden bei Vergleichen tabellarische Ziffern; Einheiten, Zeitbezug und
Vorzeichen stehen direkt beim Wert. Lange Titel und deutsche Komposita mitdenken.

Ein gemeinsames Abstandsmaß, beispielsweise ein 8-Pixel-Raster mit 4-Pixel-
Zwischenschritten, sorgt für Rhythmus. Zusammengehöriges steht enger, neue
Abschnitte erhalten mehr Raum. Inhaltsbreite und Außenabstände richten sich nach
der Aufgabe: Eine Liste, ein Formular und eine Erzählseite benötigen nicht
dieselbe maximale Breite. Auf großen Displays bleibt die Ansicht komponiert,
auf kleinen Displays nutzbar; bloßes gleichmäßiges Verkleinern reicht nicht.

## 5. Bilder vermitteln etwas — oder entfallen

Ein Bild muss Orientierung, Identifikation, Atmosphäre oder Verständnis liefern.
Keine generische Dekoration als Ersatz für Inhalt. Eine erzählerische Seite
kann von einem starken Leitmotiv profitieren; ein Arbeitswerkzeug braucht vor
allem glaubwürdige, gut lesbare Informationen und gegebenenfalls echte Produktfotos.

Eigene Assets werden passend dimensioniert und optimiert. Feste Bildrahmen und
explizite Abmessungen verhindern Layoutsprünge. Der Ausschnitt ist beabsichtigt:
Bei dokumentierenden Produktbildern ist vollständige Erkennbarkeit oft wichtiger
als flächiges Füllen. Wichtige Bilder im ersten Sichtbereich laden früh; andere
dürfen verzögert laden. Fehlende oder blockierte Bilder erhalten einen klaren,
brauchbaren Ersatz und, wenn sinnvoll, einen zulässigen Originalverweis.

Eine eigene Marke braucht eine eigenständige Silhouette, nicht nur eine andere
Farbe für ein bekanntes Zeichen. Einfache Signets bleiben als lokale Vektoren
scharf; Kopfzeile und Favicon verwenden dieselbe Form. Die Wortmarke bleibt
lesbar und der Navigationslink verständlich benannt, auch ohne das Bild.

Fremde Bilder nicht ungeprüft kopieren oder über einen eigenen Proxy verteilen.
Ladeprobleme rechtfertigen keine Umgehung von Schutz- oder Privatsphäreeinstellungen.
Konzept- und KI-Bilder nicht als Beweis realer Gegenstände oder Fortschritte
verwenden. Exakte Zahlen, Beschriftungen und Diagramme bleiben in HTML, SVG oder
anderen geeigneten nativen Formaten statt in generierten Bildflächen.

## 6. Bewegung hat eine Aufgabe

Bewegung erklärt Zustandswechsel, räumliche Zusammenhänge oder Fortschritt.
Eine diskrete Rückmeldung ist meist hilfreicher als eine große Animation.
Als Ausgangspunkt: ungefähr 160–240 ms für kleine Interaktionen; längere
Übergänge nur, wenn Inhalt und Orientierung davon profitieren.

Natives Scrollen bleibt erhalten. Keine erzwungenen Scrollpfade, dekorativen
Endlosschleifen, dauernden Pulse oder automatisch startenden Töne. Animationen
bevorzugt über `transform` und `opacity`; Arbeit außerhalb des sichtbaren Bereichs
oder bei verborgenem Tab begrenzen. `prefers-reduced-motion` erhält eine
vollständige, verständliche Alternative, nicht eine unbenutzbare Restansicht.

## 7. Zugänglichkeit ist Teil des Designs

Mobile Ansichten werden neu geordnet, nicht nur geschrumpft. Die primäre Aufgabe
bleibt erreichbar; sekundäre Filter und Details können aufklappbar sein.
Interaktive Ziele sind nach Möglichkeit mindestens 44 × 44 CSS-Pixel groß.
Inhalte müssen bei Schriftvergrößerung und schmalen Fenstern funktionieren;
breite Datentabellen erhalten bei Bedarf einen eigenen Scrollbereich.

Native semantische Elemente zuerst verwenden: echte Links, Schaltflächen,
Formulare und Überschriften. Beschriftungen bleiben sichtbar. Tastaturbedienung,
deutlicher Fokus, sinnvolle Reihenfolge und bei Dialogen Escape sowie
Fokusrückgabe gehören zum vollständigen Ablauf. Kein wesentlicher Inhalt ist
nur per Hover zugänglich. Aussagekräftige Alternativtexte verwenden; rein
dekorative Elemente für assistive Technik ausblenden.

Laden, leerer Bestand, kein Filtertreffer, Teilfehler, fehlendes Bild, deaktivierter
Zustand und Erfolg werden ebenso gestaltet wie der Idealzustand. Ein Fehler
erklärt Auswirkung und nächsten sinnvollen Schritt, statt die Oberfläche
kommentarlos verschwinden zu lassen.

## 8. Die Oberfläche bleibt wahrheitsgetreu

Sprache ist konkret, ruhig und zum Publikum passend. Technische Implementierungs-
oder Agentenhinweise gehören in Entwicklerdokumentation, nicht in den normalen
Arbeitsablauf. Methodik und längere Erklärungen sind gut erreichbar, ohne jede
Nutzung mit derselben Einleitung zu beginnen. Erfolgreiche Routinezustände
brauchen keine dauerhafte Statusbox. Persönliche Such- oder Profildaten dürfen
nur dann öffentlich ausgeliefert werden, wenn sie für die Nutzung notwendig sind.

Daten, redaktionelle Interpretation und Darstellung bleiben getrennt. Unbekannt
ist nicht null; ein Score ist nicht automatisch eine Wahrscheinlichkeit;
gespeichert ist nicht aktuell verifiziert. Zeitbezug, Unsicherheit, Herkunft und
inhaltliche Grenzen dürfen durch die Gestaltung nicht verschwinden. Keine
fingierten Live-Indikatoren, Fortschritte, Nutzerzahlen oder Erfolgsbehauptungen.

## 9. Konsistenz wird umgesetzt und geprüft

Gemeinsame Komponenten und Tokens schaffen Zusammenhang. CSS bewusst begrenzen,
doppelte globale Navigation und konkurrierende Zustände vermeiden. Abhängigkeiten
und clientseitige Arbeit klein halten. Cache-Versionen, Fehlerfälle und Übergänge
von bereits geöffneten älteren Ansichten gehören zur Produktqualität.

Vor der Umsetzung genügt eine kurze produktspezifische Design-Notiz: Zweck,
visuelle Leitidee, Informationshierarchie, Daten-/Navigationsvertrag, mobile
Anordnung, Bild- und Bewegungsverhalten. Danach passend zur Änderung prüfen:

- Texte und Daten: stimmen Aussage, Quelle, Einheiten und sichtbarer Zeitbezug?
- Layout: breite, schmale und niedrige Fenster; lange Texte und Schriftvergrößerung.
- Interaktion: Tastatur, Fokus, Filter, Dialoge, Laden, Leer- und Fehlerzustände.
- Bilder und Bewegung: Ladeausfall, stabile Rahmen, sinnvolle Ausschnitte,
  reduzierte Bewegung und keine verdeckte Hauptinformation.
- Veröffentlichung: automatisierte Tests, sichtbare Browserprüfung und die
  tatsächlich veröffentlichte Version prüfen; Screenshots unterstützen die
  visuelle Kontrolle, ersetzen keine Funktionstests.

Nicht durchgeführte Prüfungen werden als solche benannt. Alte Seiten gelten
nicht allein durch diese Datei als bereits überarbeitet. Bewusste Ausnahmen
werden kurz begründet; neue dauerhafte Regeln werden hier gepflegt.

---

Die konkrete Anwendung für dieses Repository steht in
[docs/design-fundgrube.md](docs/design-fundgrube.md). Diese allgemeine Philosophie
bleibt auch für andere Websites ohne die Fundgrube-spezifische Notiz verwendbar.
