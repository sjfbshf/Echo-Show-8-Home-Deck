// A flick-to-pick wheel, used by the timer, the alarm and the calendar's event times.
// It's a snapping scroll list: flick it, or tap a row to bring it to the middle.
// onPick(i) runs once it settles. set(i) moves it without calling onPick.
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
    busy = true; paint();
    clearTimeout(settle);
    settle = setTimeout(function () {
      var i = at();
      if (Math.abs(box.scrollTop - i * H) > 1) box.scrollTo({ top: i * H, behavior: "smooth" });  // in case snapping isn't supported
      busy = false;
      if (i !== value) { value = i; onPick(i); }
    }, 160);
  }, { passive: true });
  box.addEventListener("touchstart", function () { busy = true; }, { passive: true });
  return {
    set: function (i) { if (busy) return; value = i; box.scrollTop = i * H; paint(); },
    get busy() { return busy; }
  };
}
