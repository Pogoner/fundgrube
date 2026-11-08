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

test("copied triage instructions preserve absolute scores and separate source freshness", async () => {
  const ui = await render();
  await ui.copy();
  assert.match(ui.copied, /ursprünglichen Zeitpunkt weiter/);
  assert.match(ui.copied, /keine pauschale altersbasierte Bewertungsfrist/);
  assert.match(ui.copied, /niedrige Heuristikwerte sind kein Ausschluss/);
  assert.match(ui.copied, /nicht relativ zu den anderen Funden/);
  assert.match(ui.copied, /Ein neues gutes Angebot senkt keine vorhandene Bewertung/);
  assert.match(ui.copied, /Preis, Gebote und Verfügbarkeit separat/);
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
});
