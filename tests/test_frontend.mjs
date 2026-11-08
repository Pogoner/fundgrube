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
const styles = readFileSync(new URL("styles.css", site), "utf8");
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
    this.attributes = new Map(Object.entries(attrs));
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
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    if (this.childQueries.has(selector)) return this.childQueries.get(selector);
    if (selector === ".offer-image") {
      const images = [];
      for (const match of this.markup.matchAll(/<div class="(card-visual[^"]*)">([\s\S]*?)<\/div>/g)) {
        const imageTag = match[2].match(/<img\b[^>]*>/)?.[0];
        if (!imageTag) continue;
        const frame = new Element(this.document, {class: match[1]});
        frame.innerHTML = match[2];
        const image = new Element(this.document, attributes(imageTag));
        image.parentElement = frame;
        image.complete = this.document.imageDefaults?.complete || false;
        image.naturalWidth = this.document.imageDefaults?.naturalWidth || 0;
        images.push(image);
      }
      this.childQueries.set(selector, images);
      return images;
    }
    if ([".image-fallback", ".image-state", ".image-source-link", ".image-help-link"].includes(selector)) {
      const matches = [];
      for (const match of this.markup.matchAll(/<([a-z]+)\b([^>]*)>([^<]*)/g)) {
        const attrs = attributes(match[2]);
        if (!attrs.class?.split(/\s+/).includes(selector.slice(1))) continue;
        const node = new Element(this.document, attrs);
        node.hidden = /\bhidden\b/.test(match[2]);
        node.textContent = decode(match[3]);
        matches.push(node);
      }
      this.childQueries.set(selector, matches);
      return matches;
    }
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
  return {schema_version: 3, snapshot_id: "snapshot-a", generated_at: iso(0), last_successful_scan_at: iso(0), source_status: "live", blockers: [], location: demo.location, criteria: {...demo.criteria, lookahead_hours: 72}, freshness: {retention_basis: "auction_end", maximum_source_age_hours: null, active_window_hours: 72, source_warning_after_hours: 2}, ai_enabled: false, stats: {received: 0}, coverage: {}, deals: [], ...overrides};
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
  const document = {activeElement: null, hidden: false, listeners: new Map(), imageDefaults: options.imageDefaults,
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
    browseIds(axis) { return nodes.get(`browse-${axis}-list`).querySelectorAll(".detail-open").map(node => node.dataset.id); },
    setFetch(callback) { fetcher = callback; },
    async value(id, value) { nodes.get(id).value = String(value); await nodes.get(id).dispatch(["search", "minimum-interest"].includes(id) ? "input" : "change"); },
    async check(id, checked) { nodes.get(id).checked = checked; await nodes.get(id).dispatch("change"); },
    async click(id) { await nodes.get(id).dispatch("click"); },
    async detail(id, listId = "cards") { const button = nodes.get(listId).querySelectorAll(".detail-open").find(node => node.dataset.id === id); assert(button, `Detail button exists for ${id} in ${listId}`); button.focus(); await nodes.get(listId).dispatch("click", {target: button}); },
    tick(milliseconds = 0) { clock.now += milliseconds; for (const interval of intervals) interval.callback(); }
  };
}

test("home is a compact radar; long documentation lives on a real linked Info page", () => {
  const home = pages.index.html;
  const info = pages.info.html;
  assert.match(home, /href="\.\/info\.html"/);
  assert(home.indexOf('id="cards"') < home.indexOf('id="browse"'));
  assert.match(home, /href="#browse-heading"/);
  assert.match(home, /Top 10 Scalp/);
  assert.match(home, /Top 10 Useability/);
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
  assert(versions.has("design-v3.6"), "new design cannot reuse an older asset cache key");
});

test("cards without seller text show the semantic rationale before generic price reasons", async () => {
  const h = harness(fixture({deals: [offer({description: "", value_reason: "Vollständiges eigenständiges Laborgerät.", reasons: ["Aktueller Auktionspreis höchstens 1 Euro"]})]}));
  await flush();
  assert.match(h.nodes.get("cards").innerHTML, /<span class="reason-label">Einordnung<\/span>Vollständiges eigenständiges Laborgerät\./);
  const withText = harness(fixture({deals: [offer({description: "Tatsächlicher Verkäufertext.", value_reason: "Semantische Einordnung.", detail_path: null})]}));
  await flush();
  assert.match(withText.nodes.get("cards").innerHTML, /<span class="reason-label">Einordnung<\/span>Semantische Einordnung\./);
  await withText.detail("offer-a");
  assert.match(withText.nodes.get("dialog-content").innerHTML, /Tatsächlicher Verkäufertext\./);
});

test("image loading is early for visible cards, dimensioned and has a safe recoverable error state", async () => {
  const image = "https://i.ebayimg.com/images/g/test/s-l1600.jpg";
  const h = harness(fixture({deals: Array.from({length: 5}, (_, index) => offer({id: `image-${index}`, image, title: `Gerät ${index} <img onerror="injected">`}))}));
  await flush();
  const images = h.nodes.get("cards").querySelectorAll(".offer-image");
  assert.equal(images.length, 5);
  assert.deepEqual(images.map(node => node.attributes.get("loading")), ["eager", "eager", "eager", "lazy", "lazy"]);
  assert.equal(images[0].attributes.get("fetchpriority"), "high");
  for (const node of images) {
    assert.equal(node.attributes.get("width"), "720");
    assert.equal(node.attributes.get("height"), "450");
    assert.equal(node.attributes.get("src"), image);
  }
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /onerror="injected"|onload="/);
  const first = images[0];
  assert.equal(first.complete, false);
  assert.equal(first.naturalWidth, 0);
  assert(first.parentElement.classList.contains("image-loading"), "a pending image is not a failed image");
  const help = first.parentElement.querySelector(".image-help-link");
  assert.equal(help.hidden, true);
  await first.dispatch("error");
  assert(first.parentElement.classList.contains("image-failed"));
  assert.equal(first.hidden, true);
  assert.equal(first.parentElement.querySelector(".image-fallback").hidden, false);
  assert.equal(first.parentElement.querySelector(".image-state").textContent, "Bild konnte nicht geladen werden");
  const original = first.parentElement.querySelector(".image-source-link");
  assert.equal(original.hidden, false);
  assert.equal(original.attributes.get("href"), image);
  assert.equal(original.attributes.get("rel"), "noopener noreferrer");
  assert.equal(help.hidden, false);
  assert.equal(help.attributes.get("href"), "./info.html#images");
  await first.dispatch("load");
  assert(first.parentElement.classList.contains("image-loaded"));
  assert.equal(first.hidden, false);
  assert.equal(first.parentElement.querySelector(".image-fallback").hidden, true);
  assert.equal(original.hidden, true);
  assert.equal(help.hidden, true);
  assert.equal(h.requests.length, 1, "No image proxy, image prefetch or unexpected API call is introduced");
});

test("image help and automation guidance are reachable without claiming a Firefox fix or automatic LLM", () => {
  assert.match(pages.info.html, /href="#images"/);
  assert.match(pages.info.html, /id="images"/);
  assert.match(pages.info.html, /Schild links neben der Firefox-Adresszeile/);
  assert.match(pages.info.html, /mögliche Ursache, kein Nachweis für deinen Browser/);
  assert.match(pages.info.html, /id="automation"/);
  assert.match(pages.info.html, /Ein eingestellter Zeitplan belegt noch keinen ausgeführten Abruf/);
  assert.match(pages.info.html, /Eine reine Shortlist-Aufgabe verändert keine GUI-Scores/);
  assert.match(pages.info.html, /docs\/recurring-triage\.md/);
});

test("cached, missing and unsafe pictures use honest fallbacks without fabricated photos", async () => {
  for (const [naturalWidth, expected] of [[900, "image-loaded"], [0, "image-failed"]]) {
    const cached = harness(fixture({deals: [offer({image: "https://i.ebayimg.com/images/test.jpg"})]}), {imageDefaults: {complete: true, naturalWidth}});
    await flush();
    assert(cached.nodes.get("cards").querySelectorAll(".offer-image")[0].parentElement.classList.contains(expected));
  }
  const missing = harness(fixture({deals: [offer({id: "missing", image: null}), offer({id: "unsafe", image: "https://ebayimg.com.evil.example/image.jpg"}), offer({id: "script", image: "javascript:alert(1)"})]}));
  await flush();
  assert.equal(missing.nodes.get("cards").querySelectorAll(".offer-image").length, 0);
  assert.match(missing.nodes.get("cards").innerHTML, /Kein Bild in der Quelle/);
  assert.doesNotMatch(missing.nodes.get("cards").innerHTML, /src="https:\/\/ebayimg\.com\.evil|href="javascript/);
});

test("design tokens meet text contrast and preserve responsive, keyboard and reduced-motion treatment", () => {
  const tokens = Object.fromEntries([...styles.matchAll(/--([a-z-]+):(#[0-9a-f]{6})/g)].map(match => [match[1], match[2]]));
  const luminance = color => {
    const channels = [1, 3, 5].map(offset => parseInt(color.slice(offset, offset + 2), 16) / 255).map(channel => channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4);
    return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
  };
  const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
  for (const foreground of ["ink", "muted", "quiet", "accent", "warning", "error"]) for (const background of ["bg", "panel", "panel-raised"]) assert(contrast(tokens[foreground], tokens[background]) >= 4.5, `${foreground} on ${background} is at least 4.5:1`);
  assert(contrast(tokens["accent-ink"], tokens.accent) >= 4.5);
  assert(contrast(tokens.focus, tokens.panel) >= 3);
  assert.match(styles, /aspect-ratio:16 \/ 10/);
  assert.match(styles, /object-fit:contain/);
  assert.match(styles, /\.filter-select select,\.compact-select select\{[^}]*min-height:44px/);
  assert.match(styles, /font-variant-numeric:tabular-nums/);
  assert.match(styles, /:focus-visible\{outline:2px solid var\(--focus\)/);
  assert.match(styles, /@media\(max-width:760px\)[\s\S]*\.cards\{grid-template-columns:minmax\(0,1fr\)/);
  assert.match(styles, /@media\(prefers-reduced-motion:reduce\)/);
  assert.doesNotMatch(styles, /@import|@font-face|url\(["']?https?:/);
});

test("dialog close returns keyboard focus, even after the selected auction expires", async () => {
  const h = harness(fixture({deals: [offer({ends_at: iso(60000), detail_path: null})]})); await flush();
  await h.detail("offer-a"); h.nodes.get("detail-dialog").close();
  assert.equal(h.document.activeElement.dataset.id, "offer-a");
  await h.detail("offer-a"); h.tick(60000); h.nodes.get("detail-dialog").close();
  assert.equal(h.document.activeElement, h.nodes.get("search"));
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

test("routine partial scans are compact while real blockers and aged partial snapshots stay prominent", async () => {
  const standard = [{code: "aggregator_claims_only", message: "Long routine source explanation"}, {code: "bounded_coverage", message: "Long routine coverage explanation"}];
  const data = fixture({source_status: "partial", provider: "auktionen-ohne-gebot", blockers: standard});
  const current = harness(data); await flush();
  assert.match(current.nodes.get("notice").className, /compact-notice/);
  assert.match(current.nodes.get("notice").innerHTML, /Begrenzter Teilscan/);
  assert.match(current.nodes.get("notice").innerHTML, /info\.html#coverage/);
  assert.doesNotMatch(current.nodes.get("notice").innerHTML, /Long routine/);
  const blocked = harness({...data, blockers: [...standard, {code: "upstream_error", message: "Source returned an error"}]}); await flush();
  assert.doesNotMatch(blocked.nodes.get("notice").className, /compact-notice/);
  assert.match(blocked.nodes.get("notice").innerHTML, /Source returned an error/);
  const aged = harness({...data, last_successful_scan_at: iso(-3 * hour)}); await flush();
  assert.doesNotMatch(aged.nodes.get("notice").className, /compact-notice/);
  assert.match(aged.nodes.get("notice").innerHTML, /Datenstand prüfen/);
  const completed = harness({...data, source_status: "live", blockers: standard.slice(0, 1), coverage: {complete: false, traversal_completed: true}}); await flush();
  assert.match(completed.nodes.get("notice").className, /compact-notice/);
  assert.match(completed.nodes.get("notice").innerHTML, /Quellseiten durchlaufen/);
  assert.match(completed.nodes.get("notice").innerHTML, /keine direkte eBay-Prüfung/);
  assert.doesNotMatch(completed.nodes.get("notice").innerHTML, /Begrenzter Teilscan/);
});

test("reported nonempty searches without records never look like a healthy completed source scan", async () => {
  for (const coverage of [
    {traversal_completed: true, reported_nonempty_but_no_records: true},
    {traversal_completed: true, searches: {pickup: {enabled: true, traversal_completed: true, reported_nonempty_but_no_records: true, stop_reason: "reported_end"}}}
  ]) {
    const h = harness(fixture({provider: "auktionen-ohne-gebot", source_status: "live", coverage, blockers: [{code: "aggregator_claims_only"}], status_message: "Ein vermeintlich vollständiger Lauf"})); await flush();
    assert.match(h.nodes.get("notice").innerHTML, /Quellenabgleich nicht bestätigt/);
    assert.match(h.nodes.get("notice").innerHTML, /keinen einzigen Datensatz/);
    assert.doesNotMatch(h.nodes.get("notice").innerHTML, /Quellseiten durchlaufen|vermeintlich vollständiger Lauf/);
    assert.match(h.nodes.get("sync-status").className, /partial/);
    assert.doesNotMatch(h.nodes.get("notice").className, /compact-notice/);
  }
});

test("actual seven-item demo is fictitious and starts at Interest 40", async () => {
  assert.equal(demo.deals.length, 7);
  assert(!demo.deals.some(item => item.url || item.image || item.comparison_links.length));
  assert.equal(demo.deals[0].interest_score, 86);
  assert.equal(demo.deals[3].interest_score, 18);
  const h = harness(undefined, {demo: true}); await flush();
  assert.equal(h.nodes.get("minimum-interest").value, "40"); assert.equal(h.count, 4);
  assert.equal(h.nodes.get("result-count").textContent, "(4 / 7)");
  assert.deepEqual(h.ids, ["demo-001", "demo-002", "demo-007", "demo-003"]);
  assert.equal(h.nodes.get("metric-candidates").textContent, "5");
  assert.equal(h.nodes.get("metric-highvalue").textContent, "4");
  assert.deepEqual(h.browseIds("scalp"), ["demo-004"]);
  assert.deepEqual(h.browseIds("useability"), ["demo-004"]);
  assert.equal(h.nodes.get("info-link").href, "./info.html?demo=1");
  assert.match(h.nodes.get("notice").innerHTML, /Keine echten Angebote/);
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /href="https:/);
  assert.doesNotMatch(h.nodes.get("browse-scalp-list").innerHTML, /href="https:/);
  await h.click("show-all-interest"); assert.equal(h.count, 5);
  assert.deepEqual(h.browseIds("scalp"), []);
  assert(h.ids.includes("demo-004"));
  await h.value("transport", "shipping"); assert.equal(h.count, 4);
  await h.value("transport", "pickup"); assert.equal(h.count, 2);
  await h.click("reset-filters"); assert.equal(h.count, 4);
  await h.value("search", "not-in-demo"); assert.equal(h.count, 0);
  assert.equal(h.nodes.get("empty-show-all-interest").hidden, false);
  await h.value("search", ""); await h.click("empty-show-all-interest"); assert.equal(h.count, 5);
  await h.check("show-expired", true); assert.equal(h.count, 6);
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

test("browsing ranks unique below-threshold offers by each axis, with stable ties and a top-ten cap", async () => {
  const module = offer({id: "module", scalp_score: 95, useability_score: 20});
  const rows = [
    module, {...module},
    offer({id: "module-higher-use", scalp_score: 95, useability_score: 30}),
    offer({id: "tie-b", scalp_score: 90, useability_score: 30, ends_at: iso(2 * hour)}),
    offer({id: "tie-a", scalp_score: 90, useability_score: 30, ends_at: iso(2 * hour)}),
    offer({id: "tie-later", scalp_score: 90, useability_score: 30, ends_at: iso(3 * hour)}),
    offer({id: "easy-a", scalp_score: 35, useability_score: 100}),
    offer({id: "easy-b", scalp_score: 30, useability_score: 100}),
    offer({id: "zero", scalp_score: 100, useability_score: 0}),
    ...Array.from({length: 12}, (_, index) => offer({id: `bulk-${String(index).padStart(2, "0")}`, scalp_score: 78 - index, useability_score: 20, ends_at: iso(index === 11 ? 60000 : 4 * hour)})),
    offer({id: "at-threshold", scalp_score: 40, useability_score: 100}),
    offer({id: "above"}),
    offer({id: "pending", scalp_score: 100, useability_score: null}),
    offer({id: "legacy", score_version: 1, scalp_score: 100, useability_score: 100}),
    offer({id: "invalid-score", scalp_score: 100.5, useability_score: 1})
  ];
  const h = harness(fixture({deals: rows})); await flush();
  assert.deepEqual(h.ids, ["above", "at-threshold"]);
  assert.deepEqual(h.browseIds("scalp"), ["zero", "module-higher-use", "module", "tie-a", "tie-b", "tie-later", "bulk-00", "bulk-01", "bulk-02", "bulk-03"]);
  assert.deepEqual(h.browseIds("useability"), ["easy-a", "easy-b", "module-higher-use", "tie-a", "tie-b", "tie-later", "module", "bulk-00", "bulk-01", "bulk-02"]);
  assert.equal(h.nodes.get("browse-scalp-count").textContent, "(10)");
  assert.match(h.nodes.get("browse-summary").textContent, /20 vollständig bewertete Angebote mit Interest < 40/);
  assert.match(h.nodes.get("browse-summary").textContent, /Überschneidungen möglich/);
  assert.match(h.nodes.get("browse-scalp-list").innerHTML, /Interest <strong>0<\/strong>/);
  for (const axis of ["scalp", "useability"]) assert.equal(new Set(h.browseIds(axis)).size, 10);
  const before = h.browseIds("scalp");
  await h.value("sort", "ending"); assert.deepEqual(h.browseIds("scalp"), before);
  h.tick(60000);
  assert.deepEqual(h.browseIds("scalp"), before, "an off-list expiration does not reorder the top ten");
  assert.match(h.nodes.get("browse-summary").textContent, /19 vollständig bewertete Angebote/);
  assert.match(h.nodes.get("result-summary").textContent, /Bestand bis Auktionsende · aktiv: Restlaufzeit < 72 Std\./);
});

test("browsing follows the current Interest slider, computes the dual score and handles zero honestly", async () => {
  const h = harness(fixture({deals: [
    offer({id: "under", scalp_score: 95, useability_score: 20, interest_score: 99}),
    offer({id: "at", scalp_score: 20, useability_score: 100}),
    offer({id: "half-up", scalp_score: 1, useability_score: 50}),
    offer({id: "pending", scalp_score: 99, useability_score: null})
  ]})); await flush();
  assert.deepEqual(h.browseIds("scalp"), ["under", "at", "half-up"]);
  assert.match(h.nodes.get("browse-scalp-list").innerHTML, /Interest <strong>19<\/strong>/);
  assert.match(h.nodes.get("browse-scalp-list").innerHTML, /Interest <strong>1<\/strong>/);
  await h.value("minimum-interest", 20);
  assert.deepEqual(h.ids, ["at"]);
  assert.deepEqual(h.browseIds("scalp"), ["under", "half-up"]);
  await h.click("show-all-interest");
  assert.equal(h.count, 4);
  for (const axis of ["scalp", "useability"]) {
    assert.deepEqual(h.browseIds(axis), []);
    assert.equal(h.nodes.get(`browse-${axis}-empty`).hidden, false);
  }
  assert.match(h.nodes.get("browse-summary").textContent, /Bei Interest 0/);
  await h.click("reset-filters");
  assert.deepEqual(h.browseIds("scalp"), ["under", "at", "half-up"]);
});

test("personal exclusions suppress even high LLM scores in the main list and both Top 10 lists", async () => {
  const excluded = values => offer({assessment_basis: "llm", semantic_review_mode: "interactive", semantic_assessed_at: iso(-hour), title: "Historische Drucksache", personal_interest_excluded: true, personal_interest_reason: "Kein gewünschtes Gerät", ...values});
  const rows = [
    excluded({id: "media-main", scalp_score: 95, useability_score: 99}),
    excluded({id: "media-scalp", scalp_score: 99, useability_score: 30}),
    excluded({id: "media-useability", scalp_score: 20, useability_score: 99}),
    excluded({id: "media-debug", scalp_score: 100, useability_score: 100, ends_at: iso(-hour), last_seen_at: null, eligibility: "excluded"}),
    offer({id: "machine", personal_interest_excluded: false}),
    offer({id: "component", title: "Glas - Teil E1 für Carl Zeiss Jena Refraktometer Mikroskop Objektiv Okular Tubus", scalp_score: 90, useability_score: 20, personal_interest_excluded: false}),
    offer({id: "tool", scalp_score: 20, useability_score: 95, personal_interest_excluded: false})
  ];
  const h = harness(fixture({deals: rows})); await flush();
  assert.deepEqual(h.ids, ["machine"]);
  assert.deepEqual(h.browseIds("scalp"), ["component", "tool"]);
  assert.deepEqual(h.browseIds("useability"), ["tool", "component"]);
  assert.equal(h.nodes.get("metric-candidates").textContent, "3");
  assert.equal(h.nodes.get("metric-highvalue").textContent, "1");
  assert.doesNotMatch(h.nodes.get("browse-useability-list").innerHTML, /media-useability|Historische Drucksache/);
  await h.click("show-all-interest");
  await h.check("show-expired", true); await h.check("show-stale", true);
  assert.deepEqual(h.ids, ["machine", "tool", "component"]);
  await h.value("eligibility", "excluded"); assert.deepEqual(h.ids, []);
  assert.equal(rows[0].useability_score, 99, "personal selection never rewrites the authored score");
  assert.equal(rows[0].assessment_basis, "llm");
  assert.equal(rows[0].semantic_assessed_at, iso(-hour));
  const info = harness(fixture({deals: rows}), {page: "info"}); await flush();
  assert.match(info.nodes.get("info-profile").textContent, /4 von 7 gespeicherten Angeboten/);
  assert.match(info.nodes.get("info-profile").textContent, /unveränderten Scores/);
});

test("old snapshots exclude offered brochures, not real machines bundled with manuals or media holders", async () => {
  const excludedTitles = [
    "KAESER FAHRBARE BAUKOMPRESSOREN MOBILAIR M 27 / M 31 BROSCHÜRE 12 Seiten RAR",
    "Altes BMW Heft 1998", "Handbuch für Carl Zeiss Mikroskop", "Prospekt mit Bedienungsanleitung",
    "Baumaschinen Broschuere", "Betriebsanleitung Drehmaschine", "Werkzeug Katalog", "BMW Aufkleber"
  ];
  const allowedTitles = [
    "Bohrmaschine mit Koffer und Bedienungsanleitung", "Autoklav inkl. gedrucktem Handbuch",
    "Bohrmaschine ohne Bedienungsanleitung", "Tischbohrmaschine Betriebsanleitung fehlt",
    "Laborgerät Bedienungsanleitung nicht vorhanden", "Stabiler Prospekthalter",
    "Broschürenständer aus Stahl", "Prospekt-Ständer fahrbar", "Halter für Prospekte",
    "Glas - Teil E1 für Carl Zeiss Jena Refraktometer Mikroskop Objektiv Okular Tubus",
    "Schmelzpunktgerät defekt als Ersatzteil"
  ];
  const rows = [
    ...excludedTitles.map((title, i) => offer({id: `unwanted-${i}`, title, scalp_score: i % 2 ? 20 : 95, useability_score: 99, assessment_basis: "llm"})),
    ...allowedTitles.map((title, i) => offer({id: `wanted-${String(i).padStart(2, "0")}`, title}))
  ];
  const h = harness(fixture({deals: rows})); await flush();
  const expected = allowedTitles.map((_, i) => `wanted-${String(i).padStart(2, "0")}`);
  assert.deepEqual(h.ids, expected);
  for (const axis of ["scalp", "useability"]) assert.deepEqual(h.browseIds(axis), []);
  await h.click("show-all-interest"); assert.deepEqual(h.ids, expected);
  await h.check("show-expired", true); await h.check("show-stale", true);
  assert.deepEqual(h.ids, expected);
  const info = harness(fixture({deals: rows}), {page: "info"}); await flush();
  assert.match(info.nodes.get("info-profile").textContent, /8 von 19 gespeicherten Angeboten/);
});

test("dashboard and Info share the same conservative legacy profile guard", () => {
  const guard = script => script.match(/  function personalInterestExcluded\(item\) \{[\s\S]*?\n  \}/)?.[0];
  assert(guard(pages.index.script));
  assert.equal(guard(pages.index.script), guard(pages.info.script));
});

test("media-title plus signs and ampersands cannot masquerade as bundled manuals", async () => {
  const papers = [
    "BROSHUIS NEWS MÄRZ 2005 & MÄRZ 2006 2x 8 Seiten BROSCHÜRE",
    "HYDREMA 707-SERIE. DER MINI-TRUCK 707G/707G POWER+ 2 Seiten BROSCHÜRE RAR",
    "DAEMO VERSCHROTTUNGS-& ABRISSAUFSÄTZE 12 Seiten DinA4 Broschüre 2014",
    "DARDA ABBRUCHGERÄTE & ANBAUWERKZEUGE 80Seiten KATALOG DinA4 Broschüre 2019"
  ];
  const devices = [
    "Bohrmaschine + Bedienungsanleitung", "Bohrmaschine & Bedienungsanleitung",
    "Autoklav\t+\tHandbuch", "Schmelzpunktgerät inkl. deutschem Handbuch",
    "Mikroskop plus Koffer und Bedienungsanleitung"
  ];
  const rows = [
    ...papers.map((title, i) => offer({id: `paper-${i}`, title, scalp_score: i % 2 ? 20 : 95, useability_score: 99, assessment_basis: "llm"})),
    ...devices.map((title, i) => offer({id: `device-${i}`, title}))
  ];
  const h = harness(fixture({deals: rows})); await flush();
  const wanted = devices.map((_, i) => `device-${i}`);
  assert.deepEqual(h.ids, wanted);
  for (const axis of ["scalp", "useability"]) assert.deepEqual(h.browseIds(axis), []);
  await h.value("search", "Broschüre");
  assert.deepEqual(h.ids, []);
  for (const axis of ["scalp", "useability"]) assert.deepEqual(h.browseIds(axis), []);
  await h.value("search", ""); await h.value("minimum-interest", 100);
  for (const axis of ["scalp", "useability"]) assert.deepEqual(h.browseIds(axis), wanted);
  await h.click("show-all-interest");
  await h.check("show-expired", true); await h.check("show-stale", true);
  assert.deepEqual(h.ids, wanted);
  const info = harness(fixture({deals: rows}), {page: "info"}); await flush();
  assert.match(info.nodes.get("info-profile").textContent, /4 von 9 gespeicherten Angeboten/);
  assert.equal(rows[0].useability_score, 99, "profile exclusion does not rewrite an authored score");
});

test("known printed compounds are excluded without a generic book or prospect prefix rule", async () => {
  const papers = [
    "Papke Verpackungstechnik Prospektblätter", "COLLICO Plibox Prospektblatt",
    "Brandner & Söhne Prospektblaetter", "ZIRA Preisliste 2/85+2 Prospektblätter",
    "ZIRA Preisliste 2/85", "FARMTECH Stalldungstreuer für alpines Gelände ProspektTyp 400 500 550",
    "Reparatur und Justieranleitung für OPMI Operationsmikroskop Mikroskop",
    "Faltblätter von Mikroskop Geräte und Labor Instrumente Carl Zeiss",
    "Faltblaetter zu Laborgeräten", "Datenblatt für Laborwaage", "Faltblatt für Laborgerät",
    "Flyer für Autoklav", "Teilekatalog für Bohrmaschine", "Parts Manual für Drehmaschine",
    "Benutzerhandbuch für Oszilloskop", "Wartungshandbuch für Maschine", "Bedienungshandbuch Laborgerät",
    "Bedinungshandbuch Laborgerät", "Kollegenhandbuch für Betrieb", "Faltprospekt Carl Zeiss",
    "Landmaschinenprospekte 1998", "Kugellagerpreisliste", "Analog Devices Gesamtübersicht (mit Preisliste)",
    "Elektronik Gesamtuebersicht", "Kochbuch", "Sachbuch", "Lehrbuch", "Ernährungsbuch",
    "Taschenbuch", "Klappbuch", "Gartenbuch", "Datenbuch"
  ];
  const physical = [
    "Prospekthalter", "Prospekthüllen DIN A4", "Prospekthuellen DIN A4", "Prospekt Hüllen DIN A4",
    "Prospektständer", "Flyerhalter", "Flyer Ständer", "Ringbuch DIN A4",
    "Bohrmaschine mit Datenblatt", "Oszilloskop + Benutzerhandbuch", "Drehmaschine mit Wartungshandbuch"
  ];
  const rows = [
    ...papers.map((title, i) => offer({id: `paper-${i}`, title, scalp_score: i % 2 ? 20 : 95, useability_score: 99, assessment_basis: "llm"})),
    ...physical.map((title, i) => offer({id: `physical-${String(i).padStart(2, "0")}`, title}))
  ];
  const h = harness(fixture({deals: rows})); await flush();
  const wanted = physical.map((_, i) => `physical-${String(i).padStart(2, "0")}`);
  assert.deepEqual(h.ids, wanted);
  for (const axis of ["scalp", "useability"]) assert.deepEqual(h.browseIds(axis), []);
  await h.value("minimum-interest", 100);
  for (const axis of ["scalp", "useability"]) assert.deepEqual(h.browseIds(axis), wanted.slice(0, 10));
  await h.click("show-all-interest");
  await h.check("show-expired", true); await h.check("show-stale", true);
  assert.deepEqual(h.ids, wanted);
  const info = harness(fixture({deals: rows}), {page: "info"}); await flush();
  assert.match(info.nodes.get("info-profile").textContent, new RegExp(`^${papers.length} von ${rows.length} gespeicherten Angeboten`));
});

test("browsing shares search, transport, value and eligible/review filters but never factual exclusions", async () => {
  const rows = [
    offer({id: "ship", title: "Labormodul", scalp_score: 80, useability_score: 30, value_band: "around_500", shipping_eur: 6, local_pickup_confirmed: false, pickup_available: false, eligibility: "eligible"}),
    aggregate({id: "pickup", title: "Prozesssensor", scalp_score: 70, useability_score: 30, value_band: "unknown", shipping_eur: null, pickup_available: true, pickup_search_confirmed: true, pickup_search_postcode: "69121", pickup_search_radius_km: 50, eligibility: "review"}),
    offer({id: "small", scalp_score: 20, useability_score: 100, value_band: "around_5", shipping_eur: 8, local_pickup_confirmed: false, pickup_available: false, eligibility: "eligible"}),
    offer({id: "excluded", scalp_score: 99, useability_score: 20, eligibility: "excluded", eligibility_without_time_window: "excluded"})
  ];
  const h = harness(fixture({deals: rows})); await flush();
  assert.deepEqual(h.browseIds("scalp"), ["ship", "pickup", "small"]);
  await h.value("search", "LABORMODUL"); assert.deepEqual(h.browseIds("scalp"), ["ship"]);
  await h.value("search", ""); await h.value("transport", "shipping");
  assert.deepEqual(h.browseIds("scalp"), ["ship", "small"]);
  await h.value("value-filter", "plausible"); assert.deepEqual(h.browseIds("scalp"), ["ship"]);
  await h.value("value-filter", "all"); await h.value("transport", "pickup");
  assert.deepEqual(h.browseIds("scalp"), ["pickup"]);
  assert.match(h.nodes.get("browse-scalp-list").innerHTML, /Abholsuche · 50 km.*Nähe laut Quelle; Standort im Original prüfen/);
  await h.value("eligibility", "eligible"); assert.deepEqual(h.browseIds("scalp"), []);
  await h.value("eligibility", "review"); assert.deepEqual(h.browseIds("scalp"), ["pickup"]);
  await h.value("transport", "all"); await h.value("eligibility", "excluded");
  assert.deepEqual(h.browseIds("scalp"), []); assert.deepEqual(h.browseIds("useability"), []);
});

test("browsing ignores debug time overrides, retains old observations and updates even when the main list is empty", async () => {
  const low = overrides => offer({scalp_score: 80, useability_score: 30, ...overrides});
  const h = harness(fixture({deals: [
    low({id: "old", last_seen_at: iso(-96 * hour), ends_at: iso(5 * hour)}),
    low({id: "ending", ends_at: iso(60000)}),
    low({id: "enters", ends_at: iso(73 * hour), eligibility: "excluded", eligibility_without_time_window: "review"}),
    low({id: "at-boundary", ends_at: iso(72 * hour), eligibility: "excluded", eligibility_without_time_window: "review"}),
    low({id: "fact-excluded", ends_at: iso(73 * hour), eligibility: "excluded", eligibility_without_time_window: "excluded"}),
    low({id: "ended", ends_at: iso(0)}),
    low({id: "unknown-end", ends_at: null}),
    low({id: "unknown-source", last_seen_at: null}),
    low({id: "future-source", last_seen_at: iso(24 * hour)})
  ]})); await flush();
  assert.equal(h.count, 0);
  assert.deepEqual(h.browseIds("scalp"), ["ending", "old"]);
  assert.match(h.nodes.get("browse-scalp-list").innerHTML, /Quellenstand: vor 4 Tagen · ältere Beobachtung, nicht aktuell bestätigt/);
  await h.check("show-expired", true); await h.check("show-stale", true);
  assert.deepEqual(h.browseIds("scalp"), ["ending", "old"]);
  h.tick(60000); assert.deepEqual(h.browseIds("scalp"), ["old", "at-boundary"]);
  h.tick(hour); assert.deepEqual(h.browseIds("scalp"), ["old", "at-boundary", "enters"]);
  const ages = h.nodes.get("browse-scalp-list").querySelectorAll("[data-last-seen]");
  const ends = h.nodes.get("browse-scalp-list").querySelectorAll("[data-ends-at]");
  assert(ages.some(node => node.classList.contains("freshness-warning")));
  const before = ends[0].textContent;
  h.tick(60000);
  assert.notEqual(ends[0].textContent, before, "countdowns update in compact lists without a re-render");
  assert.equal(h.requests.length, 1, "the clock is not an extra source scan");
  await h.value("eligibility", "eligible");
  assert.deepEqual(h.browseIds("scalp"), [], "old or review-baseline items do not become verified candidates");
});

test("compact browsing details keep safe links, unknown facts and keyboard focus in their original list", async () => {
  const h = harness(fixture({deals: [offer({id: "unsafe", title: '<img src=x onerror="bad()"> Bauteil', scalp_score: 80, useability_score: 30, bid_count: null, shipping_eur: null, condition: "Ungeprüft", url: "javascript:alert(1)", detail_path: null, ends_at: iso(60000)})]})); await flush();
  const markup = h.nodes.get("browse-useability-list").innerHTML;
  assert.match(markup, /&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);
  assert.match(markup, /Gebote unbekannt/); assert.match(markup, /Versand ungeklärt/);
  assert.match(markup, /Heuristik · Konfidenz niedrig/); assert.match(markup, /Ungeprüft/);
  assert.doesNotMatch(markup, /<img|onerror="|href="javascript/);
  const opener = h.nodes.get("browse-useability-list").querySelectorAll(".detail-open")[0];
  await h.detail("unsafe", "browse-useability-list"); h.nodes.get("detail-dialog").close();
  assert.equal(h.document.activeElement, opener, "overlap between lists returns focus to the actual opener");
  await h.detail("unsafe", "browse-useability-list"); h.tick(60000); h.nodes.get("detail-dialog").close();
  assert.equal(h.document.activeElement, h.nodes.get("search"));
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

test("observations older than three days remain visible, marked and never rejuvenated by import", async () => {
  const rows = [aggregate({id: "seven", source_observed_at: iso(-7 * hour)}), aggregate({id: "twenty-three", source_observed_at: iso(-23 * hour)}), aggregate({id: "four-days", source_observed_at: iso(-96 * hour), valid_until: iso(-72 * hour)}), aggregate({id: "unknown", source_observed_at: null}), aggregate({id: "future", source_observed_at: iso(2 * hour)})];
  const h = harness(fixture({provider: "auktionen-ohne-gebot", deals: rows})); await flush();
  assert.equal(h.count, 3);
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 7 Std\. · ältere Beobachtung, nicht aktuell bestätigt/);
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 23 Std\. · ältere Beobachtung, nicht aktuell bestätigt/);
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 4 Tagen · ältere Beobachtung, nicht aktuell bestätigt/);
  assert.equal(h.nodes.get("cards").querySelectorAll("[data-last-seen]").filter(node => node.classList.contains("freshness-warning")).length, 3);
  assert.match(h.nodes.get("retention-note").textContent, /weniger als 72 Stunden/);
  h.tick(hour);
  assert(h.ids.includes("twenty-three")); assert(h.ids.includes("four-days"));
  h.setFetch(async () => ({ok: true, json: async () => fixture({provider: "auktionen-ohne-gebot", generated_at: iso(hour), deals: rows.map(item => ({...item, last_seen_at: iso(hour)}))})}));
  await h.click("refresh-button");
  assert(h.ids.includes("twenty-three")); assert(h.ids.includes("four-days"));
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 8 Std\./);
  assert.match(h.nodes.get("cards").innerHTML, /Quellenstand: vor 4 Tagen/);
});

test("active window is strict at both boundaries and unknown or future observation times stay hidden", async () => {
  const rows = [offer({id: "just-started", ends_at: iso(1)}), offer({id: "almost-72", ends_at: iso(72 * hour - 1)}), offer({id: "at-72", ends_at: iso(72 * hour)}), offer({id: "over-72", ends_at: iso(72 * hour + 1)}), offer({id: "just-ended", ends_at: iso(0)}), offer({id: "unknown-end", ends_at: null}), offer({id: "future-source", last_seen_at: iso(1)}), offer({id: "unknown-source", last_seen_at: null})];
  const h = harness(fixture({deals: rows})); await flush();
  assert.deepEqual(h.ids, ["almost-72", "just-started"]);
  assert.match(h.nodes.get("criteria").innerHTML, /Ende &lt; 72 Std\./);
  assert.match(h.nodes.get("result-summary").textContent, /3 außerhalb Zeitfenster.*3 Zeitstempel ungeklärt/);
  await h.check("show-expired", true); assert.equal(h.count, 5);
  await h.check("show-stale", true); assert.equal(h.count, 8);
  assert.match(h.nodes.get("cards").innerHTML, /Nur durch Debug-Filter sichtbar/);
});

test("V1/V2 migration ignores legacy age limits and valid_until but keeps auction expiry", async () => {
  for (const schema_version of [1, 2, 3]) {
    const h = harness(fixture({schema_version, criteria: {...demo.criteria, lookahead_hours: schema_version < 3 ? 24 : 72, max_freshness_hours: 6}, freshness: {maximum_source_age_hours: schema_version < 3 ? 6 : null}, deals: [offer({id: "old", last_seen_at: iso(-120 * hour), ends_at: iso(70 * hour), valid_until: iso(-96 * hour)}), offer({id: "null-validity", valid_until: null}), offer({id: "expired", ends_at: iso(0), valid_until: iso(hour)})]})); await flush();
    assert.deepEqual(h.ids, ["null-validity", "old"]);
    assert.match(h.nodes.get("retention-note").textContent, /72 Stunden/);
    assert.doesNotMatch(h.nodes.get("cards").innerHTML, /Pufferfrist/);
  }
});

test("valid V3 configured horizons override the 72-hour default consistently on both pages", async () => {
  for (const freshness of [{active_window_hours: 96, maximum_source_age_hours: null}, {maximum_source_age_hours: null}]) {
    const data = fixture({criteria: {...demo.criteria, lookahead_hours: 96}, freshness, deals: [offer({id: "inside-96", ends_at: iso(80 * hour), last_seen_at: iso(-120 * hour)}), offer({id: "at-96", ends_at: iso(96 * hour)})]});
    const h = harness(data); await flush();
    assert.deepEqual(h.ids, ["inside-96"]);
    assert.match(h.nodes.get("retention-note").textContent, /weniger als 96 Stunden/);
    const info = harness(data, {page: "info"}); await flush(); await info.click("copy-instructions");
    assert.match(info.nodes.get("info-retention").textContent, /jetzt \+ 96 Stunden/);
    assert.match(info.copied, /jetzt < ends_at < jetzt \+ 96 Stunden/);
  }
});

test("timer admits retained future items only with explicit factual baseline and removes ended auctions", async () => {
  const h = harness(fixture({deals: [
    offer({id: "enters", ends_at: iso(73 * hour), eligibility: "excluded", eligibility_without_time_window: "review", time_window_eligible: false}),
    offer({id: "price-excluded", ends_at: iso(73 * hour), eligibility: "excluded", eligibility_without_time_window: "excluded", price_eur: 50}),
    offer({id: "legacy-excluded", ends_at: iso(73 * hour), eligibility: "excluded", reasons: ["Nur zeitbedingt ausgeschlossen"]}),
    offer({id: "ending", ends_at: iso(60000)}),
    offer({id: "old-validity", valid_until: iso(60000), last_seen_at: iso(-150 * hour)}),
    offer({id: "remaining"})
  ]})); await flush();
  assert.equal(h.count, 3); assert.deepEqual(h.intervals.map(item => item.delay), [60000]);
  h.tick(60000); assert.deepEqual(h.ids, ["old-validity", "remaining"]);
  h.tick(hour - 60000); assert.deepEqual(h.ids, ["old-validity", "remaining"]);
  h.tick(1); assert.deepEqual(h.ids, ["enters", "old-validity", "remaining"]);
  assert.equal(h.requests.length, 1);
  assert(h.nodes.get("cards").querySelectorAll("[data-last-seen]").some(node => node.textContent === "Quellenstand: vor 1 Std."));
  await h.check("show-expired", true); assert.equal(h.count, 4);
  assert.match(h.nodes.get("cards").innerHTML, /Auktion abgelaufen/);
});

test("aggregator pickup claims are provisional, scoped and independently aged", async () => {
  const pickup = overrides => aggregate({pickup_available: true, pickup_search_confirmed: true, pickup_search_postcode: "69121", pickup_search_radius_km: 50, shipping_eur: null, eligibility: "review", ...overrides});
  const h = harness(fixture({provider: "auktionen-ohne-gebot", deals: [pickup({id: "matching"}), pickup({id: "smaller", pickup_search_radius_km: 25}), pickup({id: "wrong-postcode", pickup_search_postcode: "69115"}), pickup({id: "wide", pickup_search_radius_km: 51}), pickup({id: "only-available", pickup_search_confirmed: false}), pickup({id: "old", source_observed_at: iso(-25 * hour)}), pickup({id: "unknown", source_observed_at: null})]})); await flush();
  await h.value("transport", "pickup"); assert.deepEqual(h.ids, ["matching", "old", "smaller"]);
  const markup = h.nodes.get("cards").innerHTML;
  assert.match(markup, /Abholsuche · 50 km/); assert.match(markup, /Abholsuche · 25 km/);
  assert.match(markup, /Nähe laut Quelle; Standort im Original prüfen/);
  assert.doesNotMatch(markup, /Abholung ca\.|Abholung im 50-km/);
  await h.check("show-stale", true); assert.equal(h.count, 3);
  await h.value("transport", "shipping"); assert.equal(h.count, 0);
  const aging = harness(fixture({provider: "auktionen-ohne-gebot", deals: [pickup({source_observed_at: iso(-24 * hour + 60000)})]})); await flush();
  await aging.check("show-stale", true); assert.match(aging.nodes.get("cards").innerHTML, /Abholsuche/);
  aging.tick(120000); assert.match(aging.nodes.get("cards").innerHTML, /Abholsuche · 50 km/);
  assert(aging.nodes.get("cards").querySelectorAll("[data-last-seen]").some(node => /ältere Beobachtung, nicht aktuell bestätigt/.test(node.textContent)));
  aging.tick(4 * hour); assert.equal(aging.count, 0);
});

test("older pickup-search evidence never ages a fresh price observation or masquerades as new evidence", async () => {
  const h = harness(fixture({provider: "auktionen-ohne-gebot", deals: [aggregate({
    id: "independent-pickup", pickup_available: true, pickup_search_confirmed: true,
    pickup_search_postcode: "69121", pickup_search_radius_km: 50,
    pickup_search_observed_at: iso(-2 * hour + 60000), source_observed_at: iso(-60000),
    eligibility: "review", detail_path: null
  })]})); await flush();
  assert.doesNotMatch(h.nodes.get("cards").innerHTML, /Umkreissuchstand:/);
  h.tick(120000);
  assert.match(h.nodes.get("cards").innerHTML, /Umkreissuchstand: .*älter als 2 Std\.; nicht aktuell bestätigt/);
  const sourceAges = h.nodes.get("cards").querySelectorAll("[data-last-seen]").map(node => node.textContent);
  assert(sourceAges.some(value => value === "Quellenstand: vor 3 Min."));
  assert(!sourceAges.some(value => /ältere Beobachtung/.test(value)));
  await h.detail("independent-pickup");
  assert.match(h.nodes.get("dialog-content").innerHTML, /Abholsuchstand \(Berlin\)<\/small><strong>.*älter als 2 Std/);
  assert.match(h.nodes.get("dialog-content").innerHTML, /Beim Aggregator beobachtet: 02\.01\.30, 12:59/);
});

test("crossing two hours changes a candidate to review without hiding it from the normal list", async () => {
  const h = harness(fixture({deals: [aggregate({source_observed_at: iso(-2 * hour + 60000), eligibility: "eligible", eligibility_without_time_window: "eligible"})]}));
  await flush();
  assert.match(h.nodes.get("cards").innerHTML, /KANDIDAT/);
  h.tick(60000); assert.match(h.nodes.get("cards").innerHTML, /KANDIDAT/);
  h.tick(1); assert.equal(h.count, 1);
  assert.match(h.nodes.get("cards").innerHTML, /PRÜFFALL/);
  assert.match(h.nodes.get("cards").innerHTML, /ältere Beobachtung, nicht aktuell bestätigt/);
  await h.value("eligibility", "eligible"); assert.equal(h.count, 0);
  await h.value("eligibility", "review"); assert.equal(h.count, 1);
  assert.equal(h.requests.length, 1);
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
  assert.match(h.nodes.get("info-retention").textContent, /jetzt \+ 72 Stunden.*unabhängig vom Quellenalter.*keine erneute Prüfung/);
  h.setFetch(async () => { throw Error("Offline"); }); await h.click("info-refresh");
  assert.match(h.nodes.get("info-status").innerHTML, /Abruf fehlgeschlagen/);
  assert.match(h.nodes.get("info-status").innerHTML, /zuletzt geladene Snapshot bleibt unverändert sichtbar/);
  assert.match(h.nodes.get("info-source").innerHTML, /Auktionen ohne Gebot/);
});

test("Info copy handoff is pinned, dual-score aware, read-only; demo/back navigation and fallback work", async () => {
  const h = harness(fixture({criteria: {...demo.criteria, max_freshness_hours: 12}}), {page: "info"}); await flush(); await h.click("copy-instructions");
  for (const phrase of ["demselben Commit-SHA", "alle paginierten Angebotsseiten", "counts.total", "jetzt < ends_at < jetzt + 72 Stunden", "ends_at und valid_until", "eligibility_without_time_window", "floor((Scalp * Useability + 50) / 100)", "Useability", "Keine Gebote", "ausdrücklichen Speicherauftrag"]) assert(h.copied.includes(phrase), phrase);
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
