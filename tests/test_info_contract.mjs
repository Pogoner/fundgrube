/** Dependency-free Info metadata tests; no browser or live-network access. */
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import test from "node:test";

const info = new URL("../site/info.html", import.meta.url);
const html = readFileSync(info, "utf8");
const source = readFileSync(new URL("info.js", info), "utf8");
const latest = "2026-11-08T09:00:00Z";
const oldest = "2026-11-03T07:30:00Z";
const review = overrides => ({status: "applied", reviewer: "ChatGPT", applied: 2, unreviewed: 3, reviewed_at: latest, oldest_reviewed_at: oldest, latest_reviewed_at: latest, expires_at: null, retention_basis: "content_and_auction", ...overrides});

async function render(overrides = {}) {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], {
    innerHTML: "", textContent: "", listeners: new Map(),
    addEventListener(name, handler) { this.listeners.set(name, handler); }
  }]));
  const feed = {schema_version: 3, source_status: "partial", source_provider: "aog", generated_at: latest, last_successful_scan_at: latest, ai_enabled: false, deals: [], stats: {received: 144, carried: 600}, coverage: {complete: false, source_total_reported: 4174, retention_capacity: 5000}, interactive_review: review(), ...overrides};
  let copied = "";
  vm.runInNewContext(source, {
    document: {querySelector(selector) { const node = nodes.get(selector.slice(1)); assert(node, `Expected Info node ${selector}`); return node; }},
    URLSearchParams, Intl, Date, location: {search: ""},
    navigator: {clipboard: {async writeText(value) { copied = value; }}},
    fetch: async () => ({ok: true, json: async () => structuredClone(feed)})
  }, {filename: "site/info.js"});
  await new Promise(resolve => setImmediate(resolve));
  return {nodes, get copied() { return copied; }, async copy() { await nodes.get("copy-instructions").listeners.get("click")(); }};
}

test("Info shows the range of original per-item review times, not one new shared date", async () => {
  const ui = await render();
  const text = ui.nodes.get("info-review").innerHTML;
  assert.match(text, /ChatGPT-Sichtung: 2 Angebote/);
  assert.match(text, /Gespeicherte Bewertungszeitpunkte: 03\.11\.26, 08:30 bis 08\.11\.26, 10:00 \(Berlin\)/);
  assert.match(text, /3 Angebote ohne angewendete vollständige ChatGPT-Sichtung/);
  assert.match(text, /Automatische LLM-Aufrufe aus/);
  assert.doesNotMatch(text, /abgelaufen|Datum:|48 Stunden/);
});

test("legacy metadata remains readable without implying that every article was re-reviewed", async () => {
  const ui = await render({interactive_review: review({oldest_reviewed_at: undefined, latest_reviewed_at: undefined, reviewed_at: oldest})});
  const text = ui.nodes.get("info-review").innerHTML;
  assert.match(text, /Letzter ausgewiesener Bewertungszeitpunkt: 03\.11\.26, 08:30/);
  assert.match(text, /einzelne Angebote können früher bewertet worden sein/);
  assert.doesNotMatch(text, /abgelaufen|Gespeicherte Bewertungszeitpunkte:/);
});

test("missing and invalid review dates do not become a valid time range", async () => {
  for (const fields of [
    {oldest_reviewed_at: null, latest_reviewed_at: null, reviewed_at: null},
    {oldest_reviewed_at: "<script>bad</script>", latest_reviewed_at: "not a date", reviewed_at: "invalid"}
  ]) {
    const ui = await render({interactive_review: review(fields)});
    const text = ui.nodes.get("info-review").innerHTML;
    assert.match(text, /Bewertungszeitpunkte nicht angegeben/);
    assert.doesNotMatch(text, /Gespeicherte Bewertungszeitpunkte:|Invalid Date|<script>/);
  }
  const reversed = await render({interactive_review: review({oldest_reviewed_at: latest, latest_reviewed_at: oldest})});
  assert.doesNotMatch(reversed.nodes.get("info-review").innerHTML, /Gespeicherte Bewertungszeitpunkte:/);
});

test("malformed applied and pending counts are not displayed as real counts", async () => {
  for (const invalid of [-1, 1.5, "2", null]) {
    const ui = await render({interactive_review: review({applied: invalid})});
    assert.match(ui.nodes.get("info-review").innerHTML, /ChatGPT-Sichtung: Status unklar/);
  }
  const pending = await render({interactive_review: review({unreviewed: -1})});
  assert.doesNotMatch(pending.nodes.get("info-review").innerHTML, /-1 Angebote/);
  const zero = await render({interactive_review: review({applied: 0, unreviewed: 0})});
  assert.match(zero.nodes.get("info-review").innerHTML, /ChatGPT-Sichtung: 0 Angebote/);
});

test("storage capacity is bounded metadata, not a source coverage fraction", async () => {
  const ui = await render();
  const text = ui.nodes.get("info-coverage").innerHTML;
  assert.match(text, /Speicherkapazität/);
  assert.match(text, /5\.000 Datensätze; keine Vollabdeckungszusage/);
  assert.match(text, /In diesem Lauf erhalten/);
  assert.match(text, /Allgemeine Treffer laut Quelle/);
  assert.match(text, /Begrenzter Ausschnitt, keine Vollabdeckung/);
  assert.doesNotMatch(text, /144 \/ 4174|600 \/ 4174/);
  assert.match(ui.nodes.get("info-summary").textContent, /Restlaufzeit, nicht dem Funddatum/);
  for (const invalid of [undefined, 0, -1, 5001, 42.5, "5000"]) {
    const other = await render({coverage: {retention_capacity: invalid}});
    assert.doesNotMatch(other.nodes.get("info-coverage").innerHTML, /Speicherkapazität/);
  }
});

test("completed source traversal is distinct from stable pagination or full eBay coverage", async () => {
  const ui = await render({source_status: "live", coverage: {
    complete: false, pagination_is_stable: false, traversal_completed: true,
    source_total_reported: 4174, planned_interval_minutes: 30, pages_fetched: 93,
    elapsed_seconds: 940, max_run_seconds: 1500, max_api_requests_per_run: 160,
    requests_today: 95, daily_request_budget: 8000,
    scan_started_at: "2026-11-08T08:44:00Z", scan_finished_at: latest, scan_completed_at: latest,
    last_successful_full_scan_at: latest, source_cache_max_age_minutes: 15,
    searches: {
      general: {enabled: true, traversal_completed: true, pages_fetched: 87, unique_records: 4160, duplicate_records: 14, total_reported_initial: 4177, total_reported_latest: 4174, total_changed: true, stop_reason: "reported_end"},
      pickup: {enabled: true, traversal_completed: true, pages_fetched: 6, unique_records: 260, duplicate_records: 0, total_reported_initial: 260, total_reported_latest: 260, total_changed: false, stop_reason: "reported_end"}
    }
  }});
  const text = ui.nodes.get("info-coverage").innerHTML;
  assert.match(text, /Alle aktivierten Suchlisten bis zum gemeldeten Ende durchlaufen/);
  assert.match(text, /Beginn \/ Ende des Quellenlaufs/);
  assert.match(text, /08\.11\.26, 09:44 \/ 08\.11\.26, 10:00 \(Berlin\)/);
  assert.match(text, /Letzter vollständiger Quellenlauf<\/small><strong>08\.11\.26, 10:00/);
  assert.match(text, /Allgemeine Suche<\/small><strong>Listenende erreicht · 87 Seiten · 4160 unterschiedliche Treffer · 14 wiederholte Treffer/);
  assert.match(text, /4177 bei Start \/ 4174 zuletzt · während des Laufs verändert/);
  assert.match(text, /Abholsuche<\/small><strong>Listenende erreicht · 6 Seiten · 260 unterschiedliche Treffer/);
  assert.match(text, /30 Min\.; Ausführung kann sich verzögern/);
  assert.match(text, /Angesetzter Cache-Abstand<\/small><strong>bis 15 Min\. in diesem Lauf; keine eBay-Frischegarantie/);
  assert.match(text, /Tatsächliche Laufdauer<\/small><strong>15 Min\. 40 Sek\./);
  assert.match(text, /Limit der Datenanfragen pro Lauf<\/small><strong>160/);
  assert.match(text, /95 \/ 8000; einschließlich robots\.txt/);
  assert.match(text, /1500 Sekunden; kein zugesicherter Abschluss/);
  assert.match(text, /Quellenlisten durchlaufen; veränderliche Pagination, keine eBay-Vollabdeckung/);
  assert.doesNotMatch(text, /Begrenzter Ausschnitt|Keine Datenlücken|100\s*%/);
});

test("partial traversals keep the last completed scan and explain per-search interruption", async () => {
  const ui = await render({coverage: {
    traversal_completed: false, complete: false, scan_started_at: "2026-11-08T08:44:00Z", scan_finished_at: latest,
    scan_completed_at: null, last_successful_full_scan_at: oldest,
    searches: {
      general: {enabled: true, traversal_completed: false, pages_fetched: 3, unique_records: 96, total_reported_initial: 4174, total_reported_latest: 4200, total_changed: true, unobserved_estimate: 4104, stop_reason: "repeated_page"},
      pickup: {enabled: true, traversal_completed: true, pages_fetched: 1, unique_records: 12, total_reported_initial: 12, total_reported_latest: 12, stop_reason: "reported_end"}
    }
  }});
  const text = ui.nodes.get("info-coverage").innerHTML;
  assert.match(text, /Nicht vollständig durchlaufen; Teilstände möglich/);
  assert.match(text, /Letzter vollständiger Quellenlauf<\/small><strong>03\.11\.26, 08:30/);
  assert.match(text, /Allgemeine Suche · Unterbrechung<\/small><strong>Wiederholte Quellseite/);
  assert.match(text, /4104 laut Schätzung; veränderliche Ergebnisliste/);
  assert.match(text, /Abholsuche<\/small><strong>Listenende erreicht/);
  assert.doesNotMatch(text, /Alle aktivierten Suchlisten bis zum gemeldeten Ende durchlaufen/);
});

test("a completed traversal cannot hide records omitted by the storage bound", async () => {
  const ui = await render({coverage: {
    complete: false, traversal_completed: true, retention_capacity: 5000,
    retention_omitted_records: 74, all_collected_records_retained: false
  }});
  const text = ui.nodes.get("info-coverage").innerHTML;
  assert.match(text, /Alle aktivierten Suchlisten bis zum gemeldeten Ende durchlaufen/);
  assert.match(text, /Durch Speichergrenze ausgelassen<\/small><strong>74 Datensätze; getrennt vom Quellenlauf/);
  const unknown = await render({coverage: {retention_omitted_records: -1, all_collected_records_retained: false}});
  assert.match(unknown.nodes.get("info-coverage").innerHTML, /Speicherübernahme unvollständig/);
  assert.doesNotMatch(unknown.nodes.get("info-coverage").innerHTML, /-1 Datensätze/);
});

test("invalid traversal fields cannot imply completion or inject HTML", async () => {
  const ui = await render({coverage: {
    traversal_completed: false, planned_interval_minutes: -30,
    scan_started_at: "invalid", scan_finished_at: "<script>bad</script>", last_successful_full_scan_at: null,
    searches: {
      general: {enabled: true, traversal_completed: "true", pages_fetched: -1, unique_records: "100", duplicate_records: 1.5, total_reported_initial: -1, unobserved_estimate: -5, stop_reason: "<img src=x onerror=bad()>"},
      pickup: {enabled: false, traversal_completed: true}
    }
  }});
  const text = ui.nodes.get("info-coverage").innerHTML;
  assert.match(text, /Noch nicht belegt/);
  assert.match(text, /unbekannt \/ unbekannt/);
  assert.match(text, /Allgemeine Suche<\/small><strong>Nicht vollständig durchlaufen/);
  assert.match(text, /Abholsuche<\/small><strong>Nicht aktiviert/);
  assert.match(text, /&lt;img src=x/);
  assert.doesNotMatch(text, /<script>|<img |-1 Seiten|-5 laut|100 unterschiedliche|1\.5 wiederholte|Geplanter Startabstand|Listenende erreicht/);
});

test("personal-profile counts do not reinterpret stored LLM scores", async () => {
  const ui = await render({deals: [
    {title: "Ungewöhnliche Drucksache", personal_interest_excluded: true, assessment_basis: "llm", scalp_score: 90, useability_score: 99},
    {title: "KAESER MOBILAIR M 27 BROSCHÜRE 12 Seiten RAR", assessment_basis: "llm", scalp_score: 90, useability_score: 99},
    {title: "Bohrmaschine mit Bedienungsanleitung", personal_interest_excluded: false, scalp_score: 80, useability_score: 90},
    {title: "Prospekthalter aus Metall", scalp_score: 30, useability_score: 95}
  ]});
  const text = ui.nodes.get("info-profile").textContent;
  assert.match(text, /2 von 4 gespeicherten Angeboten durch das persönliche Profil ausgeblendet/);
  assert.match(text, /Hauptliste und beide Top 10, unabhängig von der Interest-Schwelle/);
  assert.match(text, /unveränderten Scores/);
  assert.match(text, /personal_interest_excluded/);
});

test("copied triage instructions preserve absolute scores and separate source freshness", async () => {
  const ui = await render();
  await ui.copy();
  assert.match(ui.copied, /ursprünglichen Zeitpunkt weiter/);
  assert.match(ui.copied, /keine pauschale altersbasierte Bewertungsfrist/);
  assert.match(ui.copied, /niedrige Heuristikwerte sind kein Ausschluss/);
  assert.match(ui.copied, /nicht relativ zu den anderen Funden/);
  assert.match(ui.copied, /Ein neues gutes Angebot senkt keine vorhandene Bewertung/);
  assert.match(ui.copied, /Preis, Gebote und Verfügbarkeit separat/);
  assert.match(ui.copied, /coverage\.traversal_completed/);
  assert.match(ui.copied, /30-Minuten-Takt ist keine Aktualitätszusage/);
  assert.match(ui.copied, /personal_interest_excluded unabhängig von Scores/);
  assert.match(ui.copied, /Verändere für diesen Profilausschluss keine gespeicherten LLM-Scores/);
  assert.match(ui.copied, /Keine Gebote, Käufe oder externen Schreibaktionen/);
  assert.doesNotMatch(ui.copied, /Bewertungen haben separate Fristen|48.Stunden.Frist/);
});

test("static Info explains incremental merge without a semantic age expiry", () => {
  assert.match(html, /pro Artikel nach einem festen Maßstab/);
  assert.match(html, /ursprünglichen Bewertungszeitpunkt/);
  assert.match(html, /höchstens 5\.000 Datensätzen/);
  assert.match(html, /verfallen nicht pauschal nach 48 Stunden/);
  assert.match(html, /nicht nach einer pauschalen Stundenfrist/);
  assert.doesNotMatch(html, /Kapazitätsgrenze ist 800|eigene Frist von 48 Stunden/);
  assert.match(html, /kein Nachweis für deinen Browser/);
  assert.match(html, /alle 30 Minuten/);
  assert.match(html, /Minute 07 und 37/);
  const workflow = readFileSync(new URL("../.github/workflows/publish.yml", import.meta.url), "utf8");
  assert.match(workflow, /cron: "7,37 \* \* \* \*"/);
  assert.match(html, /abgeschlossenen Quellenlauf/);
  assert.match(html, /kein atomarer Snapshot und keine garantierte Vollabdeckung/);
  assert.match(html, /Separat angebotene Prospekte, Broschüren/);
  assert.match(html, /Ein echtes Gerät mit beiliegender Anleitung bleibt dagegen im Profil/);
  assert.doesNotMatch(html, /alle zehn Minuten|13, 23, 33|rotierende Folgeseiten|vier Daten-API/);
});
