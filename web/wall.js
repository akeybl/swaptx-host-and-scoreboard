// SWAPTX Arena - wall / projector view.
(function () {
  const $ = (id) => document.getElementById(id);
  const W = window.SWX;
  let snap = null, protocol = null, clockBase = null, lastSeenEventId = 0;
  let timelineMode = false, sound = localStorage.getItem("swx_sound") === "1";
  // Rear projection: mirror the whole wall so it reads right from the other side of the screen.
  // Remembered by this browser; ?mirror=1 / ?mirror=0 in the address sets it for the projector machine.
  const mirrorParam = new URLSearchParams(location.search).get("mirror");
  let mirror = false;
  try {
    if (mirrorParam != null) localStorage.setItem("swx_mirror", mirrorParam === "0" || mirrorParam === "off" ? "0" : "1");
    mirror = localStorage.getItem("swx_mirror") === "1";
  } catch (e) { mirror = mirrorParam != null && mirrorParam !== "0" && mirrorParam !== "off"; }
  const applyMirror = () => document.documentElement.classList.toggle("mirror", mirror);
  applyMirror();
  // A phone gets the pocket view: status and clock, the settings in force, team scores, dongle.
  const phoneMq = window.matchMedia ? window.matchMedia("(max-width: 700px)") : null;
  const isPhone = () => !!(phoneMq && phoneMq.matches);
  const applyPhone = () => document.documentElement.classList.toggle("phone", isPhone());
  applyPhone();
  if (phoneMq && phoneMq.addEventListener) phoneMq.addEventListener("change", () => { applyPhone(); if (snap) render(); });
  let view = new URLSearchParams(location.search).get("view") || "board";   // board | stats
  const players = () => new Map((snap?.players || []).map(p => [p.number, p]));

  // ---------------------------------------------------------------- sound
  let audio;
  function beep(seq) {
    if (!sound) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      let t = audio.currentTime;
      for (const [f, d] of seq) {
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = "square"; o.frequency.value = f; o.connect(g); g.connect(audio.destination);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, t + d); o.start(t); o.stop(t + d + 0.02); t += d;
      }
    } catch (e) {}
  }
  const SFX = { kill: [[880, .07], [1320, .09]], callout: [[660, .1], [880, .1], [1320, .18]], start: [[440, .12], [554, .12], [659, .12], [880, .3]], over: [[880, .15], [659, .15], [554, .15], [440, .4]], join: [[600, .06]] };

  // ---------------------------------------------------------------- render
  // In Free for All every player gets a distinct colour (there are no teams). Everywhere else
  // colours come from the team. pColor/pColorN resolve the right one for the current game.
  // Lone-wolf colours: none of them red, blue, green or yellow, which are the team picks.
  const FFA_COLORS = ["#b57bff","#2ad6c8","#ff9a3d","#ff6fae","#6fd0ff","#e05cd0","#ffb56b","#4de3ff",
    "#c77dff","#ff8c42","#7ef0e0","#ff5c7a","#9b8cff","#ff9de2","#d4a5ff","#ffc3a0"];
  const isSolo = () => snap && snap.game && snap.game.kind === "royale" && !snap.game.squads;   // a royale with no colours: everyone for themselves
  // A free-for-all player is a side of one: their own colour everywhere, in every layout.
  const lone = (t) => t === 2;
  function pColor(p) { return (isSolo() || lone(p.effective_team)) ? FFA_COLORS[(p.number - 1) % 16] : W.teamVar(p.effective_team); }
  function pColorN(n) { const p = players().get(n); if (isSolo() || (p && lone(p.effective_team))) return FFA_COLORS[(n - 1) % 16]; return W.teamVar(p ? p.effective_team : null); }
  function nameOf(n) { const p = players().get(n); return p ? p.display_name : `Player ${n}`; }
  const teamName = (t) => (snap && snap.team_names && snap.team_names[String(t)]) || W.TEAM_NAMES[t];
  const teamCustom = (t) => !!(snap && snap.team_custom && snap.team_custom[String(t)]);
  function teamOf(n) { const p = players().get(n); return p ? p.effective_team : null; }
  function nameHtml(n, cls) {
    return `<b class="${cls || ""}" data-pn="${n}" style="color:${pColorN(n)}">${W.esc(nameOf(n))}</b>`;
  }

  function render() {
    if (!snap) return;
    const g = snap.game, wd = W.WORDS;
    const wall = $("wall");
    wall.className = `wall phase-${g.phase}`;
    // top bar
    const kind = g.kind || (g.mode === 1 ? "royale" : "team");
    // the host's mode is the game; "free for all" is a layout (everyone picked the lone-wolf colour), not a mode
    $("modeBadge").textContent = g.phase === "idle" ? "SWAPTX ARENA" : (g.arcade ? "ARCADE" : (g.mode_label || g.kind_label || "GAME"));
    const nTeams = (snap.teams || []).length, teamBoard = g.phase === "live" && view === "board" && kind === "team";
    wall.classList.toggle("feed-narrow", teamBoard && nTeams === 3);
    wall.classList.toggle("feed-narrower", teamBoard && nTeams >= 4);
    const chips = [];
    const s = g.settings || {};
    if (s.lives_label) {
      // one host setting, two meanings: hit points in Battle Royale, hearts (lives) everywhere else
      const cfg = snap.config || {};
      chips.push(g.kind === "royale" ? `${s.lives === 0 ? (cfg.hp_low || 200) : (cfg.hp_high || 500)} HP`
                                     : (s.lives === 0 ? `${cfg.lives_low || 5} LIVES` : "UNLIMITED LIVES"));
    }
    const houseMins = kind !== "royale" && g.rules && g.rules.time_limit_s ? Math.round(g.rules.time_limit_s / 60) : 0;
    if (s.time_label) chips.push(kind === "royale" ? (s.time === 1 ? "STORM ON" : "NO STORM") : (houseMins ? `${houseMins} MIN TIMER` : (s.time === 1 ? "10 MIN TIMER" : "UNTIMED")));
    if (s.lighting_label) chips.push(s.lighting === 1 ? "INDOOR" : "OUTDOOR");
    if (g.rules && g.rules.label) chips.push(g.rules.label.toUpperCase());
    if (g.rules && g.rules.team_kill_target) chips.push(`FIRST TO ${W.n(g.rules.team_kill_target, "KILL", "KILLS")}`);
    if (g.rules && g.rules.player_kill_target) chips.push(`FIRST PLAYER TO ${g.rules.player_kill_target}`);
    const host = snap.host || {};
    // Hosting: the settings are toggles, and the mode toggle stands in for the big badge. When the
    // game starts the same controls collapse down to their chosen values (the elements stay, only
    // a class changes, so the collapse animates). When a gun hosts, the header is the plain chip list.
    const toggles = !!host.enabled;                     // a phone gets the same toggles, sized for a thumb
    // the mode toggle stands in for the big badge; when Royale is hidden there is no mode toggle, so the badge stays
    const modeToggle = toggles && !(snap.config && snap.config.hide_royale);
    $("modeBadge").style.display = modeToggle ? "none" : "";
    if (toggles) {
      setHtml($("settingChips"), hostChipsHtml(host.settings || {}, host.rules || {}));
      $("settingChips").classList.toggle("collapsed", g.phase === "live");
    } else {
      $("settingChips").classList.remove("collapsed");
      setHtml($("settingChips"), chips.map(c => `<span class="chip on">${W.esc(c)}</span>`).join(""));
    }
    balanceChips();
    renderHostControls(g, host);
    $("phaseText").textContent = { idle: "IDLE", lobby: "LOBBY", live: "LIVE", ended: "GAME OVER" }[g.phase] || g.phase.toUpperCase();
    const inGame = g.players_in_game, alive = g.players_alive;
    $("aliveCount").textContent = g.phase === "live" ? (kind === "royale" ? `${alive} / ${inGame} STANDING` : (statusMatters((snap.players || []).filter(p => p.in_game)) ? `${alive} / ${inGame} IN · ${W.n(g.total_kills, wd.kill, wd.kills)}` : `${W.n(inGame, "PLAYER", "PLAYERS")} · ${W.n(g.total_kills, wd.kill, wd.kills)}`)) : (g.phase === "lobby" ? `${inGame} JOINED` : "");
    renderClock();
    // board
    const board = $("board");
    let html;
    if (g.phase === "idle") html = idleHtml();
    else if (g.phase === "lobby") html = lobbyHtml();
    else if (view === "stats") html = statsHtml(wd);
    else if (kind === "royale" && !snap.game.squads) html = royaleHtml(wd);
    else html = teamsHtml(wd);
    // The server ticks a couple of times a second. Only rebuild the board when what it shows has
    // actually changed: a rebuild re-runs fonts, fitting and the move animations, and a wall that
    // is redrawn twice a second for nothing looks like it is jittering.
    if (html !== lastBoardHtml) {
      const before = boardPositions();
      const beforeCols = new Set([...board.querySelectorAll(".team-col")].map(c => c.dataset.key));
      board.innerHTML = html;
      lastBoardHtml = html;
      // a change of screen (idle -> lobby -> live) fades in instead of cutting
      const screen = board.firstElementChild ? board.firstElementChild.className.split(" ")[0] : "";
      if (boardDrawn && screen !== lastView && board.firstElementChild) board.firstElementChild.classList.add("view-enter");
      lastView = screen;
      fitRows();
      animateBoard(before);
      boardDrawn = true;
      bumpChanges();
      fitNames(board);
      refineInkFromBoard();
      alignRankPills();
      growNewColumns(beforeCols);
      scheduleRefit();
    }
    // overlay
    const ov = $("overlay");
    if (g.phase === "ended" && g.summary && !timelineMode) {
      const sig = String(g.id) + "|" + JSON.stringify((g.summary.players || []).map(p => [p.number, p.display_name, p.kills, p.deaths, p.effective_team])) + "|" + (g.winner_label || "");
      const fresh = ov.classList.contains("hidden") || ov.dataset.game !== String(g.id);
      if (fresh || ov.dataset.sig !== sig) {
        ov.innerHTML = gameOverHtml(g.summary, wd);
        ov.dataset.game = String(g.id); ov.dataset.sig = sig;
        ov.classList.toggle("dense", (g.summary.players || []).length > 8);
        if (!fresh) ov.querySelectorAll(".go-title,.awards,.final").forEach(el => el.style.animation = "none");
      }
      ov.classList.remove("hidden");
      fitRows();
      fitNames(ov);
    }
    else ov.classList.add("hidden");
    renderFeed(wd);
    if (timelineMode) renderTimelineFull(wd);
    renderStatus();
  }

  // Setting chips wrap like balanced text: if they need two lines, spread them 2+2 rather than 3+1.
  function balanceChips() {
    const box = $("settingChips");
    box.style.maxWidth = "";
    if (box.querySelector(".hc-wrap")) return;                // the host toggles keep their own constant layout
    const chips = [...box.children];
    if (chips.length < 2) return;
    const gap = parseFloat(getComputedStyle(box).columnGap) || 0;
    const chipH = chips[0].offsetHeight;
    const linesOf = () => Math.max(1, Math.round((box.offsetHeight + gap) / (chipH + gap)));
    const lines = linesOf();
    if (lines <= 1) return;
    const total = chips.reduce((s, c) => s + c.offsetWidth, 0) + gap * (chips.length - 1);
    let width = total / lines;
    for (let i = 0; i < 12; i++) {                 // widen a little at a time until the line count holds
      box.style.maxWidth = Math.ceil(width) + "px";
      if (linesOf() <= lines) break;
      width *= 1.04;
    }
  }

  function renderClock() {
    if (!snap) return;
    const g = snap.game, c = g.clock || {};
    const el = $("clock"), lbl = $("clockLabel");
    el.className = "clock";
    const bar = $("clockBar"), fill = $("clockFill");
    const barOn = !!(c.limit_s && c.elapsed_s != null && g.phase === "live");
    if (bar) bar.classList.toggle("on", barOn);
    if (g.phase === "idle") { el.textContent = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); lbl.textContent = "WAITING FOR A GAME"; return; }
    if (g.phase === "lobby") { el.textContent = "READY"; lbl.textContent = "PICK TEAMS · HOST STARTS"; return; }
    let elapsed = c.elapsed_s;
    if (g.phase === "live" && clockBase) elapsed = clockBase.elapsed + (Date.now() - clockBase.at) / 1000;
    if (elapsed == null) { el.textContent = "--:--"; lbl.textContent = ""; return; }
    if (c.limit_s) {
      const left = c.limit_s - elapsed;
      if (fill) fill.style.width = `${Math.max(0, Math.min(100, 100 * left / c.limit_s))}%`;
      el.textContent = W.mmss(left);
      if (left <= 0) { el.textContent = g.phase === "ended" ? W.mmss(Math.min(elapsed, c.limit_s)) : "0:00"; el.classList.add("over"); lbl.textContent = g.phase === "ended" ? (g.end_reason === "time_expired" ? "TIME'S UP · FINAL" : "FINAL TIME") : "TIME! WAITING FOR HOST"; }
      else { if (left < 60) el.classList.add("warn"); lbl.textContent = g.phase === "ended" ? "TIME LEFT AT END" : "TIME LEFT"; }
    } else if (c.storm && c.storm_at_s && g.phase === "live") {
      // Battle Royale with the storm: the guns run it silently from the start beacon, so the
      // board counts down to it, then up through it, and flags each siren
      const toStorm = c.storm_at_s - elapsed;
      if (toStorm > 0) {
        el.textContent = W.mmss(toStorm);
        if (toStorm < 30) el.classList.add("warn");
        lbl.textContent = "STORM IN";
      } else {
        const inStorm = -toStorm;
        const toSiren = c.storm_siren_s ? c.storm_siren_s - (inStorm % c.storm_siren_s) : null;
        el.textContent = W.mmss(inStorm);
        el.classList.add("storm");
        lbl.textContent = toSiren != null && toSiren <= 10 ? "STORM · SIREN" : "IN THE STORM";
      }
    } else {
      el.textContent = W.mmss(elapsed);
      lbl.textContent = g.phase === "ended" ? "FINAL TIME" : (c.storm ? "ELAPSED · STORM MODE" : "ELAPSED");
    }
    if (c.stale && g.phase === "live") lbl.textContent = "NO RADIO TRAFFIC - GAME STILL OPEN";
  }

  // ---- optical centering -------------------------------------------------------------
  // Display type (DIN Condensed) and the ♥ glyphs (a fallback font) sit at different heights
  // inside their line boxes, so "centered" boxes still look off. Measure the real ink of each
  // in this browser once and push it to the true centre via CSS variables.
  function calibrateInk() {
    try {
      const c = document.createElement("canvas").getContext("2d");
      const host = document.createElement("div");
      host.style.cssText = "position:absolute;left:-99999px;top:0;visibility:hidden;pointer-events:none";
      document.body.appendChild(host);
      // Render a probe in the exact CSS context, find the real baseline with a zero-size
      // inline-block marker, then compare the glyph ink (canvas, same font) to the line centre.
      const measure = (family, weight, lineHeight, text) => {
        const el = document.createElement("div");
        el.style.cssText = `font-family:${family};font-weight:${weight};font-size:100px;line-height:${lineHeight};white-space:nowrap`;
        el.innerHTML = `${text}<span style="display:inline-block;width:0;height:0;vertical-align:baseline"></span>`;
        host.appendChild(el);
        const box = el.getBoundingClientRect(), mark = el.lastChild.getBoundingClientRect();
        const baseline = mark.top - box.top;
        c.font = `${weight} 100px ${family}`;
        const m = c.measureText(text);
        const inkCenter = baseline - (m.actualBoundingBoxAscent - m.actualBoundingBoxDescent) / 2;
        return (box.height / 2 - inkCenter) / 100;                // em to push DOWN so ink is centred
      };
      const cs = getComputedStyle(document.documentElement);
      const display = cs.getPropertyValue("--display"), body = cs.getPropertyValue("--body");
      const d = measure(display, 700, 1, "Alex4");
      const bd = measure(body, 800, 1.2, "IN");
      const h = measure(display, 700, 1.2, "\u2665\u2665");
      host.remove();
      const root = document.documentElement.style;
      root.setProperty("--nudge-display", d.toFixed(3) + "em");
      root.setProperty("--nudge-body", bd.toFixed(3) + "em");
      root.setProperty("--nudge-hearts", h.toFixed(3) + "em");
    } catch (e) {}
  }
  calibrateInk();
  // Fonts (notably the colour-emoji font behind 🔥) can finish loading after a fit was measured;
  // recalibrate and re-fit everything whenever that happens.
  if (document.fonts) {
    const refitAll = () => { calibrateInk(); if (snap) { fitRows(); fitNames($("board")); fitNames($("overlay")); refitFeed(true); refineInkFromBoard(); } };
    if (document.fonts.ready) document.fonts.ready.then(refitAll);
    document.fonts.addEventListener && document.fonts.addEventListener("loadingdone", refitAll);
  }

  // The standalone probe gets within a tenth of an em; the rest depends on how the real row
  // cells lay out. Once real rows exist, measure the residual in place and cancel it.
  function residualEm(el) {
    const s = getComputedStyle(el), text = el.textContent.trim();
    if (!text) return null;
    const row = el.closest(".r-row,.p-row,.s-row");
    if (!row) return null;
    const mk = document.createElement("span");
    mk.style.cssText = "display:inline-block;width:0;height:0;vertical-align:baseline";
    el.appendChild(mk);
    const baseline = mk.getBoundingClientRect().top;
    mk.remove();
    const c = document.createElement("canvas").getContext("2d");
    c.font = `${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
    const m = c.measureText(text);
    const inkCenter = baseline - (m.actualBoundingBoxAscent - m.actualBoundingBoxDescent) / 2;
    const rr = row.getBoundingClientRect();
    return (inkCenter - (rr.top + rr.height / 2)) / parseFloat(s.fontSize);     // +ve = ink sits low
  }
  // Each board type (team columns, royale/FFA list, stats table) lays its cells out a little
  // differently, so the residual is measured and cancelled per container, every render.
  function refineInkFromBoard() {
    const rootCs = getComputedStyle(document.documentElement);
    document.querySelectorAll("#board .teams, #board .royale, #board .stats").forEach(box => {
      const adjust = (key, sel) => {
        const el = box.querySelector(sel);
        box.style.removeProperty(`--nudge-${key}`);     // fall back to the page-wide value until measured
        if (!el || !el.getClientRects().length) return;  // hidden (the phone's rosters): nothing to measure
        const r = residualEm(el);
        if (r == null || !isFinite(r)) return;
        const base = parseFloat(rootCs.getPropertyValue(`--nudge-${key}`)) || 0;
        box.style.setProperty(`--nudge-${key}`, (base - r).toFixed(3) + "em");
      };
      adjust("display", ".p-k, .num[data-f=kills]");     // single-line cells only; names may carry a second line
      adjust("body", ".p-status > .alive, .p-status > .out");
      adjust("hearts", ".lives");
    });
  }

  // ---- motion: every change on the board animates ------------------------------
  let prevVals = new Map(), prevTeamScores = new Map(), prevAlive = "", boardDrawn = false, lastView = "";
  function boardPositions() {
    const m = new Map();
    document.querySelectorAll("#board [data-key]").forEach(el => m.set(el.dataset.key, el.getBoundingClientRect()));
    return m;
  }
  function animateBoard(before) {
    document.querySelectorAll("#board [data-key]").forEach(el => {
      const was = before.get(el.dataset.key);
      if (!was) { if (boardDrawn) el.classList.add("enter"); return; }
      const now = el.getBoundingClientRect();
      const dx = was.left - now.left, dy = was.top - now.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      // Classic FLIP, done synchronously: park the element at its old spot, force the browser
      // to commit that style, then release it so the change transitions. No rAF, so it also
      // works when the tab is hidden or the frame loop is throttled.
      el.style.transition = "none";
      el.style.transform = `translate(${dx}px,${dy}px)`;
      el.classList.add("moving");                        // lifted, with a shadow, while it travels
      el.getBoundingClientRect();
      el.style.transition = "transform .9s cubic-bezier(.45,.05,.2,1)";
      el.style.transform = "";
      el.addEventListener("transitionend", () => el.classList.remove("moving"), { once: true });
    });
  }
  function bumpChanges() {
    const cur = new Map((snap.players || []).map(p => [p.number, { kills: p.kills, deaths: p.deaths, streak: p.streak,
      lives: p.eliminated ? "out" : String(p.lives_left), alive: p.alive }]));
    document.querySelectorAll("#board [data-n]").forEach(row => {
      const was = prevVals.get(+row.dataset.n), now = cur.get(+row.dataset.n);
      if (!was || !now) return;
      let changed = false;
      row.querySelectorAll("[data-f]").forEach(cell => { if (was[cell.dataset.f] !== now[cell.dataset.f]) { changed = true; cell.classList.add("bump"); } });
      if (changed) row.classList.add("changed");
    });
    prevVals = cur;
    const teams = new Map((snap.teams || []).map(t => [t.team, t.score]));
    document.querySelectorAll("#board .team-score[data-team]").forEach(el => {
      const t = +el.dataset.team;
      if (prevTeamScores.has(t) && prevTeamScores.get(t) !== teams.get(t)) el.classList.add("bump");
    });
    prevTeamScores = teams;
    const ac = $("aliveCount");
    if (prevAlive && ac.textContent !== prevAlive) { ac.classList.remove("bump"); void ac.offsetWidth; ac.classList.add("bump"); }
    prevAlive = ac.textContent;
  }
  // A glyph font (colour emoji, say) can arrive after a fit was measured; re-fit shortly after
  // every render so nothing is ever left clipped.
  let refitTimers = [];
  function scheduleRefit() {
    refitTimers.forEach(clearTimeout);
    refitTimers = [350, 1500].map(ms => setTimeout(() => { fitRows(); fitNames($("board")); fitNames($("overlay")); refitFeed(true); alignRankPills(); }, ms));
  }
  // Every player is always on screen: measure the space each list really has and set a
  // uniform row height from it; the type inside is sized off the row, so it shrinks with it.
  function fitRows() {
    if (isPhone()) return;                                // the pocket view has no rosters to fit
    const vmin = Math.min(innerWidth, innerHeight) / 100;
    const floor = 2.2 * vmin;
    const cols = [...document.querySelectorAll("#board .team-col")];
    if (cols.length) {
      let best = Infinity;
      cols.forEach(col => {
        col.style.removeProperty("--rowh");
        const roster = col.querySelector(".roster"), rows = col.querySelectorAll(".p-row");
        if (!roster || !rows.length) return;
        const cs = getComputedStyle(roster), gap = parseFloat(cs.rowGap) || 0;
        const head = roster.querySelector(".roster-head"), foot = col.querySelector(".team-foot");
        const colRect = col.getBoundingClientRect(), rosterRect = roster.getBoundingClientRect();
        const footH = foot ? foot.getBoundingClientRect().height + (parseFloat(getComputedStyle(foot).marginTop) || 0) + (parseFloat(getComputedStyle(foot).marginBottom) || 0) : 0;
        const avail = colRect.bottom - parseFloat(getComputedStyle(col).borderBottomWidth) - rosterRect.top
          - parseFloat(cs.paddingBottom) - (head ? head.offsetHeight : 0) - gap * rows.length - footH;
        const fit = avail / rows.length;
        if (fit < rows[0].offsetHeight) best = Math.min(best, fit);
      });
      if (best < Infinity) cols.forEach(col => col.style.setProperty("--rowh", Math.max(best, floor) + "px"));
    }
    document.querySelectorAll("#board .royale, #board .stats").forEach(box => {
      box.style.removeProperty("--rowh");
      const rows = box.querySelectorAll(".r-row, .s-row");
      if (!rows.length) return;
      const gap = parseFloat(getComputedStyle(box).rowGap) || 0;
      const avail = box.getBoundingClientRect().bottom - rows[0].getBoundingClientRect().top - gap * (rows.length - 1);
      const fit = avail / rows.length;
      if (fit < rows[0].offsetHeight) box.style.setProperty("--rowh", Math.max(fit, floor) + "px");
    });
    const ov = $("overlay"), fin = ov.querySelector(".final");
    if (fin && !ov.classList.contains("hidden")) {
      fin.style.removeProperty("--frowh");
      const rows = fin.querySelectorAll(".f-row");
      const cols = parseInt(getComputedStyle(fin).getPropertyValue("--cols")) || 2;
      const perCol = parseInt(fin.style.getPropertyValue("--per-col")) || Math.ceil(rows.length / cols);
      const ovCs = getComputedStyle(ov);
      const inner = ov.clientHeight - parseFloat(ovCs.paddingTop) - parseFloat(ovCs.paddingBottom);
      const gapOv = parseFloat(ovCs.rowGap) || 0;
      let used = 0, n = 0;
      [...ov.children].forEach(ch => { if (ch !== fin) { used += ch.offsetHeight; n++; } });
      const head = fin.querySelector(".f-head"), thead = fin.querySelector(".f-team-head");
      const gapFin = parseFloat(getComputedStyle(fin).rowGap) || 0;
      const avail = inner - used - gapOv * n - (head ? head.offsetHeight : 0) - (thead ? thead.offsetHeight + gapFin : 0) - gapFin * perCol;
      if (rows.length && perCol > 0) {
        const fit = avail / perCol;
        if (fit < rows[0].offsetHeight) fin.style.setProperty("--frowh", Math.max(fit, floor) + "px");
      }
    }
  }

  // A team column that appears mid-board grows from zero width in its place while the others
  // shrink, on the same curve as everything else. (Grid tracks interpolate when the count matches,
  // so the new column starts as a 0fr track and eases to 1fr.)
  function growNewColumns(beforeCols) {
    const teams = document.querySelector("#board .teams");
    if (!teams || !beforeCols.size) return;
    const cols = [...teams.children].filter(c => c.classList.contains("team-col"));
    const fresh = cols.filter(c => !beforeCols.has(c.dataset.key));
    if (!fresh.length || fresh.length === cols.length) return;
    fresh.forEach(c => { c.classList.remove("enter"); c.querySelectorAll(".enter").forEach(e => e.classList.remove("enter")); });
    if (beforeCols.has("col-placeholder") && fresh.length === 1 && !cols.some(c => c.dataset.key === "col-placeholder")) {
      fresh[0].classList.add("view-enter");        // the slot was already there: just fade the team in
      return;
    }
    teams.style.transition = "none";
    teams.style.gridTemplateColumns = cols.map(c => beforeCols.has(c.dataset.key) ? "1fr" : "0fr").join(" ");
    teams.getBoundingClientRect();
    teams.style.transition = "grid-template-columns .9s cubic-bezier(.45,.05,.2,1)";
    teams.style.gridTemplateColumns = cols.map(() => "1fr").join(" ");
  }

  // The rank pill's top edge lines up with the top of the score digits' ink, measured live.
  function alignRankPills() {
    const c = document.createElement("canvas").getContext("2d");
    document.querySelectorAll("#board .team-col").forEach(col => {
      const pill = col.querySelector(".team-rank"), score = col.querySelector(".team-score");
      if (!pill || !score) return;
      const s = getComputedStyle(score);
      const mk = document.createElement("span");
      mk.style.cssText = "display:inline-block;width:0;height:0;vertical-align:baseline";
      score.appendChild(mk);
      const baseline = mk.getBoundingClientRect().top;
      mk.remove();
      c.font = `${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
      const inkTop = baseline - c.measureText(score.textContent.trim() || "0").actualBoundingBoxAscent;
      const colTop = col.getBoundingClientRect().top + parseFloat(getComputedStyle(col).borderTopWidth);
      pill.style.top = Math.max(0, inkTop - colTop) + "px";
    });
  }

  // Names never truncate: shrink the type until the name fits its column.
  // Column-header rows shrink as a whole (one size for every heading) until each heading fits its cell.
  function fitHeads(scope) {
    scope.querySelectorAll(".roster-head, .s-head, .r-head").forEach(head => {
      head.style.fontSize = "";
      const start = parseFloat(getComputedStyle(head).fontSize);
      if (!(start > 0)) return;
      let ratio = 1;
      head.querySelectorAll("span").forEach(cell => {
        if (!cell.textContent.trim()) return;
        const r = document.createRange(); r.selectNodeContents(cell);
        const need = r.getBoundingClientRect().width, have = cell.getBoundingClientRect().width;
        if (need > have + 0.05 && need > 0) ratio = Math.min(ratio, have / need);
      });
      if (ratio < 1) head.style.fontSize = Math.max(start * 0.5, start * ratio * 0.97) + "px";
    });
  }

  // Shrink-to-fit, never truncate. Widths are measured fractionally on a hidden clone laid out
  // at its natural width: integer scrollWidth/clientWidth can agree while the text still
  // overruns by a fraction of a pixel, and Safari draws an ellipsis for exactly that case.
  function fitNames(scope) {
    scope.querySelectorAll(".p-name, .team-name, .award .an, .award .al, .award .av, .tile, .w-name").forEach(el => {
      el.style.fontSize = "";
      const cs = getComputedStyle(el);
      const start = parseFloat(cs.fontSize);
      const avail = el.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
        - parseFloat(cs.borderLeftWidth) - parseFloat(cs.borderRightWidth);
      if (!(start > 0) || !(avail > 0)) return;
      const probe = el.cloneNode(true);
      probe.removeAttribute("data-key");
      probe.style.cssText += ";position:absolute;left:0;top:0;visibility:hidden;pointer-events:none;width:max-content;max-width:none;min-width:0;overflow:visible;text-overflow:clip;padding:0;border:0;margin:0";
      el.parentNode.appendChild(probe);
      const natural = () => probe.getBoundingClientRect().width;
      let size = start, guard = 8, w = natural();
      // Content-sized boxes (tiles, team names) measure exactly equal to their text and must be
      // left alone; anything wider than its box, even by a fraction of a pixel, shrinks.
      while (w > avail + 0.05 && size > start * 0.5 && guard--) {
        size = Math.max(start * 0.5, size * Math.min(0.97, (avail - 1) / w));
        probe.style.fontSize = size + "px";
        w = natural();
      }
      probe.remove();
      if (size < start) el.style.fontSize = size + "px";
    });
    fitHeads(scope);
  }

  function idleHtml() {
    const last = (snap.recent_games || []).slice(-1)[0] || (snap.game.summary);
    let lg = "";
    if (last && last.started_at) {
      const mvp = (last.awards || []).find(a => a.key === "mvp");
      lg = `<div class="lastgame">LAST GAME: <b>${W.esc(last.winner_label || "no winner")}</b> · ${W.n(last.total_kills, W.WORDS.kill, W.WORDS.kills)} · ${W.mmss(last.duration_s)}${mvp ? ` · MVP <b>${W.esc(nameOf(mvp.player))}</b>` : ""}</div>`;
    }
    const d = snap.dongle || {};
    const listening = d.connected ? `LISTENING ON CHANNEL ${d.channel || "?"}` : "PLUG IN THE DONGLE";
    return `<div class="center"><div class="brand">SWAPTX ARENA</div><div class="sub">${listening}</div>${lg}</div>`;
  }

  function lobbyHtml() {
    const ps = (snap.players || []).filter(p => p.in_game);
    const tiles = ps.map(p => `<div class="tile" style="--tc:${pColor(p)}" data-key="l-${p.number}">${W.esc(p.display_name)}${isSolo() && !p.late_join ? "" : `<small>${isSolo() ? "" : (p.team_name ? p.team_name + (p.effective_team === 2 ? "" : " team") + (p.team_source === "carried" ? " (last game)" : "") : "team ?")}${p.late_join ? (isSolo() ? "late" : " · late") : ""}</small>`}</div>`).join("");
    const s = snap.game.settings || {};
    return `<div class="center"><div class="title">GET READY</div><div class="sub">${W.esc(s.mode_label || "GAME")} · <b>${ps.length}</b> PLAYERS ONLINE</div>${snap.host && snap.host.enabled && snap.host.awaiting_rejoin ? `<div class="sub warn">MODE CHANGED · SWITCH GUNS OFF AND ON IN ${W.esc((s.mode_label || "THE NEW MODE").toUpperCase())}</div>` : ""}<div class="tiles">${tiles || '<div class="sub">turn on headsets, then guns</div>'}</div></div>`;
  }

  // Free-for-all players inside a team game are each their own side: one stacked card per wolf,
  // in the player's own colour, ranked among the wolves.
  function wolvesHtml(list, wd, showStatus, maxRows) {
    list = list.sort(scoreSort);
    const ranks = rankLabels(list, scoreKey);
    const rows = list.map((p, i) => `<div class="p-row wolf ${p.eliminated ? "out" : ""} ${p.online ? "" : "offline"}" style="--tc:${pColor(p)}" data-n="${p.number}" data-key="w-${p.number}"><span class="p-rank">${ranks[i]}</span><span class="p-name">${W.esc(p.display_name)}</span><span class="p-k" data-f="kills">${p.kills}</span><span class="p-d" data-f="deaths">${p.deaths}</span><span class="p-status" data-f="lives">${livesHtml(p)}</span></div>`).join("");
    const foot = showStatus && list.length ? `<div class="team-foot">${list.filter(p => p.alive).length}/${list.length} IN</div>` : "";
    return `<div class="team-col wolves ${maxRows >= 6 ? "dense" : ""}" style="--tc:var(--t2);--rows:${maxRows}" data-key="col2"><div class="team-head"><div class="team-name">${teamName(2)}</div><div class="team-score lone"></div></div><div class="team-sub">EACH FOR THEMSELVES</div><div class="roster"><div class="roster-head"><span></span><span>PLAYER</span><span>${wd.K}</span><span>${wd.D}</span><span>STATUS</span></div>${rows}</div>${foot}</div>`;
  }

  // ---- host mode: this board as the admin headset ----------------------------------
  const hostPost = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }).catch(() => {});
  // What each host chip can cycle through. In Team Battle the time chip folds the guns' own
  // 10-minute timer and the board's house lengths into one choice, so they can never disagree;
  // a house length goes out on the air as "untimed" and the board ends the game itself.
  function hostOptions(hs, hr) {
    const royale = hs.mode === 1;
    return {
      mode: (snap && snap.config && snap.config.hide_royale) ? [[0, "TEAM BATTLE"]] : [[0, "TEAM BATTLE"], [1, "BATTLE ROYALE"]],
      lives: royale ? [[1, "500 HP"], [0, "200 HP"]]
                    : ((snap && snap.config && snap.config.hide_five_lives) ? [[1, "UNLIMITED LIVES"]] : [[1, "UNLIMITED LIVES"], [0, "5 LIVES"]]),
      time: royale ? [[0, "NO STORM"], [1, "STORM ON"]]
                   : [["gun", "10 MIN TIMER"], ["off", "UNTIMED"], [300, "5 MIN"], [420, "7 MIN"], [900, "15 MIN"], [1200, "20 MIN"]],
      lighting: [[0, "OUTDOOR"], [1, "INDOOR"]],
      target: [[0, "NO KILL TARGET"], [5, "FIRST TO 5"], [10, "FIRST TO 10"], [15, "FIRST TO 15"], [20, "FIRST TO 20"], [25, "FIRST TO 25"], [30, "FIRST TO 30"]],
    };
  }
  // The host's chips never move: two fixed rows, every chip as wide as its widest label, and the
  // length and kill target as steppers, so a click changes a word, never the layout.
  let hostMins = 5, hostKills = 10;      // what the steppers show while UNTIMED / NO TARGET is selected
  // drawn chevrons: a text ‹ › sits low in the face, a path is centred by geometry
  const CHEV = {
    left: '<svg viewBox="0 0 10 16" aria-hidden="true"><path d="M8 2 2 8l6 6"/></svg>',
    right: '<svg viewBox="0 0 10 16" aria-hidden="true"><path d="M2 2l6 6-6 6"/></svg>',
  };
  const MINS = [1, 60], KILLS = [1, 99], GUN_MINS = 10;
  const timeBody = (mins) => mins === GUN_MINS ? { time: 1, time_limit_s: 0 } : { time: 0, time_limit_s: mins * 60 };
  function hostChipsHtml(hs, hr) {
    const royale = hs.mode === 1;
    if (hr.time_limit_s) hostMins = Math.max(MINS[0], Math.min(MINS[1], Math.round(hr.time_limit_s / 60)));
    else if (hs.time === 1 && !royale) hostMins = GUN_MINS;                 // the guns' own timer shows as 10
    if (hr.team_kill_target) hostKills = Math.max(KILLS[0], Math.min(KILLS[1], hr.team_kill_target));
    const opts = hostOptions(hs, hr);
    const seg = (k, buttons, stepper) => `<span class="seg" data-k="${k}">${buttons}${stepper}</span>`;
    const btn = (k, v, label, on) => `<button class="segb ${on ? "on" : ""}" data-k="${k}" data-v="${v}">${label}</button>`;
    // every plain setting is a two-way segmented toggle built from its option list
    const toggle = (k) => seg(k, opts[k].map(([v, label]) => btn(k, v, label, hs[k] === v)).join(""), "");
    const step = (k, on, n, unit, pre) => `<span class="stepper ${on ? "on" : ""}" data-k="${k}"><button class="arrow" data-k="${k}" data-step="-1">${CHEV.left}</button>${pre ? `<i class="pre">${pre}</i>` : ""}<b class="val">${n}</b><i>${unit}</i><button class="arrow" data-k="${k}" data-step="1">${CHEV.right}</button></span>`;
    let time;
    if (royale) {
      time = seg("time", btn("time", 0, "NO STORM", hs.time !== 1) + btn("time", 1, "STORM ON", hs.time === 1), "");
    } else {
      // 10 on the stepper is the guns' own timer (time bit 1, no house length); any other number
      // is a board length sent to the guns as untimed
      const timed = !!hr.time_limit_s || hs.time === 1;
      time = seg("time", btn("time", "off", "UNTIMED", !timed), step("time", timed, hostMins, "MIN TIMER"));
    }
    const target = royale ? "" : seg("target", btn("target", 0, "NO TARGET", !hr.team_kill_target), step("target", !!hr.team_kill_target, hostKills, hostKills === 1 ? "KILL" : "KILLS", "FIRST TO"));
    // one wrapping row: every control keeps a constant width (both labels are always drawn), so
    // where the line breaks never depends on what is selected
    // ordered so the pairs pack: mode + light, lives + time, then the target. A hidden option
    // (Royale, five lives) takes its whole toggle with it: a one-way switch is nothing to show.
    const cfg = (snap && snap.config) || {};
    const modeT = cfg.hide_royale ? "" : toggle("mode");
    const livesT = (!royale && cfg.hide_five_lives) ? "" : toggle("lives");
    return `<div class="hc-wrap"><span class="hc-pair">${modeT}${toggle("lighting")}</span><span class="hc-pair">${livesT}${time}</span>${target}</div>`;
  }
  // the current value of a chip, and the settings call that selects a value
  function hostValue(k, hs, hr) {
    if (k === "target") return hr.team_kill_target || 0;
    if (k === "time" && hs.mode === 0) return hr.time_limit_s ? hr.time_limit_s : (hs.time === 1 ? "gun" : "off");
    return hs[k];
  }
  function hostBody(k, v, hs) {
    if (k === "target") return { team_kill_target: v };
    if (k === "time" && hs.mode === 0) return v === "gun" ? { time: 1, time_limit_s: 0 } : v === "off" ? { time: 0, time_limit_s: 0 } : { time: 0, time_limit_s: v };
    return { [k]: v };
  }
  // A kill-target bar: how far a side (or the leader) is from the house target.
  function progress(value, target, kind) {
    const pct = Math.max(0, Math.min(100, 100 * (value || 0) / target));
    return `<div class="target ${kind}"><div class="target-bar"><div class="target-fill" style="width:${pct}%"></div></div><span class="target-txt">${value || 0} / ${target}</span></div>`;
  }
  let armed = null;           // a destructive button asks for a second click within 3 s
  let lastHostHtml = null;
  let lastBoardHtml = null;   // the board is only rebuilt when its markup changes
  // The header sizes to its content; the game-over overlay starts where the header ends.
  const topbarEl = document.querySelector(".topbar");
  const syncTopbar = () => { if (topbarEl) document.documentElement.style.setProperty("--tb", topbarEl.offsetHeight + "px"); };
  if (topbarEl && window.ResizeObserver) new ResizeObserver(syncTopbar).observe(topbarEl);
  syncTopbar();
  // Never rebuild what a finger may be on: remember the string last set, since the browser's own
  // serialisation of it (self-closing SVG tags, attribute order) never compares equal to the source.
  const lastHtml = new WeakMap();
  const setHtml = (el, html) => { if (lastHtml.get(el) !== html) { el.innerHTML = html; lastHtml.set(el, html); } };
  function renderHostControls(g, host) {
    const box = $("hostCtl");
    if (!box) return;
    let html = "";
    if (!host.available && !host.enabled) html = "";
    else if (!host.enabled) html = `<button class="hostbtn quiet" data-act="host-on"><span>HOST A GAME</span></button>`;
    else if (g.phase === "live") {
      const arm = armed === "end";
      html = `<button class="hostbtn ${arm ? "arm" : ""}" data-act="end"><span>${arm ? "END GAME · SURE?" : "END GAME"}</span></button>`;
    } else {
      const arm = armed === "host-off";
      html = `<button class="hostbtn" data-act="start"><span>START GAME</span></button>` +
        `<button class="hostbtn quiet ${arm ? "arm" : ""}" data-act="host-off"><span>${arm ? "STOP HOSTING · SURE?" : "STOP HOSTING"}</span></button>`;
    }
    if (html !== lastHostHtml) { box.innerHTML = html; lastHostHtml = html; }
  }
  document.addEventListener("click", (ev) => {
    const hosting = snap && snap.host && snap.host.enabled && snap.game && snap.game.phase !== "live";
    const arrow = ev.target.closest && ev.target.closest(".stepper .arrow");
    if (arrow && hosting) {
      const k = arrow.dataset.k, d = +arrow.dataset.step;
      if (k === "time") { hostMins = Math.max(MINS[0], Math.min(MINS[1], hostMins + d)); hostPost("/api/host/settings", timeBody(hostMins)); }
      else { hostKills = Math.max(KILLS[0], Math.min(KILLS[1], hostKills + d)); hostPost("/api/host/settings", { team_kill_target: hostKills }); }
      return;
    }
    const segb = ev.target.closest && ev.target.closest(".segb");
    if (segb && hosting) {
      const k = segb.dataset.k, hs = snap.host.settings || {};
      const raw = segb.dataset.v, v = isNaN(+raw) ? raw : +raw;
      hostPost("/api/host/settings", hostBody(k, v, hs));
      return;
    }
    const num = ev.target.closest && ev.target.closest(".stepper");
    if (num && hosting && !num.classList.contains("on")) {          // tapping the number picks the custom value
      const k = num.dataset.k;
      hostPost("/api/host/settings", k === "time" ? timeBody(hostMins) : { team_kill_target: hostKills });
      return;
    }
    const chip = ev.target.closest && ev.target.closest(".chip.ctl");
    if (chip && snap && snap.host && snap.host.enabled) {
      const k = chip.dataset.k, hs = snap.host.settings || {}, hr = snap.host.rules || {}, opts = hostOptions(hs, hr)[k];
      if (!opts) return;
      const i = opts.findIndex(o => o[0] === hostValue(k, hs, hr));
      hostPost("/api/host/settings", hostBody(k, opts[(i + 1) % opts.length][0], hs));
      return;
    }
    const btn = ev.target.closest && ev.target.closest(".hostbtn");
    if (!btn) return;
    const act = btn.dataset.act;
    if (act === "host-on") { hostPost("/api/host", { enabled: true }); return; }
    if (act === "start") { hostPost("/api/host/start"); return; }
    if (act === "end" || act === "host-off") {
      if (armed !== act) { armed = act; renderHostControls(snap.game, snap.host); setTimeout(() => { if (armed === act) { armed = null; if (snap) renderHostControls(snap.game, snap.host); } }, 3000); return; }
      armed = null;
      if (act === "end") hostPost("/api/host/end"); else hostPost("/api/host", { enabled: false });
    }
  });

  // Status is only ever hearts, IN or OUT; the streak lives in a badge beside the name.
  function livesHtml(p) {
    if (p.eliminated) return `<span class="out">OUT</span>`;
    if (p.lives_left == null || (snap.game && snap.game.kind === "royale")) return `<span class="alive">IN</span>`;   // one life: no hearts to count
    const max = Math.max(p.lives_left, snap.config.lives_low || 5);
    let h = "";
    for (let i = 0; i < max; i++) h += `<span class="${i < p.lives_left ? "" : "off"}">♥</span>`;
    return `<span class="lives">${h}</span>`;
  }
  const statusMatters = (ps) => ps.some(p => p.lives_left != null || p.eliminated);
  // Competition ranking: the first of a tied group shows its rank, the rest are blank, and the
  // next distinct result picks up at its position (1, blank, 3 ...).
  const gameStarted = () => !!(snap && snap.game && snap.game.total_kills > 0);
  const rankLabels = (list, keyOf) => gameStarted() ? list.map((p, i) => (i > 0 && keyOf(p) === keyOf(list[i - 1])) ? "" : String(i + 1)) : list.map(() => "");
  const sameTop = (p, list, keyOf) => gameStarted() && list.length > 0 && keyOf(p) === keyOf(list[0]);
  // The one player out in front of a sorted list, or null when the top spot is shared (or nobody has scored).
  const soleLeaderOf = (list, keyOf) => (gameStarted() && list.length && list[0].kills > 0 && (list.length === 1 || keyOf(list[0]) !== keyOf(list[1]))) ? list[0].number : null;

  function teamsHtml(wd) {
    const ps = (snap.players || []).filter(p => p.in_game);
    const squads = !!(snap.game && snap.game.squads);     // Battle Royale in teams: one life, last squad standing
    const showStatus = statusMatters(ps) || squads;
    const rules = snap.game.rules || {};
    const byTeam = new Map();
    const unknown = [], other = [];   // other: tagged by the only colour heard so far, so on the other team
    for (const p of ps) {
      const t = p.effective_team;
      if (t == null) { ((p.team_not || []).length ? other : unknown).push(p); continue; }
      if (!byTeam.has(t)) byTeam.set(t, []);
      byTeam.get(t).push(p);
    }
    const teamRows = new Map((snap.teams || []).map(t => [t.team, t]));
    const scoreOf = (t) => teamRows.get(t)?.score ?? 0;
    // Columns never move: they sit in the order their colour was first heard this game (the backend
    // remembers it, so a reload agrees). Standing is a rank pill, and the leader is outlined.
    const seen = (snap.game && snap.game.team_order) || [];
    const pos = (t) => { const i = seen.indexOf(t); return i < 0 ? 100 + t : i; };
    const order = [...byTeam.keys()].sort((a, b) => pos(a) - pos(b) || a - b);
    const real = order.filter(t => t !== 2);        // free-for-all players are lone wolves, not a team
    // The backend ranks sides: a side with nobody left has lost whatever its kills, then kills.
    const rankOf = new Map(real.map(t => [t, teamRows.get(t)?.rank ?? 1]));
    const topScore = real.length ? Math.max(...real.map(scoreOf)) : 0;
    const ORD = ["", "1ST", "2ND", "3RD", "4TH"];
    if (!order.length && !unknown.length) return `<div class="center"><div class="title">GAME ON</div><div class="sub">waiting for the first ${wd.kill}…</div></div>`;
    const n = order.length;
    if (order.length !== 1) unknown.push(...other);   // three or more colours: "not Red" doesn't place them
    const maxRows = Math.max(4, ...order.map(t => byTeam.get(t).length), order.length === 1 ? other.length : 0);
    const GUESS = { carried: "team carried over from the last game", inferred: "worked out from who tagged them" };
    // With hearts on show the KO'd count is redundant (hearts lost = times KO'd), so that column goes.
    const hearts = ps.some(p => p.lives_left != null);
    const wolvesOnly = order.length === 1 && order[0] === 2;
    let html = `<div class="teams teams-${Math.max(n, 1)} ${showStatus ? "" : "no-status"} ${hearts ? "hearts" : ""} ${squads ? "onelife" : ""} ${wolvesOnly ? "wolves-only" : ""}">`;
    if (!order.length) html += `<div class="center" style="grid-column:1/-1"><div class="title">GAME ON</div><div class="sub">teams show up after the first ${wd.kill}</div></div>`;
    html += order.map(t => {
      if (t === 2) return wolvesHtml(byTeam.get(t), wd, showStatus, maxRows);   // lone wolves: a card each
      const row = teamRows.get(t) || { kills: 0, deaths: 0, score: 0, objective_points: 0, alive: 0 };
      const list = byTeam.get(t).sort(squads ? royaleSort : scoreSort);
      const tc = W.teamVar(t), tname = teamName(t);
      const key = squads ? royaleKey : scoreKey;
      const ranks = rankLabels(list, key);
      const rows = list.map((p, i) => `<div class="p-row ${p.eliminated ? "out" : ""} ${p.online ? "" : "offline"} ${GUESS[p.team_source] ? "guess" : ""}" data-n="${p.number}" data-key="t${t}-${p.number}"><span class="p-rank">${ranks[i]}</span><span class="p-name">${W.esc(p.display_name)}${GUESS[p.team_source] ? `<span class="guess" title="${GUESS[p.team_source]}">?</span>` : ""}</span><span class="p-k" data-f="kills">${p.kills}</span><span class="p-d" data-f="deaths">${p.deaths}</span><span class="p-status" data-f="lives">${livesHtml(p)}</span></div>`).join("");
      // Only what the big score and the other columns don't already say: with two teams a
      // team's "tagged" is the other team's tags, so it only earns a place with three or more.
      const parts = [];
      if (order.length > 2) parts.push(`${row.deaths} ${wd.deaths}`);
      if (row.objective_points) parts.push(`${row.objective_points} JBOX BASES`);
      const sub = parts.length ? `<div class="team-sub">${parts.join(" · ")}</div>` : `<div class="team-sub empty"></div>`;
      const target = rules.team_kill_target ? progress(row.score, rules.team_kill_target, "team") : "";
      // A team game always has two sides: the silent one in the open column counts as a team on 0.
      // Only a sole leader is outlined; a dead heat outlines nobody.
      const lone = t === 2;                          // the free-for-all column: each player for themselves
      const sides = Math.max(real.length, 2);
      const tiedWith = real.filter(x => rankOf.get(x) === rankOf.get(t)).length;
      const lead = !lone && !squads && topScore > 0 && rankOf.get(t) === 1 && tiedWith === 1;   // a royale has no leader, only survivors
      const ordinal = ORD[rankOf.get(t)] || rankOf.get(t) + "TH";
      // every team level = just "TIED"; a partial tie keeps its place ("TIED 2ND")
      const label = tiedWith === sides ? "TIED" : (tiedWith > 1 ? `TIED ${ordinal}` : ordinal);
      const rankPill = !lone && topScore > 0 && !squads ? `<div class="team-rank">${label}</div>` : "";
      if (lone) parts.length = 0, parts.push("EACH FOR THEMSELVES");
      return `<div class="team-col ${maxRows >= 6 ? "dense" : ""} ${lead ? "lead" : ""}" style="--tc:${tc};--rows:${maxRows}" data-key="col${t}">${rankPill}<div class="team-head"><div class="team-name">${tname}</div><div class="team-score${lone ? " lone" : ""}" data-team="${t}">${lone ? "" : (row.score ?? row.kills)}</div></div>${target}${sub}<div class="roster"><div class="roster-head"><span></span><span>PLAYER</span><span>${wd.K}</span><span>${wd.D}</span><span>STATUS</span></div>${rows}</div>${showStatus ? `<div class="team-foot">${row.alive}/${list.length} IN</div>` : ""}</div>`;
    }).join("");
    if (order.length === 1 && order[0] !== 2) {
      // A team battle always has at least two teams: hold the second slot open instead of
      // stretching one column across the board. (Only wolves so far: they take the whole board.)
      // Players tagged by the one team heard so far must be on the other side: list them here
      // with a "?" until their own first tag names the colour.
      const list = other.sort(scoreSort);
      const ranks = rankLabels(list, scoreKey);
      const rows = list.map((p, i) => `<div class="p-row guess ${p.eliminated ? "out" : ""} ${p.online ? "" : "offline"}" data-n="${p.number}" data-key="to-${p.number}"><span class="p-rank">${ranks[i]}</span><span class="p-name">${W.esc(p.display_name)}<span class="guess" title="tagged by ${W.esc(teamName(order[0]))}, so on the other team">?</span></span><span class="p-k" data-f="kills">${p.kills}</span><span class="p-d" data-f="deaths">${p.deaths}</span><span class="p-status" data-f="lives">${livesHtml(p)}</span></div>`).join("");
      const foot = showStatus && list.length ? `<div class="team-foot">${list.filter(p => p.alive).length}/${list.length} IN</div>` : "";
      const body = list.length ? `<div class="roster"><div class="roster-head"><span></span><span>PLAYER</span><span>${wd.K}</span><span>${wd.D}</span><span>STATUS</span></div>${rows}</div>` : `<div class="waiting">waiting for their first ${wd.kill}</div>`;
      html += `<div class="team-col placeholder ${maxRows >= 6 ? "dense" : ""}" style="--tc:var(--tn);--rows:${maxRows}" data-key="col-placeholder"><div class="team-head"><div class="team-name">Team ?</div><div class="team-score">0</div></div><div class="team-sub empty"></div>${body}${foot}</div>`;
    }
    html += `</div>`;
    if (unknown.length) {
      html += `<div class="unassigned"><span class="ul">TEAM NOT HEARD YET</span>` + unknown.sort((a, b) => a.number - b.number).map(p => `<span class="up ${p.eliminated ? "out" : ""}" data-n="${p.number}" data-key="u-${p.number}">${W.esc(p.display_name)}</span>`).join("") + `</div>`;
    }
    return html;
  }

  function statsHtml(wd) {
    const ps = (snap.players || []).filter(p => p.in_game).sort(scoreSort);
    const rules = snap.game.rules || {};
    const sranks = rankLabels(ps, scoreKey);
    const rows = ps.map((p, i) => `<div class="s-row ${p.eliminated ? "out" : ""}" style="--tc:${pColor(p)}" data-n="${p.number}" data-key="s-${p.number}"><span class="p-rank">${sranks[i]}</span><span class="p-name">${W.esc(p.display_name)}${(() => { const sub = [isSolo() ? "" : (p.team_name || ""), rules.player_kill_target ? p.kills + "/" + rules.player_kill_target : ""].filter(Boolean).join(" · "); return sub ? `<small>${W.esc(sub)}</small>` : ""; })()}</span><span class="num" data-f="kills">${p.kills}</span><span class="num dim" data-f="deaths">${p.deaths}</span><span class="num">${(p.deaths ? p.kd : p.kills).toFixed(1)}</span><span class="num dim">${p.best_streak || "–"}</span><span class="num dim">${W.mmss(p.time_alive_total)}</span><span class="num small">${p.nemesis ? W.esc(nameOf(p.nemesis)) : "–"}</span><span class="num small">${p.favorite_victim ? W.esc(nameOf(p.favorite_victim)) : "–"}</span><span class="num" data-f="lives">${p.eliminated ? `<span class="out">${wd.out}</span>` : (p.lives_left != null && snap.game.kind !== "royale" ? `<span class="lives">${"♥".repeat(p.lives_left)}</span>` : '<span class="alive">IN</span>')}</span></div>`).join("");
    return `<div class="stats ${statusMatters(ps) ? "" : "no-status"} ${snap.game.kind === "royale" ? "onelife" : ""}" style="--rows:${Math.max(6, ps.length)}"><div class="s-head"><span></span><span>PLAYER</span><span>${wd.K}</span><span>${wd.D}</span><span>${wd.ratio}</span><span>BEST STREAK</span><span>TIME IN</span><span>NEMESIS</span><span>TARGET</span><span>STATUS</span></div>${rows}</div>`;
  }

  // Royale order: survivors by tags, then the fallen by when they went out (later = higher).
  // Going out fixes your place, so there are no ties among the fallen.
  const outTime = (p) => p.out_at || p.last_death_at || 0;
  const royaleSort = (a, b) => (b.alive - a.alive) || (a.alive ? (b.kills - a.kills || a.deaths - b.deaths || a.number - b.number) : (outTime(b) - outTime(a) || a.number - b.number));
  const royaleKey = (p) => p.alive ? "in/" + p.kills + "/" + p.deaths : "out/" + outTime(p);
  // Everywhere else: score first (tags, then tagged); with an equal score, still in beats out,
  // and among the out the later exit ranks higher. Only players still in can tie.
  const scoreSort = (a, b) => b.kills - a.kills || a.deaths - b.deaths || (b.alive - a.alive) || (a.alive ? 0 : outTime(b) - outTime(a)) || a.number - b.number;
  const scoreKey = (p) => p.kills + "/" + p.deaths + (p.alive ? "/in" : "/out/" + outTime(p));

  function royaleHtml(wd) {
    const ps = (snap.players || []).filter(p => p.in_game).sort(royaleSort);
    const key = royaleKey;
    const ranks = rankLabels(ps, key);
    const top = "";
    const rows = ps.map((p, i) => `<div class="r-row ffa ${p.alive ? "" : "out"}" style="--tc:${pColor(p)}" data-n="${p.number}" data-key="r-${p.number}"><span class="p-rank">${ranks[i]}</span><span class="p-name" style="color:${pColor(p)}">${W.esc(p.display_name)}</span><span class="p-k" data-f="kills">${p.kills}</span><span class="p-d" data-f="deaths">${p.deaths}</span><span class="p-life ${p.alive ? "alive" : "out"}" data-f="alive">${p.alive ? '<span class="alive">IN</span>' : `<span class="out">${wd.out}</span>`}</span></div>`).join("");
    const rules = snap.game.rules || {};
    const target = rules.player_kill_target && ps.length ? progress(ps[0].kills, rules.player_kill_target, "player") : "";
    return `<div class="royale onelife" style="--rows:${Math.max(6, ps.length)}">${top}${target}<div class="r-head"><span></span><span>PLAYER</span><span>${wd.K}</span><span>${wd.D}</span><span>STATUS</span></div>${rows}</div>`;
  }

  // Medal icons for the post-game cards (inline SVG: deterministic width, no emoji font).
  // One family of outline icons (2px stroke, round joins) so every award card reads the same.
  const MEDAL = {
    mvp: '<path d="M12 2.5l3.1 6.3 6.9 1-5 4.9 1.2 6.9L12 18.3l-6.2 3.3 1.2-6.9-5-4.9 6.9-1z"/>',
    kd: '<circle cx="12" cy="12" r="8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/><circle cx="12" cy="12" r="1.2"/>',
    streak: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.4-.5-2-1-3-1.1-2.1-.2-4.1 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.2.4-2.3 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    first_blood: '<path d="M13 2L3 14h9l-1 8 10-12h-9z"/>',
    easy_target: '<circle cx="12" cy="12" r="9.5"/><circle cx="12" cy="12" r="5.5"/><circle cx="12" cy="12" r="1.5"/>',
    untouchable: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    captures: '<path d="M5 22V3M5 4h13l-2.5 4.5L18 13H5"/>',
    closer: '<path d="M5 22V3M5 4h14v9H5"/><path d="M9 8.5l2 2 4-4"/>',
    survivor: '<path d="M3 8l4.5 4L12 5l4.5 7L21 8l-2 11H5z"/>',
  };
  const medal = (key) => `<svg class="medal" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${MEDAL[key] || MEDAL.mvp}</svg>`;

  function gameOverHtml(sm, wd) {
    const tn = sm.winner_team != null ? teamName(sm.winner_team).toUpperCase() : "";
    const byPlayer = !!(sm.winner_player && sm.kind !== "team");          // a colour with one player on it: name the player
    const title = sm.draw ? "DRAW" : sm.winner_label ? ((sm.winner_team != null && !byPlayer) ? (teamCustom(sm.winner_team) ? `${tn} ${tn.endsWith("S") ? "WIN" : "WINS"}` : `${tn} TEAM WINS`) : `${nameOf(sm.winner_player)} WINS`) : "GAME OVER";
    const titleColor = (sm.winner_team != null && !byPlayer) ? W.teamVar(sm.winner_team) : (sm.winner_player ? pColorN(sm.winner_player) : "var(--ink)");
    const awards = (sm.awards || []).map((a, i) => `<div class="award ${a.key}">${medal(a.key)}<div class="al">${W.esc(a.label)}</div><div class="an" style="color:${pColorN(a.player)}">${W.esc(nameOf(a.player))}</div><div class="av">${W.esc(a.value || "")}</div></div>`).join("");
    const ps = [...(sm.players || [])].sort(sm.kind === "royale" ? royaleSort : scoreSort);
    const fkey = sm.kind === "royale" ? royaleKey : scoreKey;
    const labelsFor = (list) => sm.total_kills > 0 ? list.map((p, i) => (i > 0 && fkey(p) === fkey(list[i - 1])) ? "" : String(i + 1)) : list.map(() => "");
    const royaleFinish = (sm.kind || (snap.game && snap.game.kind)) === "royale";   // one life: KO'd and ratio say nothing
    const rowHtml = (p, rank) => `<div class="f-row ${p.eliminated ? "out" : ""}" style="--tc:${pColor(p)}"><span class="p-rank">${rank}</span><span class="p-name">${W.esc(p.display_name)}</span><span class="p-k">${p.kills}</span><span class="p-d">${p.deaths}</span><span class="f-kd">${(p.deaths ? p.kd : p.kills).toFixed(1)}</span></div>`;
    let standings;
    if ((sm.kind === "team" || sm.squads) && (sm.teams || []).length) {
      // Team game: one block per team, winner first, players in the same order as the live roster.
      // lone wolves are listed under "Free for all" but never summed into a team score
      const groups = (sm.teams || []).map(t => ({ t: t.team, name: t.name, score: t.team === 2 ? null : t.score, players: ps.filter(p => p.effective_team === t.team) }));
      // The host's verdict places the winner first; the rest follow the backend's standing order
      // (a side with nobody left has lost whatever its kills, then kills).
      const rankOfT = new Map((sm.teams || []).map(t => [t.team, t.rank == null ? 99 : t.rank]));
      groups.sort((a, b) => (b.t === sm.winner_team) - (a.t === sm.winner_team) || (rankOfT.get(a.t) ?? 99) - (rankOfT.get(b.t) ?? 99) || a.t - b.t);
      const loose = ps.filter(p => p.effective_team == null || !groups.some(g => g.t === p.effective_team));
      if (loose.length) groups.push({ t: null, name: "No team", score: null, players: loose });
      const ORD = ["", "1ST", "2ND", "3RD", "4TH"];
      const ranked = groups.filter(g => g.score != null);
      const placeOf = (g) => {
        if (g.score == null) return "";
        if (sm.winner_team != null && g.t === sm.winner_team) return "1ST";
        const peers = ranked.filter(x => !(sm.winner_team != null && x.t === sm.winner_team));
        const r = rankOfT.get(g.t);
        const pos = peers.findIndex(x => rankOfT.get(x.t) === r) + (sm.winner_team != null ? 2 : 1);
        const tied = peers.filter(x => rankOfT.get(x.t) === r).length > 1;
        if (tied && peers.every(x => rankOfT.get(x.t) === r) && sm.winner_team == null) return "TIED";
        return (tied ? "TIED " : "") + (ORD[pos] || pos + "TH");
      };
      const perCol = Math.max(...groups.map(g => g.players.length));
      standings = `<div class="final team-final ${sm.squads ? "onelife" : ""}" style="--cols:${groups.length};--per-col:${perCol}"><div class="f-head"><span>FINAL STANDINGS</span><span>${sm.squads ? wd.kills : `${wd.kills} · ${wd.deaths} · ${wd.ratio}`}</span></div>` +
        groups.map((g, gi) => {
          const place = placeOf(g);
          const labels = labelsFor(g.players);
          return `<div class="f-team" style="--tc:${g.t == null ? "var(--tn)" : W.teamVar(g.t)}"><div class="f-team-head"><span class="place">${place}</span><span class="tname">${W.esc(g.name)}</span><span class="tscore">${g.score == null ? "" : g.score}</span></div>${g.players.map((p, i) => rowHtml(p, labels[i])).join("")}</div>`;
        }).join("") + `</div>`;
    } else {
      const franks = labelsFor(ps);
      const cols = ps.length <= 10 ? 2 : 3;
      const rows = ps.map((p, i) => rowHtml(p, franks[i])).join("");
      standings = `<div class="final ${royaleFinish ? "onelife" : ""}" style="--cols:${cols};--per-col:${Math.ceil(ps.length / cols)}"><div class="f-head"><span>FINAL STANDINGS</span><span>${royaleFinish ? wd.kills : `${wd.kills} · ${wd.deaths} · ${wd.ratio}`}</span></div>${rows}</div>`;
    }
    const rate = sm.duration_s >= 60 ? ` · ${Number(sm.kills_per_min).toFixed(1)} KILLS/MIN` : "";
    return `<div class="go-title" style="color:${titleColor}">${W.esc(title)}</div><div class="go-sub">${W.esc((sm.mode_label || sm.kind_label || "game").toUpperCase())} · ${W.mmss(sm.duration_s)} · ${W.n(sm.total_kills, wd.kill, wd.kills)}${rate}</div><div class="awards">${awards}</div>${standings}`;
  }

  function eventHtml(e, wd, full) {
    let txt = "";
    if (e.kind === "kill" && e.data && e.data.storm) {
      txt = `<span class="storm">STORM</span> <span class="arrow">➜</span> ` + nameHtml(e.data.victim) +
        (e.detail && /is OUT/.test(e.detail) ? `<span class="sub">${wd.out}</span>` : "");
    } else if (e.kind === "kill" && e.data && e.data.killer) {
      txt = nameHtml(e.data.killer) + (e.data.victim ? ` <span class="arrow">➜</span> ` + nameHtml(e.data.victim) : ` <span>${wd.scored}</span>`);
      const bits = [];
      if (e.data.first_blood) bits.push(wd.first_blood);
      if (e.data.streak >= 3) bits.push(`${wd.streak} ×${e.data.streak}`);
      if (e.detail && /is OUT/.test(e.detail)) bits.push(wd.out);
      if (bits.length) txt += `<span class="sub">${bits.join(" · ")}</span>`;
    } else if (e.kind === "join" || e.kind === "rejoin" || e.kind === "out") {
      txt = (e.players[0] ? nameHtml(e.players[0]) : "") + " " + W.esc(e.title.replace(nameOf(e.players[0]), "").trim());
    } else if (e.kind === "callout") {
      txt = W.esc(e.title === "FIRST BLOOD" ? wd.first_blood : e.title) + (e.detail ? `<span class="sub">${W.esc(e.detail)}</span>` : "");
    } else {
      txt = W.esc(e.title) + (e.detail && (full || e.kind !== "game_start") ? `<span class="sub">${W.esc(e.detail)}</span>` : "");
    }
    return txt;
  }
  function sevClass(e) { return e.severity === "callout_soft" ? "callout" : e.severity; }

  // Feed rows are kept in the DOM and new ones are prepended with a drop-in animation.
  // If the viewer is at the top they stay at the top; if they scrolled down, Chrome's scroll
  // anchoring keeps what they are reading in place. Names and colours are refreshed in place
  // (a player's team is often learned mid-game), so the list is only ever rebuilt on first
  // paint or after the server recomputed history.
  const feedState = { maxId: 0, built: false };
  const feedEvents = () => (snap.timeline || []).filter(e => e.wall !== false && e.feed !== false);
  function feedRow(e, wd, drop) {
    const li = document.createElement("li");
    li.className = `${e.kind} ${sevClass(e)}${drop ? " drop" : ""}`;
    li.dataset.id = e.id;
    if (drop) li.addEventListener("animationend", () => li.classList.remove("drop"), { once: true });
    li.innerHTML = `<span class="t">${W.clock(e.ts)}</span><span class="txt">${eventHtml(e, wd, false)}</span>`;
    return li;
  }
  // Feed rows are one line of fixed height: the text shrinks (never wraps) if it would overrun.
  function fitFeedRow(li) {
    const txt = li.querySelector(".txt");
    if (!txt) return;
    txt.style.fontSize = "";
    let size = parseFloat(getComputedStyle(txt).fontSize), guard = 24;
    const min = size * 0.42;
    while (txt.scrollWidth > txt.clientWidth && size > min && guard--) { size *= 0.95; txt.style.fontSize = size + "px"; }
  }
  let feedWidthSeen = 0;
  function refitFeed(force) {
    const ol = $("feed");
    if (!force && ol.clientWidth === feedWidthSeen) return;
    feedWidthSeen = ol.clientWidth;
    ol.querySelectorAll("li").forEach(fitFeedRow);
  }
  function renderFeed(wd) {
    const ol = $("feed");
    const evs = feedEvents();
    const serverMax = evs.length ? Math.max(...evs.map(e => e.id)) : 0;
    const replayed = feedState.maxId > serverMax;          // ids restarted: server recomputed history
    if (!feedState.built || replayed) {
      ol.innerHTML = "";
      for (const e of evs.slice(-300).reverse()) ol.appendChild(feedRow(e, wd, false));
      feedState.built = true;
      refitFeed(true);
    } else {
      const atTop = ol.scrollTop <= 2;
      const fresh = evs.filter(e => e.id > feedState.maxId);
      for (const e of fresh) { const li = feedRow(e, wd, true); ol.prepend(li); fitFeedRow(li); }
      refitFeed(false);
      while (ol.children.length > 300) ol.lastElementChild.remove();
      if (atTop) ol.scrollTop = 0;
      ol.querySelectorAll("b[data-pn]").forEach(b => {
        const n = +b.dataset.pn, nm = nameOf(n), col = pColorN(n);
        if (b.style.color !== col) b.style.color = col;
        if (b.textContent !== nm) b.textContent = nm;
      });
    }
    feedState.maxId = serverMax;
    $("feedTitle").textContent = snap.game.phase === "ended" ? "HOW IT WENT DOWN" : "LIVE FEED";
  }

  function renderTimelineFull(wd) {
    const evs = feedEvents().slice(-22).reverse();
    $("timelineList").innerHTML = evs.map(e => `<li class="${e.kind} ${sevClass(e)}"><span class="w">${W.clock(e.ts)}</span><span class="t">${e.game_t != null ? "T+" + W.mmss(e.game_t) : ""}</span><span class="txt">${eventHtml(e, wd, true)}</span></li>`).join("");
  }

  let clockOffset = 0;   // local clock minus server clock, learned from ticks
  function renderStatus(d) {
    d = d || (snap && snap.dongle) || {};
    const dot = $("dongleDot");
    const since = d.last_frame_ts ? Math.max(0, Math.round(Date.now() / 1000 - clockOffset - d.last_frame_ts)) : d.seconds_since_frame;
    dot.className = d.connected ? (since != null && since > 120 ? "quiet" : "ok") : "";
    const sim = snap && snap.server && snap.server.simulate ? '<span class="sim">SIMULATION</span> · ' : "";
    const port = d.port || (d.reader && d.reader.port) || "";
    $("dongleText").innerHTML = sim + (d.connected ? `dongle ${W.esc(port)} · ch ${d.channel || "?"} · ${d.frames_total || 0} frames${since != null ? ` · last ${Math.round(since)}s ago` : ""}` : `no dongle${d.reader && d.reader.last_error ? " · " + W.esc(d.reader.last_error) : ""}`);
  }

  // ---------------------------------------------------------------- events
  function handleEvents(evs) {
    const wd = W.WORDS;
    for (const e of evs) {
      if (e.id <= lastSeenEventId) continue;
      lastSeenEventId = e.id;
      if (e.wall === false) continue;
      if (e.kind === "kill") beep(SFX.kill);
      else if (e.kind === "callout") beep(SFX.callout);
      else if (e.kind === "game_start") beep(SFX.start);
      else if (e.kind === "game_over") beep(SFX.over);
      else if (e.kind === "join") beep(SFX.join);
    }
  }

  // ---------------------------------------------------------------- wiring
  W.connect({
    onMessage(msg) {
      if (msg.type === "snapshot" || msg.type === "update") {
        snap = msg.snapshot;
        if (msg.protocol) protocol = msg.protocol;
        const c = snap.game.clock;
        clockBase = (snap.game.phase === "live" && c && c.elapsed_s != null) ? { elapsed: c.elapsed_s, at: Date.now() } : null;
        if (msg.type === "snapshot") lastSeenEventId = Math.max(0, ...(snap.timeline || []).map(e => e.id));
        render();
        if (msg.events && msg.events.length) handleEvents(msg.events);
      } else if (msg.type === "tick") {
        clockOffset = Date.now() / 1000 - msg.now;
        if (snap) { snap.dongle = Object.assign({}, snap.dongle, msg.dongle); renderStatus(); }
      }
    },
    onClose() { $("dongleDot").className = ""; $("dongleText").textContent = "reconnecting to scoreboard server…"; },
  });
  // Instant first paint: pull the current state over HTTP without waiting for the first
  // WebSocket push (helps a projector show the board the moment the page loads, and lets
  // headless screenshots render a populated page). The WebSocket then takes over live.
  (async () => {
    try {
      const s = await W.api("/api/state");
      if (!snap) {
        snap = s;
        const c = snap.game.clock;
        clockBase = (snap.game.phase === "live" && c && c.elapsed_s != null) ? { elapsed: c.elapsed_s, at: Date.now() } : null;
        lastSeenEventId = Math.max(0, ...(snap.timeline || []).map(e => e.id));
        render();
      }
    } catch (e) {}
  })();

  window.addEventListener("resize", () => { if (snap) { render(); refitFeed(true); } });
  setInterval(renderClock, 250);
  setInterval(() => { if (snap) renderStatus(); }, 1000);
  setInterval(() => { if (snap && snap.game.phase === "idle") renderClock(); }, 5000);

  document.addEventListener("keydown", (ev) => {
    const k = ev.key.toLowerCase();
    if (k === "f") { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen(); }
    else if (k === "t") { timelineMode = !timelineMode; $("timelineFull").classList.toggle("hidden", !timelineMode); render(); }
    else if (k === "s") { sound = !sound; localStorage.setItem("swx_sound", sound ? "1" : "0"); if (sound) beep(SFX.join); }
    else if (k === "a") { window.open("/admin", "_blank"); }
    else if (k === "m") { mirror = !mirror; try { localStorage.setItem("swx_mirror", mirror ? "1" : "0"); } catch (e) {} applyMirror(); }
    else if (k === "arrowdown" || k === "arrowup") { const ol = $("feed"); ol.scrollBy({ top: (k === "arrowdown" ? 1 : -1) * ol.clientHeight * 0.6, behavior: "smooth" }); }
    else if (k === "home") { $("feed").scrollTo({ top: 0, behavior: "smooth" }); }
    else if (k === "1" || k === "2") { view = k === "2" ? "stats" : "board"; timelineMode = false; $("timelineFull").classList.add("hidden"); render(); }
  });
})();
