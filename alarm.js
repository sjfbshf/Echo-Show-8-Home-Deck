// Timer and alarm shared by the deck and the timer page.
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
    "@keyframes ring-shake{from{transform:rotate(-8deg)}to{transform:rotate(8deg) scale(1.05)}}";
  document.head.appendChild(css);

  // sound is made with Web Audio, so no file is needed; browsers may hold it until the first tap
  var ctx, beeping, over;
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
  function ring() {
    if (beeping) return;
    over = document.createElement("div");
    over.className = "ring-over";
    over.innerHTML = '<img src="https://api.iconify.design/fluent-emoji-flat/alarm-clock.svg" alt=""><div>Tap to stop</div>';
    document.body.appendChild(over);
    beeping = setInterval(beep, 1000); beep();
  }
  function stop() { clearInterval(beeping); beeping = null; if (over) over.remove(); over = null; }
  document.addEventListener("touchstart", unlock, { passive: true, capture: true });
  document.addEventListener("click", function (e) {
    unlock();
    if (beeping) { stop(); e.stopPropagation(); e.preventDefault(); }
  }, true);

  setInterval(function () {
    var now = Date.now();
    if (S.tEnd && now >= S.tEnd) {
      var late = now - S.tEnd;
      S.tEnd = 0; S.tLeft = S.tSet; save();
      if (late < 10 * 60000) ring();  // finished while another site was open: still ring if it's recent
    }
    var d = new Date(), m = d.getHours() * 60 + d.getMinutes(), stamp = d.toDateString() + " " + m;
    if (S.aOn && m === S.aMin && S.aFired !== stamp) { S.aFired = stamp; S.aOn = false; save(); ring(); }
    if (window.onAlarmTick) window.onAlarmTick();
  }, 250);

  window.Alarm = { S: S, save: save };
})();
