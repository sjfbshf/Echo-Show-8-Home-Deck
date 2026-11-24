// Timer, alarm and calendar reminders, shared by every deck page.
// Their state lives in localStorage, so they keep counting on either page,
// and whichever page is open rings (a full-screen overlay plus a beep; tap to stop).
// They can't ring while the Echo shows another website (like Google); they ring when a deck page opens again,
// if it's soon enough.
(function () {
  // aAt: when the alarm next rings (ms); worked out when it's turned on, and cleared when it's off or moved
  var S = { tSet: 300, tLeft: 300, tEnd: 0, aMin: 420, aOn: false, aAt: 0 };
  try { Object.assign(S, JSON.parse(localStorage.getItem("deckAlarm") || "{}")); } catch (e) {}
  function save() { try { localStorage.setItem("deckAlarm", JSON.stringify(S)); } catch (e) {} }  // a failed save must never stop a ring

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
  // what = { title, sub } for a calendar reminder; nothing for the timer and alarm.
  // Alerts that come while another is showing wait their turn; each shows after the one before is tapped.
  // A reminder that has gone quiet (no more beeping) steps aside straight away for a new one.
  var queue = [];
  function ring(what) {
    if (beeping === -1) stop(true);
    if (beeping) { queue.push(what); return; }
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
  function stop(keepQueue) {
    clearInterval(beeping); clearTimeout(quiet); beeping = null; if (over) over.remove(); over = null;
    if (!keepQueue && queue.length) setTimeout(function () { if (!beeping && queue.length) ring(queue.shift()); }, 400);
  }
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
    var list, due = [], changed = false;
    try { list = JSON.parse(localStorage.getItem("deckEvents") || "[]"); } catch (e) { return; }
    list.forEach(function (e) {
      if (e.fired || e.remind == null || e.remind === -1) return;  // -1 = no reminder
      var at = e.at - e.remind * 60000;
      if (now < at) return;
      e.fired = true; changed = true;
      // rings if it's under 10 minutes late, and (for timed events) before the event starts. All-day reminders can
      // fall after midnight ("morning of"), so those only need to be on time.
      if (now - at < 10 * 60000 && (e.remind <= 0 || e.allDay || now < e.at)) due.push(e);
    });
    if (!changed) return;
    try { localStorage.setItem("deckEvents", JSON.stringify(list)); } catch (e) {}
    due.forEach(function (due) {  // every reminder that's due rings, one after another
      var start = new Date(due.at), mins = Math.round((due.at - now) / 60000);
      if (due.allDay) {  // "Today · all day" or "Tomorrow · all day"
        var days = Math.round((new Date(start).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86400000);
        ring({ title: due.title, sub: (days === 0 ? "Today" : days === 1 ? "Tomorrow" : start.toLocaleDateString([], { weekday: "long" })) + " · all day" });
        return;
      }
      var when = (start.toDateString() === new Date(now).toDateString() ? "" : start.toLocaleDateString([], { weekday: "long" }) + " ") +
        start.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      ring({ title: due.title, sub: mins > 0 ? when + " · in " + (mins >= 60 ? Math.floor(mins / 60) + " hr" + (mins % 60 ? " " + mins % 60 + " min" : "") : mins + " min") : "Starting now · " + when });
    });
    dispatchEvent(new Event("deckreminder"));
  }

  setInterval(function () {
    var now = Date.now();
    if (S.tEnd && now >= S.tEnd) {
      var late = now - S.tEnd;
      S.tEnd = 0; S.tLeft = S.tSet; save();
      if (late < 10 * 60000) ring();  // finished while another site was open: still ring if it's recent
    }
    // the alarm rings at its set time, or as soon as a deck page is open again within 30 minutes after it
    // (the screen asleep or showing Google at 7:00 no longer pushes it to tomorrow)
    if (S.aOn && !S.aAt) { S.aAt = Alarm.next(S.aMin); save(); }
    if (!S.aOn && S.aAt) { S.aAt = 0; save(); }
    if (S.aOn && now >= S.aAt) {
      var lateA = now - S.aAt;
      S.aOn = false; S.aAt = 0; save();
      if (lateA < 30 * 60000) ring();
    }
    if (now - lastCheck > 1000) { lastCheck = now; reminders(now); }
    if (window.onAlarmTick) window.onAlarmTick();
  }, 250);

  window.Alarm = {
    S: S, save: save,
    next: function (min) {  // the next time the clock shows this minute of the day, as ms
      var d = new Date(), t = new Date(d.getFullYear(), d.getMonth(), d.getDate(), Math.floor(min / 60), min % 60);
      if (+t <= +d) t = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1, Math.floor(min / 60), min % 60);
      return +t;
    }
  };
})();
