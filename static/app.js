/* VyOS WAN Emulator - frontend logic */

const $ = (sel) => document.querySelector(sel);

const state = {
  connected: false,
  liveTimer: null,
  routesTimer: null,
  routes: [],
  liveBusy: false,
  routesBusy: false,
};

/* ------------------------------------------------------------------ utils */

async function api(path, opts = {}) {
  const res = await fetch("/api/" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(message, kind = "ok", ms = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

const fmt = (n) => (typeof n === "number" ? n.toLocaleString() : n ?? "—");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function setStatus(connected, text) {
  state.connected = connected;
  const dot = $("#statusDot");
  dot.className = "dot " + (text === "busy" ? "busy" : connected ? "on" : "off");
  $("#statusText").textContent =
    text === "busy" ? "Connecting…" : connected ? `Connected` : "Disconnected";
  $("#connectBtn").textContent = connected ? "Reconnect" : "Connect";
}

function withBusy(btn, fn) {
  return async (...args) => {
    const old = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin">↻</span>';
    try { await fn(...args); }
    finally { btn.disabled = false; btn.textContent = old; }
  };
}

/* ------------------------------------------------------------------ theme */

function initTheme() {
  const saved = localStorage.getItem("vyos-theme") || "dark";
  document.documentElement.dataset.theme = saved;
  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("vyos-theme", next);
  });
}

/* ------------------------------------------------------------- connection */

async function initConnection() {
  try {
    const cached = await api("cached-credentials");
    if (cached.host) $("#host").value = cached.host;
    if (cached.username) $("#username").value = cached.username;
    if (cached.password) $("#password").value = cached.password;
  } catch (_) { /* no cache yet */ }

  try {
    const st = await api("status");
    if (st.connected) onConnected();
  } catch (_) { /* backend fresh */ }

  $("#connectForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#connectBtn");
    btn.disabled = true;
    setStatus(false, "busy");
    try {
      await api("connect", {
        method: "POST",
        body: JSON.stringify({
          host: $("#host").value,
          username: $("#username").value,
          password: $("#password").value,
        }),
      });
      toast(`Connected to ${$("#host").value}`, "ok");
      onConnected();
    } catch (err) {
      setStatus(false);
      toast(err.message, "err", 7000);
    } finally {
      btn.disabled = false;
    }
  });
}

function onConnected() {
  setStatus(true);
  loadInterfaces();
  startPolling();
}

function startPolling() {
  stopPolling();
  state.liveTimer = setInterval(refreshLive, 5000);
  state.routesTimer = setInterval(refreshRoutes, 10000);
  refreshLive();
  refreshRoutes();
}

function stopPolling() {
  clearInterval(state.liveTimer);
  clearInterval(state.routesTimer);
}

function handlePollError(err) {
  // connection probably dropped; show it once via status dot
  if (/Not connected/i.test(err.message)) {
    setStatus(false);
    stopPolling();
  }
  console.warn("poll failed:", err.message);
}

/* -------------------------------------------------------- interface cards */

const MGMT_INTERFACE = "eth0"; // management interface: IP config only

const NETEM_SLIDERS = [
  { key: "corruption", label: "Corruption (%)" },
  { key: "reordering", label: "Reordering (%)" },
  { key: "duplicate", label: "Duplication (%)" },
  { key: "loss", label: "Packet Loss (%)" },
];

function splitBandwidth(bw) {
  const m = /^([\d.]+)(bit|kbit|mbit|gbit|tbit)?$/.exec(bw || "");
  return m ? { value: m[1], unit: m[2] || "mbit" } : { value: "", unit: "mbit" };
}

async function loadInterfaces() {
  const grid = $("#ifGrid");
  try {
    const { interfaces } = await api("interfaces");
    grid.innerHTML = "";
    if (!interfaces.length) {
      grid.innerHTML = '<p class="empty">No ethernet interfaces found.</p>';
      return;
    }
    interfaces.forEach((iface) => grid.appendChild(buildCard(iface)));
  } catch (err) {
    grid.innerHTML = `<p class="empty">Failed to load interfaces: ${esc(err.message)}</p>`;
  }
}

function buildCard(iface) {
  const ne = iface.netem || {};
  const bw = splitBandwidth(ne.bandwidth);
  const addr = iface.addresses?.[0] || "";
  const card = document.createElement("div");
  card.className = "if-card";
  card.dataset.if = iface.name;

  if (iface.name === MGMT_INTERFACE) {
    card.classList.add("mgmt");
    card.innerHTML = `
      <div class="if-card-head">
        <span class="if-name">${esc(iface.name)}</span>
        <span class="if-state ${iface.oper_state === "UP" ? "up" : "down"}">${esc(iface.oper_state)}</span>
        <span class="mgmt-badge">MGMT</span>
        <span class="if-mac">${esc(iface.mac)}</span>
      </div>
      <div class="if-addr-row">
        <span class="if-addr ${addr ? "" : "none"}">${addr ? esc(iface.addresses.join(", ")) : "no address"}</span>
        <button class="btn ghost small" data-edit-ip>Edit IP</button>
      </div>
      <p class="mgmt-note">Management interface — IP configuration only, no network emulation.</p>`;
    card.querySelector("[data-edit-ip]").addEventListener("click", () =>
      openIpModal(iface.name, addr));
    return card;
  }

  const sliders = NETEM_SLIDERS.map((s) => {
    const v = ne[s.key] !== undefined ? parseFloat(ne[s.key]) : 0;
    return `
      <div class="netem-row">
        <label>${s.label}</label>
        <input type="range" min="0" max="100" step="1" value="${v}" data-slider="${s.key}">
        <input type="number" min="0" max="100" step="1" value="${v}" data-num="${s.key}">
      </div>`;
  }).join("");

  card.innerHTML = `
    <div class="if-card-head">
      <span class="if-name">${esc(iface.name)}</span>
      <span class="if-state ${iface.oper_state === "UP" ? "up" : "down"}">${esc(iface.oper_state)}</span>
      <span class="if-mac">${esc(iface.mac)}</span>
    </div>
    <div class="if-addr-row">
      <span class="if-addr ${addr ? "" : "none"}">${addr ? esc(iface.addresses.join(", ")) : "no address"}</span>
      <button class="btn ghost small" data-edit-ip>Edit IP</button>
    </div>
    <div class="netem-row">
      <label>Bandwidth</label>
      <div class="unit-wrap">
        <input type="number" min="0" step="any" value="${esc(bw.value)}" placeholder="—" data-num="bandwidth_value">
        <select data-num="bandwidth_unit">
          ${["kbit", "mbit", "gbit"].map((u) => `<option ${u === bw.unit ? "selected" : ""}>${u}</option>`).join("")}
        </select>
      </div>
      <span class="suffix"></span>
    </div>
    <div class="netem-row">
      <label>Delay</label>
      <input type="number" min="0" step="any" value="${esc(ne.delay ?? "")}" placeholder="—" data-num="delay">
      <span class="suffix">milli&nbsp;sec</span>
    </div>
    ${sliders}
    <div class="netem-row">
      <label>Queue Limit</label>
      <input type="number" min="0" step="1" value="${esc(ne["queue-limit"] ?? "")}" placeholder="—" data-num="queue_limit">
      <span class="suffix">packets</span>
    </div>
    <div class="apply-row">
      <button class="btn primary" data-apply>Apply Change</button>
    </div>`;

  // slider <-> number sync
  NETEM_SLIDERS.forEach((s) => {
    const range = card.querySelector(`[data-slider="${s.key}"]`);
    const num = card.querySelector(`[data-num="${s.key}"]`);
    range.addEventListener("input", () => (num.value = range.value));
    num.addEventListener("input", () => (range.value = num.value || 0));
  });

  card.querySelector("[data-edit-ip]").addEventListener("click", () =>
    openIpModal(iface.name, addr));

  const applyBtn = card.querySelector("[data-apply]");
  applyBtn.addEventListener("click", withBusy(applyBtn, () => applyNetem(card, iface.name)));

  return card;
}

async function applyNetem(card, ifname) {
  const get = (k) => card.querySelector(`[data-num="${k}"]`).value.trim();
  const bwVal = get("bandwidth_value");
  const payload = {
    bandwidth: bwVal ? bwVal + get("bandwidth_unit") : "",
    delay: get("delay"),
    queue_limit: get("queue_limit"),
  };
  NETEM_SLIDERS.forEach((s) => {
    const v = get(s.key);
    payload[s.key] = v && parseFloat(v) > 0 ? v : "";
  });
  try {
    const res = await api(`interfaces/${ifname}/netem`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(res.message, "ok");
  } catch (err) {
    toast(`${ifname}: ${err.message}`, "err", 8000);
  }
}

/* ---------------------------------------------------------------- IP modal */

let ipModalTarget = null;

function openIpModal(ifname, current) {
  ipModalTarget = ifname;
  $("#ipModalIf").textContent = ifname;
  $("#ipModalInput").value = current || "";
  $("#ipModal").hidden = false;
  $("#ipModalInput").focus();
}

function initIpModal() {
  const save = $("#ipModalSave");
  save.addEventListener("click", withBusy(save, async () => {
    try {
      const res = await api(`interfaces/${ipModalTarget}/address`, {
        method: "POST",
        body: JSON.stringify({ address: $("#ipModalInput").value.trim() }),
      });
      toast(res.message, "ok");
      $("#ipModal").hidden = true;
      loadInterfaces();
    } catch (err) {
      toast(err.message, "err", 8000);
    }
  }));
}

/* ------------------------------------------------------------ live tables */

async function refreshLive() {
  if (!state.connected || state.liveBusy) return;
  state.liveBusy = true;
  try {
    const { counters, arp } = await api("live");
    renderCounters(counters);
    renderArp(arp);
  } catch (err) {
    handlePollError(err);
  } finally {
    state.liveBusy = false;
  }
}

function renderCounters(rows) {
  const body = $("#countersBody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty">No counters</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r) => `
    <tr>
      <td class="if-tag">${esc(r.interface)}</td>
      <td>${fmt(r.rx_packets)}</td><td>${fmt(r.rx_bytes)}</td>
      <td>${fmt(r.rx_drops)}</td><td>${fmt(r.rx_errors)}</td>
      <td>${fmt(r.tx_packets)}</td><td>${fmt(r.tx_bytes)}</td>
      <td>${fmt(r.tx_drops)}</td><td>${fmt(r.tx_errors)}</td>
      <td class="actions"><button class="btn ghost small" data-clear="${esc(r.interface)}">Clear</button></td>
    </tr>`).join("");

  body.querySelectorAll("[data-clear]").forEach((btn) => {
    btn.addEventListener("click", withBusy(btn, async () => {
      try {
        const res = await api(`interfaces/${btn.dataset.clear}/clear-counters`, { method: "POST" });
        toast(res.message, "ok");
        state.liveBusy = false;
        refreshLive();
      } catch (err) {
        toast(err.message, "err");
      }
    }));
  });
}

function renderArp(rows) {
  const body = $("#arpBody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">ARP table is empty</td></tr>';
    return;
  }
  body.innerHTML = rows.map((r) => `
    <tr>
      <td class="mono">${esc(r.ip)}</td>
      <td class="mono">${esc(r.mac || "—")}</td>
      <td class="if-tag">${esc(r.interface)}</td>
      <td>${esc(r.state)}</td>
    </tr>`).join("");
}

/* ----------------------------------------------------------------- routes */

async function refreshRoutes() {
  if (!state.connected || state.routesBusy) return;
  state.routesBusy = true;
  try {
    const { routes } = await api("routes");
    state.routes = routes;
    renderRoutes(routes);
  } catch (err) {
    handlePollError(err);
  } finally {
    state.routesBusy = false;
  }
}

function renderRoutes(routes) {
  const body = $("#routesBody");
  if (!routes.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">No static routes configured</td></tr>';
    return;
  }
  body.innerHTML = routes.map((r, i) => `
    <tr>
      <td class="mono">${esc(r.network)}</td>
      <td class="mono">${esc(r.next_hops.join(", ") || "—")}</td>
      <td class="if-tag">${esc(r.interfaces.join(", ") || "—")}</td>
      <td class="actions">
        <button class="btn ghost small" data-edit-route="${i}" title="Edit route">✎ Edit</button>
        <button class="btn danger small" data-del-route="${i}" title="Delete route">🗑</button>
      </td>
    </tr>`).join("");

  body.querySelectorAll("[data-edit-route]").forEach((btn) =>
    btn.addEventListener("click", () => openRouteModal(state.routes[btn.dataset.editRoute])));

  body.querySelectorAll("[data-del-route]").forEach((btn) =>
    btn.addEventListener("click", withBusy(btn, async () => {
      const route = state.routes[btn.dataset.delRoute];
      if (!confirm(`Delete static route ${route.network}?`)) return;
      try {
        const res = await api("routes", {
          method: "DELETE",
          body: JSON.stringify({ network: route.network }),
        });
        toast(res.message, "ok");
        state.routesBusy = false;
        refreshRoutes();
      } catch (err) {
        toast(err.message, "err", 8000);
      }
    })));
}

let routeModalOriginal = null; // null => add mode

function openRouteModal(route) {
  routeModalOriginal = route ? route.network : null;
  $("#routeModalTitle").textContent = route ? `Edit Route — ${route.network}` : "Add Static Route";
  $("#routeNetwork").value = route ? route.network : "";
  $("#routeNextHop").value = route ? route.next_hops.join(", ") : "";
  $("#routeModal").hidden = false;
  $("#routeNetwork").focus();
}

function initRouteModal() {
  $("#addRouteBtn").addEventListener("click", () => openRouteModal(null));
  const save = $("#routeModalSave");
  save.addEventListener("click", withBusy(save, async () => {
    const payload = {
      network: $("#routeNetwork").value.trim(),
      next_hop: $("#routeNextHop").value.split(",")[0].trim(),
    };
    try {
      let res;
      if (routeModalOriginal) {
        res = await api("routes", {
          method: "PUT",
          body: JSON.stringify({ ...payload, original_network: routeModalOriginal }),
        });
      } else {
        res = await api("routes", { method: "POST", body: JSON.stringify(payload) });
      }
      toast(res.message, "ok");
      $("#routeModal").hidden = true;
      state.routesBusy = false;
      refreshRoutes();
    } catch (err) {
      toast(err.message, "err", 8000);
    }
  }));
}

/* ----------------------------------------------------------------- modals */

function initModals() {
  document.querySelectorAll(".modal-backdrop").forEach((bd) => {
    bd.addEventListener("click", (e) => { if (e.target === bd) bd.hidden = true; });
    bd.querySelectorAll("[data-close]").forEach((btn) =>
      btn.addEventListener("click", () => (bd.hidden = true)));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape")
      document.querySelectorAll(".modal-backdrop").forEach((bd) => (bd.hidden = true));
  });
}

/* ------------------------------------------------------------------- init */

initTheme();
initModals();
initIpModal();
initRouteModal();
initConnection();
$("#refreshIfBtn").addEventListener("click", () => loadInterfaces());
