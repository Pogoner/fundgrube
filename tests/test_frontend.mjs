/** Dependency-free behavior tests for both static pages; not browser/layout QA. */
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {fileURLToPath} from "node:url";
import vm from "node:vm";
import test from "node:test";

const site = new URL("../site/", import.meta.url);
const pages = Object.fromEntries(["index", "info"].map(name => [name, {
  html: readFileSync(new URL(`${name}.html`, site), "utf8"),
  script: readFileSync(new URL(name === "index" ? "app.js" : "info.js", site), "utf8")
}]));
const demo = JSON.parse(readFileSync(new URL("data/demo.json", site), "utf8"));
const NOW = Date.parse("2030-01-02T12:00:00Z");
const hour = 3600000;
const iso = offset => new Date(NOW + offset).toISOString();
const flush = () => new Promise(resolve => setImmediate(resolve));
const decode = value => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const attributes = text => Object.fromEntries([...text.matchAll(/([\w-]+)="([^"]*)"/g)].map(entry => [entry[1], entry[2]]));

class Element {
  constructor(document, attrs = {}) {
    this.document = document;
    this.id = attrs.id || "";
    this.className = attrs.class || "";
    this.dataset = {};
    for (const [key, value] of Object.entries(attrs)) if (key.startsWith("data-")) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = decode(value);
    this.value = attrs.value || "";
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
      const attrs = attributes(match[2]);
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
  return {schema_version: 2, snapshot_id: "snapshot-a", generated_at: iso(0), last_successful_scan_at: iso(0), source_status: "live", blockers: [], location: demo.location, criteria: demo.criteria, ai_enabled: false, stats: {received: 0}, coverage: {}, deals: [], ...overrides};
}
function offer(overrides = {}) {
  return {...structuredClone(demo.deals[0]), id: "offer-a", last_seen_at: iso(0), ends_at: iso(4 * hour), title: "Eigenständiges Messgerät", description: "Gespeicherter Kurztext.", url: "https://www.ebay.de/itm/123456789012", detail_path: "api/items/offer-a.json", score_version: 2, scalp_score: 80, useability_score: 80, interest_score: 64, score: 64, assessment_basis: "heuristic", semantic_assessed: true, assessment_confidence: "low", ...overrides};
}
function aggregate(overrides = {}) {
  return offer({source_provider: "auktionen-ohne-gebot", verification_level: "aggregator", source_observed_at: iso(-60000), local_pickup_confirmed: false, city: "", postcode: "", distance_km: null, ...overrides});
}

function harness(data = fixture(), options = {}) {
  const page = options.page || "index";
  const nodes = new Map();
  const document = {activeElement: null, hidden: false, listeners: new Map(),
    querySelector(selector) { assert(nodes.has(selector.slice(1)), `${page} HTML element exists: ${selector}`); return nodes.get(selector.slice(1)); },
    addEventListener(type, callback) { this.listeners.set(type, callback); }
  };
  for (const match of pages[page].html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
    assert(!nodes.has(match[1]), `Unique ${page} HTML ID: ${match[1]}`);
    nodes.set(match[1], new Element(document, attributes(match[0])));
  }
  for (const match of pages[page].html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) nodes.get(match[1]).value = match[2].match(/<option\b[^>]*value="([^"]*)"/)?.[1] || "";
  const clock = {now: NOW};
  class TestDate extends Date {
    constructor(...args) { super(...(args.length ? args : [clock.now])); }
    static now() { return clock.now; }
  }
  const intervals = [];
  const requests = [];
  let copied = "";
  let fetcher = options.fetch || (async url => ({ok: true, status: 200, json: async () => structuredClone(String(url).endsWith("demo.json") ? demo : data)}));
  const context = {URL, URLSearchParams, Intl, Date: TestDate, Map, console, document,
    location: {href: `https://pogoner.github.io/fundgrube/${page === "info" ? "info.html" : ""}`, origin: "https://pogoner.github.io", search: options.demo ? "?demo=1" : ""},
    navigator: {clipboard: {writeText: async value => { if (options.clipboardFailure) throw Error("Clipboard denied"); copied = value; }}},
    fetch: async (url, init) => { requests.push({url: String(url), init}); return fetcher(url, init); },
    setInterval(callback, delay) { intervals.push({callback, delay}); return intervals.length; }
  };
  vm.runInNewContext(pages[page].script, context, {filename: fileURLToPath(new URL(page === "index" ? "app.js" : "info.js", site))});
  return {nodes, document, clock, requests, intervals,
    get copied() { return copied; },
    get count() { return Number(nodes.get("result-count").textContent.match(/^\((\d+)/)?.[1]); },
    get ids() { return nodes.get("cards").querySelectorAll(".detail-open").map(node => node.dataset.id); },
    setFetch(callback) { fetcher = callback; },
    async value(id, value) { nodes.get(id).value = String(value); await nodes.get(id).dispatch(["search", "minimum-interest"].includes(id) ? "input" : "change"); },
    async check(id, checked) { nodes.get(id).checked = checked; await nodes.get(id).dispatch("change"); },
    async click(id) { await nodes.get(id).dispatch("click"); },
    async detail(id) { const button = nodes.get("cards").querySelectorAll(".detail-open").find(node => node.dataset.id === id); assert(button, `Detail button exists for ${id}`); await nodes.get("cards").dispatch("click", {target: button}); },
    tick(milliseconds = 0) { clock.now += milliseconds; for (const interval of intervals) interval.callback(); }
  };
}

test("home is a compact radar; long documentation lives on a real linked Info page", () => {
  const home = pages.index.html;
  const info = pages.info.html;
  assert.match(home, /href="\.\/info\.html"/);
  assert.doesNotMatch(home, /class="hero|id="interface"|id="coverage-details"|id="copy-instructions"|id="tool-instructions"|class="footnote"/);
  assert(home.indexOf('id="minimum-interest"') < home.indexOf('id="cards"'));
  assert.match(info, /id="home-link"[^>]*href="\.\/"/);
  assert.match(info, /src="\.\/info\.js\?v=[^"]+"/);
  for (const path of ["manifest.json", "openapi.json", "guide.md"]) assert(info.includes(`href="./api/${path}"`));
  for (const anchor of ["0–9", "10–24", "25–44", "45–64", "65–79", "80–89", "90–100"]) assert(info.includes(anchor));
  assert.match(info, /Interest = ⌊\(Scalp × Useability \+ 50\) \/ 100⌋/);
  assert.match(info, /Useability ist nicht der Zustand/);
  assert.match(info, /defektes, aber eigenständig nutzbares Gerät/);
  assert.match(info, /Sensoren, Verdrahtung und eine konkrete Messaufgabe/);
  assert.match(info, /kein laufender MCP-Server/);
});

test("both pages use one shared asset version and reference existing local CSS and JS", () => {
  const versions = new Set();
  for (const [name, page] of Object.entries(pages)) {
    const assets = [...page.html.matchAll(/<(link|script)\b[^>]*>/g)].flatMap(match => {
      const attrs = attributes(match[0]);
      return match[1] === "script" && attrs.src ? [attrs.src]
        : match[1] === "link" && attrs.rel === "stylesheet" ? [attrs.href] : [];
    });
    assert.equal(assets.length, 2, `${name} has one stylesheet and one script`);
    const filenames = [];
    for (const asset of assets) {
      assert.match(asset, /^\.\/[a-z]+\.(css|js)\?v=[a-zA-Z0-9.-]+$/);
      const resolved = new URL(asset, site);
      versions.add(resolved.searchParams.get("v"));
      resolved.search = "";
      filenames.push(resolved.pathname.split("/").at(-1));
      assert(readFileSync(resolved, "utf8").length > 0, `${asset} exists and is not empty`);
    }
    assert.deepEqual(filenames.sort(), [name === "index" ? "app.js" : "info.js", "styles.css"]);
  }
  assert.equal(versions.size, 1, "CSS and both scripts share one deployment cache key");
});

test("cards without seller text show the semantic rationale before generic price reasons", async () => {
  const h = harness(fixture({deals: [offer({description: "", value_reason: "Vollständiges eigenständiges Laborgerät.", reasons: ["Aktueller Auktionspreis höchstens 1 Euro"]})]}));
  await flush();
  const preview = h.nodes.get("cards").innerHTML.match(/<p class="card-descr">([^<]*)<\/p>/)?.[1];
  assert.equal(preview, "Einordnung: Vollständiges eigenständiges Laborgerät.");
  const withText = harness(fixture({deals: [offer({description: "Tatsächlicher Verkäufertext.", value_reason: "Semantische Einordnung."})]}));
  await flush();
  assert.match(withText.nodes.get("cards").innerHTML, /<p class="card-descr">Tatsächlicher Verkäufertext\.<\/p>/);
});

test("loading, missing source, blockers and refresh failure remain visible on the lean home", async () => {
  let release; const waiting = new Promise(resolve => { release = resolve; });
  const data = fixture({source_status: "unconfigured", last_successful_scan_at: null, blockers: [{message: "<script>Not configured</script>", action: "Add credentials"}]});
  const h = harness(data, {fetch: async () => { await waiting; return {ok: true, json: async () => data}; }});
  assert.equal(h.nodes.get("cards").attributes.get("aria-busy"), "true");
  assert.equal(h.nodes.get("refresh-button").disabled, true);
  release(); await flush();
  assert.equal(h.count, 0); assert.equal(h.nodes.get("empty").hidden, false);
  assert.match(h.nodes.get("notice").innerHTML, /&lt;script&gt;Not configured/);
  assert.match(h.nodes.get("notice").innerHTML, /info\.html#source/);
  const ready = harness(fixture({deals: [offer()]})); await flush();
  const before = ready.nodes.get("cards").innerHTML;
  ready.setFetch(async () => { throw Error("Offline"); }); await ready.click("refresh-button");
  assert.equal(ready.nodes.get("cards").innerHTML, before);
  assert.match(ready.nodes.get("notice").innerHTML, /zuletzt geladene Snapshot bleibt sichtbar/);
  assert.match(ready.nodes.get("sync-status").className, /error/);
});

test("actual seven-item demo is fictitious and starts at Interest 40", async () => {
  assert.equal(demo.deals.length, 7);
  assert(!demo.deals.some(item => item.url || item.image || item.comparison_links.length));
  assert.equal(demo.deals[0].interest_score, 86);
  assert.equal(demo.deals[3].interest_score, 18);
  const h = harness(undefined, {demo: true}); await flush();
  assert.equal(h.nodes.get("minimum-interest").value, "40"); assert.equal(h.count, 3);
  assert.equal(h.nodes.get("result-count").textContent, "(3 / 7)");
  assert.deepEqual(h.ids, ["demo-001", "demo-002", "demo-003"]);
  assert.equal(h.nodes.get("metric-candidates").textContent, "4");
  assert.equal(h.nodes.get("metric-highvalue").textContent, "3");
  assert.equal(h.nodes.get("info-link").href, "./info.html?demo=1");
  assert.match(h.nodes.get("notice").innerHTML, /Keine echten Angebote/);
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /href="https:/);
  await h.click("show-all-interest"); assert.equal(h.count, 4);
  assert(h.ids.includes("demo-004"));
  await h.value("transport", "shipping"); assert.equal(h.count, 3);
  await h.value("transport", "pickup"); assert.equal(h.count, 2);
  await h.click("reset-filters"); assert.equal(h.count, 3);
  await h.value("search", "not-in-demo"); assert.equal(h.count, 0);
  assert.equal(h.nodes.get("empty-show-all-interest").hidden, false);
  await h.value("search", ""); await h.click("empty-show-all-interest"); assert.equal(h.count, 4);
  await h.check("show-expired", true); assert.equal(h.count, 5);
  await h.check("show-stale", true); assert.equal(h.count, 6);
  assert.match(h.nodes.get("result-summary").textContent, /ACHTUNG/);
});

test("Interest is half-up multiplication; all three sorts and minimum filtering work", async () => {
  const rows = [
    offer({id: "complete", scalp_score: 90, useability_score: 95, interest_score: 999, score: 999}),
    offer({id: "module", scalp_score: 90, useability_score: 20}),
    offer({id: "easy-small", scalp_score: 20, useability_score: 100}),
    offer({id: "half-up", scalp_score: 1, useability_score: 50}),
    offer({id: "legacy", score_version: 1, scalp_score: 99, useability_score: null, interest_score: 99, score: 99})
  ];
  const h = harness(fixture({deals: rows})); await flush();
  assert.deepEqual(h.ids, ["complete"]);
  assert.match(h.nodes.get("cards").innerHTML, /<small>INTEREST<\/small><strong>86<\/strong>/);
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /999/);
  await h.click("show-all-interest");
  assert.deepEqual(h.ids, ["complete", "easy-small", "module", "half-up", "legacy"]);
  assert.match(h.nodes.get("cards").innerHTML, /<small>INTEREST<\/small><strong>1<\/strong>/);
  await h.value("sort", "scalp"); assert.deepEqual(h.ids, ["legacy", "complete", "module", "easy-small", "half-up"]);
  await h.value("sort", "useability"); assert.deepEqual(h.ids, ["easy-small", "complete", "half-up", "module", "legacy"]);
  await h.value("minimum-interest", 19); assert.deepEqual(h.ids, ["easy-small", "complete"]);
  await h.value("minimum-interest", 87); assert.equal(h.count, 0);
  await h.click("empty-show-all-interest"); assert.equal(h.count, 5);
  assert.equal(h.nodes.get("minimum-interest-value").textContent, "0");
});

test("legacy or incomplete pairs never relabel an old score as new Interest; null is not zero", async () => {
  const rows = [
    offer({id: "v1", score_version: 1, scalp_score: 88, useability_score: null, interest_score: 88, score: 88}),
    offer({id: "missing-useability", scalp_score: 90, useability_score: null, interest_score: 90, score: 90}),
    offer({id: "invalid-component", scalp_score: 101, useability_score: 90}),
    offer({id: "v1-complete-looking", score_version: 1, scalp_score: 90, useability_score: 90, interest_score: 81}),
    offer({id: "legitimate-zero", scalp_score: 90, useability_score: 0, interest_score: 99})
  ];
  const h = harness(fixture({deals: rows})); await flush(); assert.equal(h.count, 0);
  await h.click("empty-show-all-interest"); assert.equal(h.count, 5);
  const markup = h.nodes.get("cards").innerHTML;
  assert.equal([...markup.matchAll(/<small>INTEREST<\/small><strong>—<\/strong>/g)].length, 4);
  assert.equal([...markup.matchAll(/<small>INTEREST<\/small><strong>0<\/strong>/g)].length, 1);
  assert.match(markup, /Scalp · Altwert/);
  await h.detail("missing-useability");
  assert.match(h.nodes.get("dialog-content").innerHTML, /Useability<\/small><strong>Noch offen/);
  assert.match(h.nodes.get("dialog-content").innerHTML, /alter Gesamtwert wird nicht als neuer Interest übernommen/);
});

test("legacy stray Useability is ignored and Scalp fallback matches the export contract", async () => {
  const h = harness(fixture({deals: [
    offer({id: "v1-stray", score_version: 1, scalp_score: null, interest_score: 62, score: 61, useability_score: 99, detail_path: null}),
    offer({id: "unversioned", score_version: undefined, scalp_score: undefined, interest_score: 21, score: 20, useability_score: 100, detail_path: null}),
    offer({id: "current", scalp_score: 80, useability_score: 1, detail_path: null})
  ]}));
  await flush(); await h.click("show-all-interest");
  await h.value("sort", "useability");
  assert.deepEqual(h.ids, ["current", "unversioned", "v1-stray"]);
  await h.detail("v1-stray");
  assert.match(h.nodes.get("dialog-content").innerHTML, /Scalp · Altwert<\/small><strong>62/);
  assert.match(h.nodes.get("dialog-content").innerHTML, /Useability<\/small><strong>Noch offen/);
  await h.detail("unversioned");
  assert.match(h.nodes.get("dialog-content").innerHTML, /Scalp · Altwert<\/small><strong>21/);
  assert.match(h.nodes.get("dialog-content").innerHTML, /Useability<\/small><strong>Noch offen/);
});

test("defect remains a separate risk and does not silently lower standalone Useability", async () => {
  const h = harness(fixture({deals: [offer({scalp_score: 90, useability_score: 95, condition: "Defekt", risks: ["Reparatur offen"], value_reason: "Hochwertiges vollständiges Gerät", useability_reason: "Eigenständig, ohne Anlagenintegration", dependencies: ["Standard-Stromversorgung"], detail_path: null})]})); await flush();
  assert.match(h.nodes.get("cards").innerHTML, /<small>INTEREST<\/small><strong>86<\/strong>/);
  assert.match(h.nodes.get("cards").innerHTML, /Useability<\/span><strong>95<\/strong>/);
  assert.match(h.nodes.get("cards").innerHTML, /Defekt|Reparatur offen/);
  await h.detail("offer-a");
  assert.match(h.nodes.get("dialog-content").innerHTML, /Eigenständig, ohne Anlagenintegration/);
  assert.match(h.nodes.get("dialog-content").innerHTML, /Standard-Stromversorgung/);
  assert.match(h.nodes.get("dialog-content").innerHTML, /Useability bestätigt keine Funktionsfähigkeit/);
});

test("unknown facts, seller HTML and unsafe links remain safe", async () => {
  const h = harness(fixture({deals: [offer({title: '<img src=x onerror="alert(1)">', description: "<script>bad()</script>", bid_count: null, shipping_eur: null, reserve_status: "unknown", url: "javascript:alert(1)", image: "https://evil.example/image.jpg"})]})); await flush();
  const markup = h.nodes.get("cards").innerHTML;
  assert.match(markup, /GEBOTE UNBEKANNT/); assert.doesNotMatch(markup, /· 0 GEBOTE/);
  assert.match(markup, /Versand ungeklärt/); assert.match(markup, /Mindestpreis ungeklärt/);
  assert.match(markup, /&lt;img|&lt;script&gt;/);
  assert.doesNotMatch(markup, /<script|onerror="|href="javascript|src="https:\/\/evil/);
});

test("same-snapshot details cannot replace prices, scores or dependencies", async () => {
  const item = offer({dependencies: ["Original dependency"], comparison_links: [{url: "https://www.ebay.de/itm/123", title: "Vergleich"}, {url: "javascript:alert(1)", title: "Unsafe"}]});
  const h = harness(fixture({deals: [item]})); await flush();
  h.setFetch(async () => ({ok: true, json: async () => ({snapshot_id: "snapshot-a", item: {...item, description: "Volltext <script>unsafe()</script>", price_eur: 999, scalp_score: 0, useability_score: 0, dependencies: ["Injected dependency"]}})}));
  await h.detail(item.id);
  const markup = h.nodes.get("dialog-content").innerHTML;
  assert.match(markup, /Volltext &lt;script&gt;/); assert.match(markup, /1,00/);
  assert.match(markup, /Interest<\/small><strong>64 \/ 100/);
  assert.match(markup, /Original dependency/);
  assert.doesNotMatch(markup, /999,00|Injected dependency|href="javascript/);
});

test("mixed snapshot IDs and wrong item IDs cannot leak new description text", async () => {
  for (const [snapshot, id, expected] of [["snapshot-b", "offer-a", /Snapshot neu laden/], [null, "offer-a", /Snapshot neu laden/], ["snapshot-a", "wrong", /Detail-Feed nicht verfügbar/]]) {
    const h = harness(fixture({deals: [offer()]})); await flush();
    h.setFetch(async () => ({ok: true, json: async () => ({snapshot_id: snapshot, item: offer({id, description: "MIXED_TEXT"})})}));
    await h.detail("offer-a");
    assert.match(h.nodes.get("dialog-content").innerHTML, expected);
    assert.match(h.nodes.get("dialog-content").innerHTML, /Gespeicherter Kurztext/);
    assert.doesNotMatch(h.nodes.get("dialog-content").innerHTML, /MIXED_TEXT/);
  }
});

test("24-hour buffer retains 7/23-hour observations, marks age, rejects exact boundary and future", async () => {
  const rows = [aggregate({id: "seven", source_observed_at: iso(-7 * hour)}), aggregate({id: "twenty-three", source_observed_at: iso(-23 * hour)}), aggregate({id: "at-24", source_observed_at: iso(-24 * hour)}), aggregate({id: "unknown", source_observed_at: null}), aggregate({id: "future", source_observed_at: iso(1)})];
  const h = harness(fixture({provider: "auktionen-ohne-gebot", deals: rows})); await flush();
  assert.equal(h.count, 2);
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 7 Std\. · ältere Beobachtung, nicht aktuell bestätigt/);
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 23 Std\. · ältere Beobachtung, nicht aktuell bestätigt/);
  assert.equal(h.nodes.get("cards").querySelectorAll("[data-last-seen]").filter(node => node.classList.contains("freshness-warning")).length, 2);
  assert.match(h.nodes.get("retention-note").textContent, /24-Stunden-Puffer/);
  h.tick(hour);
  assert(!h.ids.includes("twenty-three"));
  h.setFetch(async () => ({ok: true, json: async () => fixture({provider: "auktionen-ohne-gebot", generated_at: iso(hour), deals: rows.map(item => ({...item, last_seen_at: iso(hour)}))})}));
  await h.click("refresh-button");
  assert(!h.ids.includes("twenty-three"));
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 8 Std\./);
});

test("smaller configured retention is honored; no configured value extends beyond 24 hours", async () => {
  for (const [criteria, metadata, expected] of [[8, undefined, 8], [24, 12, 12], [6, 4, 4], [99, 99, 24], [0, -1, 24]]) {
    const h = harness(fixture({criteria: {...demo.criteria, max_freshness_hours: criteria}, freshness: {maximum_source_age_hours: metadata}, deals: [offer({id: "inside", last_seen_at: iso(-(expected - 1) * hour)}), offer({id: "limit", last_seen_at: iso(-expected * hour)})]})); await flush();
    assert.deepEqual(h.ids, ["inside"]);
    assert(h.nodes.get("show-stale-label").textContent.includes(`${expected}-Stunden-Puffer`));
    assert(h.nodes.get("retention-note").textContent.includes(`${expected}-Stunden-Puffer`));
  }
});

test("timer enforces ends_at, valid_until and source age without another request", async () => {
  const h = harness(fixture({deals: [offer({id: "validity", valid_until: iso(60000)}), offer({id: "ending", ends_at: iso(60000)}), offer({id: "aging", last_seen_at: iso(-24 * hour + 60000)}), offer({id: "remaining"}), offer({id: "bad-validity", valid_until: null})]})); await flush();
  assert.equal(h.count, 4); assert.deepEqual(h.intervals.map(item => item.delay), [60000]);
  h.tick(30000); assert.equal(h.count, 4);
  h.tick(30000); assert.deepEqual(h.ids, ["remaining"]);
  assert.equal(h.requests.length, 1);
  h.tick(120000);
  assert(h.nodes.get("cards").querySelectorAll("[data-last-seen]").some(node => node.textContent === "Quellenstand: vor 3 Min."));
  await h.check("show-expired", true); assert.equal(h.count, 3);
  assert.match(h.nodes.get("cards").innerHTML, /Pufferfrist abgelaufen|Auktion abgelaufen/);
  await h.check("show-stale", true); assert.equal(h.count, 5);
  assert.match(h.nodes.get("cards").innerHTML, /Pufferfrist ungeklärt/);
});

test("aggregator pickup claims are provisional, scoped and independently aged", async () => {
  const pickup = overrides => aggregate({pickup_available: true, pickup_search_confirmed: true, pickup_search_postcode: "69121", pickup_search_radius_km: 50, shipping_eur: null, eligibility: "review", ...overrides});
  const h = harness(fixture({provider: "auktionen-ohne-gebot", deals: [pickup({id: "matching"}), pickup({id: "smaller", pickup_search_radius_km: 25}), pickup({id: "wrong-postcode", pickup_search_postcode: "69115"}), pickup({id: "wide", pickup_search_radius_km: 51}), pickup({id: "only-available", pickup_search_confirmed: false}), pickup({id: "old", source_observed_at: iso(-25 * hour)}), pickup({id: "unknown", source_observed_at: null})]})); await flush();
  await h.value("transport", "pickup"); assert.deepEqual(h.ids, ["matching", "smaller"]);
  const markup = h.nodes.get("cards").innerHTML;
  assert.match(markup, /Abholsuche · 50 km/); assert.match(markup, /Abholsuche · 25 km/);
  assert.match(markup, /Nähe laut Quelle; Standort im Original prüfen/);
  assert.doesNotMatch(markup, /Abholung ca\.|Abholung im 50-km/);
  await h.check("show-stale", true); assert.equal(h.count, 2);
  await h.value("transport", "shipping"); assert.equal(h.count, 0);
  const aging = harness(fixture({provider: "auktionen-ohne-gebot", deals: [pickup({source_observed_at: iso(-24 * hour + 60000)})]})); await flush();
  await aging.check("show-stale", true); assert.match(aging.nodes.get("cards").innerHTML, /Abholsuche/);
  aging.tick(120000); assert.doesNotMatch(aging.nodes.get("cards").innerHTML, /Abholsuche · 50 km/);
  assert.match(aging.nodes.get("cards").innerHTML, /Abholung möglich · Nähe ungeklärt/);
});

test("ChatGPT, API LLM and heuristic bases remain separate on cards and details", async () => {
  const h = harness(fixture({interactive_review: {status: "applied", reviewer: "ChatGPT", applied: 1, unreviewed: 2, reviewed_at: iso(-hour)}, deals: [offer({id: "chatgpt", assessment_basis: "llm", semantic_review_mode: "interactive", semantic_assessed_at: iso(-hour), detail_path: null}), offer({id: "heuristic", semantic_review_mode: "interactive"}), offer({id: "api", assessment_basis: "llm", semantic_review_mode: "api"})]})); await flush();
  const cards = h.nodes.get("cards").innerHTML;
  assert.match(cards, /ChatGPT · Konfidenz niedrig/); assert.match(cards, /Heuristik · Konfidenz niedrig/); assert.match(cards, /LLM · Konfidenz niedrig/);
  assert.match(h.nodes.get("metric-source").textContent, /ChatGPT-Sichtung: 1 Angebot/);
  await h.detail("chatgpt"); assert.match(h.nodes.get("dialog-content").innerHTML, /ChatGPT-Sichtung \(Berlin\)/);
});

test("Info fetches relative data, preserves provenance, explains bounded coverage and current review status", async () => {
  const data = fixture({provider: "auktionen-ohne-gebot", stats: {received: 80}, interactive_review: {status: "applied", reviewer: "ChatGPT", applied: 2, available: 2, unreviewed: 1, reviewed_at: iso(-hour)}, coverage: {source_total_reported: 1200, pages_fetched: 2, requests_this_run: 3, page_size: 48, request_cap: 4, source_cache_max_age_minutes: 15, complete: false, categories_total: 999, cycle_estimate_hours: 4321}, deals: [aggregate()]});
  const h = harness(data, {page: "info"}); await flush();
  assert.equal(h.requests[0].url, "./data/deals.json");
  assert.equal(h.nodes.get("home-link").href, "./");
  assert.match(h.nodes.get("info-source").innerHTML, /https:\/\/auktionen-ohne-gebot\.de\/business-industrie\//);
  assert.match(h.nodes.get("info-source").innerHTML, /nicht direkt bei eBay verifiziert/);
  assert.match(h.nodes.get("info-coverage").innerHTML, /80 \/ 1200|Quellseiten|Quellabrufe|bis 15 Min\./);
  assert.doesNotMatch(h.nodes.get("info-coverage").innerHTML, /4321|Unterkategorien/);
  assert.match(h.nodes.get("info-review").innerHTML, /ChatGPT-Sichtung: 2 Angebote/);
  assert.match(h.nodes.get("info-review").innerHTML, /Automatische LLM-Aufrufe aus/);
  assert.match(h.nodes.get("info-retention").textContent, /24-Stunden-Puffer.*keine erneute Prüfung/);
  h.setFetch(async () => { throw Error("Offline"); }); await h.click("info-refresh");
  assert.match(h.nodes.get("info-status").innerHTML, /Abruf fehlgeschlagen/);
  assert.match(h.nodes.get("info-status").innerHTML, /zuletzt geladene Snapshot bleibt unverändert sichtbar/);
  assert.match(h.nodes.get("info-source").innerHTML, /Auktionen ohne Gebot/);
});

test("Info copy handoff is pinned, dual-score aware, read-only; demo/back navigation and fallback work", async () => {
  const h = harness(fixture({criteria: {...demo.criteria, max_freshness_hours: 12}}), {page: "info"}); await flush(); await h.click("copy-instructions");
  for (const phrase of ["demselben Commit-SHA", "alle paginierten Angebotsseiten", "counts.total", "12-Stunden-Puffer", "ends_at und valid_until", "floor((Scalp * Useability + 50) / 100)", "Useability", "Keine Gebote", "ausdrücklichen Speicherauftrag"]) assert(h.copied.includes(phrase), phrase);
  assert.match(h.copied, /Fehlende Useability bleibt null/);
  const fallback = harness(undefined, {page: "info", demo: true, clipboardFailure: true}); await flush();
  assert.equal(fallback.requests[0].url, "./data/demo.json");
  assert.equal(fallback.nodes.get("home-link").href, "./?demo=1");
  assert.match(fallback.nodes.get("info-status").innerHTML, /fiktiv|Fiktive/);
  await fallback.click("copy-instructions");
  assert.match(fallback.nodes.get("tool-instructions").textContent, /Scalp und Useability getrennt/);
  assert.match(fallback.nodes.get("copy-status").textContent, /manuell/);
});

test("Info exposes full blocker actions safely and never treats expired review counts as applied", async () => {
  for (const [status, label] of [["none", "noch keine"], ["expired", "abgelaufen"], ["invalid", "ungültig"]]) {
    const h = harness(fixture({source_status: "error", blockers: [{message: "<script>Blocked</script>", action: '<img src=x onerror="bad()">'}], interactive_review: {status, reviewer: "ChatGPT", applied: 0, available: 408, unreviewed: 1}}), {page: "info"}); await flush();
    assert.match(h.nodes.get("info-status").innerHTML, /&lt;script&gt;Blocked/);
    assert.match(h.nodes.get("info-status").innerHTML, /&lt;img/);
    assert.match(h.nodes.get("info-status").className, /error/);
    assert(h.nodes.get("info-review").innerHTML.includes(`ChatGPT-Sichtung: ${label}`));
    assert.doesNotMatch(h.nodes.get("info-review").innerHTML, /ChatGPT-Sichtung: 408/);
  }
});
