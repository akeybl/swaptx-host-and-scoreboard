// Shared helpers: WebSocket with auto-reconnect, formatting, vocabulary.
(function () {
  const W = window.SWX = window.SWX || {};

  W.connect = function (handlers) {
    let ws, backoff = 500, alive = false;
    function open() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => { backoff = 500; alive = true; handlers.onOpen && handlers.onOpen(); };
      ws.onmessage = (m) => {
        let msg; try { msg = JSON.parse(m.data); } catch (e) { return; }
        handlers.onMessage && handlers.onMessage(msg);
      };
      ws.onclose = () => { alive = false; handlers.onClose && handlers.onClose(); setTimeout(open, backoff); backoff = Math.min(backoff * 2, 8000); };
      ws.onerror = () => { try { ws.close(); } catch (e) {} };
    }
    open();
    setInterval(() => { if (alive && ws.readyState === 1) ws.send("ping"); }, 15000);
    return { isAlive: () => alive };
  };

  W.api = async function (path, opts) {
    const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    if (!r.ok) { let t = await r.text(); throw new Error(t || r.statusText); }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  };

  W.mmss = function (s, showHours) {
    if (s == null || isNaN(s)) return "--:--";
    const neg = s < 0; s = Math.abs(Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const core = (h || showHours) ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
    return (neg ? "-" : "") + core;
  };
  W.clock = function (ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  };
  W.esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); };
  W.teamClass = function (t) { return (t === 0 || t === 1 || t === 2 || t === 3) ? `team-${t}` : "team-n"; };
  W.teamVar = function (t) { return (t === 0 || t === 1 || t === 2 || t === 3) ? `var(--t${t})` : "var(--tn)"; };
  W.TEAM_NAMES = ["Red", "Blue", "Free for all", "Green"];   // the Evolver's picks; token 2 (yellow) is solo

  // One vocabulary: the guns' own. Their screens count kills and deaths (reload twice to see
  // them), the radio only ever reports a death (never single hits or damage), lives are hearts,
  // and with no hearts left you're out.
  W.WORDS = { K: "KILLS", D: "DEATHS", kills: "KILLS", deaths: "DEATHS", kill: "KILL", ratio: "RATIO",
    first_blood: "FIRST KILL", tagged: "is out", out: "OUT", streak: "STREAK", scored: "got a kill" };
  W.n = function (count, one, many) { return `${count} ${count === 1 ? one : (many || one + "s")}`; };   // 1 KO, 3 KOs
  W.wording = function () { return W.WORDS; };
})();
