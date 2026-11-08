(() => {
  "use strict";
  const $ = (selector) => document.querySelector(selector);
  const els = Object.fromEntries([
    "sync-status", "sync-label", "notice", "source-attribution", "metric-scanned", "metric-scanned-note", "metric-candidates",
    "metric-candidate-note", "metric-highvalue", "metric-highvalue-label", "metric-updated",
    "metric-source", "location-label", "criteria", "result-count", "result-summary", "search", "info-link",
    "transport", "sort", "eligibility", "value-filter", "show-expired", "show-stale", "show-stale-label", "retention-note", "reset-filters",
    "cards", "empty", "empty-description", "detail-dialog", "dialog-title", "dialog-content",
    "demo-button", "refresh-button", "minimum-interest", "minimum-interest-value", "show-all-interest", "empty-show-all-interest"
  ].map(id => [id, $("#" + id)]));
  const euro = new Intl.NumberFormat("de-DE", {style: "currency", currency: "EUR"});
  const time = new Intl.DateTimeFormat("de-DE", {dateStyle: "short", timeStyle: "short", timeZone: "Europe/Berlin"});
  const statuses = {
    unconfigured: "Datenquelle offen", live: "Snapshot geladen", partial: "Teilscan / Hinweise",
    error: "Import gestört", disabled: "Import deaktiviert", demo: "Demo-Modus"
  };
  let feed = null;
  let isDemo = false;
  let loading = false;
  let loadError = "";
  let selectedId = null;
  let detailSequence = 0;
  let visibleIds = "";
  const detailCache = new Map();

  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[char]);
  const number = value => typeof value === "number" && Number.isFinite(value) ? value : null;
  const money = value => number(value) === null ? "unbekannt" : euro.format(value);
  const timestamp = value => typeof value === "string" && value ? Date.parse(value) : NaN;
  const dateText = value => Number.isFinite(timestamp(value)) ? time.format(new Date(value)) : "unbekannt";
  const array = value => Array.isArray(value) ? value : [];
  const aggregatorProvider = "auktionen-ohne-gebot";
  const aggregatorSourceUrl = "https://auktionen-ohne-gebot.de/business-industrie/";
  const providerOf = item => item?.source_provider || feed?.provider || feed?.source_provider || (typeof feed?.source === "string" ? feed.source : feed?.source?.provider) || "";
  const isAggregator = item => providerOf(item) === aggregatorProvider || item?.verification_level === "aggregator";
  const aggregatorFeed = () => !isDemo && (isAggregator() || array(feed?.deals).some(isAggregator));
  // An import timestamp cannot make independently observed aggregator data fresh again.
  const observedAt = item => isAggregator(item) ? item.source_observed_at : item.last_seen_at;
  function activeWindowHours() {
    // V1/V2 used a different, observation-based policy. Their old 24-hour
    // settings must not silently reinstate that policy during migration.
    const configured = feed?.schema_version >= 3
      ? number(feed?.freshness?.active_window_hours) ?? number(feed?.criteria?.lookahead_hours) : null;
    return configured !== null && configured > 0 && configured <= 744 ? configured : 72;
  }
  const windowLabel = () => `Ende in weniger als ${String(activeWindowHours()).replace(".", ",")} Stunden`;
  const auctionExpired = item => Number.isFinite(timestamp(item.ends_at)) && timestamp(item.ends_at) <= Date.now();
  const staleTimestamp = value => !Number.isFinite(timestamp(value)) || Date.now() - timestamp(value) > 7200000 || timestamp(value) > Date.now();
  const expired = item => auctionExpired(item);
  const stale = item => staleTimestamp(observedAt(item));
  const unknownTime = item => !Number.isFinite(timestamp(observedAt(item))) || timestamp(observedAt(item)) > Date.now() || !Number.isFinite(timestamp(item.ends_at));
  const outsideWindow = item => Number.isFinite(timestamp(item.ends_at)) && (expired(item) || timestamp(item.ends_at) >= Date.now() + activeWindowHours() * 3600000);
  const active = item => !outsideWindow(item) && !unknownTime(item);
  const eligibility = item => {
    const baseline = item.eligibility_without_time_window;
    // Only an explicit V3 baseline can release a time-only exclusion when
    // an auction enters the live reading window; never infer it from prose.
    const state = feed?.schema_version >= 3 && active(item) && ["eligible", "review", "excluded"].includes(baseline)
      ? baseline : ["eligible", "review", "excluded"].includes(item.eligibility) ? item.eligibility : "review";
    return state === "eligible" && stale(item) ? "review" : state;
  };
  const semanticAssessed = item => item.semantic_assessed === true;
  const valueBand = item => semanticAssessed(item) && ["around_5", "around_50", "around_500"].includes(item.value_band) ? item.value_band : "unknown";
  const scoreValue = value => Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
  function scores(item) {
    const modern = item.score_version === 2;
    const scalp = scoreValue(item.scalp_score) ?? (!modern ? scoreValue(item.interest_score) ?? scoreValue(item.score) : null);
    const useability = modern ? scoreValue(item.useability_score) : null;
    const complete = modern && scalp !== null && useability !== null;
    return {scalp, useability, interest: complete ? Math.floor((scalp * useability + 50) / 100) : null, complete, legacy: !modern};
  }
  const interest = item => scores(item).interest;
  function minimumInterest() {
    const value = els["minimum-interest"].value.trim();
    const parsed = value === "" ? 40 : Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.min(100, Math.round(parsed))) : 40;
  }
  const meetsInterest = item => minimumInterest() === 0 || (interest(item) !== null && interest(item) >= minimumInterest());
  const confidenceText = item => ({low:"niedrig", medium:"mittel", high:"hoch"})[item.assessment_confidence] || "nicht angegeben";
  const interactiveAssessed = item => semanticAssessed(item) && item.assessment_basis === "llm" && item.semantic_review_mode === "interactive";
  const basisText = item => !semanticAssessed(item) ? "Noch nicht eingeordnet" : interactiveAssessed(item) ? "ChatGPT" : ({heuristic:"Heuristik", llm:"LLM"})[item.assessment_basis] || "Basis unbekannt";
  const automaticLlmText = () => feed?.ai_enabled ? "Automatische LLM-Aufrufe aktiv" : "Automatische LLM-Aufrufe aus";
  function interactiveReviewText() {
    const review = feed?.interactive_review;
    if (!review || review.reviewer !== "ChatGPT") return "";
    if (review.status === "applied" && number(review.applied) !== null) return `ChatGPT-Sichtung: ${review.applied} ${review.applied === 1 ? "Angebot" : "Angebote"}`;
    return ({none:"ChatGPT-Sichtung: noch keine", expired:"ChatGPT-Sichtung: abgelaufen", invalid:"ChatGPT-Sichtung: ungültig"})[review.status] || "ChatGPT-Sichtung: Status unklar";
  }
  const sourceLink = item => !isDemo && providerOf(item) === aggregatorProvider ? `<a href="${aggregatorSourceUrl}" target="_blank" rel="noopener noreferrer">Auktionen ohne Gebot ↗</a>` : "";
  const descriptionText = item => typeof item.description === "string" && item.description.trim() ? item.description : isAggregator(item) ? "Quelle liefert keinen Beschreibungstext. Originalanzeige bei eBay prüfen." : "Beschreibung nicht verfügbar – Originalanzeige prüfen.";

  function httpsUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return null;
      return url;
    } catch { return null; }
  }
  function itemUrl(value) {
    const url = httpsUrl(value);
    return url && /(^|\.)ebay\.(de|com|at|ch|fr|it|es|nl|be|ie|co\.uk)$/.test(url.hostname) ? url.href : null;
  }
  function imageUrl(value) {
    const url = httpsUrl(value);
    return url && /(^|\.)ebayimg\.com$/.test(url.hostname) ? url.href : null;
  }
  function comparisonUrl(value) {
    const url = httpsUrl(value);
    if (!url || !url.hostname.includes(".") || /(^|\.)(localhost|local|internal|test|invalid)$/.test(url.hostname) || /^[\d.]+$/.test(url.hostname) || url.hostname.includes(":")) return null;
    return url.href;
  }
  function detailUrl(value) {
    if (typeof value !== "string" || !/^api\/items\/[A-Za-z0-9_-][A-Za-z0-9._-]*\.json$/.test(value)) return null;
    const url = new URL("./" + value, location.href);
    return url.origin === location.origin ? url.href : null;
  }
  function comparisons(item) {
    return array(item.comparison_links).map(entry => {
      const raw = typeof entry === "string" ? {url: entry} : entry;
      if (!raw || typeof raw !== "object") return null;
      const url = comparisonUrl(raw.url);
      return url ? {...raw, url} : null;
    }).filter(Boolean).slice(0, 15);
  }
  function remaining(value) {
    const ms = timestamp(value) - Date.now();
    if (!Number.isFinite(ms)) return "Endzeit unbekannt";
    if (ms <= 0) return "Abgelaufen";
    const minutes = Math.ceil(ms / 60000);
    if (minutes >= 1440) return `Noch ${Math.floor(minutes / 1440)} T. ${Math.floor(minutes % 1440 / 60)} Std.`;
    return minutes < 60 ? `Noch ${minutes} Min.` : `Noch ${Math.floor(minutes / 60)} Std. ${minutes % 60} Min.`;
  }
  function ageText(value) {
    const age = Date.now() - timestamp(value);
    if (!Number.isFinite(age)) return "Datenalter unbekannt";
    if (age < 0) return "Zeitstempel liegt in der Zukunft";
    const minutes = Math.max(0, Math.floor(age / 60000));
    if (minutes < 1) return "Stand: gerade eben";
    if (minutes < 60) return `Stand: vor ${minutes} Min.`;
    if (minutes < 1440) return `Stand: vor ${Math.floor(minutes / 60)} Std.`;
    return `Stand: vor ${Math.floor(minutes / 1440)} Tagen`;
  }
  function sourceAgeText(value) {
    const label = ageText(value).replace(/^Stand:/, "Quellenstand:");
    const age = Date.now() - timestamp(value);
    return Number.isFinite(age) && age > 7200000 ? `${label} · ältere Beobachtung, nicht aktuell bestätigt` : label;
  }
  function timeLabel(item) {
    if (auctionExpired(item)) return "Auktion abgelaufen";
    return remaining(item.ends_at);
  }
  function bidText(item) {
    const count = number(item.bid_count);
    return count === null ? "Gebote unbekannt" : count === 1 ? "1 Gebot" : `${count} Gebote`;
  }
  function reserveText(item) {
    return ({none: "Kein Mindestpreis", not_met: "Mindestpreis nicht erreicht", met: "Mindestpreis erreicht", unknown: "Mindestpreis ungeklärt"})[item.reserve_status] || "Mindestpreis ungeklärt";
  }
  function pickupRadius(item) {
    return number(item.pickup_radius_km) ?? number(feed?.location?.pickup_radius_km);
  }
  function confirmedNearby(item) {
    if (item.local_pickup_confirmed === true) return true;
    const radius = pickupRadius(item);
    return item.pickup_available === true && radius !== null && number(item.distance_km) !== null && item.distance_km <= radius;
  }
  function matchingPickupSearch(item) {
    const searchRadius = number(item.pickup_search_radius_km);
    const profileRadius = number(feed?.location?.pickup_radius_km);
    const postcode = item.pickup_search_postcode;
    return isAggregator(item) && item.pickup_search_confirmed === true && active(item)
      && typeof postcode === "string" && /^\d{5}$/.test(postcode) && postcode === feed?.location?.postcode
      && searchRadius !== null && searchRadius > 0 && profileRadius !== null && searchRadius <= profileRadius;
  }
  function nearby(item) {
    return confirmedNearby(item) || matchingPickupSearch(item);
  }
  function visibleSignature(items) {
    return JSON.stringify(items.map(item => [item.id, outsideWindow(item), unknownTime(item), eligibility(item), matchingPickupSearch(item)]));
  }
  function cheapShipping(item) {
    const max = number(feed?.criteria?.max_shipping_eur);
    return item.shipping_confirmed !== false && number(item.shipping_eur) !== null && max !== null && item.shipping_eur <= max;
  }
  function transportText(item) {
    const parts = [];
    if (number(item.shipping_eur) !== null) parts.push(`Versand ${money(item.shipping_eur)}${item.shipping_confirmed === false ? " (ungeklärt)" : ""}`);
    else parts.push("Versand ungeklärt");
    if (confirmedNearby(item)) {
      const distance = number(item.distance_km);
      const radius = pickupRadius(item);
      parts.push(distance !== null ? `Abholung ca. ${distance} km` : radius !== null ? `Abholung im ${radius}-km-Suchradius` : "Abholung im Suchgebiet");
    } else if (matchingPickupSearch(item)) {
      parts.push(`Abholsuche · ${item.pickup_search_radius_km} km`);
      parts.push("Nähe laut Quelle; Standort im Original prüfen");
    } else if (item.pickup_available === true) parts.push("Abholung möglich · Nähe ungeklärt");
    if (item.city) parts.push(String(item.city));
    return parts.join(" · ");
  }
  function valueText(item) {
    return ({around_5:"Eher 5 €", around_50:"Eher 50 €", around_500:"Eher 500 €", unknown:"Unklar"})[valueBand(item)];
  }
  function potentialLabel(item) {
    if (eligibility(item) === "excluded") return "AUSGESCHLOSSEN";
    if (!scores(item).complete) return "BEWERTUNG OFFEN";
    return eligibility(item) === "review" ? "PRÜFFALL" : "KANDIDAT";
  }
  function demoTimes(data) {
    const now = Date.now();
    data.generated_at = new Date(now).toISOString();
    data.last_successful_scan_at = null;
    for (const item of data.deals) {
      if (number(item.demo_hours_remaining) !== null) item.ends_at = new Date(now + item.demo_hours_remaining * 3600000).toISOString();
      if (number(item.demo_last_seen_hours) !== null) item.last_seen_at = new Date(now - item.demo_last_seen_hours * 3600000).toISOString();
    }
    return data;
  }

  async function load(demo = false) {
    if (loading) return;
    loading = true;
    els["refresh-button"].disabled = true;
    els["demo-button"].disabled = true;
    els.cards.setAttribute("aria-busy", "true");
    els["sync-label"].textContent = "Lade Snapshot";
    try {
      const response = await fetch(demo ? "./data/demo.json" : "./data/deals.json", {cache: "no-store"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const json = await response.json();
      if (!json || !Array.isArray(json.deals) || ![1, 2, 3].includes(json.schema_version)) throw new Error("Nicht unterstütztes Datenformat");
      if (json.deals.some(item => !item || typeof item !== "object" || typeof item.id !== "string")) throw new Error("Angebotsdaten unvollständig");
      feed = demo ? demoTimes(json) : json;
      isDemo = demo || feed.source_status === "demo";
      loadError = "";
      detailCache.clear();
      els["demo-button"].textContent = isDemo ? "Produktivdaten" : "Demo";
      render();
    } catch (error) {
      loadError = `Snapshot konnte nicht geladen werden (${error.message}).`;
      if (feed) render();
      else {
        renderStatus();
        els.empty.hidden = false;
        els["empty-description"].textContent = "Keine Daten verfügbar. Versuche den Snapshot erneut zu laden oder öffne die explizit gekennzeichnete Demo.";
      }
    } finally {
      loading = false;
      els.cards.setAttribute("aria-busy", "false");
      els["refresh-button"].disabled = false;
      els["demo-button"].disabled = false;
    }
  }

  function renderStatus() {
    const status = isDemo ? "demo" : feed?.source_status || "unconfigured";
    const generated = timestamp(feed?.generated_at);
    const scan = timestamp(feed?.last_successful_scan_at);
    const aged = !isDemo && status === "live" && (!Number.isFinite(scan) || Date.now() - scan > 7200000 || !Number.isFinite(generated) || Date.now() - generated > 7200000);
    els["sync-status"].className = "sync-status " + (loadError ? "error" : aged ? "stale" : status);
    els["sync-label"].textContent = loadError ? "Ladefehler" : aged ? "Stand älter / ungeklärt" : statuses[status] || "Status unbekannt";
    const notices = [];
    if (loadError) notices.push(`<strong>Abruf fehlgeschlagen</strong><p>${esc(loadError)}${feed ? " Der zuletzt geladene Snapshot bleibt sichtbar; sein Datenstand ist unverändert." : ""}</p>`);
    if (isDemo) notices.push("<strong>DEMO · Keine echten Angebote</strong><p>Fiktive Artikel, Preise und Scores. Keine echten Auktionslinks.</p>");
    else if (feed && (status !== "live" || aged || array(feed.blockers).length)) {
      const defaults = {unconfigured: "Die Datenquelle ist noch nicht vollständig eingerichtet.", partial: "Der Scan ist nur teilweise abgeschlossen; Hinweise unten beachten.", error: "Der letzte Import ist fehlgeschlagen. Bereits gespeicherte Angebote können veraltet sein.", disabled: "Der automatische Import ist derzeit deaktiviert."};
      notices.push(`<strong>${esc(aged ? "Datenstand prüfen" : statuses[status] || "Unbekannter Quellenstatus")}</strong><p>${esc(String(feed.status_message || defaults[status] || "Dieser Snapshot ist keine Echtzeit-Abfrage.").slice(0, 240))}${aged ? " Abrufstand älter als zwei Stunden oder ungeklärt." : ""} <a href="./info.html#source">Statusdetails ↗</a></p>`);
      const blockers = array(feed.blockers).filter(blocker => blocker && typeof blocker === "object");
      if (blockers.length) notices.push(`<p>${blockers.slice(0, 2).map(blocker => esc(String(blocker.message || blocker.code || "Offener Einrichtungsschritt").slice(0, 180))).join(" · ")}</p>`);
    }
    els.notice.hidden = !notices.length;
    els.notice.className = "notice" + (loadError || status === "error" ? " error" : "");
    els.notice.innerHTML = notices.join("");
  }

  function renderProfile() {
    const loc = feed.location || {};
    const criteria = feed.criteria || {};
    const place = loc.city || (loc.postcode === "69121" ? "Heidelberg" : "");
    els["location-label"].textContent = [place, loc.postcode].filter(Boolean).join(" · ") || "Suchgebiet offen";
    els["info-link"].href = isDemo ? "./info.html?demo=1" : "./info.html";
    const badges = [];
    if (number(criteria.max_price_eur) !== null) badges.push(`bis ${money(criteria.max_price_eur)}`);
    if (number(criteria.max_bids) !== null) badges.push(criteria.max_bids === 0 ? "0 Gebote gesucht" : `bis ${criteria.max_bids} Gebote`);
    if (number(criteria.max_shipping_eur) !== null) badges.push(`Versand ≤ ${money(criteria.max_shipping_eur)}`);
    if (number(loc.pickup_radius_km) !== null) badges.push(`Abholsuche ${loc.pickup_radius_km} km`);
    badges.push(`Ende < ${activeWindowHours()} Std.`);
    els.criteria.innerHTML = (badges.length ? badges : ["Suchkriterien offen"]).map(value => `<span>${esc(value)}</span>`).join("");
    const buffered = feed.deals.filter(item => active(item) && eligibility(item) !== "excluded");
    const pending = buffered.filter(item => !scores(item).complete).length;
    const review = buffered.filter(item => eligibility(item) === "review").length;
    const minimum = minimumInterest();
    els["metric-scanned"].textContent = number(feed.stats?.received) === null ? "—" : String(feed.stats.received);
    els["metric-scanned-note"].textContent = isDemo ? "fiktive Datensätze" : aggregatorFeed() ? "Aggregator-Datensätze" : "eingelesene Datensätze";
    els["metric-candidates"].textContent = String(buffered.length);
    els["metric-candidate-note"].textContent = `${review} Prüffälle${pending ? ` · ${pending} Scores offen` : ""}`;
    els["metric-highvalue-label"].textContent = minimum ? `INTEREST ≥ ${minimum}` : "ALLE SCORE-STÄNDE";
    els["metric-highvalue"].textContent = String(buffered.filter(meetsInterest).length);
    els["minimum-interest-value"].textContent = String(minimum);
    els["show-all-interest"].setAttribute("aria-pressed", String(minimum === 0));
    els["show-stale-label"].textContent = "Unklare Zeitstempel anzeigen";
    els["retention-note"].textContent = `${windowLabel()} · Quellenalter bleibt sichtbar`;
    els["metric-updated"].textContent = isDemo ? "DEMO" : dateText(feed.generated_at);
    els["metric-source"].textContent = isDemo ? "Keine echten Auktionen" : interactiveReviewText() || statuses[feed.source_status] || "Status unbekannt";
    els["source-attribution"].innerHTML = isDemo ? "Fiktive Beispieldaten" : aggregatorFeed() ? `Aggregator-Cache · nicht direkt verifiziert · ${sourceLink() || sourceLink(feed.deals.find(item => providerOf(item) === aggregatorProvider)) || "Drittquelle"}` : "Quellenstand aus veröffentlichtem Snapshot";
  }

  function visibleDeals() {
    const query = els.search.value.trim().toLocaleLowerCase("de-DE");
    const list = (feed?.deals || []).filter(item => {
      const state = eligibility(item);
      if (!els["show-expired"].checked && outsideWindow(item)) return false;
      if (!els["show-stale"].checked && unknownTime(item)) return false;
      if (!meetsInterest(item)) return false;
      if (els.eligibility.value === "active" ? state === "excluded" : state !== els.eligibility.value) return false;
      if (query && ![item.title, item.description, item.city, item.condition].filter(Boolean).join(" ").toLocaleLowerCase("de-DE").includes(query)) return false;
      if (els.transport.value === "shipping" && !cheapShipping(item)) return false;
      if (els.transport.value === "pickup" && !nearby(item)) return false;
      const value = valueBand(item);
      if (els["value-filter"].value === "plausible" && value !== "around_500") return false;
      if (els["value-filter"].value === "unknown" && value !== "unknown") return false;
      if (els["value-filter"].value === "unlikely" && !["around_5", "around_50"].includes(value)) return false;
      return true;
    });
    const unknownLast = value => number(value) ?? Number.POSITIVE_INFINITY;
    const total = item => number(item.price_eur) !== null && number(item.shipping_eur) !== null ? item.price_eur + item.shipping_eur : Number.POSITIVE_INFINITY;
    list.sort((a, b) => {
      let diff = 0;
      if (els.sort.value === "ending") diff = unknownLast(timestamp(a.ends_at)) - unknownLast(timestamp(b.ends_at));
      else if (els.sort.value === "price") diff = unknownLast(a.price_eur) - unknownLast(b.price_eur);
      else if (els.sort.value === "total") diff = total(a) - total(b);
      else if (els.sort.value === "freshness") diff = (number(timestamp(observedAt(b))) ?? 0) - (number(timestamp(observedAt(a))) ?? 0);
      else if (els.sort.value === "scalp") diff = (scores(b).scalp ?? -1) - (scores(a).scalp ?? -1);
      else if (els.sort.value === "useability") diff = (scores(b).useability ?? -1) - (scores(a).useability ?? -1);
      else diff = (interest(b) ?? -1) - (interest(a) ?? -1);
      return diff || String(a.id).localeCompare(String(b.id));
    });
    return list;
  }

  function card(item, index) {
    const picture = isDemo ? null : imageUrl(item.image);
    const url = isDemo ? null : itemUrl(item.url);
    const state = eligibility(item);
    const rating = scores(item);
    const rationale = typeof item.value_reason === "string" && item.value_reason.trim() ? item.value_reason : array(item.reasons)[0];
    const preview = rationale || "Noch keine begründete Einordnung. Lieferumfang und Zustand im Original prüfen.";
    const risks = array(item.risks).slice(0, 2);
    return `<article class="deal-card${expired(item) ? " expired" : ""}" data-id="${esc(item.id)}">
      <div class="card-visual${picture ? " image-loading" : " image-missing"}">
        ${picture ? `<img class="offer-image" alt="${esc("Angebotsfoto: " + (item.title || "Objekt"))}" width="720" height="450" loading="${index < 3 ? "eager" : "lazy"}" fetchpriority="${index === 0 ? "high" : "auto"}" decoding="async" referrerpolicy="no-referrer" src="${esc(picture)}" />` : ""}
        <span class="image-fallback"><svg viewBox="0 0 48 48" width="40" height="40" fill="none" aria-hidden="true"><rect x="7" y="10" width="34" height="28" rx="4" stroke="currentColor" stroke-width="1.5"/><circle cx="17" cy="20" r="3" stroke="currentColor" stroke-width="1.5"/><path d="m9 35 10-10 7 6 6-7 8 10" stroke="currentColor" stroke-width="1.5"/></svg><strong class="image-state">${picture ? "Bild wird geladen …" : isDemo ? "Fiktives Beispiel" : "Kein Bild in der Quelle"}</strong><span>${picture ? "Originalfoto von eBay" : isDemo ? "Keine echte Auktion" : "Objekt im Originalangebot ansehen"}</span></span>
        ${picture ? `<a class="image-source-link" href="${esc(picture)}" target="_blank" rel="noopener noreferrer" hidden>Originalbild öffnen ↗</a>` : ""}
      </div>
      <div class="card-content">
        <div class="card-kicker"><span class="score-badge ${state}">${esc(potentialLabel(item))}</span><span class="time-badge" data-ends-at="${esc(item.ends_at)}">${esc(timeLabel(item))}</span></div>
        <h3>${esc(item.title || "Angebot ohne Titel")}</h3>
        <p class="card-descr"><span class="reason-label">Einordnung</span>${esc(preview)}</p>
        <div class="score-panel${rating.complete ? "" : " score-pending"}" aria-label="Getrennte Angebotsbewertung"><div class="interest-score"><small>INTEREST</small><strong>${rating.interest ?? "—"}</strong><span>${rating.interest === null ? "noch offen" : "/ 100"}</span></div><div class="component-scores"><div><span>Scalp${rating.legacy && rating.scalp !== null ? " · Altwert" : ""}</span><strong>${rating.scalp ?? "—"}</strong></div><div><span>Useability</span><strong>${rating.useability ?? "—"}</strong></div><p>${esc(basisText(item))}${semanticAssessed(item) ? ` · Konfidenz ${esc(confidenceText(item))}` : ""}</p></div></div>
        <div class="offer-price"><div><small>${isAggregator(item) ? "LAUT QUELLE" : "BEIM SCAN"} · ${esc(bidText(item).toUpperCase())}</small><strong>${esc(money(item.price_eur))}</strong></div><span>${esc(item.condition || "Zustand unbekannt")}</span></div>
        <p class="delivery">${esc(transportText(item))}</p>
        <p class="card-notes">${esc(reserveText(item))}</p>
        ${risks.length ? `<p class="card-risks"><span aria-hidden="true">!</span> ${risks.map(value => esc(String(value).slice(0, 180))).join(" · ")}</p>` : ""}
        <p class="card-meta"><span class="${stale(item) ? "freshness-warning" : ""}" data-last-seen="${esc(observedAt(item))}">${esc(sourceAgeText(observedAt(item)))}</span></p>
        ${unknownTime(item) || outsideWindow(item) ? `<p class="card-notes freshness-warning">${unknownTime(item) ? "Zeitstempel ungeklärt." : "Außerhalb des aktiven Zeitfensters."} Nur durch Debug-Filter sichtbar.</p>` : ""}
        <div class="card-action"><button type="button" class="primary-button detail-open" data-id="${esc(item.id)}" aria-label="${esc("Details prüfen: " + (item.title || item.id))}">Einordnung & Details</button>${url ? `<a class="outline-button" target="_blank" rel="noopener noreferrer" href="${esc(url)}">eBay ↗</a>` : `<button class="outline-button" type="button" disabled>${isDemo ? "Demo" : "Kein Link"}</button>`}</div>
      </div></article>`;
  }
  function initializeImages() {
    els.cards.querySelectorAll(".offer-image").forEach(image => {
      const frame = image.parentElement;
      const update = loaded => {
        frame.classList.toggle("image-loading", false);
        frame.classList.toggle("image-loaded", loaded);
        frame.classList.toggle("image-failed", !loaded);
        image.hidden = !loaded;
        frame.querySelector(".image-fallback").hidden = loaded;
        frame.querySelector(".image-source-link").hidden = loaded;
        if (!loaded) frame.querySelector(".image-state").textContent = "Bild konnte nicht geladen werden";
      };
      image.addEventListener("load", () => update(true));
      image.addEventListener("error", () => update(false));
      // A cached image may have completed before listeners were registered.
      if (image.complete) update(image.naturalWidth > 0);
    });
  }
  function renderCards() {
    const list = visibleDeals();
    const activeId = document.activeElement?.classList?.contains("detail-open") ? document.activeElement.dataset.id : null;
    visibleIds = visibleSignature(list);
    els["result-count"].textContent = `(${list.length} / ${feed.deals.length})`;
    els.cards.innerHTML = list.map(card).join("");
    initializeImages();
    const hiddenOutside = feed.deals.filter(outsideWindow).length;
    const hiddenUnknown = feed.deals.filter(item => unknownTime(item) && (els["show-expired"].checked || !outsideWindow(item))).length;
    const pending = feed.deals.filter(item => active(item) && !scores(item).complete).length;
    const minimum = minimumInterest();
    els["result-summary"].textContent = `${list.length} von ${feed.deals.length} gespeicherten Angeboten sichtbar · ${minimum ? `Interest ≥ ${minimum}` : "alle Score-Stände"}${pending ? ` · ${pending} duale Bewertungen offen` : ""}${!els["show-expired"].checked && hiddenOutside ? ` · ${hiddenOutside} außerhalb Zeitfenster` : ""}${!els["show-stale"].checked && hiddenUnknown ? ` · ${hiddenUnknown} Zeitstempel ungeklärt` : ""}${els["show-stale"].checked || els["show-expired"].checked ? " · ACHTUNG: Debug-Filter lässt ungültige Zeitstände zu" : ""}${isDemo ? " · fiktive Beispiele" : ""}`;
    els.empty.hidden = list.length > 0;
    els["empty-show-all-interest"].hidden = minimum === 0 || !feed.deals.length;
    els["empty-description"].textContent = feed.deals.length ? `Kein Angebot erfüllt diese Filter${minimum ? ` und Interest ≥ ${minimum}` : ""}. „Alles (0)“ zeigt auch unvollständige Bewertungen. Aktives Zeitfenster: ${windowLabel().toLowerCase()}; bereits abgelaufene Angebote bleiben ausgeblendet.` : "Noch keine Angebote im Snapshot. Den Quellenstatus oben prüfen; die Demo enthält ausschließlich fiktive Beispiele.";
    if (activeId) Array.from(els.cards.querySelectorAll(".detail-open")).find(button => button.dataset.id === activeId)?.focus({preventScroll: true});
  }
  function render() {
    if (!feed) return;
    renderStatus();
    renderProfile();
    renderCards();
  }

  function renderDetail(item, detailState = "") {
    els["dialog-title"].textContent = item.title || "Angebotsdetails";
    const list = entries => array(entries).length ? `<ul>${array(entries).map(text => `<li>${esc(text)}</li>`).join("")}</ul>` : "<p>Keine Angaben vorhanden.</p>";
    const url = isDemo ? null : itemUrl(item.url);
    const jsonUrl = isDemo ? null : detailUrl(item.detail_path);
    const evidence = comparisons(item);
    const rating = scores(item);
    const fields = [
      [isAggregator(item) ? "Gebotspreis laut Quelle" : "Gebotspreis beim Scan", money(item.price_eur), false], [isAggregator(item) ? "Gebote laut Quelle" : "Gebote beim Scan", bidText(item), number(item.bid_count) === null],
      ["Auktionsende (Berlin)", dateText(item.ends_at), !Number.isFinite(timestamp(item.ends_at))], ["Mindestpreis", reserveText(item), !["none", "met"].includes(item.reserve_status)],
      ["Metadaten bis Auktionsende", dateText(item.ends_at), expired(item) || unknownTime(item)],
      ["Versand", money(item.shipping_eur), number(item.shipping_eur) === null], ["Standort", [item.city, item.postcode].filter(Boolean).join(" · ") || "Unbekannt", false],
      [isAggregator(item) ? "Zustand laut Quelle" : "Zustand laut Anzeige", item.condition || "Unbekannt", !item.condition], ["Prüfstatus", ({eligible:"Kandidat", review:"Noch zu prüfen", excluded:"Ausgeschlossen"})[eligibility(item)], eligibility(item) !== "eligible"],
      ["Interest", rating.interest === null ? "Noch offen" : `${rating.interest} / 100`, rating.interest === null],
      [rating.legacy ? "Scalp · Altwert" : "Scalp", rating.scalp === null ? "Noch offen" : `${rating.scalp} / 100`, rating.scalp === null],
      ["Useability", rating.useability === null ? "Noch offen" : `${rating.useability} / 100`, rating.useability === null],
      ["Basis / Konfidenz", `${basisText(item)} / ${confidenceText(item)}`, false]
    ];
    if (interactiveAssessed(item)) fields.push(["ChatGPT-Sichtung (Berlin)", dateText(item.semantic_assessed_at), !Number.isFinite(timestamp(item.semantic_assessed_at))]);
    els["dialog-content"].innerHTML = `${isDemo ? '<p class="demo-stamp">FIKTIVES BEISPIEL · KEINE ECHTE AUKTION</p>' : ""}
      <p class="detail-disclaimer">${isDemo ? "Alle folgenden Angaben sind erfunden und dienen nur zur Demonstration." : `${isAggregator(item) ? "Beim Aggregator beobachtet" : "Quellenstand"}: ${esc(dateText(observedAt(item)))} (Berlin). ${isAggregator(item) ? "Zwischengespeicherte Drittquelle, nicht direkt bei eBay verifiziert. " : ""}${stale(item) ? "Ältere Quellenbeobachtung oder Datenalter nicht belegt: nicht aktuell bestätigt. " : ""}${outsideWindow(item) || unknownTime(item) ? "Außerhalb des aktiven Zeitfensters oder Zeitstempel ungeklärt. " : ""}Metadaten bleiben bis zum Auktionsende erhalten; ihr Quellenalter wird dadurch nicht erneuert. Aktive Auswahl: ${esc(windowLabel().toLowerCase())}. Vor jedem Gebot Preise, Mitbieter, Zustand, Versand und Mindestpreis bei eBay kontrollieren.`}</p>
      <div class="detail-grid">${fields.map(([label, value, warning]) => `<div class="detail-cell${warning ? " warning" : ""}"><small>${esc(label)}</small><strong>${esc(value)}</strong></div>`).join("")}</div>
      <p>${esc(transportText(item))}</p>
      <div class="dialog-section"><h3>Warum auf der Liste?</h3>${list(item.reasons)}</div>
      <div class="dialog-section"><h3>Scalp: Fundpotenzial</h3><p>${esc(item.value_reason || "Noch keine Scalp-Begründung vorhanden.")}</p><p>${esc(valueText(item))} · grobe Größenordnung, keine Wertermittlung.</p></div>
      <div class="dialog-section"><h3>Useability: eigenständige Nutzbarkeit</h3><p>${esc(item.useability_reason || "Useability ist noch nicht bewertet.")}</p><h3>Abhängigkeiten</h3>${list(item.dependencies)}<p>Useability bestätigt keine Funktionsfähigkeit. Defekte und Reparaturbedarf sind getrennte Risiken.</p></div>
      <div class="dialog-section"><h3>Interest: kombinierte Priorität</h3><p>${rating.complete ? `${rating.scalp} × ${rating.useability} / 100, auf ganze Punkte gerundet → ${rating.interest}.` : "Die vollständige duale Bewertung in Version 2 fehlt. Interest bleibt unbekannt; ein alter Gesamtwert wird nicht als neuer Interest übernommen."} <a href="./info.html#scores">Score-Erklärung ↗</a></p></div>
      ${evidence.length && !isDemo ? `<div class="dialog-section"><h3>Vergleichsquellen</h3><ul class="comparison-list">${evidence.map(entry => `<li><a target="_blank" rel="noopener noreferrer" href="${esc(entry.url)}">${esc(entry.title || entry.label || new URL(entry.url).hostname)} ↗</a>${entry.kind || number(entry.price_eur) !== null ? `<small>${esc([entry.kind, number(entry.price_eur) !== null ? money(entry.price_eur) : ""].filter(Boolean).join(" · "))}</small>` : ""}</li>`).join("")}</ul></div>` : ""}
      <div class="dialog-section risk"><h3>Vor dem Bieten prüfen</h3>${list(item.risks)}</div>
      <div class="dialog-section"><h3>Angebotsbeschreibung</h3><p class="offer-text">${esc(descriptionText(item))}</p></div>
      ${!isDemo && isAggregator(item) ? `<p class="detail-source">Datenquelle: ${sourceLink(item) || "Aggregator"}. Keine direkte eBay-Verifikation; fehlender Standort und Mindestpreisstatus bleiben unbekannt.</p>` : ""}
      <p class="detail-load-status" role="status">${esc(detailState)}</p>
      <div class="dialog-links">${url ? `<a class="dialog-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Originalangebot bei eBay ↗</a>` : ""}${jsonUrl ? `<a class="detail-json-link" href="${esc(jsonUrl)}" target="_blank" rel="noopener noreferrer">Angebot als JSON ↗</a>` : ""}</div>`;
  }
  async function showDetail(id) {
    const item = feed?.deals.find(deal => deal.id === id);
    if (!item) return;
    selectedId = id;
    const sequence = ++detailSequence;
    const dashboardSnapshot = feed.snapshot_id;
    const url = isDemo ? null : detailUrl(item.detail_path);
    const cached = detailCache.get(id);
    renderDetail(cached || item, url && !cached ? "Veröffentlichten Detaildatensatz laden …" : "");
    if (!els["detail-dialog"].open) els["detail-dialog"].showModal();
    if (!url || cached) return;
    try {
      const response = await fetch(url, {cache: "no-store"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const raw = data.item || data;
      if (!raw || typeof raw !== "object" || raw.id !== id) throw new Error("Detaildatensatz passt nicht zum Angebot");
      if (!dashboardSnapshot || data.snapshot_id !== dashboardSnapshot || feed.snapshot_id !== dashboardSnapshot) {
        if (selectedId === id && sequence === detailSequence && els["detail-dialog"].open) renderDetail(item, "Die Detaildaten gehören zu einem anderen oder nicht belegten Veröffentlichungsstand. Bitte den Snapshot neu laden; die Beschreibung wurde nicht übernommen.");
        return;
      }
      // Even within one verified snapshot, detail text must not overwrite price or scoring fields.
      const detail = {...item, description: typeof raw.description === "string" ? raw.description : item.description};
      detailCache.set(id, detail);
      if (selectedId === id && sequence === detailSequence && els["detail-dialog"].open) renderDetail(detail, `${detail.description ? "Beschreibung aus dem Detail-Feed." : isAggregator(item) ? "Quelle liefert keinen Beschreibungstext." : "Detaildatensatz enthält keinen Beschreibungstext."} Preis- und Statusangaben stammen unverändert aus diesem Dashboard-Snapshot.`);
    } catch {
      if (selectedId === id && sequence === detailSequence && els["detail-dialog"].open) renderDetail(item, "Detail-Feed nicht verfügbar. Die Angaben aus dem geladenen Snapshot bleiben sichtbar.");
    }
  }

  [els.search, els.transport, els.sort, els.eligibility, els["value-filter"], els["show-expired"], els["show-stale"], els["minimum-interest"]].forEach(control => control.addEventListener(control === els.search || control === els["minimum-interest"] ? "input" : "change", () => { if (feed) { renderProfile(); renderCards(); } }));
  function showAllInterest() {
    els["minimum-interest"].value = "0";
    if (feed) { renderProfile(); renderCards(); }
  }
  els["show-all-interest"].addEventListener("click", showAllInterest);
  els["empty-show-all-interest"].addEventListener("click", showAllInterest);
  els["reset-filters"].addEventListener("click", () => {
    els.search.value = "";
    els.transport.value = "all";
    els.sort.value = "score";
    els.eligibility.value = "active";
    els["value-filter"].value = "all";
    els["show-expired"].checked = false;
    els["show-stale"].checked = false;
    els["minimum-interest"].value = "40";
    if (feed) { renderProfile(); renderCards(); }
  });
  els.cards.addEventListener("click", event => {
    const button = event.target.closest(".detail-open");
    if (button && els.cards.contains(button)) showDetail(button.dataset.id);
  });
  els["demo-button"].addEventListener("click", () => load(!isDemo));
  els["refresh-button"].addEventListener("click", () => load(isDemo));
  $("#close-dialog").addEventListener("click", () => els["detail-dialog"].close());
  els["detail-dialog"].addEventListener("click", event => { if (event.target === els["detail-dialog"]) els["detail-dialog"].close(); });
  els["detail-dialog"].addEventListener("close", () => {
    const id = selectedId;
    selectedId = null;
    ++detailSequence;
    const opener = Array.from(els.cards.querySelectorAll(".detail-open")).find(button => button.dataset.id === id);
    (opener || els.search).focus({preventScroll: true});
  });
  setInterval(() => {
    if (!feed || loading || document.hidden) return;
    renderStatus();
    renderProfile();
    if (visibleSignature(visibleDeals()) !== visibleIds) renderCards();
    else {
      els.cards.querySelectorAll("[data-ends-at]").forEach(node => { node.textContent = timeLabel({ends_at: node.dataset.endsAt}); });
      els.cards.querySelectorAll("[data-last-seen]").forEach(node => {
        node.textContent = sourceAgeText(node.dataset.lastSeen);
        node.classList.toggle("freshness-warning", staleTimestamp(node.dataset.lastSeen));
      });
    }
  }, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && feed && !loading) render(); });
  load(new URLSearchParams(location.search).get("demo") === "1");
})();
