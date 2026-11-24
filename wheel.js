// A flick-to-pick wheel, used by the timer, the alarm and the calendar's event times.
// It's a snapping scroll list: flick it, or tap a row to bring it to the middle.
// onPick(i) runs once it settles. set(i) moves it without calling onPick.
// commit() takes the row in the middle right now (call it before Save or Start); cancel() drops a pending pick.
// H is the row height in px (the page's CSS must give the rows that height).
function Wheel(box, labels, onPick, H) {
  H = H || 76;
  var rows = [], settle, busy = false, value = 0;
  box.innerHTML = "";
  function pad() { box.appendChild(document.createElement("div")); }
  pad(); pad();
  labels.forEach(function (t, i) {
    var d = document.createElement("div"); d.textContent = t;
    d.onclick = function () { box.scrollTo({ top: i * H, behavior: "smooth" }); };
    box.appendChild(d); rows.push(d);
  });
  pad(); pad();
  function at() { return Math.max(0, Math.min(rows.length - 1, Math.round(box.scrollTop / H))); }
  function paint() {
    var i = at();
    rows.forEach(function (r, n) { r.className = n === i ? "mid" : Math.abs(n - i) === 1 ? "near" : ""; });
  }
  box.addEventListener("scroll", function () {
    if (!box.offsetParent) return;  // hiding a wheel resets its scroll to the top: that isn't a pick
    busy = true; paint();
    clearTimeout(settle);
    settle = setTimeout(function () {
      settle = null;
      if (!box.offsetParent) { busy = false; return; }
      var i = at();
      if (Math.abs(box.scrollTop - i * H) > 1) box.scrollTo({ top: i * H, behavior: "smooth" });  // in case snapping isn't supported
      busy = false;
      if (i !== value) { value = i; onPick(i); }
    }, 160);
  }, { passive: true });
  box.addEventListener("touchstart", function () { busy = true; }, { passive: true });
  // a touch that didn't scroll (a tap on the middle row) mustn't leave the wheel "busy" forever
  ["touchend", "touchcancel"].forEach(function (ev) {
    box.addEventListener(ev, function () { setTimeout(function () { if (!settle) busy = false; }, 250); }, { passive: true });
  });
  function commit() {
    clearTimeout(settle); settle = null; busy = false;
    var i = at();
    if (i !== value) { value = i; onPick(i); }
  }
  return {
    set: function (i) { if (busy) return; value = i; box.scrollTop = i * H; paint(); },
    commit: commit,
    cancel: function () { clearTimeout(settle); settle = null; busy = false; },
    get busy() { return busy; }
  };
}
