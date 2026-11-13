(() => {
  "use strict";
  const status = document.querySelector("#info-status");
  const demo = new URLSearchParams(location.search).get("demo") === "1";
  const show = message => {
    status.textContent = message;
    status.hidden = !message;
  };
  const demoMessage = "Demo: Die angezeigten Angebote sind fiktiv.";

  if (demo) {
    document.querySelector("#home-link").href = "./?demo=1";
    show(demoMessage);
    return;
  }

  function sourceMessage(data) {
    const coverage = data.coverage || {};
    const unconfirmed = coverage.reported_nonempty_but_no_records === true
      || ["general", "pickup"].some(key => {
        const search = coverage.searches?.[key];
        return search?.enabled !== false && (search?.reported_nonempty_but_no_records === true
          || search?.stop_reason === "reported_nonempty_but_no_records");
      });
    if (data.source_status === "demo") return demoMessage;
    if (["disabled", "unconfigured"].includes(data.source_status)) return "Die Angebotsquelle ist derzeit nicht aktiv.";
    if (data.source_status === "error") return "Die Angebote konnten zuletzt nicht aktualisiert werden. Bitte im Original prüfen.";
    if (unconfirmed) return "Der aktuelle Abgleich ist nicht bestätigt. Angaben können älter sein.";
    if (data.source_status === "partial") {
      const blockers = Array.isArray(data.blockers) ? data.blockers : [];
      return blockers.some(blocker => blocker?.code === "source_cooldown")
        ? "Es gab keinen neuen Abruf. Es gelten die Zeitangaben der Angebote."
        : "Die Angaben wurden zuletzt nur teilweise aktualisiert. Bitte im Original prüfen.";
    }
    return data.source_status === "live" ? "" : "Der aktuelle Datenstatus ist unbekannt. Hinweise im Radar beachten.";
  }

  async function loadStatus() {
    try {
      const response = await fetch("./api/manifest.json", {cache: "no-store"});
      if (!response.ok) throw new Error("Status nicht verfügbar");
      const data = await response.json();
      if (!data || ![1, 2, 3].includes(data.schema_version) || typeof data.source_status !== "string") {
        throw new Error("Statusformat unbekannt");
      }
      show(sourceMessage(data));
    } catch {
      show("Der aktuelle Datenstatus ist nicht erreichbar. Zeitangaben und Hinweise im Radar beachten.");
    }
  }
  loadStatus();
})();
