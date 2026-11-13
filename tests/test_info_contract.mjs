import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../site/info.html", import.meta.url), "utf8");
const source = readFileSync(new URL("../site/info.js", import.meta.url), "utf8");
const healthy = {schema_version: 3, kind: "manifest", source_status: "live"};

async function render({data = healthy, search = "", ok = true, failure = null} = {}) {
  const nodes = new Map([...html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)].map(match => [match[1], {
    textContent: "", hidden: /\bhidden\b/.test(match[0]), href: "./"
  }]));
  const requests = [];
  vm.runInNewContext(source, {
    document: {querySelector(selector) {
      const node = nodes.get(selector.slice(1));
      assert(node, `Expected Info node ${selector}`);
      return node;
    }},
    URLSearchParams, location: {search},
    fetch: async (url, options) => {
      requests.push({url, options});
      if (failure) throw new Error(failure);
      return {ok, json: async () => structuredClone(data)};
    }
  }, {filename: "site/info.js"});
  await new Promise(resolve => setImmediate(resolve));
  return {nodes, requests, status: nodes.get("info-status")};
}

test("Info is a short guide with score, source and image help, without private or operational detail", () => {
  const body = html.split("<body")[1].replace(/^[^>]*>/, "").replace(/<[^>]+>/g, " ").trim();
  const words = body.split(/\s+/).length;
  assert(words >= 150 && words <= 250, `Expected a compact guide, got ${words} words`);
  for (const id of ["scores", "source", "bilder", "images"]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /wirtschaftliche Potenzial/);
  assert.match(html, /eigenständig.*Geräte oder Systeme/);
  assert.match(html, /Interest = Scalp × Useability ÷ 100/);
  assert.match(html, /90 × 95 ergibt 86/);
  assert.match(html, /weder Marktwert noch Vollständigkeit oder Funktion/);
  assert.match(html, /zwischengespeicherten/);
  assert.match(html, /Originalangebot/);
  assert.match(html, /Gebote, Zustand, Lieferumfang und Transport/);
  assert.match(html, /Abholhinweis bestätigt keinen Abholort/);
  assert.match(html, /erkennt die Ursache nicht automatisch/);
  assert.doesNotMatch(html, /69121|Heidelberg|PLZ|Suchradius|Snapshot|Budget|Schnittstelle|coverage|copy-instructions|info-profile|info-review|<table\b/i);
  assert.match(html, /id="info-status"[^>]*role="status"[^>]*hidden/);
  for (const asset of ["styles.css", "info.js"]) assert(html.includes(`${asset}?v=design-v3.9`));
});

test("healthy data adds no success box and only requests the small manifest", async () => {
  const ui = await render();
  assert.equal(ui.status.hidden, true);
  assert.equal(ui.status.textContent, "");
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].url, "./api/manifest.json");
  assert.equal(ui.requests[0].options.cache, "no-store");
});

test("partial updates and deferred requests stay distinguishable without crawl diagnostics", async () => {
  const partial = await render({data: {...healthy, source_status: "partial"}});
  assert.equal(partial.status.hidden, false);
  assert.match(partial.status.textContent, /nur teilweise aktualisiert/);
  const paused = await render({data: {...healthy, source_status: "partial", blockers: [{code: "source_cooldown"}]}});
  assert.equal(paused.status.hidden, false);
  assert.match(paused.status.textContent, /keinen neuen Abruf/);
  assert.doesNotMatch(paused.status.textContent, /teilweise|erfolgreich|aktuell bestätigt|Snapshot|Cooldown|Seiten|Budget/);
});

test("a contradictory live result cannot hide an unconfirmed source response", async () => {
  for (const coverage of [
    {reported_nonempty_but_no_records: true, traversal_completed: true},
    {searches: {pickup: {enabled: true, reported_nonempty_but_no_records: true}}},
    {searches: {general: {enabled: true, stop_reason: "reported_nonempty_but_no_records"}}}
  ]) {
    const ui = await render({data: {...healthy, coverage}});
    assert.equal(ui.status.hidden, false);
    assert.match(ui.status.textContent, /nicht bestätigt/);
    assert.doesNotMatch(ui.status.textContent, /erfolgreich|vollständig aktualisiert/);
  }
  const inactive = await render({data: {...healthy, coverage: {searches: {pickup: {enabled: false, reported_nonempty_but_no_records: true}}}}});
  assert.equal(inactive.status.hidden, true);
});

test("source errors and disabled sources remain visible and never imply current offers", async () => {
  const failed = await render({data: {...healthy, source_status: "error"}});
  assert.equal(failed.status.hidden, false);
  assert.match(failed.status.textContent, /konnten zuletzt nicht aktualisiert werden/);
  for (const source_status of ["disabled", "unconfigured"]) {
    const ui = await render({data: {...healthy, source_status}});
    assert.equal(ui.status.hidden, false);
    assert.match(ui.status.textContent, /nicht aktiv/);
    assert.doesNotMatch(ui.status.textContent, /aktualisiert|neue Angebote/);
  }
});

test("network and format failures keep a small actionable note without exposing raw errors", async () => {
  for (const options of [
    {failure: "private path /69121/Heidelberg <script>bad()</script>"},
    {ok: false},
    {data: null},
    {data: {schema_version: 999, source_status: "live"}},
    {data: {schema_version: 3}}
  ]) {
    const ui = await render(options);
    assert.equal(ui.status.hidden, false);
    assert.match(ui.status.textContent, /Datenstatus ist nicht erreichbar/);
    assert.match(ui.status.textContent, /Hinweise im Radar/);
    assert.doesNotMatch(ui.status.textContent, /69121|Heidelberg|private path|<script>|HTTP|Snapshot/);
  }
});

test("private profile data, source messages and markup are not copied into the page", async () => {
  const privateText = "69121 Heidelberg <img src=x onerror=bad()>";
  for (const source_status of ["live", "partial", "error", privateText]) {
    const ui = await render({data: {
      ...healthy, source_status,
      location: {postcode: "69121", city: "Heidelberg", pickup_radius_km: 50},
      status_message: privateText,
      blockers: [{code: "other", message: privateText, action: privateText}],
      coverage: {source_url: privateText},
      interactive_review: {reviewer: privateText}
    }});
    assert.doesNotMatch(ui.status.textContent, /69121|Heidelberg|<img|onerror|reviewer|50/);
  }
  assert.doesNotMatch(source, /innerHTML|\.location\b|postcode|pickup_radius_km|status_message|clipboard/);
});

test("demo mode preserves navigation and clearly labels fictitious offers", async () => {
  const ui = await render({search: "?demo=1"});
  assert.equal(ui.nodes.get("home-link").href, "./?demo=1");
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.status.hidden, false);
  assert.match(ui.status.textContent, /Demo.*fiktiv/);
  const demoFeed = await render({data: {...healthy, source_status: "demo"}});
  assert.match(demoFeed.status.textContent, /fiktiv/);
});
