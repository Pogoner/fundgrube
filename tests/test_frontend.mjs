/** Dependency-free behavioral smoke tests. This is a fake DOM, not browser/layout QA. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const site = new URL("../site/", import.meta.url);
const app = readFileSync(new URL("app.js", site), "utf8");
const html = readFileSync(new URL("index.html", site), "utf8");
const demo = JSON.parse(readFileSync(new URL("data/demo.json", site), "utf8"));
const NOW = Date.parse("2030-01-02T12:00:00Z");
const iso = offset => new Date(NOW + offset).toISOString();
const hour = 3600000;
const flush = () => new Promise(resolve => setImmediate(resolve));
const decode = value => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

class Element {
  constructor(document, attributes = {}) {
    this.document = document;
    this.id = attributes.id || "";
    this.className = attributes.class || "";
    this.dataset = {};
    for (const [key, value] of Object.entries(attributes)) {
      if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = decode(value);
    }
    this.value = "";
    this.textContent = "";
    this.checked = this.hidden = this.disabled = this.open = false;
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = {
      contains: value => this.className.split(/\s+/).includes(value),
      toggle: (value, enabled) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        if (enabled ?? !names.has(value)) names.add(value); else names.delete(value);
        this.className = [...names].join(" ");
      }
    };
    this.innerHTML = "";
  }
  set innerHTML(value) { this.markup = value; this.childQueries = new Map(); }
  get innerHTML() { return this.markup; }
  setAttribute(key, value) { this.attributes.set(key, value); }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  async dispatch(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) await callback({target: this, ...event});
    await flush();
  }
  showModal() { this.open = true; }
  close() { this.open = false; for (const callback of this.listeners.get("close") || []) callback({target: this}); }
  focus() { this.document.activeElement = this; }
  closest(selector) { return selector.startsWith(".") && this.classList.contains(selector.slice(1)) ? this : null; }
  contains(element) { return element === this || element.parent === this; }
  querySelectorAll(selector) {
    if (this.childQueries.has(selector)) return this.childQueries.get(selector);
    const attribute = selector === "[data-ends-at]" ? "data-ends-at" : selector === "[data-last-seen]" ? "data-last-seen" : null;
    assert(attribute || selector === ".detail-open", `Unsupported test selector: ${selector}`);
    const matches = [];
    for (const match of this.markup.matchAll(/<(span|button)\b([^>]*)>([^<]*)<\/(?:span|button)>/g)) {
      const attrs = Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(entry => [entry[1], entry[2]]));
      if (attribute ? !(attribute in attrs) : !attrs.class?.split(/\s+/).includes("detail-open")) continue;
      const node = new Element(this.document, attrs);
      node.textContent = decode(match[3]);
      node.parent = this;
      matches.push(node);
    }
    this.childQueries.set(selector, matches);
    return matches;
  }
}

function fixture(overrides = {}) {
  return {
    schema_version: 2, snapshot_id: "snapshot-a", generated_at: iso(0),
    last_successful_scan_at: iso(0), source_status: "live", blockers: [],
    location: demo.location, criteria: demo.criteria, ai_enabled: false,
    stats: {received: 0}, coverage: {}, deals: [], ...overrides
  };
}

function offer(overrides = {}) {
  return {
    ...structuredClone(demo.deals[0]), id: "offer-a", last_seen_at: iso(0),
    ends_at: iso(hour), title: "Messgerät", description: "Gespeicherter Kurztext.",
    url: "https://www.ebay.de/itm/123456789012", detail_path: "api/items/offer-a.json",
    ...overrides
  };
}

function harness(data = fixture(), options = {}) {
  const nodes = new Map();
  const document = {
    activeElement: null, hidden: false, listeners: new Map(),
    querySelector(selector) {
      assert(nodes.has(selector.slice(1)), `HTML element exists: ${selector}`);
      return nodes.get(selector.slice(1));
    },
    addEventListener(type, callback) { this.listeners.set(type, callback); }
  };
  for (const match of html.matchAll(/id="([^"]+)"/g)) {
    assert(!nodes.has(match[1]), `Unique HTML ID: ${match[1]}`);
    nodes.set(match[1], new Element(document, {id: match[1]}));
  }
  for (const match of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    nodes.get(match[1]).value = match[2].match(/<option\b[^>]*value="([^"]*)"/)?.[1] || "";
  }
  const clock = {now: NOW};
  class TestDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  const intervals = [];
  const requests = [];
  let copied = "";
  let fetcher = options.fetch || (async url => ({ok: true, status: 200, json: async () => structuredClone(String(url).endsWith("demo.json") ? demo : data)}));
  const context = {
    URL, URLSearchParams, Intl, Date: TestDate, Map, console, document,
    location: {href: "https://pogoner.github.io/fundgrube/", origin: "https://pogoner.github.io", search: options.demo ? "?demo=1" : ""},
    navigator: {clipboard: {writeText: async value => { copied = value; }}},
    fetch: async (url, init) => { requests.push({url: String(url), init}); return fetcher(url, init); },
    setInterval(callback, delay) { intervals.push({callback, delay}); return intervals.length; }
  };
  vm.runInNewContext(app, context, {filename: fileURLToPath(new URL("app.js", site))});
  return {
    nodes, document, clock, requests, intervals,
    get copied() { return copied; },
    setFetch(callback) { fetcher = callback; },
    async value(id, value) { nodes.get(id).value = value; await nodes.get(id).dispatch(id === "search" ? "input" : "change"); },
    async check(id, checked) { nodes.get(id).checked = checked; await nodes.get(id).dispatch("change"); },
    async click(id) { await nodes.get(id).dispatch("click"); },
    async detail(id) {
      const button = nodes.get("cards").querySelectorAll(".detail-open").find(node => node.dataset.id === id);
      assert(button, `Detail button exists for ${id}`);
      await nodes.get("cards").dispatch("click", {target: button});
    },
    tick(milliseconds = 0) { clock.now += milliseconds; for (const interval of intervals) interval.callback(); }
  };
}

test("loading and unconfigured empty state remain explicit", async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const empty = fixture({source_status: "unconfigured", last_successful_scan_at: null, blockers: [{message: "<script>Not configured</script>", action: "Add credentials"}]});
  const h = harness(empty, {fetch: async () => { await waiting; return {ok: true, json: async () => empty}; }});
  assert.equal(h.nodes.get("cards").attributes.get("aria-busy"), "true");
  assert.equal(h.nodes.get("refresh-button").disabled, true);
  assert.equal(h.nodes.get("sync-label").textContent, "Lade Snapshot");
  release(); await flush();
  assert.equal(h.nodes.get("cards").attributes.get("aria-busy"), "false");
  assert.equal(h.nodes.get("result-count").textContent, "(0)");
  assert.equal(h.nodes.get("empty").hidden, false);
  assert.match(h.nodes.get("notice").innerHTML, /&lt;script&gt;Not configured/);
  assert.match(h.nodes.get("sync-label").textContent, /Datenquelle offen/);
});

test("actual seven-item demo, semantic classes and interactive filters", async () => {
  assert.equal(demo.deals.length, 7);
  assert.equal(demo.stats.received, 7);
  assert(!demo.deals.some(item => item.url || item.image || item.comparison_links.length));
  const h = harness(undefined, {demo: true}); await flush();
  const count = expected => assert.equal(h.nodes.get("result-count").textContent, `(${expected})`);
  count(4);
  assert.equal(h.nodes.get("metric-candidates").textContent, "2");
  assert.equal(h.nodes.get("metric-highvalue").textContent, "2");
  assert.match(h.nodes.get("notice").innerHTML, /Keine echten Angebote/);
  assert.match(h.nodes.get("cards").innerHTML, /Eher 500 €/);
  assert.match(h.nodes.get("cards").innerHTML, /Eher 50 €/);
  assert.match(h.nodes.get("cards").innerHTML, /Heuristik · Konfidenz niedrig/);
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /href="https:|demo-006|demo-007/);
  await h.value("value-filter", "plausible"); count(2);
  await h.value("value-filter", "unknown"); count(1);
  await h.value("value-filter", "unlikely"); count(1);
  await h.value("eligibility", "excluded"); count(1);
  assert.match(h.nodes.get("cards").innerHTML, /Eher 5 €/);
  await h.click("reset-filters"); await h.value("transport", "pickup"); count(2);
  assert.match(h.nodes.get("cards").innerHTML, /Abholung im 50-km-Suchradius/);
  await h.value("transport", "shipping"); count(3);
  await h.click("reset-filters"); await h.value("search", "Steuergerät"); count(1);
  await h.click("reset-filters"); await h.check("show-expired", true); count(5);
  await h.check("show-stale", true); count(6);
  assert.match(h.nodes.get("result-summary").textContent, /ACHTUNG/);
  await h.click("reset-filters"); count(4);
  await h.click("copy-instructions");
  assert.match(h.copied, /Commit-SHA/);
  assert.match(h.copied, /demselben Commit-SHA/);
  assert.match(h.copied, /Interesse 0–100/);
  assert.match(h.copied, /Keine Preisempfehlung/);
});

test("unknown facts stay unknown; hostile markup and URLs cannot become active content", async () => {
  const item = offer({title: '<img src=x onerror="alert(1)">', description: "<script>bad()</script>", bid_count: null, shipping_eur: null, reserve_status: "unknown", url: "javascript:alert(1)", image: "https://evil.example/image.jpg", interest_score: 41, score: 99, value_band: "unknown", market_value_low_eur: 9999, market_value_high_eur: 29999, valuation_status: "verified", value_over_100_plausible: true});
  const h = harness(fixture({deals: [item]})); await flush();
  const markup = h.nodes.get("cards").innerHTML;
  assert.match(markup, /GEBOTE UNBEKANNT/);
  assert.doesNotMatch(markup, /· 0 GEBOTE/);
  assert.match(markup, /Versand ungeklärt/);
  assert.match(markup, /Mindestpreis ungeklärt/);
  assert.match(markup, /&lt;img/);
  assert.match(markup, /&lt;script&gt;/);
  assert.doesNotMatch(markup, /<script|onerror="|href="javascript|src="https:\/\/evil/);
  assert.match(markup, /Interesse 41\/100/);
  assert.match(markup, />Unklar</);
  assert.doesNotMatch(markup, /9\.999|29\.999|≥ 100/);
});

test("same-snapshot detail text is escaped and never overwrites prices or interest", async () => {
  const item = offer({assessment_basis: "heuristic", semantic_assessed: true, comparison_links: [{url: "https://www.ebay.de/itm/123456789013", title: "Vergleich"}, {url: "javascript:alert(1)", title: "Unsafe"}]});
  const feed = fixture({deals: [item], coverage: {cycle_estimate_hours: 18, cycle_estimate_is_lower_bound: true}});
  const h = harness(feed); await flush();
  h.setFetch(async () => ({ok: true, json: async () => ({snapshot_id: "snapshot-a", item: {...item, description: "Vollständiger Text.\n<script>unsafe()</script>", price_eur: 999, interest_score: 0}})}));
  await h.detail(item.id);
  assert.equal(h.nodes.get("detail-dialog").open, true);
  const markup = h.nodes.get("dialog-content").innerHTML;
  assert.match(markup, /Vollständiger Text/);
  assert.match(markup, /&lt;script&gt;unsafe/);
  assert.match(markup, /1,00/);
  assert.doesNotMatch(markup, /999,00|href="javascript/);
  assert.match(markup, /93 \/ 100/);
  assert.match(markup, /Heuristik/);
  assert.match(markup, /Grobe Größenordnung, keine Wertermittlung/);
  assert.match(markup, /weder einen Marktpreis noch Zustand, Vollständigkeit oder Funktionsfähigkeit/);
  assert.match(h.nodes.get("coverage-content").innerHTML, /Mindestens 18 Std./);
  assert.match(h.nodes.get("coverage-content").innerHTML, /vollständige Abdeckung nicht garantiert/);
});

test("mixed snapshots and wrong item IDs are rejected without losing the summary", async () => {
  for (const [snapshot, id, expected] of [["snapshot-b", "offer-a", /Snapshot neu laden/], [null, "offer-a", /Snapshot neu laden/], ["snapshot-a", "wrong-item", /Detail-Feed nicht verfügbar/]]) {
    const h = harness(fixture({deals: [offer()]})); await flush();
    h.setFetch(async () => ({ok: true, json: async () => ({snapshot_id: snapshot, item: offer({id, description: "MIXED_DESCRIPTION_MUST_NOT_APPEAR"})})}));
    await h.detail("offer-a");
    const markup = h.nodes.get("dialog-content").innerHTML;
    assert.match(markup, expected);
    assert.match(markup, /Gespeicherter Kurztext/);
    assert.doesNotMatch(markup, /MIXED_DESCRIPTION_MUST_NOT_APPEAR/);
  }
});

test("six-hour cutoff includes missing/future timestamps; minute timer updates and expires data", async () => {
  const h = harness(fixture({deals: [
    offer({id: "fresh", ends_at: iso(4 * hour)}),
    offer({id: "aging", last_seen_at: iso(-6 * hour + 60000), ends_at: iso(4 * hour)}),
    offer({id: "ending", ends_at: iso(60000)}),
    offer({id: "old", last_seen_at: iso(-7 * hour)}),
    offer({id: "undated", last_seen_at: null}),
    offer({id: "future", last_seen_at: iso(hour)})
  ]})); await flush();
  assert.equal(h.nodes.get("result-count").textContent, "(3)");
  assert.deepEqual(h.intervals.map(interval => interval.delay), [60000]);
  h.tick(30000);
  assert(h.nodes.get("cards").querySelectorAll("[data-ends-at]").some(node => node.textContent === "Noch 1 Min."));
  h.tick(90000);
  assert.equal(h.nodes.get("result-count").textContent, "(1)");
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /data-id="aging"|data-id="ending"/);
  h.tick(60000);
  assert(h.nodes.get("cards").querySelectorAll("[data-last-seen]").some(node => node.textContent === "Stand: vor 3 Min."));
  await h.check("show-stale", true);
  assert.equal(h.nodes.get("result-count").textContent, "(5)");
  assert.match(h.nodes.get("cards").innerHTML, /Datenalter unbekannt/);
  await h.check("show-expired", true);
  assert.equal(h.nodes.get("result-count").textContent, "(6)");
});

test("failed refresh retains prior data and honestly reports the error", async () => {
  const h = harness(fixture({deals: [offer()]})); await flush();
  const previous = h.nodes.get("cards").innerHTML;
  h.setFetch(async () => { throw new Error("Offline"); });
  await h.click("refresh-button");
  assert.equal(h.nodes.get("cards").innerHTML, previous);
  assert.match(h.nodes.get("notice").innerHTML, /zuletzt geladene Snapshot bleibt sichtbar/);
  assert.match(h.nodes.get("sync-status").className, /error/);
  assert.equal(h.nodes.get("refresh-button").disabled, false);
});
