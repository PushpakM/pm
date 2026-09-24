// Weighbridge UI helpers. No inline scripts (the page's CSP forbids them).
(function () {
  "use strict";

  function fmtKg(v) {
    if (v === null || v === undefined) return "—";
    return Math.round(v).toLocaleString("en-IN");
  }

  // Print buttons
  document.querySelectorAll("#print-btn").forEach(function (b) {
    b.addEventListener("click", function () { window.print(); });
  });

  // Report bars
  document.querySelectorAll(".bar-fill[data-pct]").forEach(function (el) {
    el.style.width = Math.max(0, Math.min(100, parseFloat(el.dataset.pct) || 0)) + "%";
  });

  // --- live weight ---------------------------------------------------------
  var scale = document.getElementById("scale");
  if (scale) {
    var kgEl = document.getElementById("scale-kg");
    var mtEl = document.getElementById("scale-mt");
    var statusEl = document.getElementById("scale-status");
    var rawEl = document.getElementById("scale-raw");
    var captureBtn = document.getElementById("capture-btn");
    var tareBtn = document.getElementById("tare-btn");
    var manualBox = document.getElementById("manual");

    function setButtons(stable) {
      var manual = manualBox && manualBox.checked;
      [captureBtn, tareBtn].forEach(function (b) { if (b) b.disabled = !(stable || manual); });
    }
    if (manualBox) manualBox.addEventListener("change", function () { setButtons(scale.dataset.state === "stable"); });

    function poll() {
      fetch("/api/weight", { credentials: "same-origin", cache: "no-store" })
        .then(function (r) {
          if (r.status === 401) { window.location = "/login"; throw new Error("logged out"); }
          return r.json();
        })
        .then(function (w) {
          var state = "nosignal";
          if (w.ok) {
            if (w.message === "Over capacity") state = "over";
            else if (w.message === "Platform empty") state = "empty";
            else state = w.stable ? "stable" : "settling";
          }
          scale.dataset.state = state;
          kgEl.textContent = fmtKg(w.kg);
          mtEl.textContent = w.kg === null ? "— MT" : (w.kg / 1000).toFixed(3) + " MT";
          statusEl.textContent = w.message;
          rawEl.textContent = w.source === "simulator" ? "SIMULATOR" : (w.raw || "");
          setButtons(state === "stable");
        })
        .catch(function () {
          scale.dataset.state = "nosignal";
          statusEl.textContent = "App not responding";
          setButtons(false);
        })
        .finally(function () { setTimeout(poll, 500); });
    }
    setButtons(false);
    poll();

    // --- vehicle lookup ------------------------------------------------------
    var plate = document.getElementById("vehicle_no");
    var info = document.getElementById("vehicle-info");
    var modeBox = document.getElementById("mode-box");
    var tareText = document.getElementById("tare-text");
    var party = document.getElementById("party_id");
    var material = document.getElementById("material_id");
    var timer = null;

    function setInfo(text, kind) {
      info.hidden = !text;
      info.className = "vehicle-info" + (kind ? " " + kind : "");
      info.textContent = text || "";
    }

    function lookup() {
      var value = plate.value.trim();
      if (value.length < 6) { setInfo(""); modeBox.hidden = true; return; }
      fetch("/api/vehicle?number=" + encodeURIComponent(value), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (v) {
          modeBox.hidden = true;
          if (!v.valid) { setInfo(v.message, "bad"); return; }
          if (!v.known) { setInfo(v.number + " is new. It will be added when you capture the weight.", ""); return; }
          if (v.blocked) { setInfo(v.number + " is blocked. Ask the supervisor.", "bad"); return; }
          if (v.open_ticket) {
            var t = v.open_ticket;
            setInfo("Second weighment for ticket " + t.ticket_no + ". First weight " + fmtKg(t.first_kg) +
                    " kg at " + t.first_at.slice(11, 16) + ".", "warn");
            if (t.party_id && party) party.value = String(t.party_id);
            if (t.material_id && material) material.value = String(t.material_id);
            return;
          }
          var text = v.number + (v.transporter ? " · " + v.transporter : "") + ". First weighment.";
          if (v.stored_tare_valid) {
            modeBox.hidden = false;
            tareText.textContent = "(" + fmtKg(v.stored_tare_kg) + " kg, taken " + v.tare_updated_at.slice(0, 10) + ")";
          }
          setInfo(text, "");
          if (v.last_party_id && party && !party.value) party.value = String(v.last_party_id);
          if (v.last_material_id && material && !material.value) material.value = String(v.last_material_id);
        })
        .catch(function () { setInfo(""); });
    }
    plate.addEventListener("input", function () {
      plate.value = plate.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
      clearTimeout(timer);
      timer = setTimeout(lookup, 300);
    });
    document.querySelectorAll(".plate-pick").forEach(function (b) {
      b.addEventListener("click", function () { plate.value = b.dataset.plate; lookup(); plate.focus(); });
    });

    // Prevent double submits while the server records the weighment.
    document.getElementById("weigh-form").addEventListener("submit", function () {
      setTimeout(function () { [captureBtn, tareBtn].forEach(function (b) { if (b) b.disabled = true; }); }, 0);
    });
  }
})();
