(function () {
  const $ = (id) => document.getElementById(id), W = window.SWX;
  let snap = null, protocol = null;
  const TEAMS = ["Red", "Blue", "Yellow", "Green"];

  // tabs
  document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll("main section").forEach(s => s.classList.toggle("on", s.id === "tab-" + b.dataset.tab));
    const t = b.dataset.tab;
    if (t === "frames") loadFrames(); if (t === "protocol") loadProtocol(); if (t === "history") loadGames(); if (t === "dongle") loadDongle();
  }));

  function header() {
    if (!snap) return;
    const g = snap.game, d = snap.dongle;
    $("hdrStatus").innerHTML = `phase <b>${g.phase}</b> · ${g.mode_label || "-"} · ${g.players_in_game} players · dongle <span class="pill ${d.connected ? "ok" : "bad"}">${d.connected ? (d.port || "connected") : "none"}</span> · ${d.frames_total} frames${snap.server.simulate ? ' · <span class="pill warn">SIMULATION</span>' : ""}`;
  }

  // ---------------- teams
  function renderTeams() {
    const tb = $("teamsTable").querySelector("tbody");
    if (!tb.dataset.built) {
      // Red, Blue and Green are teams; the yellow pick is always Free for all, so it takes no name
      tb.innerHTML = [0, 1, 3].map(t => `<tr data-t="${t}"><td><span class="t${t}">●</span> ${TEAMS[t]}</td><td><input class="tname" placeholder="${TEAMS[t]}"></td><td><button class="b sm tsave">Save</button></td></tr>`).join("") +
        `<tr data-t="2" class="fixed"><td><span class="t2">●</span> Yellow</td><td><span class="help">always Free for all</span></td><td></td></tr>`;
      tb.dataset.built = "1";
      tb.querySelectorAll("tr:not(.fixed)").forEach(tr => {
        const t = +tr.dataset.t, inp = tr.querySelector(".tname");
        const save = async () => { try { await W.api(`/api/teams/${t}`, { method: "PUT", body: JSON.stringify({ name: inp.value }) }); $("teamsMsg").textContent = `Saved ${TEAMS[t]} team name`; } catch (e) { $("teamsMsg").textContent = "Error: " + e.message; } };
        tr.querySelector(".tsave").addEventListener("click", save);
        inp.addEventListener("keydown", e => { if (e.key === "Enter") save(); });
      });
    }
    tb.querySelectorAll("tr:not(.fixed)").forEach(tr => {
      const t = tr.dataset.t, inp = tr.querySelector(".tname");
      if (document.activeElement !== inp) inp.value = (snap.team_custom && snap.team_custom[t]) ? snap.team_names[t] : "";
    });
  }
  const teamLabel = (t) => (snap && snap.team_names && snap.team_names[String(t)]) || TEAMS[t];

  // ---------------- players
  function renderPlayers() {
    const by = new Map((snap.players || []).map(p => [p.number, p]));
    const tb = $("playersTable").querySelector("tbody");
    if (!tb.dataset.built) {
      tb.innerHTML = Array.from({ length: 16 }, (_, i) => i + 1).map(n => `<tr data-n="${n}"><td><b>${n}</b><br><small class="u mono">…:${n.toString(16).padStart(2, "0")}</small></td><td><input class="pname" placeholder="Player ${n}"></td><td><select class="pteam"><option value="">auto</option>${TEAMS.map((t, i) => `<option value="${i}">${t}</option>`).join("")}</select></td><td class="pradio"></td><td class="pstats"></td><td><button class="b sm psave">Save</button></td></tr>`).join("");
      tb.dataset.built = "1";
      tb.querySelectorAll("tr").forEach(tr => {
        const n = +tr.dataset.n;
        tr.querySelector(".psave").addEventListener("click", async () => {
          const body = { name: tr.querySelector(".pname").value, team_override: tr.querySelector(".pteam").value === "" ? null : +tr.querySelector(".pteam").value };
          try { await W.api(`/api/players/${n}`, { method: "PUT", body: JSON.stringify(body) }); $("playersMsg").textContent = `Saved player ${n}`; } catch (e) { $("playersMsg").textContent = "Error: " + e.message; }
        });
        tr.querySelector(".pname").addEventListener("keydown", e => { if (e.key === "Enter") tr.querySelector(".psave").click(); });
      });
    }
    tb.querySelectorAll("tr").forEach(tr => {
      const n = +tr.dataset.n, p = by.get(n) || {};
      const nameEl = tr.querySelector(".pname"); if (document.activeElement !== nameEl) nameEl.value = p.name || snap.names[String(n)] || "";
      const tsel = tr.querySelector(".pteam"); if (document.activeElement !== tsel) tsel.value = p.team_override != null ? String(p.team_override) : (snap.team_overrides[String(n)] != null ? String(snap.team_overrides[String(n)]) : "");
      const team = p.effective_team;
      tr.querySelector(".pradio").innerHTML = p.last_seen ? `<span class="pill ${p.online ? "ok" : ""}">${p.online ? "online" : "quiet"}</span> ${p.rssi != null ? p.rssi + " dBm" : ""}<br><small class="u">${team != null ? `<span class="t${team}">${teamLabel(team)}</span> (${p.team_source})` : "team ?"} · seen ${W.clock(p.last_seen)}</small>` : `<span class="pill">never heard</span>`;
      tr.querySelector(".pstats").innerHTML = p.in_game ? `${p.kills} kills / ${p.deaths} deaths · streak ${p.best_streak}${p.lives_left != null ? " · lives " + p.lives_left : ""}${p.eliminated ? ' · <span class="pill bad">OUT</span>' : ""}${p.gun_restarts ? ` · <span class="pill warn">${p.gun_restarts} restart</span>` : ""}<br><small class="u">reported score ${p.score_reported ?? "-"}</small>` : `<small class="u">not in game</small>`;
    });
  }

  // ---------------- game + settings
  function renderGame() {
    const g = snap.game, c = g.clock || {};
    $("gameInfo").innerHTML = `<div class="grid">
      <div><b>Phase</b> ${g.phase} ${g.start_source ? `<small class="u">(${g.start_source})</small>` : ""}</div>
      <div><b>Mode</b> ${g.mode_label || "-"}${g.arcade ? " (arcade)" : ""}</div>
      <div><b>Started</b> ${g.started_at ? W.clock(g.started_at) : "-"} · <b>Ended</b> ${g.ended_at ? W.clock(g.ended_at) : "-"} ${g.end_reason ? `(${g.end_reason})` : ""}</div>
      <div><b>Clock</b> elapsed ${W.mmss(c.elapsed_s)}${c.limit_s ? ` · left ${W.mmss(c.left_s)}` : ""}${c.storm ? " · storm" : ""}${c.stale ? ' · <span class="pill warn">stale</span>' : ""}</div>
      <div><b>Settings</b> ${[g.settings.mode_label, g.settings.lives_label, g.settings.time_label, g.settings.respawn_label, g.settings.lighting_label].filter(Boolean).join(" · ") || "-"}</div>
      <div><b>Players</b> ${g.players_in_game} in game · ${g.players_alive} in · ${g.total_kills} kills</div>
      <div><b>Winner</b> ${g.winner_label || "-"}</div>
      <div><b>Host last heard</b> ${g.host_seen_at ? W.clock(g.host_seen_at) : "never"}</div></div>`;
  }
  function fillSettings() {
    const cfg = snap.config, r = cfg.rules || {};
    if (document.activeElement && document.activeElement.closest("#tab-game")) return;
    $("rGameType").value = r.game_type || "auto"; $("cAutoEnd").value = cfg.auto_end_grace_s ?? 45;
    $("rLabel").value = r.label || ""; $("rTime").value = r.time_limit_s ? Math.round(r.time_limit_s / 60) : ""; $("rTeamK").value = r.team_kill_target || ""; $("rPlayerK").value = r.player_kill_target || "";
    $("cHideRoyale").checked = cfg.hide_royale !== false;
    $("cHideFiveLives").checked = cfg.hide_five_lives !== false;
    const lowOpt = $("hostLives").querySelector('option[value="0"]'); if (lowOpt) lowOpt.hidden = cfg.hide_five_lives !== false;
    const royaleOpt = $("hostMode").querySelector('option[value="1"]'); if (royaleOpt) royaleOpt.hidden = cfg.hide_royale !== false;
    $("cLives").value = cfg.lives_low; $("cHpLow").value = cfg.hp_low ?? 200; $("cHpHigh").value = cfg.hp_high ?? 500; $("cTimeLimit").value = cfg.time_limit_s;
    $("cDedup").value = cfg.dedup_window_s;
  }
  $("btnSaveSettings").addEventListener("click", async () => {
    const body = { hide_royale: $("cHideRoyale").checked, hide_five_lives: $("cHideFiveLives").checked, lives_low: +$("cLives").value || 5, hp_low: +$("cHpLow").value || 200, hp_high: +$("cHpHigh").value || 500, time_limit_s: +$("cTimeLimit").value || 600,
      dedup_window_s: +$("cDedup").value,
      auto_end_grace_s: Math.max(0, +$("cAutoEnd").value || 0),
      rules: { game_type: $("rGameType").value, label: $("rLabel").value, time_limit_s: $("rTime").value ? Math.round(+$("rTime").value * 60) : null, team_kill_target: +$("rTeamK").value || null, player_kill_target: +$("rPlayerK").value || null } };
    try { await W.api("/api/settings", { method: "PUT", body: JSON.stringify(body) }); $("settingsMsg").textContent = "Saved"; } catch (e) { $("settingsMsg").textContent = "Error: " + e.message; }
  });
  const cmd = (c, body) => async () => { try { await W.api(`/api/game/${c}`, { method: "POST", body: JSON.stringify(body || {}) }); } catch (e) { alert(e.message); } };
  $("btnLobby").addEventListener("click", cmd("lobby")); $("btnStartT").addEventListener("click", cmd("start", { mode: 0 })); $("btnStartR").addEventListener("click", cmd("start", { mode: 1 }));
  // host mode
  const hostPost = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }).then(r => r.json()).then(renderHost).catch(e => { $("hostMsg").textContent = String(e); });
  function renderHost(h) {
    if (!h) return;
    $("hostEnabled").checked = !!h.enabled;
    $("hostMode").value = String(h.settings.mode); $("hostLives").value = String(h.settings.lives);
    const hr = h.rules || {};
    $("hostTime").value = hr.time_limit_s && h.settings.mode === 0 ? "custom" : String(h.settings.time);
    if (hr.time_limit_s) $("hostMins").value = String(Math.round(hr.time_limit_s / 60));
    $("hostTarget").value = String(hr.team_kill_target || 0);
    $("hostLighting").value = String(h.settings.lighting);
    $("hostRate").value = String(h.rate);
    $("hostStart").disabled = !h.can_start; $("hostEnd").disabled = !h.can_end;
    $("hostMsg").textContent = (h.enabled ? `hosting · ${h.tx_count} frames sent` : "not hosting") + (h.last_error ? ` · ${h.last_error}` : "") + (h.available ? "" : " · no dongle");
  }
  fetch("/api/host").then(r => r.json()).then(renderHost);
  $("hostEnabled").addEventListener("change", () => hostPost("/api/host", { enabled: $("hostEnabled").checked }));
  for (const [id, k] of [["hostMode", "mode"], ["hostLives", "lives"], ["hostLighting", "lighting"]])
    $(id).addEventListener("change", () => hostPost("/api/host/settings", { [k]: +$(id).value }));
  const postTime = () => {
    const v = $("hostTime").value;             // 0 off, 1 the guns' timer, custom = a board length
    const mins = Math.max(1, Math.min(60, +$("hostMins").value || 5));
    // a board length of exactly 10 minutes is the guns' own timer
    hostPost("/api/host/settings", v === "custom" ? (mins === 10 ? { time: 1, time_limit_s: 0 } : { time: 0, time_limit_s: mins * 60 }) : { time: +v, time_limit_s: 0 });
  };
  $("hostTime").addEventListener("change", postTime);
  $("hostMins").addEventListener("change", () => { if ($("hostTime").value === "custom") postTime(); });
  $("hostTarget").addEventListener("change", () => hostPost("/api/host/settings", { team_kill_target: Math.max(0, Math.min(99, +$("hostTarget").value || 0)) }));
  $("hostRate").addEventListener("change", () => hostPost("/api/host/rate", { rate: +$("hostRate").value }));
  $("hostStart").addEventListener("click", () => hostPost("/api/host/start"));
  $("hostEnd").addEventListener("click", () => { if (confirm("End the game now?")) hostPost("/api/host/end"); });
  $("btnEnd").addEventListener("click", cmd("end")); $("btnReset").addEventListener("click", () => { if (confirm("Reset the board (current game only; nothing is deleted)?")) cmd("reset")(); });

  // ---------------- dongle
  async function loadDongle() {
    const d = await W.api("/api/dongle");
    const r = d.reader || {};
    $("dongleInfo").innerHTML = `<div class="grid"><div><b>Reader</b> <span class="pill ${r.connected ? "ok" : "bad"}">${r.connected ? "connected" : "disconnected"}</span> ${r.port || ""} ${r.last_error ? `<br><small class="u">${W.esc(r.last_error)}</small>` : ""}</div>
      <div><b>Firmware</b> ${d.fw || "stock/unknown"} · mode ${d.mode || "?"} · ch ${d.channel || "?"} · boots ${d.boots}</div>
      <div><b>Frames</b> ${d.frames_total} total · last ${d.last_frame_ts ? W.clock(d.last_frame_ts) : "never"}</div>
      <div><b>Status</b> <span class="mono">${W.esc(JSON.stringify(d.status || {}))}</span></div>
      <div><b>USB ports seen</b> ${(d.ports || []).map(p => `<span class="pill">${W.esc(p)}</span>`).join(" ") || "none"}</div></div>`;
    const sel = $("dChan"); if (!sel.options.length) sel.innerHTML = Array.from({ length: 13 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join("");
    if (d.channel) sel.value = String(d.channel);
    $("dongleLog").textContent = (d.log || []).map(l => `${W.clock(l.ts)}  ${l.kind}  ${l.detail || ""}`).join("\n");
  }
  const dcmd = (line) => async () => { try { await W.api("/api/dongle/command", { method: "POST", body: JSON.stringify({ line }) }); setTimeout(loadDongle, 600); } catch (e) { alert(e.message); } };
  $("btnChan").addEventListener("click", () => dcmd("chan," + $("dChan").value)()); $("btnScan").addEventListener("click", dcmd("scan")); $("btnStatus").addEventListener("click", dcmd("status"));

  // ---------------- timeline
  function renderTimeline() {
    const tb = $("tlTable").querySelector("tbody");
    tb.innerHTML = (snap.timeline || []).slice().reverse().map(e => `<tr><td class="mono">${W.clock(e.ts)}</td><td class="mono">${e.game_t != null ? W.mmss(e.game_t) : ""}</td><td><span class="pill">${e.kind}</span>${e.wall === false ? ' <small class="u">hidden</small>' : ""}</td><td><b>${W.esc(e.title)}</b><br><small class="u">${W.esc(e.detail || "")}</small></td><td class="mono">${e.src ? W.esc(e.src.slice(-2)) + "→" + (e.dst ? W.esc(e.dst.slice(-2)) : "*") + " " : ""}${W.esc(e.raw || "")}${e.rssi != null ? ` <small class="u">${e.rssi}dBm</small>` : ""}</td></tr>`).join("");
  }

  // ---------------- frames
  async function loadFrames() {
    const fr = await W.api("/api/frames?limit=300");
    $("framesTable").querySelector("tbody").innerHTML = fr.slice().reverse().map(f => `<tr class="${f.kind}"><td class="mono">${W.clock(f.ts)}</td><td class="mono">${f.src_player ? "P" + f.src_player : (f.src_role === "host" ? "HOST" : W.esc(f.src || "?"))} → ${f.dst_player ? "P" + f.dst_player : (f.dst_role === "host" ? "HOST" : (f.dst_role === "broadcast" ? "ALL" : W.esc(f.dst || "")))}</td><td class="mono">${f.rssi ?? ""}</td><td><span class="pill">${f.kind}</span> <small class="u">${W.esc(JSON.stringify(f.fields))}</small></td><td class="mono">${W.esc(f.txt)}</td></tr>`).join("");
  }
  $("btnFramesRefresh").addEventListener("click", loadFrames);

  // ---------------- protocol lab
  let overrides = {};
  async function loadProtocol() {
    const p = await W.api("/api/protocol");
    protocol = p.registry; overrides = JSON.parse(JSON.stringify(p.registry.overrides || {}));
    const ops = Object.keys(Object.assign({}, p.registry.opcodes, p.observed)).sort((a, b) => (parseInt(a) || 999) - (parseInt(b) || 999));
    $("protoLab").innerHTML = ops.map(op => {
      const spec = p.registry.opcodes[op], obs = p.observed[op];
      const head = `<h3 style="margin:14px 0 4px">36,${op} · ${spec ? W.esc(spec.label) : "unknown opcode"} ${spec && spec.verified ? '<span class="badge v">from source</span>' : '<span class="badge">inferred</span>'} <small class="u">${obs ? obs.count + " seen" : "not seen yet"}${spec && spec.direction ? " · " + spec.direction : ""}</small></h3>${spec && spec.note ? `<p class="help">${W.esc(spec.note)}</p>` : ""}`;
      const maxTok = Math.max(...(spec ? spec.fields.map(f => f.index) : [0]), ...(obs ? Object.keys(obs.tokens).map(Number) : [0]));
      let toks = "";
      for (let i = 2; i <= maxTok; i++) {
        const f = spec ? spec.fields.find(x => x.index === i) : null, t = obs ? obs.tokens[String(i)] : null;
        const isTerm = t && t.values.length === 1 && t.values[0][0] === "42" && i === maxTok;
        if (isTerm) continue;
        const cur = f ? f.role : "unknown";
        const vals = t ? t.values.slice(0, 6).map(([v, c]) => `${W.esc(v)}×${c}`).join(" ") : "";
        const hints = t ? [t.eq_src0 ? `=sender-1:${t.eq_src0}` : "", t.eq_src1 ? `=sender:${t.eq_src1}` : "", t.eq_dst0 ? `=dest-1:${t.eq_dst0}` : ""].filter(Boolean).join(" ") : "";
        toks += `<div class="tok"><b>token ${i}</b><small class="u">${f ? W.esc(f.label) : "unlabelled"}${f && f.verified ? " ✓" : ""}</small><select data-op="${op}" data-idx="${i}">${p.roles.map(r => `<option value="${r}" ${r === cur ? "selected" : ""}>${r}</option>`).join("")}</select><small class="u">${vals}</small>${hints ? `<small class="u" style="color:var(--warn)">${hints}</small>` : ""}</div>`;
      }
      const ex = obs ? `<div class="mono" style="color:var(--ink2)">${obs.examples.map(W.esc).join("<br>")}</div>` : "";
      return `<div class="card" style="margin-bottom:8px">${head}${toks}${ex}</div>`;
    }).join("");
    $("protoLab").querySelectorAll("select").forEach(s => s.addEventListener("change", () => {
      const op = s.dataset.op, idx = s.dataset.idx;
      overrides[op] = overrides[op] || {}; overrides[op].fields = overrides[op].fields || {}; overrides[op].fields[idx] = Object.assign({}, overrides[op].fields[idx] || {}, { role: s.value });
    }));
  }
  $("btnSaveProto").addEventListener("click", async () => { try { const r = await W.api("/api/protocol/overrides", { method: "PUT", body: JSON.stringify({ overrides }) }); $("protoMsg").textContent = `Saved · replayed ${r.replayed} frames`; loadProtocol(); } catch (e) { $("protoMsg").textContent = "Error: " + e.message; } });
  $("btnResetProto").addEventListener("click", async () => { if (!confirm("Reset all protocol overrides to defaults?")) return; await W.api("/api/protocol/reset", { method: "POST" }); loadProtocol(); });

  // ---------------- history
  async function loadGames() {
    const gs = await W.api("/api/games");
    $("gamesTable").querySelector("tbody").innerHTML = gs.map(g => { const s = g.summary || {}; return `<tr><td>${g.started_at ? new Date(g.started_at * 1000).toLocaleString() : "-"}</td><td>${s.mode_label || g.mode || "-"}</td><td>${W.mmss(s.duration_s)}</td><td>${W.esc(s.winner_label || "-")}</td><td>${s.total_kills ?? "-"}</td><td><span class="pill">${g.status}</span></td><td><button class="b sm sec" data-id="${g.id}" data-act="view">View</button> <a class="b sm sec" style="text-decoration:none" href="/api/games/${g.id}/export?format=csv">CSV</a> <a class="b sm sec" style="text-decoration:none" href="/api/games/${g.id}/export?format=json">JSON</a></td></tr>`; }).join("");
    $("gamesTable").querySelectorAll("button[data-act=view]").forEach(b => b.addEventListener("click", async () => {
      const g = await W.api(`/api/games/${b.dataset.id}`);
      const s = g.summary || {};
      const lines = [`${s.mode_label || ""} · ${W.mmss(s.duration_s)} · winner ${s.winner_label || "-"}`, "", ...(s.players || []).map(p => `${String(p.display_name).padEnd(16)} K ${p.kills}  D ${p.deaths}  streak ${p.best_streak}  ${p.team_name || ""}`), "", "awards: " + (s.awards || []).map(a => `${a.label}: ${a.player}`).join(", "), "", ...(g.timeline || []).map(e => `${W.clock(e.ts)}  ${e.game_t != null ? W.mmss(e.game_t) : "     "}  ${e.kind.padEnd(10)} ${e.title}${e.detail ? " - " + e.detail : ""}`)];
      $("gameDetail").textContent = lines.join("\n"); $("gameDetail").classList.remove("hidden");
    }));
  }

  // ---------------- wiring
  W.connect({
    onMessage(msg) {
      if (msg.type === "snapshot" || msg.type === "update") {
        snap = msg.snapshot; if (msg.protocol) protocol = msg.protocol;
        header(); renderTeams(); renderPlayers(); renderGame(); fillSettings(); renderTimeline();
        if (document.querySelector("#tab-frames.on") && msg.events && msg.events.length) loadFrames();
      } else if (msg.type === "tick" && snap) { snap.dongle = Object.assign({}, snap.dongle, msg.dongle); header(); }
    },
    onClose() { $("hdrStatus").textContent = "reconnecting…"; },
  });
})();
