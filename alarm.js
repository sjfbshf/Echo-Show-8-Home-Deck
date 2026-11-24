// Timer, alarm and calendar reminders, shared by every deck page.
// Their state lives in localStorage, so they keep counting on either page,
// and whichever page is open rings (a full-screen overlay plus a beep; tap to stop).
// They can't ring while the Echo shows another website (Google, Calendar...).
(function () {
  var S = { tSet: 300, tLeft: 300, tEnd: 0, aMin: 420, aOn: false, aFired: "" };
  try { Object.assign(S, JSON.parse(localStorage.getItem("deckAlarm") || "{}")); } catch (e) {}
  function save() { localStorage.setItem("deckAlarm", JSON.stringify(S)); }

  var css = document.createElement("style");
  css.textContent =
    ".ring-over{position:fixed;inset:0;z-index:99;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px;" +
    "background:rgba(7,11,18,.94);color:#f2b84b;font:500 28px Sora,'Segoe UI',sans-serif;letter-spacing:.2em;text-transform:uppercase}" +
    ".ring-over img{width:260px;height:260px;animation:ring-shake .5s ease-in-out infinite alternate}" +
    ".ring-over .ring-title{font-size:54px;letter-spacing:0;text-transform:none;color:#eef2f8;text-align:center;max-width:1000px}" +
    ".ring-over .ring-sub{font-size:24px;letter-spacing:.04em;text-transform:none;color:#7f8aa0;margin-bottom:20px}" +
    "@keyframes ring-shake{from{transform:rotate(-8deg)}to{transform:rotate(8deg) scale(1.05)}}";
  document.head.appendChild(css);

  // sound is made with Web Audio, so no file is needed; browsers may hold it until the first tap
  var ctx, beeping, over, quiet;
  function unlock() {
    try {
      if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
    } catch (e) {}
  }
  // loud beep: square waves (much brighter than a plain sine) at full volume, three quick beeps per burst,
  // through a compressor so it's as loud as possible without crackling. The Echo's own volume still caps it.
  var out;
  function tone(t, freq) {
    var g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(1, t + 0.01);
    g.gain.setValueAtTime(1, t + 0.13); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
    g.connect(out);
    [freq, freq * 1.5].forEach(function (f) {
      var o = ctx.createOscillator(); o.type = "square"; o.frequency.value = f;
      o.connect(g); o.start(t); o.stop(t + 0.17);
    });
  }
  function beep() {
    unlock(); if (!ctx) return;
    if (!out) {
      var comp = ctx.createDynamicsCompressor(); comp.threshold.value = -20; comp.ratio.value = 12;
      out = ctx.createGain(); out.gain.value = 2.5;
      out.connect(comp); comp.connect(ctx.destination);
    }
    var t = ctx.currentTime;
    tone(t, 1760); tone(t + 0.2, 1760); tone(t + 0.4, 1760);
  }
  // what = { icon, title, sub } for a calendar reminder; nothing for the timer and alarm
  function ring(what) {
    if (beeping) return;
    over = document.createElement("div");
    over.className = "ring-over";
    over.innerHTML = '<img src="https://api.iconify.design/fluent-emoji-flat/' + (what ? "spiral-calendar" : "alarm-clock") + '.svg" alt="">';
    if (what) {
      var t = document.createElement("div"); t.className = "ring-title"; t.textContent = what.title; over.appendChild(t);
      var s = document.createElement("div"); s.className = "ring-sub"; s.textContent = what.sub; over.appendChild(s);
    }
    var tap = document.createElement("div"); tap.textContent = "Tap to stop"; over.appendChild(tap);
    document.body.appendChild(over);
    beeping = setInterval(beep, 1000); beep();
    if (what) quiet = setTimeout(function () { clearInterval(beeping); beeping = -1; }, 120000);  // a reminder beeps for 2 minutes, then waits quietly to be tapped
  }
  function stop() { clearInterval(beeping); clearTimeout(quiet); beeping = null; if (over) over.remove(); over = null; }
  document.addEventListener("touchstart", unlock, { passive: true, capture: true });
  document.addEventListener("click", function (e) {
    unlock();
    if (beeping) { stop(); e.stopPropagation(); e.preventDefault(); }
  }, true);

  // calendar reminders: events added on the calendar page live in localStorage "deckEvents".
  // Each rings once, at its start time minus its "remind" minutes. One missed while the Echo was off or on
  // another website still rings if it's less than 10 minutes late (and the event hasn't started yet).
  var lastCheck = 0;
  function reminders(now) {
    var list, due = null, changed = false;
    if (beeping) return;  // something is already ringing: check again once it's stopped
    try { list = JSON.parse(localStorage.getItem("deckEvents") || "[]"); } catch (e) { return; }
    list.forEach(function (e) {
      if (e.fired || e.remind < 0) return;
      var at = e.at - e.remind * 60000;
      if (now < at) return;
      e.fired = true; changed = true;
      if (now - at < 10 * 60000 && (e.remind === 0 || now < e.at)) due = e;
    });
    if (!changed) return;
    localStorage.setItem("deckEvents", JSON.stringify(list));
    if (due) {
      var start = new Date(due.at), mins = Math.round((due.at - now) / 60000);
      var when = (start.toDateString() === new Date(now).toDateString() ? "" : start.toLocaleDateString([], { weekday: "long" }) + " ") +
        start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      ring({ title: due.title, sub: mins > 0 ? when + " · in " + (mins >= 60 ? Math.floor(mins / 60) + " hr" + (mins % 60 ? " " + mins % 60 + " min" : "") : mins + " min") : "Starting now · " + when });
    }
    dispatchEvent(new Event("deckreminder"));
  }

  setInterval(function () {
    var now = Date.now();
    if (S.tEnd && now >= S.tEnd) {
      var late = now - S.tEnd;
      S.tEnd = 0; S.tLeft = S.tSet; save();
      if (late < 10 * 60000) ring();  // finished while another site was open: still ring if it's recent
    }
    var d = new Date(), m = d.getHours() * 60 + d.getMinutes(), stamp = d.toDateString() + " " + m;
    if (S.aOn && m === S.aMin && S.aFired !== stamp) { S.aFired = stamp; S.aOn = false; save(); ring(); }
    if (now - lastCheck > 1000) { lastCheck = now; reminders(now); }
    if (window.onAlarmTick) window.onAlarmTick();
  }, 250);

  window.Alarm = { S: S, save: save };
})();
