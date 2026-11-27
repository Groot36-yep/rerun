"use strict";

/* Rerun web page. Plain JavaScript, no build step, nothing loaded from the internet. */

// ================================================================ helpers

const $ = (sel, root = document) => root.querySelector(sel);

function clean(kids) {
  return kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false)
    .map((k) => (k instanceof Node ? k : document.createTextNode(String(k))));
}
function fill(el, ...kids) { el.replaceChildren(...clean(kids)); return el; }
function add(el, ...kids) { el.append(...clean(kids)); return el; }

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "selected" || k === "disabled") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  return fill(el, ...kids);
}

const ICONS = {
  tv: "M3 7h18v12H3z M8 3l4 4 4-4",
  genre: "M4 6h16 M4 12h11 M4 18h7",
  star: "M12 3.5l2.5 5.3 5.8.7-4.3 4 1.1 5.8L12 16.6l-5.1 2.7 1.1-5.8-4.3-4 5.8-.7z",
  folder: "M3 6.5h6l2 2h10V19H3z",
  film: "M4 4h16v16H4z M8 4v16 M16 4v16 M4 9h4 M4 15h4 M16 9h4 M16 15h4",
  music: "M9 17V5l11-2v12 M9 17a2.5 2.5 0 1 1-5 0a2.5 2.5 0 0 1 5 0z M20 15a2.5 2.5 0 1 1-5 0a2.5 2.5 0 0 1 5 0z",
  snow: "M12 2v20 M3.3 7l17.4 10 M3.3 17L20.7 7",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1 M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  up: "M12 19V5 M6 11l6-6 6 6",
  search: "M10.5 17a6.5 6.5 0 1 1 0-13a6.5 6.5 0 0 1 0 13z M15.5 15.5L20 20",
  edit: "M4 20h4L19 9l-4-4L4 16z M13.5 6.5l4 4",
  play: "M8 5.5v13l10.5-6.5z",
};

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("class", "ico");
  s.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", ICONS[name] || ICONS.tv);
  s.append(p);
  return s;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Rerun": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); } catch { throw new Error("Can't reach Rerun. Is the server running?"); }
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (r.status === 401 && !path.startsWith("/api/auth/")) {
    location.hash = "#/login";
    throw new Error("Please sign in");
  }
  if (!r.ok) throw new Error((data && (data.error || data.detail)) || `Something went wrong (${r.status})`);
  return data;
}

let timers = [];
function later(fn, ms) { const id = setTimeout(fn, ms); timers.push(id); return id; }
function every(fn, ms) { const id = setInterval(fn, ms); timers.push(id); return id; }
function clearTimers() { timers.forEach((id) => { clearTimeout(id); clearInterval(id); }); timers = []; }

function toast(msg, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3200);
}

const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const plural = (n, word, many) => `${Number(n).toLocaleString()} ${n === 1 ? word : (many || word + "s")}`;

function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} minutes ago`;
  if (s < 129600) return `${Math.round(s / 3600)} hours ago`;
  return `${Math.round(s / 86400)} days ago`;
}

async function copyText(text, input) {
  try { await navigator.clipboard.writeText(text); }
  catch { if (input) { input.select(); document.execCommand("copy"); } }  // http pages can't use the clipboard API
  toast("Copied to the clipboard");
}

function copyField(value) {
  const inp = h("input", { value, readonly: true, onfocus: (e) => e.target.select() });
  return h("div", { class: "copy" }, inp, h("button", { class: "btn", onclick: () => copyText(value, inp) }, "Copy"));
}

function debounce(fn, ms) {
  let id;
  return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); };
}

function modal(build) {
  const bg = h("div", { class: "modal-bg", onclick: (e) => { if (e.target === bg) close(); } });
  const box = h("div", { class: "modal panel", role: "dialog", "aria-modal": "true" });
  const onKey = (e) => { if (e.key === "Escape") close(); };
  function close() { bg.remove(); document.removeEventListener("keydown", onKey); }
  document.addEventListener("keydown", onKey);
  bg.append(box);
  document.body.append(bg);
  build(box, close);
  return close;
}

function checkbox(label, checked, onchange, help) {
  return h("label", { class: "check" },
    h("input", { type: "checkbox", checked, onchange: (e) => onchange(e.target.checked) }),
    h("span", null, label, help ? h("span", { class: "muted" }, help) : null));
}

function toggle(label, checked, onchange) {
  return h("label", { class: "switch" },
    h("input", { type: "checkbox", checked, onchange: (e) => onchange(e.target.checked, e.target), "aria-label": label || "On" }),
    h("span", { class: "track", "aria-hidden": "true" }), label ? h("span", null, label) : null);
}

function select(options, value, onchange, attrs) {
  return h("select", { ...attrs, onchange: (e) => onchange(e.target.value) },
    options.map(([v, label]) => h("option", { value: v, selected: String(v) === String(value) }, label)));
}

function radioCards(name, options, value, onchange) {
  return h("div", { class: "radio-cards" }, options.map(([v, title, help]) => h("label", { class: "radio-card" },
    h("input", { type: "radio", name, value: v, checked: String(v) === String(value), onchange: () => onchange(v) }),
    h("div", null, title, help ? h("span", null, help) : null))));
}

// A filter box for long lists: calls onInput with the lower-cased text as you type.
function searchBox(placeholder, onInput) {
  return h("div", { class: "search" }, icon("search"),
    h("input", { type: "search", placeholder, "aria-label": placeholder, oninput: (e) => onInput(e.target.value.trim().toLowerCase()) }));
}

function describeInfo(i) {
  if (!i) return "";
  const parts = [];
  if (i.shows) parts.push(plural(i.shows, "show"));
  if (i.movies) parts.push(plural(i.movies, "movie"));
  if (i.specials) parts.push(plural(i.specials, "holiday episode"));
  if (i.music_videos) parts.push(plural(i.music_videos, "music video"));
  return parts.join(" · ");
}

// ================================================================ page frames

const NAV = [["#/channels", "Channels"], ["#/networks", "Networks"], ["#/holidays", "Holidays"], ["#/25days", "25 Days"], ["#/commercials", "Commercials"],
  ["#/looks", "Bumpers & Up next"], ["#/guide", "TV Guide"], ["#/settings", "Settings"], ["#/help", "Help"]];

function shell(active) {
  const page = NAV.find(([href]) => href === active);
  document.title = page ? `${page[1]} · Rerun` : "Rerun";
  fill($("#app"),
    h("header", { class: "masthead" }, h("div", { class: "masthead-inner" },
      h("a", { class: "brand", href: "#/channels", "aria-label": "Rerun home" }, h("img", { src: "/static/logo.svg", alt: "Rerun" })),
      h("nav", { class: "nav", "aria-label": "Main" }, NAV.map(([href, label]) => h("a", { href, class: href === active ? "on" : null }, label))))),
    h("main", { id: "view", class: "page" }));
  // On a phone the menu scrolls sideways: keep the page you're on in view.
  const nav = $(".nav"), on = $(".nav a.on");
  if (nav && on && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = on.offsetLeft - (nav.clientWidth - on.clientWidth) / 2;
  return $("#view");
}

function stage() {
  document.title = "Rerun";
  fill($("#app"), h("div", { class: "stage" },
    h("img", { class: "brand-hero", src: "/static/logo.svg", alt: "Rerun" }),
    h("div", { class: "tagline" }, "Television, the way it used to be"),
    h("main", { id: "view" })));
  return $("#view");
}

function pageHead(eyebrow, title, dek, ...actions) {
  return h("div", { class: "page-head" },
    h("div", null, h("div", { class: "eyebrow" }, eyebrow), h("h1", null, title), dek ? h("p", { class: "dek" }, dek) : null),
    actions.length ? h("div", { class: "actions" }, actions) : null);
}

function blockHead(title, count, ...extra) {
  return h("div", { class: "block-head" }, h("h2", null, title), count ? h("span", { class: "count" }, count) : null,
    extra.length ? h("div", { class: "row" }, extra) : null);
}

// ================================================================ sign in

function viewWelcome() {
  const v = stage();
  const a = h("input", { type: "password", id: "pw1", autocomplete: "new-password" });
  const b = h("input", { type: "password", id: "pw2", autocomplete: "new-password" });
  const err = h("p", { class: "error" });
  add(v, h("form", {
    class: "panel",
    onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      if (a.value.length < 8) { err.textContent = "Use at least 8 characters."; return; }
      if (a.value !== b.value) { err.textContent = "The two passwords don't match."; return; }
      try { await api("POST", "/api/auth/setup", { password: a.value }); location.hash = "#/setup/plex"; }
      catch (x) { err.textContent = x.message; }
    },
  },
    h("div", { class: "eyebrow" }, "Welcome"),
    h("h1", null, "Your own TV network"),
    h("p", { class: "lede" }, "Rerun turns your Plex library into live channels, complete with commercials and a TV guide. First, choose a password so only you can change things."),
    h("label", { for: "pw1" }, "Password"), a,
    h("label", { for: "pw2" }, "Type it again"), b,
    err,
    h("div", { class: "row", style: { marginTop: "18px" } }, h("button", { class: "btn primary big", type: "submit" }, "Get started"))));
  a.focus();
}

function viewLogin() {
  const v = stage();
  const pw = h("input", { type: "password", id: "pw", autocomplete: "current-password" });
  const err = h("p", { class: "error" });
  add(v, h("form", {
    class: "panel",
    onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      try { await api("POST", "/api/auth/login", { password: pw.value }); location.hash = "#/"; }
      catch (x) { err.textContent = x.message; }
    },
  },
    h("div", { class: "eyebrow" }, "Welcome back"),
    h("h1", null, "Sign in"),
    h("label", { for: "pw" }, "Password"), pw, err,
    h("div", { class: "row", style: { marginTop: "18px" } }, h("button", { class: "btn primary big", type: "submit" }, "Sign in")),
    h("p", { class: "hint", style: { marginTop: "22px" } }, "Forgot it? On the server, run ", h("code", null, "rerun reset-password"), ".")));
  pw.focus();
}

// ================================================================ setup

const STEPS = [["plex", "Plex"], ["libraries", "Libraries"], ["extras", "Lookups"],
  ["scan", "Scan"], ["channels", "Channels"], ["done", "Finish"]];

function wizard(step, back) {
  if (back) {
    const v = shell("#/settings");
    const body = h("section", { class: "panel" });
    add(v, h("p", null, h("a", { href: "#/settings" }, "← Back to Settings")), body);
    return body;
  }
  const v = stage();
  const at = STEPS.findIndex((s) => s[0] === step);
  add(v, h("ol", { class: "steps", "aria-label": "Setup steps" }, STEPS.map(([k, label], i) =>
    h("li", { class: i === at ? "on" : i < at ? "done" : null, "aria-current": i === at ? "step" : null }, label))));
  const body = h("section", { class: "panel wide" });
  add(v, body);
  return body;
}

function goNext(step, back) {
  const at = STEPS.findIndex((s) => s[0] === step);
  location.hash = back || `#/setup/${STEPS[at + 1][0]}`;
}

function stepHead(body, n, title, lede) {
  add(body, h("div", { class: "eyebrow" }, `Step ${n} of ${STEPS.length}`), h("h1", null, title), lede ? h("p", { class: "lede" }, lede) : null);
}

async function setupPlex(body, back) {
  stepHead(body, 1, "Connect to Plex", "Rerun reads your Plex libraries to build its channels. Your Plex sign-in stays on this server.");
  const status = h("div");
  const area = h("div", { class: "stack", style: { marginTop: "14px" } });
  add(body, status,
    h("div", { class: "row" }, h("button", { class: "btn primary big", onclick: () => plexSignIn(area, () => goNext("plex", back)) }, "Sign in with Plex")),
    area);

  const url = h("input", { placeholder: "http://192.168.1.10:32400" });
  const tok = h("input", { type: "password", placeholder: "Your Plex token", autocomplete: "off" });
  const merr = h("p", { class: "error" });
  add(body, h("details", { class: "subpanel" },
    h("summary", { class: "plain-label", style: { cursor: "pointer", fontWeight: 600, color: "var(--teal)" } }, "Or type in your server's address"),
    h("div", { class: "grid2" }, h("div", null, h("label", null, "Server address"), url), h("div", null, h("label", null, "Plex token"), tok)),
    h("p", { class: "hint" }, "To find your token: in Plex Web open any movie, choose ⋯ → Get Info → View XML, and copy the value after X-Plex-Token= in the address bar."),
    merr,
    h("button", {
      class: "btn", onclick: async (e) => {
        const btn = e.currentTarget;
        merr.textContent = "";
        btn.disabled = true;
        try { await api("POST", "/api/plex/manual", { url: url.value, token: tok.value }); toast("Connected to Plex"); goNext("plex", back); }
        catch (x) { merr.textContent = x.message; }
        btn.disabled = false;
      },
    }, "Connect")));

  try {
    const s = await api("GET", "/api/plex/status");
    if (s.connected) {
      fill(status, h("div", { class: "notice ok" }, `Connected to ${s.name || "Plex"} at ${s.url}.`),
        h("div", { class: "row", style: { marginBottom: "18px" } },
          h("button", { class: "btn primary", onclick: () => goNext("plex", back) }, "Keep this server"),
          h("span", { class: "hint" }, "or sign in again to switch.")));
    } else if (s.error) {
      fill(status, h("div", { class: "notice bad" }, s.error));
    }
  } catch { /* not connected yet */ }
}

async function plexSignIn(area, done) {
  fill(area, h("p", { class: "muted" }, "Opening Plex…"));
  const popup = window.open("", "plexauth", "width=600,height=760");
  let pin;
  try { pin = await api("POST", "/api/plex/pin"); }
  catch (x) { if (popup) popup.close(); fill(area, h("p", { class: "error" }, x.message)); return; }
  if (popup) popup.location.href = pin.url;
  fill(area,
    h("div", { class: "notice" }, "Approve Rerun in the Plex window that just opened. This page carries on by itself."),
    h("a", { href: pin.url, target: "_blank", rel: "noopener" }, "The Plex window didn't open? Open it here."));
  const started = Date.now();
  const poll = async () => {
    if (Date.now() - started > 10 * 60 * 1000) { fill(area, h("p", { class: "error" }, "Sign-in timed out. Please try again.")); return; }
    let r;
    try { r = await api("GET", `/api/plex/pin/${pin.id}`); }
    catch (x) { fill(area, h("p", { class: "error" }, x.message)); return; }
    if (!r.authorized) { later(poll, 2000); return; }
    if (popup && !popup.closed) popup.close();
    pickServer(area, pin.id, r.servers, done);
  };
  later(poll, 2000);
}

function pickServer(area, pinId, servers, done) {
  if (!servers.length) { fill(area, h("p", { class: "error" }, "You're signed in, but this Plex account doesn't have any servers.")); return; }
  const msg = h("p", { class: "error" });
  const choose = async (s, btn) => {
    msg.className = "muted";
    msg.textContent = `Connecting to ${s.name}…`;
    btn.disabled = true;
    try {
      const r = await api("POST", "/api/plex/use-server", { pin_id: pinId, server_id: s.id });
      toast(`Connected to ${r.name}`);
      done();
    } catch (x) { msg.className = "error"; msg.textContent = x.message; btn.disabled = false; }
  };
  fill(area, h("label", null, "Which server should Rerun use?"),
    h("div", { class: "choices" }, servers.map((s) => h("button", { class: "choice", onclick: (e) => choose(s, e.currentTarget) },
      h("strong", null, s.name), h("span", { class: "muted" }, s.owned ? "Your server" : "Shared with you")))),
    msg);
  if (servers.length === 1 && servers[0].owned) choose(servers[0], area.querySelector(".choice"));
}

function folderPicker(onPick) {
  modal(async (box, close) => {
    const pick = (path) => { onPick(path); close(); };
    const render = async (path) => {
      const typed = h("input", { value: path, placeholder: "Type or paste a folder, e.g. /mnt/media/commercials", "aria-label": "Folder path",
        onkeydown: (e) => { if (e.key === "Enter") render(typed.value); } });
      let d, err = "";
      try { d = await api("GET", "/api/folders?path=" + encodeURIComponent(path)); }
      catch (x) { err = x.message; d = null; }
      fill(box,
        h("div", { class: "eyebrow" }, "Choose a folder"),
        h("h2", { style: { margin: "6px 0 10px" } }, d && d.path ? d.path.split(/[\\/]/).filter(Boolean).pop() || d.path : "Where are they?"),
        h("div", { class: "copy" }, typed, h("button", { class: "btn", onclick: () => render(typed.value) }, "Go")),
        err ? h("p", { class: "error" }, err) : null,
        d ? h("div", { class: "folder-list" },
          d.parent ? h("div", { class: "folder-row" }, h("button", { class: "folder-open", onclick: () => render(d.parent) }, icon("up"), "Up a level")) : null,
          d.path && !d.parent ? h("div", { class: "folder-row" }, h("button", { class: "folder-open", onclick: () => render("") }, icon("up"), "All places")) : null,
          d.dirs.map((x) => h("div", { class: "folder-row" },
            h("button", { class: "folder-open", onclick: () => render(x.path), title: x.path }, icon("folder"), h("span", null, x.name, x.place && x.name !== x.path ? h("small", null, x.path) : null)),
            h("button", { class: "btn small", onclick: () => pick(x.path) }, "Use"))),
          !d.dirs.length ? h("p", { class: "hint", style: { padding: "10px 14px" } }, "No folders in here.") : null) : null,
        d && d.path ? h("p", { class: "hint" }, d.videos ? `${plural(d.videos, "video")} in this folder, plus everything in its sub-folders.` : "Everything in its sub-folders is included.") : null,
        h("div", { class: "row end", style: { marginTop: "14px" } },
          h("button", { class: "btn quiet", onclick: close }, "Cancel"),
          d && d.path ? h("button", { class: "btn primary", onclick: () => pick(d.path) }, "Use this folder") : null));
    };
    render("");
  });
}

function sourceEditor(list, libs, noneText) {
  const box = h("div");
  const render = () => {
    fill(box,
      list.length ? null : h("p", { class: "muted" }, noneText),
      list.map((s, i) => h("div", { class: "src-row" },
        icon(s.type === "plex" ? "tv" : "folder"),
        h("span", { class: "grow" }, s.type === "plex" ? `Plex library “${s.name}”` : s.path),
        h("button", { class: "btn small quiet", onclick: () => { list.splice(i, 1); render(); } }, "Remove"))),
      h("div", { class: "row", style: { marginTop: "12px" } },
        h("button", { class: "btn small", onclick: () => folderPicker((path) => { if (!list.some((x) => x.path === path)) list.push({ type: "folder", path }); render(); }) }, "Add a folder"),
        libs.length ? select([["", "Add a Plex library…"], ...libs.map((l) => [l.id, l.title])], "", (id) => {
          const l = libs.find((x) => x.id === id);
          if (l && !list.some((x) => x.type === "plex" && x.id === id)) list.push({ type: "plex", id: l.id, name: l.title });
          render();
        }, { class: "inline", style: { height: "32px", fontSize: "14px" } }) : null));
  };
  render();
  return box;
}

async function setupLibraries(body, back) {
  stepHead(body, 2, "Choose your libraries", "Pick what Rerun may put on your channels. You can change this any time in Settings.");
  let libs, src;
  try { [libs, src] = await Promise.all([api("GET", "/api/plex/libraries"), api("GET", "/api/sources")]); }
  catch (x) { add(body, h("p", { class: "error" }, x.message), h("a", { class: "btn", href: "#/setup/plex" }, "Back to Plex")); return; }
  const s = src.sources;
  const fresh = !["shows", "movies", "music_videos", "commercials"].some((k) => s[k].length);
  const special = /commercial|\bads?\b|bumper|promo|trailer|music ?video/i;
  const movieLibs = libs.filter((l) => l.type === "movie");
  if (fresh) {
    s.shows = libs.filter((l) => l.type === "show").map((l) => ({ type: "plex", id: l.id, name: l.title }));
    s.movies = movieLibs.filter((l) => !special.test(l.title)).map((l) => ({ type: "plex", id: l.id, name: l.title }));
    s.music_videos = movieLibs.filter((l) => /music ?video/i.test(l.title)).map((l) => ({ type: "plex", id: l.id, name: l.title }));
    s.commercials = movieLibs.filter((l) => /commercial|\bads?\b|bumper|promo/i.test(l.title)).map((l) => ({ type: "plex", id: l.id, name: l.title }));
  }
  const picks = (kind, type) => h("div", null,
    libs.filter((l) => l.type === type).map((l) => checkbox(l.title, s[kind].some((x) => x.type === "plex" && x.id === l.id), (on) => {
      s[kind] = s[kind].filter((x) => !(x.type === "plex" && x.id === l.id));
      if (on) s[kind].push({ type: "plex", id: l.id, name: l.title });
    })),
    libs.some((l) => l.type === type) ? null : h("p", { class: "muted" }, "No libraries of this kind in Plex."));

  add(body,
    h("div", { class: "grid2" },
      h("div", null, h("h3", null, "TV shows"), picks("shows", "show")),
      h("div", null, h("h3", null, "Movies"), picks("movies", "movie"))),
    h("div", { class: "subpanel" }, h("h3", null, "Commercials & bumpers"),
      h("p", { class: "hint" }, "Optional, but it's what makes it feel like real TV. Sort them into sub-folders such as “80s”, “90s” or “nick” and each channel can choose which to use."),
      sourceEditor(s.commercials, movieLibs, "None yet — shows will play back to back.")),
    h("div", { class: "subpanel" }, h("h3", null, "Music videos"),
      h("p", { class: "hint" }, "Optional, for a music video channel. Name files like “Artist - Song (1985).mp4” so the guide can show the artist and year."),
      sourceEditor(s.music_videos, movieLibs, "None.")));
  const err = h("p", { class: "error" });
  add(body, err, h("div", { class: "row end" }, h("button", {
    class: "btn primary big", onclick: async () => {
      err.textContent = "";
      if (!s.shows.length && !s.movies.length && !s.music_videos.length) { err.textContent = "Choose at least one library."; return; }
      try { await api("PUT", "/api/sources", s); goNext("libraries", back); }
      catch (x) { err.textContent = x.message; }
    },
  }, back ? "Save" : "Continue")));
}

async function setupExtras(body, back) {
  stepHead(body, 3, "Networks & logos", "Rerun groups shows by the network they first aired on — so you get a Nickelodeon channel, a Cartoon Network channel and so on, each with its real logo.");
  const st = await api("GET", "/api/settings");
  const meta = { ...st.metadata };
  let key = null;
  const keyInput = h("input", { type: "password", autocomplete: "off", placeholder: st.has_tmdb_key ? "A key is saved — type to replace it" : "Paste your TMDB API key", oninput: (e) => { key = e.target.value; } });
  const keyMsg = h("span", { class: "hint" });
  add(body,
    checkbox("Look up TV networks and logos online", meta.online_lookups, (v) => { meta.online_lookups = v; },
      "Asks TMDB (if you add a key) or TVMaze which network each show aired on, and fetches official logos. Only a show's ID number is ever sent — never your files, address or viewing."),
    checkbox("Include “Specials” seasons", meta.include_specials, (v) => { meta.include_specials = v; },
      "Season 0 is usually behind-the-scenes extras, so it's left out by default."),
    h("label", null, "TMDB key — optional, recommended"),
    h("div", { class: "copy" }, keyInput, h("button", {
      class: "btn", onclick: async () => {
        keyMsg.textContent = "Checking…";
        const r = await api("POST", "/api/settings/test-tmdb", { key: key || "" });
        keyMsg.textContent = r.ok ? "That key works." : "TMDB didn't accept that key.";
      },
    }, "Test")),
    keyMsg,
    h("p", { class: "hint" }, "Without a key Rerun still knows the logos of about 80 well-known networks. A TVMaze key isn't needed."));
  const err = h("p", { class: "error" });
  add(body, err, h("div", { class: "row end" }, h("button", {
    class: "btn primary big", onclick: async () => {
      try {
        await api("PUT", "/api/settings", { metadata: meta, ...(key !== null ? { tmdb_key: key } : {}) });
        goNext("extras", back);
      } catch (x) { err.textContent = x.message; }
    },
  }, back ? "Save" : "Scan my library")));
}

async function setupScan(body) {
  stepHead(body, 4, "Reading your library", "A minute or two for big libraries. Network lookups are remembered, so later scans are quick.");
  const step = h("p", { class: "muted" }, "Starting…");
  const bar = h("div", { class: "progress indeterminate" }, h("div"));
  const out = h("div");
  add(body, bar, step, out);
  try { await api("POST", "/api/scan"); } catch (x) { add(out, h("p", { class: "error" }, x.message)); return; }
  const poll = async () => {
    let p;
    try { p = await api("GET", "/api/scan"); } catch (x) { step.textContent = x.message; return; }
    step.textContent = p.step + (p.total ? ` — ${p.done} of ${p.total}` : p.done ? ` — ${p.done}` : "");
    bar.classList.toggle("indeterminate", !p.total);
    bar.firstChild.style.width = p.total ? `${Math.round((100 * p.done) / p.total)}%` : "";
    if (p.running) { later(poll, 1000); return; }
    bar.remove();
    if (p.error) {
      step.textContent = "";
      fill(out, h("div", { class: "notice bad" }, p.error),
        h("div", { class: "row" }, h("a", { class: "btn", href: "#/setup/libraries" }, "Back"), h("button", { class: "btn primary", onclick: () => route() }, "Try again")));
      return;
    }
    const c = p.catalog;
    step.textContent = "All done.";
    fill(out, h("div", { class: "stats" },
      [["shows", "TV shows"], ["episodes", "Episodes"], ["movies", "Movies"], ["music_videos", "Music videos"], ["commercials", "Commercials"]]
        .map(([k, label]) => h("div", { class: "stat" }, h("b", null, (c[k] || 0).toLocaleString()), h("span", null, label)))),
    h("div", { class: "row end" }, h("button", { class: "btn primary big", onclick: () => goNext("scan") }, "Choose channels")));
  };
  poll();
}

const KIND_ICON = { tv: "tv", movies: "film", music: "music", holiday: "snow" };

function suggestionPicker(list) {
  const chosen = new Set(list.filter((s) => s.recommended).map((s) => s.key));
  const groups = {};
  list.forEach((s) => { (groups[s.group] = groups[s.group] || []).push(s); });
  const boxes = {};
  const el = h("div", null, Object.entries(groups).map(([g, items]) => h("div", { class: "subpanel" },
    h("div", { class: "row" }, h("h3", { style: { margin: 0 } }, g), h("span", { class: "spacer" }),
      h("button", { class: "btn small quiet", onclick: () => items.forEach((s) => { chosen.add(s.key); boxes[s.key].checked = true; }) }, "Select all"),
      h("button", { class: "btn small quiet", onclick: () => items.forEach((s) => { chosen.delete(s.key); boxes[s.key].checked = false; }) }, "Clear")),
    h("div", { class: "picks" }, items.map((s) => {
      const box = h("input", { type: "checkbox", checked: chosen.has(s.key), onchange: (e) => { e.target.checked ? chosen.add(s.key) : chosen.delete(s.key); } });
      boxes[s.key] = box;
      const pic = s.logo_url ? h("img", { src: s.logo_url, alt: "", loading: "lazy" })
        : s.season ? h("img", { src: `/holidaylogo/${s.season}.png`, alt: "", loading: "lazy" })
          : h("span", { class: "glyph" }, icon(KIND_ICON[s.kind] || "tv"));
      return h("label", { class: "pick" }, box, pic, h("div", null, h("strong", null, s.name), h("span", null, s.why)));
    })))));
  return { el, chosen };
}

async function setupChannels(body, back) {
  stepHead(body, 5, "Choose your channels", "Every network in your library, plus a few extras. Channels keep themselves up to date as you add shows to Plex.");
  const r = await api("GET", "/api/suggestions");
  if (!r.suggestions.length) {
    add(body, h("div", { class: "notice" }, "Rerun couldn't find enough in your library to suggest channels. You can still build your own."),
      h("div", { class: "row end" }, h("button", { class: "btn primary", onclick: () => goNext("channels", back) }, "Continue")));
    return;
  }
  if (!r.has_commercials) add(body, h("div", { class: "notice" }, "No commercials were found, so shows will play back to back. You can add a commercials folder later in Settings."));
  const { el, chosen } = suggestionPicker(r.suggestions);
  add(body, el);
  const err = h("p", { class: "error" });
  add(body, err, h("div", { class: "row end" },
    h("button", { class: "btn quiet", onclick: () => goNext("channels", back) }, "Skip"),
    h("button", {
      class: "btn primary big", onclick: async () => {
        if (!chosen.size) { err.textContent = "Choose at least one channel, or skip."; return; }
        try {
          const res = await api("POST", "/api/channels/from-suggestions", { keys: [...chosen], dismiss_others: true });
          toast(`${plural(res.created, "channel")} created`);
          goNext("channels", back);
        } catch (x) { err.textContent = x.message; }
      },
    }, "Create channels")));
}

async function addToPlexContent(body) {
  const c = await api("GET", "/api/connect");
  add(body,
    c.note ? h("div", { class: "notice" }, c.note) : null,
    h("ol", { class: "how" },
      h("li", null, h("div", null, "In Plex Web, open ", h("b", null, "Settings → Live TV & DVR → Set Up Plex DVR"), ". Live TV needs Plex Pass.")),
      h("li", null, h("div", null, "Plex looks for tuners. Choose ", h("b", null, "“Don't see your HDHomeRun device? Enter its network address manually”"), " and paste:", copyField(c.tuner.replace(/^https?:\/\//, "")))),
      h("li", null, h("div", null, "Plex finds Rerun with ", plural(c.channels, "channel"), ". Choose ", h("b", null, "Continue"), ".")),
      h("li", null, h("div", null, "When asked about a guide, choose ", h("b", null, "“Have an XMLTV guide on your server?”"), " and paste:", copyField(c.xmltv))),
      h("li", null, h("div", null, "Plex matches the channels itself. Choose ", h("b", null, "Continue"), " — your channels are now under ", h("b", null, "Live TV"), "."))),
    h("p", { class: "hint", style: { marginTop: "16px" } }, "Added or removed channels later? In Plex, open Settings → Live TV & DVR → Rerun, then “Scan channels” and “Refresh guide”."),
    h("h3", null, "Other apps"),
    h("p", { class: "hint" }, "VLC, Jellyfin, TiviMate and Channels can use this playlist, which includes the guide:"),
    copyField(c.m3u));
}

async function setupDone(body) {
  stepHead(body, 6, "Put it on Plex", "Last step. Plex sees Rerun as a TV tuner, just like an antenna box.");
  await addToPlexContent(body);
  add(body, h("div", { class: "row end", style: { marginTop: "24px" } }, h("button", {
    class: "btn primary big", onclick: async () => { await api("POST", "/api/setup/complete"); location.hash = "#/channels"; },
  }, "Finish")));
}

async function viewSetup(step, back) {
  const fns = { plex: setupPlex, libraries: setupLibraries, extras: setupExtras, scan: setupScan, channels: setupChannels, done: setupDone };
  const fn = fns[step];
  if (!fn) { location.hash = "#/setup/plex"; return; }
  await fn(wizard(step, back), back);
}

// ================================================================ channels: tonight's listing

function listing(ch, reload) {
  const n = ch.now;
  const now = Date.now() / 1000;
  const offSeason = ch.season && !ch.in_season;
  let nowCell;
  if (!ch.enabled) {
    nowCell = h("div", { class: "l-now" }, h("span", { class: "tag" }, "Off the air"), h("b", null, "Turned off"));
  } else if (offSeason) {
    nowCell = h("div", { class: "l-now" }, h("span", { class: "tag" }, "Off season"), h("b", null, `Returns ${ch.season_dates.split(" – ")[0]}`));
  } else if (!n) {
    nowCell = h("div", { class: "l-now" }, h("span", { class: "tag" }, "Warming up"), h("b", null, "Building the schedule…"));
  } else {
    const pct = Math.min(100, Math.max(0, (100 * (now - n.s)) / (n.e - n.s)));
    nowCell = h("div", { class: "l-now" },
      h("span", { class: "tag" }, "On now"),
      h("b", { title: n.title }, n.title || "—"),
      n.sub ? h("div", { class: "l-sub", title: n.sub }, n.sub) : null,
      h("div", { class: "meter" }, h("i", { style: { width: pct + "%" } })),
      h("div", { class: "l-time" }, `${fmtTime(n.s)} – ${fmtTime(n.e)}`));
  }
  const nextCell = ch.next && ch.enabled && !offSeason
    ? h("div", { class: "l-next" }, h("span", { class: "tag" }, `Next · ${fmtTime(ch.next.s)}`), h("b", { title: ch.next.title }, ch.next.title))
    : h("div", { class: "l-next" });
  const onOff = toggle(null, ch.enabled, async (val, input) => {
    input.disabled = true;
    try {
      await api("PUT", `/api/channels/${ch.id}`, { enabled: val });
      toast(val ? `${ch.name} is back on the air` : `${ch.name} is off the air`);
    } catch (x) { toast(x.message, true); }
    reload();
  });
  onOff.title = ch.enabled ? "On the air. Click to take it off." : "Off the air. Click to put it back on.";
  $("input", onOff).setAttribute("aria-label", `${ch.name} on the air`);
  return h("div", { class: "listing" + (ch.enabled ? "" : " is-off"), "data-find": `${ch.number} ${ch.name} ${ch.group || ""}`.toLowerCase() },
    h("div", { class: "l-num" }, ch.number),
    h("img", { class: "l-logo", src: ch.logo_url, alt: "" }),
    h("div", { class: "l-name" }, h("a", { href: `#/channel/${ch.id}` }, ch.name),
      ch.quick && ch.quick.until && ch.quick.until > now ? h("span", { class: "on-air" }, `Test mode until ${fmtTime(ch.quick.until)}`)
        : ch.live ? h("span", { class: "on-air" }, "Being watched") : h("span", null, ch.season ? `Holiday channel · ${ch.season_dates}` : describeInfo(ch.info))),
    nowCell, nextCell,
    h("div", { class: "l-actions" },
      onOff,
      h("button", { class: "icon-btn", title: "Preview: watch what's coming up, breaks and all", "aria-label": `Preview ${ch.name}`, onclick: () => openPreview(ch) }, icon("play")),
      h("a", { class: "icon-btn", href: `#/channel/${ch.id}`, title: "Edit this channel", "aria-label": `Edit ${ch.name}` }, icon("edit")),
      h("button", { class: "icon-btn", title: "Copy the stream address (for VLC and other players)", "aria-label": `Copy the stream address for ${ch.name}`, onclick: () => copyText(`${location.origin}/stream/${ch.number}.ts`) }, icon("link"))));
}

// ================================================================ preview: watch what's coming up

const KIND_LABEL = { show: "Show", ident: "Ident", bumper: "Bumper", commercial: "Commercial", trivia: "Trivia", card: "Card" };

function fmtStamp(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function fmtLen(s) {
  s = Math.round(s);
  if (s < 60) return `0:${String(s).padStart(2, "0")}`;
  if (s < 3600) return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/* Checking a channel without waiting for live TV: watch its next break right here, or put the channel
   itself in test mode for half an hour and watch it in Plex. Then what's coming up, show by show. */
function openPreview(ch) {
  modal(async (box, close) => {
    box.classList.add("wide", "pv");
    const screen = h("div", { class: "pv-screen", hidden: true });
    const caption = h("p", { class: "pv-caption" });
    const testBox = h("div", { class: "pv-card" });
    const list = h("div", { class: "pv-log" }, h("p", { class: "muted" }, "Reading the schedule…"));
    let busy = false, data = null;

    async function watch(at, length, what) {
      if (busy) return;
      busy = true;
      box.querySelectorAll(".pv-watch").forEach((b) => { b.disabled = true; });
      at = Math.max(at, Date.now() / 1000 + 1);
      screen.hidden = false;
      fill(screen, h("div", { class: "pv-idle" }, h("div", { class: "spinner" }), h("b", null, "Making the video…"),
        h("span", null, `${fmtLen(length)} of ${what}, made the way live TV makes it. Give it a few seconds.`)));
      fill(caption);
      screen.scrollIntoView({ behavior: "smooth", block: "nearest" });
      try {
        const r = await api("POST", `/api/channels/${ch.id}/watch`, { at, length });
        fill(screen, h("video", { src: r.url, controls: true, autoplay: true, playsinline: true }));
        fill(caption, `${what[0].toUpperCase()}${what.slice(1)}: airs ${fmtStamp(r.at)} to ${fmtStamp(r.at + r.length)}.`);
      } catch (x) {
        fill(screen, h("div", { class: "pv-idle" }, h("b", null, "Couldn't make the video"), h("span", null, x.message)));
      } finally {
        busy = false;
        box.querySelectorAll(".pv-watch").forEach((b) => { b.disabled = false; });
      }
    }
    const watchBtn = (label, cls, fn) => h("button", { class: `${cls} pv-watch`, onclick: fn }, icon("play"), ` ${label}`);

    // a break, watched from 40 seconds before it (to see the show go to it, with its "Up next") to the next show
    const watchBreak = (g) => watch(g.s - (g.after ? 40 : 0), Math.min(g.e - g.s + (g.after ? 40 : 0) + 12, 180), "the break");

    const drawTest = () => {
      const q = ch.quick || {};
      const on = q.until && q.until * 1000 > Date.now();
      if (ch.plan && ch.plan.name) {
        fill(testBox, h("b", null, "Test it in Plex"),
          h("p", null, "This channel follows a fixed historical schedule, so it can't be sped up. Use “Watch the next break” above."));
        return;
      }
      if (q.of && !q.until) {
        fill(testBox, h("b", null, "This is the old test channel"),
          h("p", null, "Rerun doesn't need it any more: any channel can go into test mode now. You can remove it."),
          h("button", { class: "btn", onclick: async () => { await api("DELETE", "/api/test-channel"); close(); location.reload(); } }, "Remove this test channel"));
        return;
      }
      fill(testBox,
        h("b", null, on ? `Test mode is on until ${fmtTime(q.until)}` : "Test it in Plex"),
        h("p", null, on
          ? `Tune Plex to channel ${ch.number}. It plays a minute or so of each show, so a bumper, commercials, “Up next” and the next show's intro come round every few minutes. It goes back to normal by itself at ${fmtTime(q.until)}.`
          : `Turns channel ${ch.number} into a sped-up version of itself for 30 minutes: a minute or so of each show, then its break, so you see every bumper, commercial and “Up next” in Plex within minutes. Nothing to set up in Plex: just tune to channel ${ch.number}. Anyone watching it is switched to the test too.`),
        h("div", { class: "row" }, on
          ? h("button", { class: "btn", onclick: async () => {
            try { await api("DELETE", `/api/channels/${ch.id}/test`); ch.quick = {}; toast(`${ch.name} is back to normal`); drawTest(); loadList(); }
            catch (x) { toast(x.message, true); }
          } }, "Stop the test")
          : h("button", { class: "btn primary", onclick: async () => {
            try {
              const r = await api("POST", `/api/channels/${ch.id}/test`, { minutes: 30 });
              ch.quick = { of: ch.id, until: r.until };
              toast(`Test mode is on: tune Plex to channel ${r.number}`);
              drawTest();
              loadList();
            } catch (x) { toast(x.message, true); }
          } }, "Start a 30-minute test")));
    };

    add(box,
      h("div", { class: "pv-head" },
        h("div", null, h("div", { class: "eyebrow" }, `Channel ${ch.number}`), h("h2", null, `Check ${ch.name}`)),
        h("button", { class: "icon-btn", "aria-label": "Close", title: "Close", onclick: close }, "×")),
      h("div", { class: "pv-cards" },
        h("div", { class: "pv-card" },
          h("b", null, "Watch it here"),
          h("p", null, "A short video of the next break, as it will air: the end of the show with its “Up next”, the bumper, the commercials and the next show's intro. Live TV isn't touched."),
          h("div", { class: "row" },
            watchBtn("Watch the next break", "btn primary", () => { const g = nextBreak(); if (g) watchBreak(g); else toast("No break coming up soon", true); }),
            watchBtn("What's on now", "btn", () => watch(Date.now() / 1000, 45, "what's on now")))),
        testBox),
      screen, caption,
      h("h3", { class: "pv-sub" }, "Coming up"),
      list);
    drawTest();

    const nextBreak = () => (data || []).find((g) => g.kind === "break" && g.s > Date.now() / 1000 + 5);

    async function loadList() {
      try { data = null; const r = await api("GET", `/api/channels/${ch.id}/rundown?hours=3`); data = group(r.items); }
      catch (x) { fill(list, h("p", { class: "error" }, x.message)); return; }
      const counts = (b) => {
        const c = {};
        b.forEach((it) => { c[it.kind] = (c[it.kind] || 0) + 1; });
        return ["bumper", "commercial", "ident", "card"].filter((k) => c[k]).map((k) => plural(c[k], KIND_LABEL[k].toLowerCase())).join(", ");
      };
      fill(list, data.length ? data.map((g) => g.kind === "show"
        ? h("div", { class: "pv-line show" },
          h("span", { class: "pv-t" }, fmtTime(g.s)),
          h("div", null, h("b", null, g.title), g.up_next ? h("small", null, `“Up next: ${g.up_next}” near the end`) : null))
        : h("div", { class: "pv-line break" },
          h("span", { class: "pv-t" }, fmtTime(g.s)),
          h("div", null,
            h("span", null, `Break, ${fmtLen(g.e - g.s)}: `, h("span", { class: "muted" }, counts(g.items) || "nothing")),
            h("details", null, h("summary", null, "What's in it"),
              h("ol", { class: "pv-items" }, g.items.map((it) => h("li", null,
                h("span", { class: `pv-k k-${it.kind}` }, KIND_LABEL[it.kind] || it.kind), " ",
                it.title || (it.kind === "card" ? "Channel card" : "—"), h("span", { class: "muted" }, ` · ${fmtLen(it.e - it.s)}`)))))),
          watchBtn("Watch", "btn small", () => watchBreak(g))))
        : h("p", { class: "muted" }, "Nothing scheduled yet."));
    }

    // a show and its parts become one line; the bumpers, commercials and idents between shows, one break
    function group(items) {
      const out = [];
      for (const it of items) {
        const last = out[out.length - 1];
        if (it.kind === "show") {
          if (last && last.kind === "show" && last.title === (it.block || it.title)) { last.e = it.e; last.up_next = last.up_next || it.up_next; continue; }
          out.push({ kind: "show", s: it.s, e: it.e, title: it.block || it.title, up_next: it.up_next });
        } else if (last && last.kind === "break") {
          last.items.push(it); last.e = it.e;
        } else {
          out.push({ kind: "break", s: it.s, e: it.e, items: [it], after: !!last });
        }
      }
      return out;
    }
    loadList();
  });
}

// ================================================================ shows to get (Nick, Disney Channel)

/* A network's best-known shows of the 90s, 2000s and 2010s against your library: what to get, what you
   have but isn't on the channel (Plex files it under another network), and what's on it already. */
function missingView(rep, onAdd) {
  const tierName = { staple: "aired all the time", regular: "aired often", occasional: "now and then" };
  const copy = h("button", { class: "btn small", onclick: async () => { copyText(await (await fetch(`/api/missing/${rep.key}.txt`)).text()); } }, "Copy the list to get");
  return h("div", { class: "missing" },
    h("div", { class: "missing-head" },
      h("p", { class: "lede" }, `You have ${rep.have} of the ${rep.total} ${rep.network} shows on this list. Get the ones that “aired all the time” first.`),
      h("div", { class: "row" }, copy, h("a", { class: "btn small quiet", href: `/api/missing/${rep.key}.txt`, target: "_blank" }, "Open as text"))),
    decadeTabs(rep, onAdd, tierName));
}

function decadeTabs(rep, onAdd, tierName) {
  const panel = h("div", { class: "decade" });
  const tabs = h("div", { class: "decade-tabs", role: "tablist" });
  const show = (d) => {
    tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", b.dataset.d === d.decade ? "true" : "false"));
    const need = d.shows.filter((x) => !x.have);
    const elsewhere = d.shows.filter((x) => x.have && onAdd && !x.on_channel);
    const got = d.shows.filter((x) => x.have && (!onAdd || x.on_channel));
    fill(panel,
      h("h4", { class: "tier" }, need.length ? `To get · ${need.length}` : "To get"),
      need.length ? h("ul", { class: "want-list" }, need.map((x) => h("li", { class: `tier-${x.tier}` },
        h("b", null, x.title), h("span", { class: "muted" }, ` (${x.year}) · ${tierName[x.tier]}`)))) : h("p", { class: "hint" }, "You have them all."),
      elsewhere.length ? [h("h4", { class: "tier" }, `In your library, not on this channel · ${elsewhere.length}`),
        h("ul", { class: "want-list" }, elsewhere.map((x) => {
          const b = h("button", { class: "btn small", onclick: () => { onAdd({ id: x.id, title: x.match || x.title }); b.disabled = true; b.textContent = "Added"; } }, "Add");
          return h("li", { class: "elsewhere" }, h("span", null, h("b", null, x.title), h("span", { class: "muted" }, ` · ${plural(x.episodes, "episode")}`)), b);
        }))] : null,
      got.length ? [h("h4", { class: "tier" }, `${onAdd ? "On this channel" : "In your library"} · ${got.length}`),
        h("ul", { class: "want-list have" }, got.map((x) => h("li", null, "✓ ", x.title)))] : null);
  };
  rep.decades.forEach((d) => add(tabs, h("button", { role: "tab", "data-d": d.decade, onclick: () => show(d) },
    h("b", null, d.decade), h("small", null, `${d.have} of ${d.total}`))));
  show(rep.decades.find((d) => d.shows.some((x) => !x.have)) || rep.decades[0]);
  return h("div", null, tabs, panel);
}

function openMissing(key) {
  modal(async (box, close) => {
    box.classList.add("wide");
    add(box, h("div", { class: "pv-head" }, h("div", null, h("div", { class: "eyebrow" }, "Shows to get"), h("h2", null, "Loading…")),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, "×")));
    try {
      const rep = await api("GET", `/api/missing/${key}`);
      $("h2", box).textContent = `${rep.network}: 90s, 2000s and 2010s`;
      add(box, missingView(rep, null));
    } catch (x) { add(box, h("p", { class: "error" }, x.message)); }
  });
}

function summaryBar(items) {
  return h("div", { class: "summary" }, items.map(([value, label]) => h("div", null, h("b", null, value), h("span", null, label))));
}

async function viewChannels() {
  const v = shell("#/channels");
  const body = h("div");
  const head = pageHead("Tonight on Rerun", "Channels", "Loading…",
    h("button", { class: "btn", onclick: rescan }, "Rescan library"),
    h("a", { class: "btn primary", href: "#/channel/new" }, "New channel"));
  const dek = $(".dek", head);
  const summary = h("div");
  let query = "";
  const bar = h("div", { class: "toolbar slim" }, searchBox("Find a channel by name or number", (q) => { query = q; applyFilter(); }));
  add(v, head, summary, body);

  function applyFilter() {
    body.querySelectorAll(".listing").forEach((row) => { row.hidden = !!query && !row.dataset.find.includes(query); });
    body.querySelectorAll("section.block").forEach((sec) => { sec.hidden = !sec.querySelector(".listing:not([hidden])"); });
    const none = $(".no-match", body);
    if (none) none.hidden = !query || !!body.querySelector(".listing:not([hidden])");
  }

  async function rescan() {
    try { await api("POST", "/api/scan"); toast("Scanning your library…"); later(load, 4000); } catch (x) { toast(x.message, true); }
  }

  async function load() {
    let chans, scan;
    try { [chans, scan] = await Promise.all([api("GET", "/api/channels"), api("GET", "/api/scan")]); }
    catch (x) { fill(body, h("p", { class: "error" }, x.message)); return; }
    const live = chans.filter((c) => c.enabled).length;
    const c = scan.catalog || {};
    dek.textContent = scan.running ? `Scanning your library: ${scan.step}…` : `Library last scanned ${fmtAgo(c.scanned_at)}.`;
    fill(summary, chans.length ? summaryBar([
      [live, live === 1 ? "Channel on the air" : "Channels on the air"],
      [chans.filter((x) => x.live).length, "Being watched now"],
      [(c.shows || 0).toLocaleString(), "TV shows"],
      [(c.movies || 0).toLocaleString(), "Movies"],
      [(c.commercials || 0).toLocaleString(), "Commercials"],
    ]) : null);
    if (!chans.length) {
      fill(body, h("div", { class: "empty" },
        h("img", { src: "/static/arcs.svg", alt: "" }),
        h("h2", null, "Nothing on the air yet"),
        h("p", null, "Switch on networks from your library, or build a channel of your own."),
        h("div", { class: "row", style: { justifyContent: "center" } },
          h("a", { class: "btn primary", href: "#/networks" }, "Choose networks"),
          h("a", { class: "btn", href: "#/channel/new" }, "Build a channel"))));
      return;
    }
    const groups = new Map();
    for (const ch of chans) {
      const g = ch.group || "My Channels";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(ch);
    }
    fill(body,
      chans.length > 6 ? bar : null,
      [...groups.entries()].map(([g, list]) => h("section", { class: "block" }, blockHead(g, plural(list.length, "channel")), list.map((ch) => listing(ch, load)))),
      h("p", { class: "no-match", hidden: true }, "No channel matches that."),
      h("div", { class: "add-row" },
        h("a", { class: "btn", href: "#/networks" }, "Switch on more networks"),
        h("a", { class: "btn", href: "#/holidays" }, "Holiday channels"),
        h("a", { class: "btn", href: "#/channel/new" }, "Build your own")));
    applyFilter();
  }
  load();
  every(load, 30000);
}

// ================================================================ networks

async function viewNetworks() {
  const v = shell("#/networks");
  add(v, pageHead("Your library", "Networks", "Every TV network found in your library. Switch one on and it becomes a channel with its official logo; switch it off and the channel goes away. Your shows are never touched."));
  const bar = h("div", { class: "toolbar" });
  const body = h("div");
  const classics = await classicChannels();
  add(v, classics, bar, body);
  const st = await api("GET", "/api/settings");
  const auto = { ...st.auto };
  let query = "";
  const filterCards = () => {
    body.querySelectorAll(".net-card").forEach((c) => { c.hidden = !!query && !c.dataset.find.includes(query); });
    body.querySelectorAll("section.block").forEach((sec) => { sec.hidden = !sec.querySelector(".net-card:not([hidden])"); });
    const small = $("details.more", body);
    if (small && query) small.open = true;
  };
  fill(bar,
    searchBox("Find a network", (q) => { query = q; filterCards(); }),
    h("div", { class: "row" },
      h("span", { class: "plain-label" }, "Smallest network that gets a channel"),
      select([[1, "1 show"], [2, "2 shows"], [3, "3 shows"], [5, "5 shows"], [10, "10 shows"]], auto.min_shows, async (val) => {
        auto.min_shows = parseInt(val, 10);
        await api("PUT", "/api/settings", { auto });
        load();
      }, { class: "inline" })),
    h("div", { class: "toolbar-foot" },
      toggle("Add new networks automatically after each scan", auto.networks, async (val) => {
        auto.networks = val;
        await api("PUT", "/api/settings", { auto });
        toast(val ? "New networks will be added automatically" : "Rerun won't add networks by itself");
      }),
      st.dismissed ? h("button", {
        class: "btn small quiet", title: "Networks you switched off stay off until you bring them back",
        onclick: async () => { const r = await api("POST", "/api/auto/restore"); toast(`Brought back ${plural(r.created, "channel")}`); load(); },
      }, "Bring back switched-off networks") : null));

  async function load() {
    let r;
    try { r = await api("GET", "/api/networks"); } catch (x) { fill(body, h("p", { class: "error" }, x.message)); return; }
    if (!r.networks.length) {
      fill(body, h("div", { class: "empty" }, h("img", { src: "/static/arcs.svg", alt: "" }), h("h2", null, "No networks found yet"),
        h("p", null, "Rescan your library, or add a TMDB key in Settings so Rerun can look networks up.")));
      return;
    }
    const card = (n) => {
      const on = !!n.channel;
      return h("div", { class: "net-card" + (on ? " on" : ""), "data-find": [n.name, ...n.sisters].join(" ").toLowerCase() },
        h("div", { class: "frame" }, h("img", { src: n.logo_url, alt: "", loading: "lazy" })),
        h("b", null, n.name),
        h("div", { class: "meta" }, plural(n.shows, "show"), n.sisters.length ? ` · with ${n.sisters.join(", ")}` : ""),
        n.missing ? h("button", { class: "btn small quiet net-missing", onclick: () => openMissing(n.missing) }, "Shows to get") : null,
        h("div", { class: "foot" },
          h("span", null, on ? `Channel ${n.number}` : n.dismissed ? "Switched off" : "Not on the air"),
          toggle(null, on, async (val, input) => {
            input.disabled = true;
            try {
              if (val) { const res = await api("POST", `/api/networks/${encodeURIComponent(n.name)}/channel`); toast(`${n.name} is on channel ${res.number}`); }
              else { await api("DELETE", `/api/networks/${encodeURIComponent(n.name)}/channel`); toast(`${n.name} switched off`); }
            } catch (x) { toast(x.message, true); }
            load();
          })));
    };
    const big = r.networks.filter((n) => n.eligible || n.channel);
    const small = r.networks.filter((n) => !n.eligible && !n.channel);
    const groups = new Map();
    big.forEach((n) => { if (!groups.has(n.group)) groups.set(n.group, []); groups.get(n.group).push(n); });
    const onAir = r.networks.filter((n) => n.channel).length;
    fill(body, h("div", { style: { marginTop: "30px" } },
      h("p", { class: "block-note", style: { marginBottom: "26px" } }, `${plural(onAir, "network")} on the air out of ${r.networks.length} in your library.`),
      [...groups.entries()].map(([g, list]) => h("section", { class: "block" }, blockHead(g, plural(list.length, "network")), h("div", { class: "net-grid" }, list.map(card)))),
      small.length ? h("section", { class: "block" },
        h("details", { class: "more", open: small.length <= 8 },
          h("summary", null, blockHead("Smaller networks", plural(small.length, "network"))),
          h("p", { class: "block-note" }, "Only a show or two each, so they don't get a channel on their own — but you can switch any of them on."),
          h("div", { class: "net-grid" }, small.map(card)))) : null));
    filterCards();
  }
  load();
}

// ================================================================ holidays

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function datePicker(md, onchange) {
  let [m, d] = (md || "01-01").split("-").map((x) => parseInt(x, 10));
  const emit = () => onchange(`${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`);
  return h("div", { class: "row" },
    select(MONTHS.map((name, i) => [i + 1, name]), m, (val) => { m = parseInt(val, 10); emit(); }, { class: "inline" }),
    select(Array.from({ length: 31 }, (_, i) => [i + 1, String(i + 1)]), d, (val) => { d = parseInt(val, 10); emit(); }, { class: "inline" }));
}

async function viewHolidays() {
  const v = shell("#/holidays");
  add(v, pageHead("Seasonal programming", "Holidays", "In season, your channels play their holiday episodes — and keep them on the shelf the rest of the year. Add a holiday channel to run nothing but holiday shows while the season lasts."));
  const body = h("div");
  add(v, body);
  async function load() {
    let r;
    try { r = await api("GET", "/api/holidays"); } catch (x) { fill(body, h("p", { class: "error" }, x.message)); return; }
    const row = (hd) => {
      const conf = { start: hd.start, end: hd.end, words: hd.words.join("\n"), enabled: hd.enabled };
      const save = async (patch, msg) => {
        try { await api("PUT", `/api/holidays/${hd.key}`, patch); if (msg) toast(msg); load(); } catch (x) { toast(x.message, true); }
      };
      const total = hd.episodes + hd.movies;
      const words = h("textarea", { placeholder: "One per line — for example: snow day", oninput: (e) => { conf.words = e.target.value; } });
      words.value = conf.words;
      return h("div", { class: "hol-row" },
        h("img", { src: hd.logo_url, alt: "" }),
        h("div", null,
          h("h3", null, hd.name, hd.on_now && hd.enabled ? h("span", { class: "badge" }, "In season") : null),
          h("div", { class: "dates" }, hd.enabled ? hd.when : "Switched off"),
          h("div", { class: "found" }, total
            ? `${[hd.episodes ? plural(hd.episodes, "episode") : null, hd.movies ? plural(hd.movies, "movie") : null].filter(Boolean).join(" and ")} found in your library.`
            : "Nothing found in your library yet. Add words to look for under “Dates & keywords”."),
          hd.samples.length ? h("div", { class: "examples", title: hd.samples.join(" · ") }, hd.samples.join(" · ")) : null),
        h("div", { class: "acts" },
          toggle("Play on my channels", hd.mix && hd.enabled, (val) => save({ mix: val }, val ? `${hd.name} episodes will play in season` : `${hd.name} episodes won't be mixed in`)),
          hd.channels.map((c) => h("a", { class: "btn small", href: `#/channel/${c.id}` }, `Edit ${c.name}`)),
          total ? h("div", { class: "row" }, [
            ["tv", "TV channel", hd.episodes], ["movies", "Movie channel", hd.movies], ["both", "Mixed channel", hd.episodes && hd.movies],
          ].filter(([kind, , have]) => have && !hd.channels.some((c) => c.kind === kind)).map(([kind, label]) => h("button", {
            class: "btn small" + (kind === "tv" ? " primary" : ""), title: kind === "tv" ? "Holiday episodes only" : kind === "movies" ? "Holiday movies only" : "Episodes and movies",
            onclick: async () => {
              try { const res = await api("POST", `/api/holidays/${hd.key}/channel`, { kind }); toast(`Added on channel ${res.number}`); load(); }
              catch (x) { toast(x.message, true); }
            },
          }, `Add ${label}`))) : null),
        h("details", null, h("summary", null, "Dates & keywords"),
          h("div", { class: "grid2" },
            h("div", null, h("label", null, "Starts"), datePicker(conf.start, (val) => { conf.start = val; })),
            h("div", null, h("label", null, "Ends"), datePicker(conf.end, (val) => { conf.end = val; }))),
          h("label", null, "Extra words to look for"), words,
          h("p", { class: "hint" }, "Rerun looks for these in episode titles and descriptions, and in your Plex collection names."),
          checkbox("Celebrate this holiday", conf.enabled, (val) => { conf.enabled = val; }),
          h("div", { class: "row" },
            h("button", { class: "btn small primary", onclick: () => save({ enabled: conf.enabled, start: conf.start, end: conf.end, words: conf.words.split("\n") }, "Saved") }, "Save"),
            h("button", { class: "btn small quiet", onclick: () => save({ start: hd.default_start, end: hd.default_end, words: [] }, "Back to the usual dates") }, "Reset to usual dates"))));
    };
    fill(body,
      h("div", { class: "toolbar" }, toggle("Festive logos — a small holiday badge on every channel logo during the season", r.festive_logos, async (val) => {
        await api("PUT", "/api/settings", { festive_logos: val });
        toast(val ? "Festive logos on" : "Festive logos off");
      })),
      h("section", { class: "block", style: { marginTop: "38px" } }, blockHead("The calendar", "Coming up first"), r.holidays.map(row)));
  }
  add(v, h("div", { class: "xmas-row" }, await christmasPanel(),
    h("a", { class: "panel xmas-link", href: "#/25days" },
      h("div", { class: "eyebrow" }, "ABC Family, December 1–25"),
      h("h2", null, "25 Days of Christmas"),
      h("p", { class: "lede" }, "The real schedule, the real commercials, and a list of what you still need."),
      h("span", { class: "btn" }, "Open 25 Days"))));
  v.append(body);
  load();
}

// ================================================================ classic channels (Boomerang)

async function classicChannels() {
  let r;
  try { r = await api("GET", "/api/lineups"); } catch (x) { return null; }
  return h("section", { class: "block", style: { marginBottom: "34px" } }, blockHead("Classic channels", "Built from their real lineups"),
    h("p", { class: "block-note" }, "Some channels ran other networks' shows, or Plex files their shows elsewhere: Scooby-Doo under CBS rather than Boomerang, Blue's Clues under Nickelodeon rather than Nick Jr. These channels are built from what they really aired."),
    h("div", { class: "lineup-cards" }, r.lineups.map((l) => h("a", { class: "lineup-card", href: `#/lineup/${l.key}` },
      h("img", { src: l.logo_url, alt: "" }),
      h("div", null, h("b", null, l.name), h("span", null, `${l.years}. You have ${l.have} of its ${l.total} shows.`),
        h("span", { class: "lineup-state" }, l.channel ? `On channel ${l.channel.number}` : "Not on the air yet"))))));
}

const TIER_NAMES = { staple: "Aired all the time", regular: "Aired often", occasional: "Aired now and then" };

async function viewLineup(key) {
  const v = shell("#/networks");
  const body = h("div");
  add(v, body);
  const load = async () => {
    let r;
    try { r = await api("GET", `/api/lineups/${key}`); } catch (x) { fill(body, h("p", { class: "error" }, x.message)); return; }
    document.title = `${r.name} · Rerun`;
    const need = r.wants.filter((w) => !w.have);
    const have = r.wants.filter((w) => w.have);
    const scanBtn = h("button", { class: "btn", disabled: !!r.scan.running, onclick: async () => {
      try { await api("POST", "/api/scan"); toast("Checking your Plex library…"); scanBtn.disabled = true; scanBtn.textContent = "Checking Plex…";
        const wait = async () => { const s = await api("GET", "/api/scan"); if (s.running) later(wait, 2000); else { toast("Plex checked"); load(); } }; later(wait, 2000);
      } catch (x) { toast(x.message, true); }
    } }, r.scan.running ? "Checking Plex…" : "Check my Plex again");
    const busy = r.finder.running;
    fill(body,
      h("p", null, h("a", { href: "#/networks" }, "← Networks")),
      h("header", { class: "lineup-head" },
        h("img", { src: r.logo_url, alt: "" }),
        h("div", null, h("h1", null, r.name), h("p", { class: "lede" }, `${r.about} This is its lineup from ${r.years}.`))),
      summaryBar([[`${r.have} / ${r.total}`, "Shows you have"], [`${r.staples.have} / ${r.staples.total}`, "Of the staples"],
        [r.bumpers, "Bumpers and idents"], [r.channel ? r.channel.number : "—", "Channel"]]),

      h("section", { class: "panel" },
        h("h2", null, r.channel ? `${r.channel.number} · ${r.channel.name}` : "Put it on the air"),
        h("p", { class: "lede" }, r.channel
          ? `Plays every ${r.name} show you have, with ${r.name}'s logo in the corner and its bumpers before each show and going into the breaks. New shows you add join automatically.`
          : `A channel with every ${r.name} show you have, its logo in the corner, and its bumpers and idents before each show and going into the breaks.`),
        h("div", { class: "row" }, r.channel ? h("a", { class: "btn", href: `#/channel/${r.channel.id}` }, "Edit channel")
          : h("button", { class: "btn primary", disabled: !have.length, onclick: async () => {
            try { const res = await api("POST", `/api/lineups/${key}/channel`); toast(`${r.name} is on channel ${res.number}`); load(); } catch (x) { toast(x.message, true); }
          } }, have.length ? `Add the ${r.name} channel` : "Get some of its shows first"))),

      h("section", { class: "panel" },
        h("h2", null, `${r.name}'s bumpers, idents and commercials`),
        h("p", { class: "lede" }, r.bumpers
          ? `You have ${plural(r.bumpers, "bumper or ident", "bumpers and idents")} and ${plural(r.commercials, "commercial")} from ${r.name.replace(/\.$/, "")}. Find more to add variety.`
          : `The bits between the shows are most of what made ${r.name} feel like ${r.name.replace(/\.$/, "")}. Rerun can find them on the Internet Archive: bumper and ident collections, ${r.name} commercial breaks from the 2000s, and whole broadcasts (only their breaks are kept).`),
        busy ? h("p", { class: "hint" }, "Finding commercials… follow along on the Commercials page.")
          : h("div", { class: "row" },
            h("button", { class: "btn primary", onclick: async () => {
              try { await api("POST", `/api/lineups/${key}/finder`, { budget_gb: 3, christmas: true }); toast(`Finding ${r.name}'s bumpers and commercials`); location.hash = "#/commercials"; }
              catch (x) { toast(x.message, true); }
            } }, `Find ${r.name}'s bumpers and commercials`),
            h("a", { class: "btn quiet", href: "#/commercials" }, "See what you have"))),

      h("section", { class: "panel" },
        h("h2", null, need.length ? `${need.length} shows to get` : `You have every ${r.name} show`),
        h("p", { class: "lede" }, `Checked against your library ${r.scanned_at ? fmtAgo(r.scanned_at) : "(not scanned yet)"}, by name and the year each series began. Staples are the shows ${r.name} ran year after year; start there.`),
        h("div", { class: "row" }, scanBtn,
          need.length ? h("button", { class: "btn", onclick: async () => { copyText(await (await fetch(`/api/lineups/${key}/need.txt`)).text()); } }, "Copy the list to get") : null,
          need.length ? h("a", { class: "btn quiet", href: `/api/lineups/${key}/need.txt`, target: "_blank" }, "Open as text") : null),
        h("div", { class: "want-cols" },
          h("div", null, h("h3", null, `Still to get · ${need.length}`),
            ["staple", "regular", "occasional"].map((tier) => {
              const list = need.filter((w) => w.tier === tier);
              return list.length ? [h("h4", { class: "tier" }, `${TIER_NAMES[tier]} · ${list.length}`),
                h("ul", { class: "want-list" }, list.map((w) => h("li", null, h("b", null, w.title), h("span", { class: "muted" }, ` (${w.year}) · on ${r.name} ${w.aired}`))))] : null;
            })),
          h("div", null, h("h3", null, `In your library · ${have.length}`),
            h("ul", { class: "want-list have" }, have.map((w) => h("li", { title: `Matched: ${w.match}` },
              h("b", null, w.title), h("span", { class: "muted" }, ` · ${plural(w.episodes, "episode")}`))))))));
  };
  load();
}

// ================================================================ Christmas: the 2000s channel

async function christmasPanel() {
  const box = h("section", { class: "panel xmas-panel" });
  const load = async () => {
    let r;
    try { r = await api("GET", "/api/christmas"); } catch (x) { fill(box, h("p", { class: "error" }, x.message)); return; }
    const need = r.essentials.filter((e) => !e.have);
    const label = (e) => e.kind === "episode" ? `${e.show}: “${e.title}”` : `${e.title} (${e.year})`;
    fill(box,
      h("div", { class: "eyebrow" }, "Christmas, the 2000s way"),
      h("h2", null, "The Christmas Channel"),
      h("p", { class: "lede" }, "Your Christmas channel runs a day like 2000s cable did, with 2000s holiday commercials first and a countdown to Christmas in the guide."),
      h("ul", { class: "xmas-day" }, r.dayparts.map((d) => h("li", null, h("b", null, `${fmtHour(d.from)}–${fmtHour(d.to)}`), d.name)),
        h("li", null, h("b", null, "Dec 24, 8pm"), r.has_story ? "24 Hours of A Christmas Story, back to back until 8pm Christmas Day"
          : "24 Hours of A Christmas Story, once A Christmas Story (1983) is in your library")),
      r.channels.length ? r.channels.map((c) => h("div", { class: "row", style: { marginTop: "6px" } },
        toggle(`${c.number} ${c.name}: 2000s-style day`, c.style === "2000s", async (val) => {
          try { await api("PUT", `/api/channels/${c.id}`, { style: val ? "2000s" : "plain" }); toast(val ? "2000s Christmas day on" : "Back to a plain shuffle"); }
          catch (x) { toast(x.message, true); }
        })))
        : h("p", { class: "hint" }, "Add a Christmas channel from the calendar below; it gets the 2000s treatment."),
      h("details", { class: "subpanel" },
        h("summary", null, `2000s Christmas essentials: you have ${r.have} of ${r.total}`),
        h("p", { class: "hint" }, "The movies, specials and episodes that were on every December in the 2000s. Rerun also counts these as Christmas titles even when the name doesn't say so (Elf, The Polar Express, Home Alone)."),
        h("div", { class: "want-cols" },
          h("div", null, h("h3", null, `Still to get · ${need.length}`), h("ul", { class: "want-list" }, need.map((e) => h("li", null, label(e))))),
          h("div", null, h("h3", null, `In your library · ${r.have}`), h("ul", { class: "want-list have" },
            r.essentials.filter((e) => e.have).map((e) => h("li", { title: e.match }, label(e)))))),
        need.length ? h("button", { class: "btn small", onclick: () => copyText(need.map(label).join("\n")) }, "Copy the list to get") : null));
  };
  load();
  return box;
}

const fmtHour = (h) => `${(h % 12) || 12}${h < 12 ? "am" : "pm"}`;
const fmtClock = (s) => { const [hh, mm] = s.split(":").map(Number); return `${(hh % 12) || 12}:${String(mm).padStart(2, "0")}${hh < 12 ? "am" : "pm"}`; };

// ================================================================ ABC Family's 25 Days of Christmas

async function viewTwentyFive() {
  const v = shell("#/25days");
  add(v, pageHead("ABC Family, December", "25 Days of Christmas",
    "ABC Family's 25 Days of Christmas, played back at the exact times it aired, with the real commercials from those Decembers. Rerun checks your Plex library for every title and tells you what you still need."));
  const body = h("div");
  add(v, body);
  let year = 0;
  const load = async () => {
    let r;
    try { r = await api("GET", `/api/25days${year ? `?year=${year}` : ""}`); } catch (x) { fill(body, h("p", { class: "error" }, x.message)); return; }
    year = r.year;
    const need = r.wants.filter((w) => !w.have);
    const pct = Math.round((100 * r.have) / Math.max(1, r.total));
    const kindName = { movie: "Movie", special: "TV special" };
    const scanBtn = h("button", { class: "btn", onclick: async () => {
      try { await api("POST", "/api/scan"); toast("Checking your Plex library…"); scanBtn.disabled = true; scanBtn.textContent = "Checking Plex…";
        const wait = async () => { const s = await api("GET", "/api/scan"); if (s.running) later(wait, 2000); else { toast("Plex checked"); load(); } }; later(wait, 2000);
      } catch (x) { toast(x.message, true); }
    } }, r.scan.running ? "Checking Plex…" : "Check my Plex again");
    scanBtn.disabled = !!r.scan.running;
    const ch = r.channel;
    const opts = { stand_ins: ch ? ch.stand_ins : true, exact_breaks: ch ? ch.exact_breaks : true };
    const saveChannel = async (msg) => {
      try { const res = await api("POST", "/api/25days/channel", { year, ...opts }); toast(msg || `On channel ${res.number}`); load(); }
      catch (x) { toast(x.message, true); }
    };
    fill(body,
      h("div", { class: "toolbar slim" },
        h("span", { class: "plain-label" }, "Schedule from"),
        h("div", { class: "chips" }, r.years.map((y) => h("span", { class: "chip toggle" + (y === year ? " on" : ""), role: "button", tabindex: 0,
          onclick: () => { year = y; load(); } }, `December ${y}`))),
        h("span", { class: "hint", style: { margin: 0 } }, r.source)),
      summaryBar([[`${r.have} / ${r.total}`, "Titles you have"], [need.length, "Still to get"],
        [r.finder.tapes.reduce((n, t) => n + t.clips, 0), "Real 25 Days ads"], [ch ? ch.number : "—", "Channel"]]),
      h("div", { class: "meter", style: { margin: "-26px 0 34px" } }, h("i", { style: { width: pct + "%" } })),

      h("section", { class: "panel" },
        h("div", { class: "eyebrow" }, "The channel"),
        h("h2", null, ch ? `${ch.number} · ${ch.name}` : "Put it on the air"),
        h("p", { class: "lede" }, ch
          ? `Playing the December ${ch.year} schedule from December 1 to 25. Between listings it runs your ABC Family shows (or Christmas episodes), like ABC Family's regular daytime.`
          : `A channel that plays the December ${year} schedule day by day: the same titles at the same times, from December 1 to 25.`),
        r.on_now ? h("p", null, h("span", { class: "on-air" }, "On now"), " ", r.on_now.title) : r.up_next ? h("p", { class: "hint" }, `Next: ${r.up_next.title}, ${new Date(r.up_next.s * 1000).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`) : null,
        checkbox("Fill titles I don't have with other Christmas movies from my library", opts.stand_ins, (val) => { opts.stand_ins = val; if (ch) saveChannel("Saved"); },
          "Off: a card says what to get instead."),
        checkbox("Replay recorded breaks where they aired", opts.exact_breaks, (val) => { opts.exact_breaks = val; if (ch) saveChannel("Saved"); },
          "When Rerun has a tape of the actual broadcast, its commercials play at the same moments in the movie, in their original order."),
        h("div", { class: "row", style: { marginTop: "14px" } },
          ch ? [ch.year !== year ? h("button", { class: "btn primary", onclick: () => saveChannel(`Now playing December ${year}`) }, `Switch to December ${year}`) : null,
            h("a", { class: "btn", href: `#/channel/${ch.id}` }, "Edit channel")]
            : h("button", { class: "btn primary", onclick: () => saveChannel() }, "Add the 25 Days channel"))),

      h("section", { class: "panel" },
        h("div", { class: "eyebrow" }, "Your Plex library"),
        h("h2", null, need.length ? `${need.length} titles to get` : "You have every title"),
        h("p", { class: "lede" }, `Checked against your library ${r.scanned_at ? fmtAgo(r.scanned_at) : "(not scanned yet)"}. Titles are matched by name and year, so the 1979 Jack Frost special and the 1998 movie are told apart. Specials can be movies in Plex or episodes of a specials show.`),
        h("div", { class: "row" }, scanBtn,
          need.length ? h("button", { class: "btn", onclick: async () => { const t = await (await fetch(`/api/25days/need.txt?year=${year}`)).text(); copyText(t); } }, "Copy the list to get") : null,
          need.length ? h("a", { class: "btn quiet", href: `/api/25days/need.txt?year=${year}`, target: "_blank" }, "Open as text") : null),
        h("div", { class: "want-cols" },
          h("div", null, h("h3", null, `Still to get · ${need.length}`),
            h("ul", { class: "want-list" }, need.map((w) => h("li", null,
              h("b", null, w.title), ` (${w.year})`,
              h("span", { class: "muted" }, ` · ${kindName[w.kind]}${w.christmas ? "" : " · not a Christmas title"} · ${w.airings > 1 ? `airs ${w.airings} times, first` : "airs"} ${w.first}`))))),
          h("div", null, h("h3", null, `In your library · ${r.have}`),
            h("ul", { class: "want-list have" }, r.wants.filter((w) => w.have).map((w) => h("li", { title: `Matched: ${w.match}` },
              h("b", null, w.title), h("span", { class: "muted" }, ` · ${w.match}`))))))),

      h("section", { class: "panel finder-panel" },
        h("div", { class: "eyebrow" }, "The real commercials"),
        h("h2", null, "Commercials from 25 Days broadcasts"),
        h("p", { class: "lede" }, `Rerun looks on the Internet Archive for ABC Family tapes from December ${year} first: commercial breaks people taped, and whole broadcasts recorded with their commercials (only the breaks are kept). Then it tries the years either side, since many ads and 25 Days promos ran for several Decembers. The 25 Days channel plays these before anything else.`),
        twentyFiveFinder(year, r.finder, load)),

      h("section", { class: "block", style: { marginTop: "38px" } }, blockHead("The schedule", `December ${year}`),
        h("p", { class: "block-note" }, "Eastern times, as ABC Family listed them; Rerun plays them at the same clock times where you are. Gaps were ABC Family's regular shows."),
        h("div", { class: "days" }, Object.entries(r.days).map(([d, slots]) => h("details", { class: "day" },
          h("summary", null, h("b", null, `December ${d}`), h("span", { class: "muted" }, ` · ${slots.length} listings`)),
          h("ul", null, slots.map((s) => {
            const w = r.wants.find((x) => x.key === s.key);
            return h("li", { class: w && w.have ? "have" : "need" }, h("span", { class: "t" }, fmtClock(s.start)), s.title,
              w && !w.have ? h("span", { class: "muted" }, " · need") : null);
          })))))));
  };
  load();
}

function twentyFiveFinder(year, d, reload) {
  const box = h("div", { class: "finder" });
  let gb = 4, minH = 0, nearby = true, youtube = true, timer = null;
  const render = (d) => {
    const st = d.status;
    const busy = st.running;
    fill(box,
      h("div", { class: "row" },
        h("span", { class: "plain-label" }, "Space"),
        select([[1, "1 GB"], [4, "4 GB"], [10, "10 GB"], [25, "25 GB"]], gb, (v) => { gb = parseFloat(v); }, { class: "inline", disabled: busy }),
        h("span", { class: "plain-label" }, "Quality"),
        select([[0, "Any (exact ads first)"], [360, "Watchable and up"], [480, "Good and up"]], minH, (v) => { minH = parseInt(v, 10); }, { class: "inline", disabled: busy })),
      checkbox("Also the years either side", nearby, (val) => { nearby = val; }),
      d.youtube ? checkbox("Search YouTube too", youtube, (val) => { youtube = val; }, "After the Internet Archive, ABC Family December commercials people have put on YouTube.") : null,
      h("p", { class: "hint" }, "Whole broadcasts are read straight from the archive and only their breaks are saved, so they need little space but can take a while."),
      h("div", { class: "row", style: { marginTop: "10px" } },
        busy ? h("button", { class: "btn", onclick: async () => { await api("POST", "/api/finder/stop"); toast("Stopping after this step…"); } }, "Stop")
          : h("button", { class: "btn primary", onclick: async () => {
            try { render(await api("POST", "/api/25days/finder", { year, budget_gb: gb, min_height: minH, nearby, youtube })); poll(); }
            catch (x) { toast(x.message, true); }
          } }, `Find December ${year}'s commercials`)),
      busy ? h("div", null,
        h("div", { class: "progress" + (st.total ? "" : " indeterminate") }, h("div", { style: { width: st.total ? `${Math.round((100 * st.done) / st.total)}%` : "" } })),
        h("p", { class: "hint" }, st.step)) : st.step ? h("p", { class: "hint" }, st.step) : null,
      st.error ? h("p", { class: "error" }, st.error) : null,
      st.log && st.log.length ? h("ul", { class: "finder-log" }, st.log.slice(-8).map((l) => h("li", null, l))) : null,
      d.tapes.length ? h("details", { class: "subpanel" },
        h("summary", null, `${plural(d.tapes.length, "tape")}, ${plural(d.tapes.reduce((n, t) => n + t.clips, 0), "commercial")}`),
        h("ul", { class: "finder-tapes" }, d.tapes.map((t) => h("li", null, `${t.title} · ${t.year} · ${t.kind === "airing" ? "broadcast" : "commercials"} · ${t.clips} ads · ${t.quality} picture`))),
        d.airings.length ? h("p", { class: "hint" }, `Recorded broadcasts whose breaks replay in place: ${d.airings.map((a) => `${a.title} (${a.breaks} breaks)`).join("; ")}`) : null,
        busy ? null : h("button", { class: "btn small quiet", onclick: async () => {
          if (!confirm("Delete the 25 Days commercials Rerun downloaded?")) return;
          try { await api("DELETE", "/api/finder/25days"); toast("Removed"); reload(); } catch (x) { toast(x.message, true); }
        } }, "Delete these")) : null);
  };
  const poll = async () => {
    try {
      const r = await api("GET", `/api/25days?year=${year}`);
      render(r.finder);
      clearTimeout(timer);
      if (r.finder.status.running) timer = later(poll, 2500); else if (r.finder.status.step) reload();
    } catch (x) { fill(box, h("p", { class: "error" }, x.message)); }
  };
  render(d);
  if (d.status.running) poll();
  return box;
}

// ================================================================ channel editor

function ruleLabel(r, names, facets) {
  if (r.type === "show") return names[r.id] || "(removed from Plex)";
  if (r.type === "movie") return `${names[r.id] || "(removed from Plex)"} — movie`;
  if (r.type === "holiday") {
    const hd = (facets.holidays || []).find((x) => x.key === r.key);
    return `${hd ? hd.name : "Holiday"} episodes & movies${r.network ? " from " + r.network : ""}`;
  }
  const media = { shows: "TV shows", movies: "Movies", music_videos: "Music videos" }[r.media] || r.media;
  const parts = [];
  if (r.network) parts.push(`from ${r.network}`);
  if (r.collection) parts.push(`in “${r.collection}”`);
  if (r.genres_any && r.genres_any.length) parts.push(r.genres_any.join(" / "));
  if (r.genres_none && r.genres_none.length) parts.push(`not ${r.genres_none.join(" / ")}`);
  if (r.year_from || r.year_to) parts.push(`${r.year_from || "…"}–${r.year_to || "…"}`);
  if (r.tags && r.tags.length) parts.push(`folder: ${r.tags.join(", ")}`);
  return parts.length ? `${media} ${parts.join(" · ")}` : `All ${media.toLowerCase()}`;
}

async function viewChannel(cid) {
  const v = shell("#/channels");
  const isNew = cid === "new";
  let ch, facets, logos;
  try {
    [ch, facets, logos] = await Promise.all([
      isNew ? Promise.resolve({ name: "", rules: [], exclude: [], order: "shuffle", slot_minutes: 30, enabled: true, group: "",
        logo: "", season: "", off_season: "card", holiday_mix: true,
        commercials: { enabled: true, break_style: "end", sources: [], tags: [], per_half_hour: 0 } }) : api("GET", `/api/channels/${cid}`),
      api("GET", "/api/catalog/facets"),
      api("GET", "/api/logos"),
    ]);
  } catch (x) { add(v, h("p", { class: "error" }, x.message)); return; }
  if (isNew && !facets.summary.commercials) ch.commercials.enabled = false;
  const ids = [...ch.rules.filter((r) => r.id).map((r) => r.id), ...ch.exclude];
  const names = ids.length ? await api("GET", "/api/catalog/names?ids=" + encodeURIComponent(ids.join(","))) : {};

  const side = h("aside", { class: "side" });
  const main = h("div");
  add(v, pageHead(isNew ? "New channel" : `Channel ${ch.number}`, isNew ? "Build a channel" : ch.name,
    isNew ? "Name it, choose what plays, and it's on the air." : null,
    isNew ? null : h("button", { class: "btn", onclick: () => openPreview(ch) }, icon("play"), " Preview"),
    h("a", { class: "btn", href: "#/channels" }, "All channels")),
  h("div", { class: "editor" }, main, side));
  let refresh = () => {};

  // -- 1. basics
  const name = h("input", { value: ch.name, placeholder: "e.g. Saturday Morning", oninput: (e) => { ch.name = e.target.value; } });
  const number = h("input", { type: "number", class: "num", min: 1, max: 9999, value: ch.number || "", placeholder: "Auto", oninput: (e) => { ch.number = parseInt(e.target.value, 10) || undefined; } });
  const logoImg = h("img", { src: ch.logo_url || "/static/icon.svg", alt: "" });
  const logoSel = select([["", "Automatic — matches the channel's network"], ...logos.map((l) => [l.ref, l.name])], ch.logo, (val) => {
    ch.logo = val;
    const l = logos.find((x) => x.ref === val);
    logoImg.src = l ? l.url : (ch.logo_url || "/static/icon.svg");
  });
  add(main, h("section", { class: "panel" },
    h("div", { class: "eyebrow" }, "One"), h("h2", null, "The basics"),
    h("div", { class: "grid2" }, h("div", null, h("label", null, "Channel name"), name), h("div", null, h("label", null, "Channel number"), number)),
    h("label", null, "Logo"),
    h("div", { class: "logo-pick" }, logoImg, logoSel),
    h("div", { style: { marginTop: "22px" } }, toggle("On the air", ch.enabled, (val) => { ch.enabled = val; }))));

  // -- 2. what's on
  const rulesBox = h("div");
  const renderRules = () => {
    fill(rulesBox, ch.rules.length
      ? h("div", { class: "chips" }, ch.rules.map((r, i) => h("span", { class: "chip" }, ruleLabel(r, names, facets),
        h("button", { "aria-label": "Remove", title: "Remove", onclick: () => { ch.rules.splice(i, 1); renderRules(); refresh(); } }, "×"))))
      : h("p", { class: "notice" }, "Nothing on this channel yet. Choose something to add below."));
  };
  renderRules();
  const addRule = (r) => { ch.rules.push(r); renderRules(); refresh(); toast("Added to the channel"); };

  const adder = h("div");
  const tiles = [
    ["network", "tv", "A TV network"], ["genre", "genre", "A genre or decade"], ["pick", "star", "Particular shows"],
    ["collection", "folder", "A Plex collection"], ["movies", "film", "Movies"], ["music", "music", "Music videos"], ["holiday", "snow", "Holiday episodes"],
  ];
  const tileEls = {};
  const openAdder = (kind) => {
    Object.entries(tileEls).forEach(([k, el]) => el.classList.toggle("on", k === kind));
    const panel = h("div", { class: "adder" });
    if (kind === "network") {
      let alsoMovies = true;
      const sel = select([["", "Choose a network…"], ...facets.networks.map((n) => [n.name, `${n.name} (${plural(n.shows, "show")})`])], "", () => {});
      fill(panel, h("label", null, "Network"), sel, checkbox("Include its movies too", true, (val) => { alsoMovies = val; }),
        h("button", {
          class: "btn primary", onclick: () => {
            if (!sel.value) return;
            if (alsoMovies) ch.rules.push({ type: "filter", media: "movies", network: sel.value });
            addRule({ type: "filter", media: "shows", network: sel.value });
          },
        }, "Add network"));
    } else if (kind === "genre") {
      const media = select([["shows", "TV shows"], ["movies", "Movies"]], "shows", () => fillGenres());
      const genre = h("select");
      const from = h("input", { type: "number", class: "num", placeholder: "1990" });
      const to = h("input", { type: "number", class: "num", placeholder: "1999" });
      const fillGenres = () => fill(genre, h("option", { value: "" }, "Any genre"),
        (media.value === "movies" ? facets.movie_genres : facets.show_genres).map((g) => h("option", { value: g.name }, `${g.name} (${g.count})`)));
      fillGenres();
      fill(panel, h("div", { class: "grid2" }, h("div", null, h("label", null, "Kind"), media), h("div", null, h("label", null, "Genre"), genre)),
        h("label", null, "Years — optional"), h("div", { class: "row" }, from, h("span", { class: "muted" }, "to"), to),
        h("div", { style: { marginTop: "16px" } }, h("button", {
          class: "btn primary", onclick: () => {
            const r = { type: "filter", media: media.value };
            if (genre.value) r.genres_any = [genre.value];
            if (from.value) r.year_from = parseInt(from.value, 10);
            if (to.value) r.year_to = parseInt(to.value, 10);
            addRule(r);
          },
        }, "Add")));
    } else if (kind === "pick") {
      const search = h("input", { type: "search", placeholder: "Type a show or movie name…" });
      const results = h("div");
      search.addEventListener("input", debounce(async () => {
        const q = search.value.trim();
        if (q.length < 2) { fill(results); return; }
        const r = await api("GET", "/api/catalog/search?q=" + encodeURIComponent(q));
        const btn = (item, type) => h("button", { onclick: () => { names[item.id] = item.title; addRule({ type, id: item.id }); } },
          h("span", null, item.title, item.year ? ` (${item.year})` : ""),
          h("span", { class: "muted" }, type === "show" ? `${plural(item.episodes, "episode")}${item.network ? " · " + item.network : ""}` : "Movie"));
        fill(results, r.shows.length || r.movies.length
          ? h("div", { class: "results" }, r.shows.map((x) => btn(x, "show")), r.movies.map((x) => btn(x, "movie")))
          : h("p", { class: "muted" }, "Nothing matches."));
      }, 250));
      fill(panel, h("label", null, "Search your library"), search, results);
      later(() => search.focus(), 50);
    } else if (kind === "collection") {
      const opts = [...facets.show_collections.map((c) => [`shows|${c.name}`, `${c.name} — ${plural(c.count, "show")}`]),
        ...facets.movie_collections.map((c) => [`movies|${c.name}`, `${c.name} — ${plural(c.count, "movie")}`])];
      const sel = select([["", opts.length ? "Choose a collection…" : "You don't have any Plex collections yet"], ...opts], "", () => {});
      fill(panel, h("label", null, "Collection"), sel,
        h("p", { class: "hint" }, "Collections you make in Plex, such as “Saturday Morning” or “Christmas Movies”, work here."),
        h("button", { class: "btn primary", onclick: () => { if (!sel.value) return; const [media, ...rest] = sel.value.split("|"); addRule({ type: "filter", media, collection: rest.join("|") }); } }, "Add collection"));
    } else if (kind === "movies") {
      fill(panel, h("p", null, `Add all ${plural(facets.summary.movies, "movie")}, or choose “A genre or decade” to pick a kind.`),
        h("button", { class: "btn primary", onclick: () => addRule({ type: "filter", media: "movies" }) }, "Add all movies"));
    } else if (kind === "music") {
      fill(panel, facets.summary.music_videos
        ? [h("button", { class: "btn primary", onclick: () => addRule({ type: "filter", media: "music_videos" }) }, `Add all ${plural(facets.summary.music_videos, "music video")}`),
          facets.music_tags.length ? h("div", null, h("label", null, "Or just one folder"),
            h("div", { class: "row" }, facets.music_tags.slice(0, 24).map((t) => h("button", { class: "btn small", onclick: () => addRule({ type: "filter", media: "music_videos", tags: [t.name] }) }, t.name)))) : null]
        : h("p", { class: "muted" }, "No music videos in your library yet. Add a music video folder in Settings."));
    } else if (kind === "holiday") {
      const sel = select(facets.holidays.map((x) => [x.key, x.name]), "christmas", () => {});
      const net = select([["", "Any network"], ...facets.networks.map((n) => [n.name, n.name])], "", () => {});
      fill(panel, h("div", { class: "grid2" }, h("div", null, h("label", null, "Holiday"), sel), h("div", null, h("label", null, "From"), net)),
        h("p", { class: "hint" }, "Adds every holiday episode and movie Rerun found. To keep this channel off the air outside the season, see Holidays below."),
        h("button", { class: "btn primary", onclick: () => { const r = { type: "holiday", key: sel.value }; if (net.value) r.network = net.value; addRule(r); } }, "Add holiday episodes"));
    }
    fill(adder, panel);
  };
  add(main, h("section", { class: "panel" },
    h("div", { class: "eyebrow" }, "Two"), h("h2", null, "What's on"),
    h("p", { class: "hint" }, "These are rules, not lists: “TV shows from Nickelodeon” keeps picking up new Nick shows you add to Plex."),
    rulesBox,
    h("label", null, "Add to this channel"),
    h("div", { class: "tiles" }, tiles.map(([k, ico, label]) => {
      tileEls[k] = h("button", { class: "tile", onclick: () => openAdder(k) }, icon(ico), label);
      return tileEls[k];
    })),
    adder));

  // -- the shows and movies themselves, as posters: remove one, put it back, add more
  const wall = h("div", { class: "wall" }, h("p", { class: "muted" }, "Loading…"));
  const wallFind = h("input", { type: "search", placeholder: "Find on this channel…", "aria-label": "Find a show on this channel" });
  let lastInfo = null;
  const removeItem = (id, title, type) => {
    names[id] = title;
    const i = ch.rules.findIndex((r) => (r.type === type || r.type === "pick") && r.id === id);
    if (i >= 0) ch.rules.splice(i, 1);  // added by hand: just take it off again
    else if (!ch.exclude.includes(id)) ch.exclude.push(id);  // part of a rule: never play it here
    renderRules();
    refresh();
    toast(`${title} is off this channel (save to keep it)`);
  };
  const putBack = (id) => { ch.exclude = ch.exclude.filter((x) => x !== id); refresh(); };
  const addItem = (item, type) => {
    names[item.id] = item.title;
    if (ch.exclude.includes(item.id)) ch.exclude = ch.exclude.filter((x) => x !== item.id);
    else if (!ch.rules.some((r) => r.id === item.id)) ch.rules.push({ type, id: item.id });
    renderRules();
    refresh();
    v.dispatchEvent(new Event("change"));  // the Add window is outside the page: let Save know
  };
  const poster = (it, type, onRemove, removeLabel) => h("div", { class: "poster-card", "data-find": it.title.toLowerCase() },
    h("div", { class: "poster" },
      h("img", { src: `/img/${encodeURIComponent(it.id)}.jpg`, alt: "", loading: "lazy",
        onerror: (e) => { e.target.replaceWith(h("span", { class: "poster-none" }, it.title)); } }),
      h("button", { class: "poster-x", title: removeLabel, "aria-label": `${removeLabel}: ${it.title}`, onclick: () => onRemove(it) }, removeLabel === "Put back" ? "+" : "×")),
    h("b", { title: it.title }, it.title),
    h("small", null, [it.year || null, type === "show" ? plural(it.episodes, "episode") : "Movie"].filter(Boolean).join(" · ")));
  const renderWall = () => {
    const info = lastInfo;
    if (!info) return;
    const q = wallFind.value.trim().toLowerCase();
    const match = (it) => !q || it.title.toLowerCase().includes(q);
    const shows = info.show_list.filter(match), movies = (info.movie_list || []).filter(match);
    const gone = ch.exclude.map((id) => ({ id, title: names[id] || id }));
    fill(wall,
      !info.show_list.length && !(info.movie_list || []).length ? h("p", { class: "notice" }, "Nothing on this channel yet. Use “Add shows or movies”.") : null,
      shows.length ? [h("h3", null, `Shows · ${shows.length}`),
        h("div", { class: "posters" }, shows.map((it) => poster(it, "show", (x) => removeItem(x.id, x.title, "show"), "Take off")))] : null,
      movies.length ? [h("h3", null, `Movies · ${movies.length}`),
        h("div", { class: "posters" }, movies.slice(0, 120).map((it) => poster(it, "movie", (x) => removeItem(x.id, x.title, "movie"), "Take off"))),
        movies.length > 120 ? h("p", { class: "hint" }, `And ${movies.length - 120} more: type a name above to find one.`) : null] : null,
      gone.length ? [h("h3", null, `Taken off · ${gone.length}`),
        h("div", { class: "posters gone" }, gone.map((it) => poster(it, "show", (x) => putBack(x.id), "Put back")))] : null);
  };
  wallFind.addEventListener("input", debounce(renderWall, 150));
  const openAdd = () => modal((box, close) => {
    box.classList.add("wide");
    const q = h("input", { type: "search", placeholder: "Type a show or movie name…", "aria-label": "Search your library" });
    const results = h("div", { class: "posters add-results" });
    const find = debounce(async () => {
      const text = q.value.trim();
      if (text.length < 2) { fill(results); return; }
      const r = await api("GET", "/api/catalog/search?q=" + encodeURIComponent(text));
      const on = new Set([...(lastInfo ? lastInfo.show_list.map((x) => x.id) : []), ...(lastInfo ? (lastInfo.movie_list || []).map((x) => x.id) : [])]);
      const card = (it, type) => {
        const here = on.has(it.id) && !ch.exclude.includes(it.id);
        const b = h("button", { class: "btn small" + (here ? "" : " primary"), disabled: here, onclick: () => {
          addItem(it, type); b.disabled = true; b.textContent = "Added"; toast(`${it.title} added (save to keep it)`);
        } }, here ? "On it" : "Add");
        return h("div", { class: "poster-card" },
          h("div", { class: "poster" }, h("img", { src: `/img/${encodeURIComponent(it.id)}.jpg`, alt: "", loading: "lazy",
            onerror: (e) => { e.target.replaceWith(h("span", { class: "poster-none" }, it.title)); } })),
          h("b", { title: it.title }, it.title),
          h("small", null, [it.year || null, type === "show" ? plural(it.episodes, "episode") : "Movie", it.network || null].filter(Boolean).join(" · ")), b);
      };
      fill(results, r.shows.length || r.movies.length ? [r.shows.map((x) => card(x, "show")), r.movies.map((x) => card(x, "movie"))]
        : h("p", { class: "muted" }, "Nothing in your library matches."));
    }, 250);
    q.addEventListener("input", find);
    add(box, h("div", { class: "pv-head" }, h("div", null, h("div", { class: "eyebrow" }, ch.name || "This channel"), h("h2", null, "Add shows or movies")),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, "×")), q, results,
    h("div", { class: "row end", style: { marginTop: "14px" } }, h("button", { class: "btn primary", onclick: close }, "Done")));
    later(() => q.focus(), 50);
  });
  add(main, h("section", { class: "panel" },
    h("div", { class: "wall-head" },
      h("div", null, h("div", { class: "eyebrow" }, "What plays"), h("h2", null, "On this channel")),
      h("div", { class: "row" }, wallFind, h("button", { class: "btn primary", onclick: openAdd }, "Add shows or movies"))),
    h("p", { class: "hint" }, "Take something off with ×; it stays off even when its network or genre is on the channel. Changes take effect when you save."),
    wall));
  const missingBox = h("div");
  add(main, missingBox);
  const loadMissing = async () => {
    let r;
    try { r = await api("POST", "/api/channels/missing", ch); } catch { return; }
    fill(missingBox, r.lists.map((rep) => h("section", { class: "panel" },
      h("div", { class: "eyebrow" }, "Shows to get"), h("h2", null, `${rep.network} by decade`),
      missingView(rep, (item) => { addItem(item, "show"); toast(`${item.title} added (save to keep it)`); }))));
  };

  // -- 3. how it airs
  const com = ch.commercials;
  const tagsBox = h("div");
  const renderTags = () => {
    fill(tagsBox, facets.commercial_tags.length
      ? h("div", null, h("label", null, "Commercials from these folders — none chosen means all"),
        h("div", { class: "chips" }, facets.commercial_tags.map((t) => h("span", {
          class: "chip toggle" + (com.tags.includes(t.name) ? " on" : ""), role: "button", tabindex: 0,
          onclick: () => { com.tags = com.tags.includes(t.name) ? com.tags.filter((x) => x !== t.name) : [...com.tags, t.name]; renderTags(); refresh(); },
        }, `${t.name} · ${t.count}`))))
      : null);
  };
  renderTags();
  add(main, h("section", { class: "panel" },
    h("div", { class: "eyebrow" }, "Three"), h("h2", null, "How it airs"),
    h("label", null, "Timing"),
    radioCards("slots", [
      [30, "Like real TV", "Shows start on the hour and half hour; commercials fill the gaps."],
      [0, "Back to back", "One show straight after another."],
    ], ch.slot_minutes ? 30 : 0, (val) => { ch.slot_minutes = parseInt(val, 10); refresh(); }),
    h("label", null, "Episodes"),
    radioCards("order", [
      ["shuffle", "Shuffled", "Every episode plays once before any repeats."],
      ["in_order", "In order", "Season by season, from where it left off."],
    ], ch.order, (val) => { ch.order = val; refresh(); }),
    h("div", { class: "subpanel" },
      h("h3", null, "Commercials"),
      facets.summary.commercials ? null : h("div", { class: "notice" }, "No commercials in your library yet. Add a commercials folder in Settings → Libraries."),
      toggle("Play commercials", com.enabled, (val) => { com.enabled = val; refresh(); }),
      h("label", null, "Where the breaks go"),
      radioCards("breaks", [
        ["end", "Between shows", "Shows and movies play straight through; the commercials come between them."],
        ["chapters", "At act breaks", "Breaks in the middle of shows, at the chapter marks of DVD and Blu-ray rips, like the original broadcast."],
        ["even", "Evenly spaced", "Breaks in the middle of shows, splitting each into even parts."],
      ], com.break_style, (val) => { com.break_style = val; refresh(); }),
      h("label", null, "How long the breaks are"),
      select([[0, "As long as it takes for shows to start on the half hour"], [2, "Up to 2 minutes a half hour"],
        [3, "Up to 3 minutes a half hour"], [4, "Up to 4 minutes a half hour"], [6, "Up to 6 minutes a half hour"],
        [8, "Up to 8 minutes a half hour"]], com.per_half_hour || 0, (val) => { com.per_half_hour = parseInt(val, 10); refresh(); }),
      h("p", { class: "hint" }, "Counted per half hour of show, so an hour-long movie gets twice as much. With a limit, a show that ends well before the half hour starts the next one early instead of filling the gap, so start times may be a little off the clock."),
      tagsBox)));

  // -- 4. holidays
  const seasonBox = h("div");
  const renderSeason = () => fill(seasonBox,
    h("label", null, "On the air"),
    select([["", "All year round"], ...facets.holidays.map((x) => [x.key, `Only during ${x.name}`])], ch.season, (val) => { ch.season = val; renderSeason(); refresh(); }),
    ch.season ? [h("label", null, "The rest of the year"), radioCards("offseason", [
      ["card", "Stay in the guide", "Shows a “returns December 1” card out of season."],
      ["hide", "Leave the guide", "Disappears from Plex until the season — Plex needs a channel rescan."],
    ], ch.off_season, (val) => { ch.off_season = val; })] : null,
    ch.season ? null : h("div", { style: { marginTop: "18px" } }, toggle("Play holiday episodes during the holidays", ch.holiday_mix, (val) => { ch.holiday_mix = val; refresh(); })));
  renderSeason();
  add(main, h("section", { class: "panel" }, h("div", { class: "eyebrow" }, "Four"), h("h2", null, "Holidays"), seasonBox));

  // -- save / remove: a bar that stays at the bottom of the window and says when something is unsaved
  const err = h("p", { class: "error" });
  const status = h("span", { class: "save-status" });
  const saveBtn = h("button", { class: "btn primary big", onclick: () => save() }, isNew ? "Put it on the air" : "Save changes");
  const snapshot = JSON.stringify(ch);
  const dirty = () => JSON.stringify(ch) !== snapshot;
  const checkDirty = () => later(() => {
    const d = dirty();
    status.textContent = isNew ? "Not on the air yet" : d ? "Unsaved changes" : "All changes saved";
    status.classList.toggle("is-dirty", isNew || d);
    saveBtn.disabled = !isNew && !d;
    leaveGuard = isNew || d ? () => dirty() || (isNew && !!ch.name) : null;
  }, 0);
  ["input", "change", "click"].forEach((ev) => v.addEventListener(ev, checkDirty));
  const onKey = (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); } };
  document.addEventListener("keydown", onKey);
  onLeave.push(() => document.removeEventListener("keydown", onKey));

  async function save() {
    err.textContent = "";
    if (!ch.name.trim()) { err.textContent = "Give the channel a name."; name.focus(); return; }
    saveBtn.disabled = true;
    try {
      const body = { ...ch };
      if (!body.number) delete body.number;
      await (isNew ? api("POST", "/api/channels", body) : api("PUT", `/api/channels/${cid}`, body));
      toast(isNew ? "Your channel is on the air" : "Changes saved");
      leaveGuard = null;
      location.hash = "#/channels";
    } catch (x) { err.textContent = x.message; saveBtn.disabled = false; }
  }

  add(main, h("div", { class: "save-dock" }, h("div", { class: "save-bar" },
    saveBtn,
    h("a", { class: "btn quiet", href: "#/channels" }, "Cancel"),
    status,
    h("span", { class: "spacer" }),
    isNew ? null : h("button", {
      class: "btn", onclick: async () => { await api("POST", `/api/channels/${cid}/reshuffle`); toast("Reshuffled — a new schedule starts after the current show"); later(loadSchedule, 1500); },
    }, "Reshuffle"),
    isNew ? null : h("button", {
      class: "btn danger", onclick: () => modal((box, close) => {
        fill(box, h("div", { class: "eyebrow" }, "Remove channel"), h("h2", null, `Take ${ch.name} off the air?`),
          h("p", { class: "lede" }, "The channel and its schedule go away. Your shows and movies aren't touched, and Rerun won't add it back by itself."),
          h("div", { class: "row end" }, h("button", { class: "btn quiet", onclick: close }, "Keep it"),
            h("button", { class: "btn danger", onclick: async () => { await api("DELETE", `/api/channels/${cid}`); close(); toast("Channel removed"); leaveGuard = null; location.hash = "#/channels"; } }, "Remove")));
      }),
    }, "Remove")), err));
  checkDirty();

  // -- side: live summary, show list, coming up
  const summary = h("div");
  const showList = h("div");
  const upcoming = h("div");
  add(side, h("section", { class: "panel" }, h("div", { class: "eyebrow" }, "At a glance"), h("h2", { style: { fontSize: "20px" } }, "On this channel"), summary, showList),
    isNew ? null : h("section", { class: "panel" }, h("div", { class: "eyebrow" }, "Schedule"), h("h2", { style: { fontSize: "20px" } }, "Coming up"), upcoming));

  const renderExcluded = () => ch.exclude.length
    ? h("div", null, h("label", null, "Never play"), h("div", { class: "chips" }, ch.exclude.map((id) => h("span", { class: "chip" }, names[id] || id,
      h("button", { "aria-label": "Allow again", title: "Allow again", onclick: () => { ch.exclude = ch.exclude.filter((x) => x !== id); refresh(); } }, "×")))))
    : null;

  refresh = debounce(async () => {
    let info;
    try { info = await api("POST", "/api/channels/preview", ch); } catch { return; }
    fill(summary, h("div", { class: "stats", style: { gap: "22px", margin: "14px 0 6px" } },
      h("div", { class: "stat" }, h("b", null, info.shows), h("span", null, "Shows")),
      h("div", { class: "stat" }, h("b", null, info.movies), h("span", null, "Movies")),
      info.specials ? h("div", { class: "stat" }, h("b", null, info.specials), h("span", null, "Holiday")) : null,
      info.music_videos ? h("div", { class: "stat" }, h("b", null, info.music_videos), h("span", null, "Videos")) : null,
      h("div", { class: "stat" }, h("b", null, info.hours.toLocaleString()), h("span", null, "Hours"))),
    com.enabled ? h("p", { class: "hint" }, info.commercials ? `${plural(info.commercials, "commercial")} to choose from.` : "No matching commercials — shows will run back to back.") : null);
    lastInfo = info;
    (info.show_list || []).concat(info.movie_list || []).forEach((x) => { names[x.id] = names[x.id] || x.title; });
    renderWall();
    loadMissing();
    fill(showList);
  }, 300);
  refresh();

  async function loadSchedule() {
    if (isNew) return;
    try {
      const blocks = await api("GET", `/api/channels/${cid}/schedule?hours=8`);
      fill(upcoming, h("ul", { class: "sched" }, blocks.slice(0, 16).map((b) => h("li", null,
        h("span", { class: "t" }, fmtTime(b.s)), h("span", { class: "w", title: [b.title, b.sub].filter(Boolean).join(" — ") }, b.title || "—")))));
    } catch (x) { fill(upcoming, h("p", { class: "error" }, x.message)); }
  }
  loadSchedule();
}

// ================================================================ guide

async function viewGuide() {
  const v = shell("#/guide");
  add(v, pageHead("Listings", "TV Guide", "The next six hours — the same guide Plex shows."));
  const wrap = h("div", { class: "guide" });
  add(v, wrap);
  const PX = 4.4; // pixels per minute
  const load = async () => {
    let g, chans;
    try { [g, chans] = await Promise.all([api("GET", "/api/guide?hours=6"), api("GET", "/api/channels")]); }
    catch (x) { fill(wrap, h("p", { class: "error", style: { padding: "20px" } }, x.message)); return; }
    if (!g.channels.length) { fill(wrap, h("p", { style: { padding: "28px" } }, "No channels yet.")); return; }
    const logo = Object.fromEntries(chans.map((c) => [c.id, c.logo_url]));
    const width = ((g.end - g.start) / 60) * PX;
    const times = [];
    for (let t = g.start; t < g.end; t += 1800) times.push(h("div", { class: "g-time", style: { width: 30 * PX + "px" } }, fmtTime(t)));
    const now = Date.now() / 1000;
    fill(wrap,
      h("div", { class: "g-times" }, h("div", { class: "g-name" }, "Channel"), h("div", { class: "g-track", style: { width: width + "px", display: "flex" } }, times)),
      g.channels.map((c) => h("div", { class: "g-row" },
        h("div", { class: "g-name" }, h("img", { src: logo[c.id], alt: "" }), h("span", null, h("small", null, `CH ${c.number}`), c.name)),
        h("div", { class: "g-track", style: { width: width + "px" } },
          now >= g.start && now < g.end ? h("div", { class: "g-nowline", style: { left: ((now - g.start) / 60) * PX + "px" } }) : null,
          c.blocks.map((b) => {
            const s = Math.max(b.s, g.start);
            const e = Math.min(b.e, g.end);
            const isNow = b.s <= now && now < b.e;
            return h("div", {
              class: "g-block" + (isNow ? " now" : b.movie ? " movie" : "") + (b.card ? " card" : ""), title: `${fmtTime(b.s)}–${fmtTime(b.e)}  ${b.title}${b.sub ? " — " + b.sub : ""}`,
              style: { left: ((s - g.start) / 60) * PX + 2 + "px", width: Math.max(4, ((e - s) / 60) * PX - 4) + "px" },
            }, h("b", null, b.title || "—"), b.sub ? h("span", null, b.sub) : null);
          })))));
  };
  load();
  every(load, 60000);
}

// ================================================================ commercials

function fmtDur(s) {
  s = Math.round(s);
  return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}

function previewAd(ad, labels = {}) {
  const heard = h("div", { class: "ad-heard" });
  modal((box, close) => {
    fill(box, h("h2", { style: { margin: "0 0 12px" } }, ad.title || "Commercial"),
      h("video", { class: "ad-video", src: `/api/commercials/${ad.id}/preview.mp4`, controls: true, autoplay: true, playsinline: true }),
      h("p", { class: "ad-preview-meta" },
        h("span", { class: `q-pill ${qualityClass(ad)}` }, quality(ad) || "Unknown"), fmtDur(ad.dur),
        ad.tags.map((t) => labels[t] || t).join(", ") || null),
      h("p", { class: "hint" }, `Picture: ${qualityText(ad.q)}`),
      heard,
      h("div", { class: "row end" }, h("button", { class: "btn quiet", onclick: close }, "Close")));
  });
  // what was heard in it, and hearing it again if that came out wrong
  let timer = null;
  const showHeard = async () => {
    clearTimeout(timer);
    let r, st;
    try { [r, st] = await Promise.all([api("GET", `/api/listen/${encodeURIComponent(ad.id)}`), api("GET", "/api/listen")]); } catch (x) { return; }
    if (!heard.isConnected) return;  // closed
    if (!st.available) { fill(heard); return; }
    const busy = st.progress.running;
    if (busy) timer = setTimeout(showHeard, 2500);
    fill(heard,
      h("p", { class: "hint" }, r.heard
        ? [r.kind ? `Heard as: ${r.kind}` : "Heard", r.shows.length ? ` · about ${r.shows.join(", ")}` : ""].join("")
        : "Not listened to yet."),
      r.text ? h("blockquote", { class: "ad-said" }, r.text) : null,
      h("div", { class: "row" },
        h("button", { class: "btn quiet small", disabled: busy, onclick: async () => {
          try { await api("POST", "/api/listen/run", { ids: [ad.id] }); toast("Listening to it again"); } catch (x) { toast(x.message, true); }
          showHeard();
        } }, r.heard ? "Listen again" : "Listen to it"),
        busy ? h("span", { class: "muted" }, st.progress.total === 1 ? "Listening…" : "Busy listening to the others") : null));
  };
  showHeard();
}

const SOURCE_NAMES = { found: "Found online", folder: "Your folders", plex: "Plex" };
// "640×480, 29.97 fps, 1.8 Mbps, h264"
const qualityText = (q) => !q || !q.h ? "quality unknown" : [`${q.w}×${q.h}`, q.eh && q.eh < q.h ? `blown up from ${q.eh} lines` : "",
  q.fps ? `${q.fps} fps` : "",
  q.kbps ? (q.kbps >= 1000 ? `${(q.kbps / 1000).toFixed(1)} Mbps` : `${q.kbps} kbps`) : "", q.codec].filter(Boolean).join(", ");
const QUALITIES = [["HD", "q-hd"], ["Good", "q-good"], ["Watchable", "q-ok"], ["Low", "q-low"], ["Unknown", "q-none"]];
const quality = (a) => !a.h ? "" : a.h >= 700 ? "HD" : a.h >= 400 ? "Good" : a.h >= 300 ? "Watchable" : "Low";
const qualityClass = (a) => (QUALITIES.find(([n]) => n === (quality(a) || "Unknown")) || QUALITIES[4])[1];

// Listening to the commercials (see listen.py): what kind each one is and which shows it's about, so the
// breaks get a mix of kinds and promos only play where they make sense.
function listenBox() {
  const box = h("section", { class: "panel listen" });
  let timer = null;
  const load = async () => {
    clearTimeout(timer);
    let r;
    try { r = await api("GET", "/api/listen"); } catch (x) { fill(box, h("p", { class: "error" }, x.message)); return; }
    const p = r.progress;
    if (p.running) timer = setTimeout(() => { if (box.isConnected) load(); }, 3000);
    const head = h("h2", null, "What your commercials are");
    if (!r.available) {
      fill(box, head, h("p", { class: "hint" }, "Rerun can listen to each commercial once to learn what it is (cereal, toys, a movie, a promo for a show...) and mix the breaks better. Run “rerun update” on the server to add the listening part."));
      return;
    }
    const status = p.running
      ? (p.paused ? `Waiting until nobody's watching (${p.done} of ${p.total} heard)` : `Listening… ${p.done} of ${p.total}${p.step ? ` · ${p.step}` : ""}`)
      : p.error ? p.error
      : r.heard >= r.total ? `All ${plural(r.total, "commercial")} heard.` : `${r.heard} of ${plural(r.total, "commercial")} heard.`;
    fill(box, head,
      h("p", { class: "hint" }, "Rerun listens to each commercial once, on this server, only while nobody's watching. Then every break gets a mix of kinds, and promos and “you're watching” bumpers only play around the shows they're about."),
      h("div", { class: "row" },
        p.running ? h("button", { class: "btn", onclick: async () => { await api("POST", "/api/listen/stop"); toast("Stopping after this one"); load(); } }, "Stop")
          : r.heard < r.total ? h("button", { class: "btn primary", onclick: async () => { try { await api("POST", "/api/listen/run"); load(); } catch (x) { toast(x.message, true); } } }, "Listen now") : null,
        !p.running && r.heard ? h("button", { class: "btn quiet", title: "Hear every commercial once more and sort them again. What was heard before stays in use until each one is redone.", onclick: async () => {
          if (!confirm(`Listen to all ${plural(r.total, "commercial")} again? It runs only while nobody's watching and can take a while.`)) return;
          try { await api("POST", "/api/listen/run", { again: true }); toast("Listening to them all again"); load(); } catch (x) { toast(x.message, true); }
        } }, "Listen to all again") : null,
        h("span", { class: p.error ? "error" : "muted" }, status)),
      r.kinds.length ? h("div", { class: "listen-kinds" }, r.kinds.map((k) => h("span", { class: "chip" }, `${k.label} · ${k.count}`))) : null);
  };
  load();
  return box;
}

async function viewCommercials() {
  const v = shell("#/commercials");
  add(v, pageHead("Your breaks", "Commercials", "Everything Rerun can play between shows. Click a commercial to watch it; remove the ones you don't want on the air (nothing is deleted)."));
  const hero = h("section", { class: "ad-hero" });
  const strip = h("div", { class: "finder-strip", hidden: true });
  const bar = h("div", { class: "toolbar slim ad-filters" });
  const grid = h("div", { class: "ad-grid" });
  const more = h("div", { class: "row", style: { justifyContent: "center", marginTop: "24px" } });
  add(v, hero, listenBox(), strip, bar, grid, more);
  let data = { items: [], labels: {}, groups: { network: [], era: [], season: [] }, stats: {} };
  const f = { query: "", kind: "", network: "", era: "", season: "", quality: "", source: "", sort: "best", hidden: false };
  let shown = 60;
  const label = (t) => data.labels[t] || t;
  const matches = (a) => (f.hidden ? a.hidden : !a.hidden)
    && (!f.kind || (f.kind === "bumper") === a.tags.includes("bumper"))
    && (!f.network || a.tags.includes(f.network)) && (!f.era || a.tags.includes(f.era))
    && (!f.season || a.tags.includes(f.season)) && (!f.source || a.source === f.source)
    && (!f.quality || (quality(a) || "Unknown") === f.quality)
    && (!f.query || `${a.title} ${a.tags.map(label).join(" ")}`.toLowerCase().includes(f.query));
  const byTitle = (x, y) => x.title.localeCompare(y.title, undefined, { numeric: true, sensitivity: "base" });
  const sorters = {
    best: (x, y) => (y.h || 0) - (x.h || 0) || ((y.q || {}).kbps || 0) - ((x.q || {}).kbps || 0) || byTitle(x, y),
    title: (x, y) => byTitle(x, y),
    short: (x, y) => x.dur - y.dur,
    long: (x, y) => y.dur - x.dur,
  };
  const setHidden = async (ids, hidden) => {
    try {
      await api("POST", "/api/commercials/hide", { ids, hidden });
      data.items.forEach((a) => { if (ids.includes(a.id)) a.hidden = hidden; });
      data.hidden = data.items.filter((a) => a.hidden).length;
      toast(hidden ? (ids.length > 1 ? `${ids.length} commercials taken off the air` : "Taken off the air") : (ids.length > 1 ? `${ids.length} commercials back on the air` : "Back on the air"));
      render();
    } catch (x) { toast(x.message, true); }
  };
  const card = (a) => h("article", { class: "ad-card" + (a.hidden ? " is-off" : "") },
    h("button", { class: "ad-thumb", onclick: () => previewAd(a, data.labels), "aria-label": `Watch ${a.title}` },
      h("img", { src: `/api/commercials/${a.id}/thumb.jpg`, alt: "", loading: "lazy" }),
      h("span", { class: `q-pill ${qualityClass(a)}`, title: qualityText(a.q) }, quality(a) || "?"),
      a.tags.includes("bumper") ? h("span", { class: "ad-kind" }, "Bumper") : a.kind && a.kind !== "Other" ? h("span", { class: "ad-kind" }, a.kind) : null,
      h("span", { class: "ad-dur" }, fmtDur(a.dur))),
    h("div", { class: "ad-body" },
      h("b", { title: a.title }, a.title || "Commercial"),
      a.about && a.about.length ? h("small", { class: "muted" }, "About " + a.about.join(", ")) : null,
      h("span", null, a.tags.filter((t) => !/^y\d{4}$/.test(t) && t !== "bumper").map(label).join(", ") || SOURCE_NAMES[a.source]),
      h("button", { class: "ad-remove", onclick: () => setHidden([a.id], !a.hidden) }, a.hidden ? "Put back" : "Remove")));

  const renderHero = () => {
    const on = data.items.filter((a) => !a.hidden);
    const counts = Object.fromEntries(QUALITIES.map(([n]) => [n, 0]));
    on.forEach((a) => { counts[quality(a) || "Unknown"] += 1; });
    const secs = on.reduce((n, a) => n + a.dur, 0);
    const time = secs >= 3600 ? `${(secs / 3600).toFixed(1)} hours of breaks` : `${Math.round(secs / 60)} minutes of breaks`;
    fill(hero,
      h("div", { class: "ad-hero-count" },
        h("b", null, on.length.toLocaleString()),
        h("span", null, on.length === 1 ? "commercial on the air" : "commercials on the air"),
        h("p", null, [time, data.stats.dupes ? `${plural(data.stats.dupes, "duplicate copy", "duplicate copies")} kept off` : null,
          data.hidden ? `${data.hidden} removed by you` : null].filter(Boolean).join(". ") + ".")),
      on.length ? h("div", { class: "ad-hero-quality" },
        h("div", { class: "qbar", role: "img", "aria-label": QUALITIES.map(([n]) => `${n}: ${counts[n]}`).join(", ") },
          QUALITIES.filter(([n]) => counts[n]).map(([n, cls]) => h("i", { class: cls, style: { flexGrow: counts[n] } }))),
        h("div", { class: "qlegend" }, QUALITIES.filter(([n]) => counts[n]).map(([n, cls]) => h("button", {
          class: "qkey" + (f.quality === n ? " on" : ""), "aria-pressed": String(f.quality === n),
          title: f.quality === n ? "Show every quality" : `Show only ${n.toLowerCase()} quality`,
          onclick: () => { f.quality = f.quality === n ? "" : n; shown = 60; render(); },
        }, h("i", { class: cls }), `${n} ${counts[n]}`)))) : null,
      h("div", { class: "ad-hero-acts" },
        h("button", { class: "btn primary", onclick: () => openFinder(onFinderStart) }, "Find more commercials"),
        h("button", { class: "btn quiet", onclick: () => openYouTube(onFinderStart) }, "Add from YouTube"),
        h("a", { class: "btn quiet", href: "#/setup/libraries/settings" }, "Add a folder")));
  };

  const facet = (key, all, opts) => select([["", all], ...opts.map((o) => [o[0], `${o[1]} (${data.items.filter((a) => !a.hidden && (key === "source" ? a.source === o[0] : a.tags.includes(o[0]))).length})`])],
    f[key], (val) => { f[key] = val; shown = 60; render(); }, { class: "inline", "aria-label": all });
  const renderBar = () => fill(bar,
    searchBox("Find a commercial", (q) => { f.query = q; shown = 60; render(); }),
    h("div", { class: "ad-facets" },
      data.groups.bumpers ? select([["", "Commercials and bumpers"], ["ad", `Commercials (${data.items.filter((a) => !a.hidden && !a.tags.includes("bumper")).length})`],
        ["bumper", `Bumpers and idents (${data.items.filter((a) => !a.hidden && a.tags.includes("bumper")).length})`]], f.kind,
      (val) => { f.kind = val; shown = 60; render(); }, { class: "inline", "aria-label": "Type" }) : null,
      data.groups.network.length ? facet("network", "All networks", data.groups.network.map((t) => [t, label(t)])) : null,
      data.groups.era.length ? facet("era", "Any decade", data.groups.era.map((t) => [t, label(t)])) : null,
      data.groups.season.length ? facet("season", "Any season", data.groups.season.map((t) => [t, label(t)])) : null,
      facet("source", "All sources", Object.entries(SOURCE_NAMES).filter(([k]) => data.items.some((a) => a.source === k))),
      select([["best", "Sharpest first"], ["title", "A to Z"], ["short", "Shortest first"], ["long", "Longest first"]], f.sort,
        (val) => { f.sort = val; render(); }, { class: "inline", "aria-label": "Sort" })),
    toggle("Removed ones", f.hidden, (val) => { f.hidden = val; shown = 60; render(); }));

  const render = () => {
    renderHero();
    const list = data.items.filter(matches).sort(sorters[f.sort]);
    const filtered = f.query || f.kind || f.network || f.era || f.season || f.quality || f.source;
    fill(grid, list.length ? list.slice(0, shown).map(card)
      : h("div", { class: "empty" }, data.items.length
        ? h("p", null, f.hidden ? "You haven't removed any commercials." : "No commercials match these filters.")
        : [h("p", null, "No commercials yet. Find classic ones on the Internet Archive, or add a folder of your own."),
          h("button", { class: "btn primary", onclick: () => openFinder(onFinderStart) }, "Find commercials")]));
    fill(more,
      list.length > shown ? h("button", { class: "btn", onclick: () => { shown += 120; render(); } }, `Show ${Math.min(120, list.length - shown)} more of ${list.length - shown}`) : null,
      list.length > 1 && filtered ? h("button", {
        class: "btn quiet", onclick: () => {
          if (confirm(`${f.hidden ? "Put back" : "Remove"} all ${list.length} commercials shown?`)) setHidden(list.map((a) => a.id), !f.hidden);
        },
      }, `${f.hidden ? "Put back" : "Remove"} all ${list.length} shown`) : null);
  };

  const load = async () => {
    try { data = await api("GET", "/api/commercials"); } catch (x) { fill(grid, h("p", { class: "error" }, x.message)); return; }
    renderBar();
    render();
  };
  // A search in progress shows as a strip at the top of the page; the list reloads when it's done.
  let timer = null, wasRunning = false;
  const watch = async () => {
    let d;
    try { d = await api("GET", "/api/finder"); } catch (x) { return; }
    const st = d.status;
    if (st.running) {
      strip.hidden = false;
      fill(strip, h("div", { class: "progress" + (st.total ? "" : " indeterminate") }, h("div", { style: { width: st.total ? `${Math.round((100 * st.done) / st.total)}%` : "" } })),
        h("div", { class: "row" }, h("b", null, "Finding commercials"), h("span", { class: "hint" }, st.step),
          h("span", { class: "spacer" }), h("button", { class: "btn small", onclick: async () => { await api("POST", "/api/finder/stop"); toast("Stopping after this step…"); } }, "Stop")),
        st.log && st.log.length ? h("p", { class: "hint" }, st.log[st.log.length - 1]) : null);
      wasRunning = true;
      clearTimeout(timer);
      timer = later(watch, 2500);
    } else if (wasRunning) {
      wasRunning = false;
      strip.hidden = false;
      fill(strip, h("div", { class: "row" }, h("b", null, st.error ? "The search stopped" : "Search finished"),
        h("span", { class: "hint" }, st.error || (st.log && st.log.length ? st.log[st.log.length - 1] : st.step)),
        h("span", { class: "spacer" }), h("button", { class: "btn small quiet", onclick: () => { strip.hidden = true; } }, "Dismiss")));
      load();
      later(watchScan, 1500); // the library scan that follows adds them
    }
  };
  // The new commercials join the list when the library scan after a search (or a download) is done; it
  // also splits each new tape into its commercials, so it can take a few minutes. Follow it, then reload.
  let scanning = false;
  const watchScan = async () => {
    let sc;
    try { sc = await api("GET", "/api/scan"); } catch (x) { return; }
    if (sc.running) {
      scanning = true;
      if (!wasRunning) {
        strip.hidden = false;
        fill(strip, h("div", { class: "progress" + (sc.total ? "" : " indeterminate") }, h("div", { style: { width: sc.total ? `${Math.round((100 * sc.done) / sc.total)}%` : "" } })),
          h("div", { class: "row" }, h("b", null, "Adding the new commercials to your library"), h("span", { class: "hint" }, sc.step || "")),
          h("p", { class: "hint" }, "New tapes are split into their commercials as they're added; the list below updates when it's done."));
      }
      later(watchScan, 3000);
    } else if (scanning) {
      scanning = false;
      await load();
      if (!wasRunning) {
        strip.hidden = false;
        fill(strip, h("div", { class: "row" }, h("b", null, "Your commercials are up to date"),
          h("span", { class: "hint" }, sc.error || `${plural(data.items.length, "commercial")} in your library.`),
          h("span", { class: "spacer" }), h("button", { class: "btn small quiet", onclick: () => { strip.hidden = true; } }, "Dismiss")));
      }
    }
  };
  const onFinderStart = () => { wasRunning = true; watch(); };
  await load();
  watch();
  watchScan();
}

// YouTube: search or paste links, pick videos, say what they are (see app/youtube.py).
async function openYouTube(onStart) {
  let st;
  try { st = await api("GET", "/api/youtube"); } catch (x) { toast(x.message, true); return; }
  if (!st.available) { toast("The YouTube downloader isn't installed yet. Run “rerun update” on the server.", true); return; }
  const picked = new Map();
  let results = [], kind = "ads", network = "", era = "", holiday = "", name = "", busy = false, note = "";
  modal((box, close) => {
    box.classList.add("wide", "yt-dialog");
    const resultsBox = h("div", { class: "yt-results" });
    const query = h("input", { type: "search", placeholder: "Search YouTube, e.g. Nickelodeon commercials 2003", "aria-label": "Search YouTube" });
    const links = h("textarea", { placeholder: "…or paste YouTube links, one per line (videos or playlists)", rows: 2, "aria-label": "YouTube links" });
    const fmt = (s) => !s ? "" : s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : fmtDur(s);
    const showResults = () => fill(resultsBox,
      busy ? h("p", { class: "hint" }, "Looking…") : null,
      note ? h("p", { class: "hint" }, note) : null,
      results.map((r) => h("label", { class: "yt-item" + (picked.has(r.url) ? " on" : "") },
        h("input", { type: "checkbox", checked: picked.has(r.url), onchange: (e) => { e.target.checked ? picked.set(r.url, r) : picked.delete(r.url); render(); } }),
        r.id ? h("img", { src: `/api/youtube/thumb/${encodeURIComponent(r.id)}.jpg`, alt: "", loading: "lazy" }) : h("span", { class: "yt-noimg" }),
        h("span", null, h("b", null, r.title), h("small", null, [r.uploader, fmt(r.duration)].filter(Boolean).join(" · "))))));
    const look = async (fn) => {
      busy = true; note = ""; showResults();
      try { const r = await fn(); results = r.results; note = r.problems && r.problems.length ? `Couldn't open: ${r.problems.join("; ")}` : (results.length ? "" : "Nothing found."); }
      catch (x) { results = []; note = x.message; }
      busy = false; showResults();
    };
    const sel = (opts, value, set, label) => select(opts, value, (v) => { set(v); }, { "aria-label": label });
    const kindBox = h("div");
    const renderKind = () => fill(kindBox,
      radioCards("yt-kind", [["ads", "Commercials, promos & bumpers", "Split into single commercials and filed by network, decade and holiday, like the ones from the Internet Archive."],
        ["template", "An “Up next” template", "A green-screen “Up next” picture or video, played near the end of a show."]], kind, (v) => { kind = v; renderKind(); }),
      kind === "ads" ? h("div", { class: "finder-opts" },
        h("label", null, "Network", sel([["", "From each video's title"], ...st.networks.map((n) => [n.key, n.label])], network, (v) => { network = v; }, "Network")),
        h("label", null, "Decade", sel([["", "From each video's title"], ...st.eras.map((e) => [e, e])], era, (v) => { era = v; }, "Decade")),
        h("label", null, "Holiday", sel([["", "From each video's title"], ["none", "Not a holiday"], ["christmas", "Christmas"], ["halloween", "Halloween"]], holiday, (v) => { holiday = v; }, "Holiday")))
        : h("label", { class: "yt-name" }, "Use it when this is up next (a show or a network)",
          h("input", { type: "text", value: name, placeholder: "e.g. SpongeBob SquarePants, or Nickelodeon", oninput: (e) => { name = e.target.value; render(); } })));
    const foot = h("div", { class: "row end modal-foot" });
    const render = () => fill(foot,
      h("span", { class: "hint", style: { marginRight: "auto" } }, picked.size ? `${plural(picked.size, "video")} picked` : "Pick the videos to add"),
      h("button", { class: "btn quiet", onclick: close }, "Cancel"),
      h("button", { class: "btn primary", disabled: !picked.size || (kind === "template" && !name.trim()), onclick: async () => {
        try {
          await api("POST", "/api/youtube", { items: [...picked.values()], kind, network, era, holiday, name });
          close(); toast(kind === "template" ? "Downloading the template" : "Downloading in the background"); onStart();
        } catch (x) { toast(x.message, true); }
      } }, kind === "template" ? "Download the template" : picked.size > 1 ? `Download ${picked.size} videos` : "Download"));
    fill(box,
      h("h2", null, "Add from YouTube"),
      h("p", { class: "lede" }, "Commercial breaks, bumper collections and “Up next” templates people have shared on YouTube. Downloaded to your server for watching at home."),
      h("form", { class: "row yt-search", onsubmit: (e) => { e.preventDefault(); if (query.value.trim()) look(() => api("GET", `/api/youtube/search?q=${encodeURIComponent(query.value.trim())}`)); } },
        query, h("button", { class: "btn", type: "submit" }, "Search")),
      h("div", { class: "row yt-links" }, links, h("button", { class: "btn", onclick: () => { if (links.value.trim()) look(() => api("POST", "/api/youtube/links", { text: links.value })); } }, "Open links")),
      resultsBox,
      h("h3", null, "What are they?"),
      kindBox,
      st.js ? null : h("p", { class: "hint" }, "Tip: some YouTube videos need a JavaScript runtime on the server (deno or Node). Without one, a few may not download; see the README."),
      foot);
    renderKind(); render(); showResults();
    query.focus();
  });
}

// Classic commercials from the Internet Archive (see app/archive.py), in a dialog.
async function openFinder(onStart) {
  let d;
  try { d = await api("GET", "/api/finder"); } catch (x) { toast(x.message, true); return; }
  if (d.status.running) { toast("Already looking for commercials"); return; }
  const picked = new Set(d.networks.filter((n) => n.have_channel).map((n) => n.key));
  const eras = new Set(d.channel_eras.length ? d.channel_eras : ["2000s"]);
  const hols = new Set();
  let gb = 1.5, minH = 360, airings = true, youtube = !!d.youtube;
  const name = (n) => n.label.replace(" / Fox Family", "");
  modal((box, close) => {
    box.classList.add("wide", "finder-dialog");
    const render = () => fill(box,
      h("h2", null, "Find classic commercials"),
      h("p", { class: "lede" }, "Real commercials, promos and bumpers people taped off TV and saved on the Internet Archive. Network channels play their own network's, decade channels play that decade's, and holiday ads air in season."),
      h("h3", null, "Networks"),
      h("div", { class: "tiles finder-tiles" }, d.networks.map((n) => h("button", {
        class: "tile" + (picked.has(n.key) ? " on" : ""), "aria-pressed": String(picked.has(n.key)),
        onclick: () => { picked.has(n.key) ? picked.delete(n.key) : picked.add(n.key); render(); },
      }, h("span", null, name(n),
        n.clips || n.have_channel ? h("small", null, n.clips ? `${n.clips.toLocaleString()} so far` : "You have this channel") : null)))),
      h("h3", null, "Decades"),
      h("div", { class: "chips" }, d.eras.map((e) => h("button", {
        class: "chip toggle" + (eras.has(e) ? " on" : ""), "aria-pressed": String(eras.has(e)),
        onclick: () => { eras.has(e) ? eras.delete(e) : eras.add(e); render(); },
      }, e))),
      h("h3", null, "Holiday commercials"),
      h("div", { class: "chips" }, d.holidays.map((k) => h("button", {
        class: "chip toggle" + (hols.has(k) ? " on" : ""), "aria-pressed": String(hols.has(k)),
        onclick: () => { hols.has(k) ? hols.delete(k) : hols.add(k); render(); },
      }, k[0].toUpperCase() + k.slice(1)))),
      h("p", { class: "hint" }, "Holiday ads only air in their season."),
      h("h3", null, "Options"),
      h("div", { class: "finder-opts" },
        h("label", null, "Picture quality", select([[0, "Any"], [360, "Watchable and up"], [480, "Good and up"]], minH, (v) => { minH = parseInt(v, 10); })),
        h("label", null, "Space per network", select([[0.5, "0.5 GB"], [1.5, "1.5 GB"], [3, "3 GB"], [6, "6 GB"]], gb, (v) => { gb = parseFloat(v); }))),
      checkbox("Use whole broadcasts too", airings, (val) => { airings = val; },
        "Shows recorded with their commercials. Rerun reads them straight from the archive and keeps only the breaks: little space, but slower."),
      d.youtube ? checkbox("Search YouTube too", youtube, (val) => { youtube = val; },
        "Commercial breaks people have put on YouTube (“Nickelodeon commercials 2003”…), judged the same way. The space is shared between the two.") : null,
      h("p", { class: "hint" }, "Rerun checks each tape's real picture before downloading, takes the sharpest first, and keeps only the best copy of any commercial it already has."),
      d.networks.some((n) => n.tapes) ? h("details", { class: "subpanel" },
        h("summary", null, "Already downloaded"),
        d.networks.filter((n) => n.tapes).map((n) => h("div", { class: "finder-have" },
          h("div", null, h("b", null, name(n)),
            h("span", { class: "muted" }, ` ${plural(n.clips, "commercial")} from ${plural(n.tapes, "tape")}, ${(n.bytes / 1e9).toFixed(1)} GB`)),
          h("button", { class: "btn small quiet", onclick: async () => {
            if (!confirm(`Delete the ${name(n)} commercials Rerun downloaded?`)) return;
            try { d = await api("DELETE", `/api/finder/${n.key}`); toast(`${name(n)} commercials deleted`); render(); } catch (x) { toast(x.message, true); }
          } }, "Delete")))) : null,
      h("div", { class: "row end modal-foot" },
        h("button", { class: "btn quiet", onclick: close }, "Cancel"),
        h("button", { class: "btn primary", disabled: !picked.size || !eras.size, onclick: async () => {
          try {
            await api("POST", "/api/finder", { networks: [...picked], eras: [...eras], holidays: [...hols], budget_gb: gb, min_height: minH, airings, youtube });
            close(); toast("Finding commercials in the background"); onStart();
          } catch (x) { toast(x.message, true); }
        } }, !picked.size ? "Pick a network" : !eras.size ? "Pick a decade" : `Find commercials for ${plural(picked.size, "network")}`)));
    render();
  });
}

// ================================================================ settings

// ================================================================ Bumpers & Up next

/* For each network: the bumpers and idents that play around its shows (play them, take any off the
   air, find more), and the "Up next" it uses, with a clip of a real show to see it working. */
async function viewLooks() {
  const v = shell("#/looks");
  const body = h("div");
  add(v, pageHead("Bumpers & Up next", "The bits between the shows",
    "Each network's bumpers and idents play before its shows and going into the breaks, on its own channel and around its shows on mixed channels. Near the end of a show, “Up next” says what's on next. Here you see what each network has, try it on a real show, and swap in the real ones from YouTube."), body);
  // Getting bumpers runs in the background: a strip follows it, then the library scan that adds them,
  // then the page reloads with the new ones.
  const strip = h("div", { class: "finder-strip", hidden: true });
  let following = false;
  const follow = async () => {
    if (following) return;
    following = true;
    strip.hidden = false;
    for (;;) {
      let f, sc;
      try { f = (await api("GET", "/api/finder")).status; sc = await api("GET", "/api/scan"); } catch (x) { break; }
      const busy = f.running ? f : sc.running ? { step: sc.step || "Adding them to your library…", done: sc.done, total: sc.total } : null;
      if (!busy) {
        fill(strip, h("div", { class: "row" }, h("b", null, f.error ? "That stopped" : "Done"),
          h("span", { class: "hint" }, f.error || (f.log && f.log.length ? f.log[f.log.length - 1] : "")),
          h("span", { class: "spacer" }), h("button", { class: "btn small quiet", onclick: () => { strip.hidden = true; } }, "Dismiss")));
        break;
      }
      fill(strip, h("div", { class: "progress" + (busy.total ? "" : " indeterminate") }, h("div", { style: { width: busy.total ? `${Math.round((100 * busy.done) / busy.total)}%` : "" } })),
        h("div", { class: "row" }, h("b", null, f.running ? "Getting bumpers" : "Adding them to your library"), h("span", { class: "hint" }, busy.step),
          h("span", { class: "spacer" }), f.running ? h("button", { class: "btn small", onclick: async () => { await api("POST", "/api/finder/stop"); toast("Stopping after this video…"); } }, "Stop") : null),
        f.log && f.log.length ? h("p", { class: "hint" }, f.log[f.log.length - 1]) : null);
      await new Promise((res) => setTimeout(res, 2500));
      if (location.hash !== "#/looks") { following = false; return; }
    }
    following = false;
    load();
  };
  const getBumpers = async (body) => {
    try { await api("POST", "/api/looks/bumpers", body); follow(); }
    catch (x) { toast(x.message, true); }
  };
  const head = h("div", { class: "looks-bar" });
  v.insertBefore(head, body);
  v.insertBefore(strip, body);
  v.insertBefore(h("section", { class: "panel" }, cutoutsBox()), body);
  const load = async () => {
    let r;
    try { r = await api("GET", "/api/looks"); } catch (x) { fill(body, h("p", { class: "error" }, x.message)); return; }
    const styleName = Object.fromEntries(r.styles.map((s) => [s.key, s.label]));
    fill(head,
      h("button", { class: "btn primary", disabled: !r.youtube || r.finder, onclick: () => getBumpers({ all: true }) }, "Get bumpers for all my networks"),
      toggle("Keep them stocked: overnight, any network channel with fewer than 5 bumpers gets more by itself", r.auto_bumpers, async (val) => {
        try { await api("PUT", "/api/looks/auto", { on: val }); toast(val ? "Rerun will keep your channels stocked with bumpers" : "Rerun won't get bumpers by itself"); }
        catch (x) { toast(x.message, true); }
      }));
    fill(body, r.networks.map((n) => lookPanel(n, r, styleName, load, getBumpers)));
    if (r.finder) follow();
  };
  load();
}

function lookOption(picked, value, img, label, sub, onPick, extra) {
  return h("div", { class: "look-opt" + (picked === value ? " on" : "") },
    h("button", { class: "look-pick", "aria-pressed": picked === value ? "true" : "false", onclick: () => onPick(value) },
      img ? h("img", { src: img, alt: "", loading: "lazy" }) : h("span", { class: "look-auto" }, "Auto"),
      h("b", null, label), sub ? h("small", null, sub) : null),
    extra || null);
}

function seeOnShow(n) {
  modal(async (box, close) => {
    box.classList.add("wide");
    const screen = h("div", { class: "pv-screen" }, h("div", { class: "pv-idle" }, h("div", { class: "spinner" }),
      h("b", null, "Making a clip…"), h("span", null, `The end of one of your ${n.label} shows, with its “Up next”, the way live TV draws it.`)));
    add(box, h("div", { class: "pv-head" }, h("div", null, h("div", { class: "eyebrow" }, n.label), h("h2", null, "“Up next” on a real show")),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, "×")), screen);
    try {
      const r = await api("POST", `/api/looks/sample/${n.tag}`);
      fill(screen, h("video", { src: r.url, controls: true, autoplay: true, playsinline: true }));
    } catch (x) { fill(screen, h("div", { class: "pv-idle" }, h("b", null, "Couldn't make the clip"), h("span", null, x.message))); }
  });
}

function lookPanel(n, r, styleName, reload, getBumpers) {
  const save = async (value) => {
    try { await api("PUT", `/api/looks/${n.tag}`, { up_next: value }); toast(`${n.label}'s “Up next” changed`); reload(); }
    catch (x) { toast(x.message, true); }
  };
  const hide = async (b, hidden) => {
    try { await api("POST", "/api/commercials/hide", { ids: [b.id], hidden }); toast(hidden ? "Taken off the air" : "Back on the air"); reload(); }
    catch (x) { toast(x.message, true); }
  };
  const findArchive = async () => {
    try { await api("POST", `/api/looks/finder/${n.tag}`); toast(`Finding ${n.label}'s bumpers and commercials`); reload(); }
    catch (x) { toast(x.message, true); }
  };
  const setN9 = async (change) => {
    try { await api("PUT", "/api/looks/n9", change); toast("Saved. Shows coming up next use it"); reload(); }
    catch (x) { toast(x.message, true); }
  };
  const on = n.bumpers.filter((b) => !b.hidden).length;
  const use = n.in_use;
  const up = n.up_next;
  const others = [
    lookOption(up, "auto", null, "Automatic", "A template named after the show or network, else Rerun's drawing", save),
    r.styles.map((s) => lookOption(up, `style:${s.key}`, `/api/looks/style/${s.key}.jpg`, s.label, "Rerun's drawing", save)),
    r.files.upnext.map((f) => lookOption(up, `file:${f.name}`, `/api/looks/thumb/upnext/${encodeURIComponent(f.name)}`,
      f.name.replace(/\.[^.]+$/, ""), f.video ? "Real template (video)" : "Real template (picture)", save,
      h("div", { class: "look-acts" },
        f.video ? h("button", { class: "btn quiet small", onclick: () => playLook("upnext", f.name) }, "Play") : null,
        h("button", { class: "btn quiet small", onclick: async () => {
          if (!confirm(`Delete “${f.name}”?`)) return;
          try { await api("DELETE", `/api/looks/file/upnext/${encodeURIComponent(f.name)}`); reload(); } catch (x) { toast(x.message, true); }
        } }, "Delete")))),
  ];
  return h("section", { class: "panel look" },
    h("header", { class: "look-head" },
      h("img", { src: n.logo_url, alt: "" }),
      h("div", null, h("h2", null, n.label),
        h("small", { class: "muted" }, n.channels.length
          ? "Its own channel: " + n.channels.map((c) => `${c.number} ${c.name}`).join(", ") + ". Its shows on mixed channels get these too."
          : "Used around its shows on any channel."))),

    h("h3", null, `Bumpers and idents · ${on} on the air`),
    n.bumpers.length
      ? h("div", { class: "bump-grid" }, n.bumpers.map((b) => h("div", { class: "bump" + (b.hidden ? " off" : "") },
        h("button", { class: "bump-play", "aria-label": `Play ${b.title}`, onclick: () => previewAd(b) },
          h("img", { src: `/api/commercials/${encodeURIComponent(b.id)}/thumb.jpg`, alt: "", loading: "lazy" }), h("span", null, "▶")),
        h("b", { title: b.title }, b.title), h("small", null, `${fmtDur(b.dur)}${b.hidden ? " · off the air" : ""}`),
        h("button", { class: "btn quiet small", onclick: () => hide(b, !b.hidden) }, b.hidden ? "Put back on" : "Take off"))))
      : h("p", { class: "notice" }, `No ${n.label} bumpers yet, so its shows go straight into the commercials. “Get bumpers” finds a few on YouTube and adds them by itself.`),
    h("div", { class: "row" },
      h("button", { class: "btn primary", disabled: !r.youtube || r.finder, onclick: () => getBumpers({ tags: [n.tag] }) }, n.bumpers.length ? "Get more bumpers" : "Get bumpers"),
      h("button", { class: "btn quiet", disabled: !r.youtube, onclick: () => findLook(n, "bumpers", reload) }, "Pick them on YouTube myself"),
      h("button", { class: "btn quiet", disabled: r.finder, onclick: findArchive }, "Internet Archive")),

    h("h3", null, "“Up next”"),
    h("div", { class: "look-now" },
      h("img", { src: use.img, alt: "" }),
      h("div", null,
        h("small", { class: "muted" }, up === "auto" ? "In use (automatic)" : "In use"),
        h("b", null, use.label),
        h("p", { class: "hint" }, use.kind === "file" ? "A real template: the show plays behind it." : "Rerun's drawing, in this network's style."),
        use.name === "nick_2009" ? h("div", { class: "look-opts" },
          toggle("Keep the words clear of the bar's slanted edge (the original cut their tops off)", r.n9.clear, (val) => setN9({ clear: val })),
          toggle("Show the next show's poster when there's no character cut-out", r.n9.poster, (val) => setN9({ poster: val }))) : null,
        h("div", { class: "row" },
          h("button", { class: "btn primary", onclick: () => seeOnShow(n) }, icon("play"), " See it on a show"),
          h("button", { class: "btn", disabled: !r.youtube, onclick: () => findLook(n, "upnext", reload) }, "Find a real one on YouTube")))),
    h("details", { class: "look-more" }, h("summary", null, "Choose a different one"), h("div", { class: "look-row" }, others)));
}

// The characters that pop up on the Nick 2009 "Up next": found on fanart.tv by Rerun, or your own PNGs.
function cutoutsBox() {
  const box = h("div", { class: "cutouts" });
  let timer = null;
  const load = async () => {
    clearTimeout(timer);
    let r;
    try { r = await api("GET", "/api/characters"); } catch (x) { fill(box, h("p", { class: "error" }, x.message)); return; }
    const p = r.progress;
    if (p.running) timer = setTimeout(() => { if (box.isConnected) load(); }, 2500);
    const head = box.closest(".setting") ? null : h("h4", null, "Character cut-outs");
    if (!r.has_key) {
      let key = "", client = "";
      fill(box, head,
        h("p", { class: "hint" }, "Rerun can find a see-through picture of each show's character on fanart.tv, checking each one's quality before using it. fanart.tv needs a free project API key (",
          h("a", { href: "https://fanart.tv/get-an-api-key/#project", target: "_blank", rel: "noopener" }, "Project API keys"),
          "); your personal API key can go with it, for newer pictures sooner."),
        h("div", { class: "row" },
          h("input", { type: "password", autocomplete: "off", placeholder: "fanart.tv project API key", "aria-label": "fanart.tv project API key", oninput: (e) => { key = e.target.value; } }),
          h("input", { type: "password", autocomplete: "off", placeholder: "Personal API key (optional)", "aria-label": "fanart.tv personal API key", oninput: (e) => { client = e.target.value; } }),
          h("button", { class: "btn primary", onclick: async (e) => {
            e.target.disabled = true;
            try { await api("PUT", "/api/characters/key", { key, client }); toast("fanart.tv accepted the key"); load(); } catch (x) { toast(x.message, true); e.target.disabled = false; }
          } }, "Save")));
      return;
    }
    const status = p.running ? `Looking… ${p.done} of ${p.total} shows${p.step ? ` (${p.step})` : ""}, ${p.found} found`
      : p.error ? p.error
      : p.finished ? `Found ${plural(p.found, "new cut-out")} for ${plural(p.total, "show")} that had none.` : "";
    const found = r.items.filter((x) => x.file);
    const missing = r.items.filter((x) => !x.file);
    // your own cut-out: pick a file, or drop one onto the show
    const upload = async (x, file) => {
      if (!file) return;
      try {
        const res = await fetch(`/api/characters/${encodeURIComponent(x.show)}/upload`, { method: "POST", body: file, headers: { "X-Rerun": "1" } });
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Couldn't add that picture");
        toast(`Added ${x.title}'s cut-out`);
        load();
      } catch (err) { toast(err.message, true); }
    };
    const picker = (x, label) => {
      const input = h("input", { type: "file", accept: "image/png,image/webp", hidden: true, onchange: (e) => upload(x, e.target.files[0]) });
      return h("span", null, input, h("button", { class: "btn quiet small", onclick: () => input.click() }, label));
    };
    const droppable = (el, x) => {
      el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop-on"); });
      el.addEventListener("dragleave", () => el.classList.remove("drop-on"));
      el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("drop-on"); upload(x, e.dataTransfer.files[0]); });
      return el;
    };
    fill(box, head,
      h("p", { class: "hint" }, "Pictures from fanart.tv, made by its fans, checked for quality. Your own PNGs in bugs/custom/characters/ always win. New shows get one overnight. ",
        h("button", { class: "linkish", onclick: async () => {
          if (!confirm("Forget the fanart.tv keys?")) return;
          try { await api("PUT", "/api/characters/key", { key: "" }); load(); } catch (x) { toast(x.message, true); }
        } }, "Change keys")),
      h("div", { class: "row" },
        h("button", { class: "btn primary", disabled: p.running, onclick: async () => {
          try { await api("POST", "/api/characters/run"); load(); } catch (x) { toast(x.message, true); }
        } }, "Find cut-outs for my shows"),
        status ? h("span", { class: p.error ? "error" : "muted" }, status) : null),
      found.length ? h("details", { class: "look-more" }, h("summary", null, plural(found.length, "show") + " with a cut-out"),
        h("div", { class: "cut-grid" }, found.map((x) => droppable(h("div", { class: "cut", title: "Drop a PNG here to add it" },
          h("div", { class: "cut-pics" }, x.pics.map((pic) => h("figure", null,
            h("img", { src: `/api/characters/img/${encodeURIComponent(pic.file)}?t=${Date.now()}`, alt: "", loading: "lazy",
              title: pic.own ? "Yours" : pic.kind === "clear art" ? "From fanart.tv, with the show's logo" : "From fanart.tv" }),
            h("button", { class: "cut-x", "aria-label": `Remove this picture of ${x.title}`, onclick: async () => {
              try { await api("DELETE", `/api/characters/${encodeURIComponent(x.show)}?file=${encodeURIComponent(pic.file)}`); load(); } catch (err) { toast(err.message, true); }
            } }, "×")))),
          h("b", { title: x.title }, x.title),
          h("small", { class: "muted" }, x.pics.length === 1 ? "1 picture" : `${x.pics.length} pictures, taking turns`),
          h("div", { class: "look-acts" },
            x.pics.length < 6 ? h("button", { class: "btn quiet small", onclick: async (e) => {
              e.target.disabled = true;
              try { const res = await api("POST", `/api/characters/${encodeURIComponent(x.show)}/another`); toast(res.files && res.files.length ? "Found one more" : "No other good ones on fanart.tv"); } catch (err) { toast(err.message, true); }
              load();
            } }, "Find more") : null,
            x.pics.length < 6 ? picker(x, "Add picture") : null)), x)))) : null,
      missing.length ? h("details", { class: "look-more" }, h("summary", null, plural(missing.length, "show") + " without one"),
        h("p", { class: "hint" }, "Add your own: drop a see-through PNG of the character onto a show, or use “Add picture”."),
        h("ul", { class: "cut-missing" }, missing.map((x) => droppable(h("li", null,
          h("b", null, x.title), h("span", { class: "muted" }, x.why || "Not found"), picker(x, "Add picture")), x)))) : null);
  };
  load();
  return box;
}

function playLook(kind, name) {
  modal((box, close) => {
    box.classList.add("wide");
    add(box, h("div", { class: "pv-head" }, h("h2", null, name.replace(/\.[^.]+$/, "")),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, "×")),
    h("div", { class: "pv-screen" }, h("video", { src: `/api/looks/file/${kind}/${encodeURIComponent(name)}`, controls: true, autoplay: true, loop: true, playsinline: true })));
  });
}

const LOOK_SEARCH = { upnext: "{label} up next green screen template", bumpers: "{label} bumpers 2000s" };

function findLook(n, kind, reload) {
  modal((box, close) => {
    box.classList.add("wide", "finder-dialog");
    const q = h("input", { value: LOOK_SEARCH[kind].replace("{label}", n.label.replace(" / Fox Family", "")), "aria-label": "Search YouTube" });
    const results = h("div", { class: "look-grid" });
    const status = h("p", { class: "hint" });
    const use = async (it, btn) => {
      results.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      btn.textContent = "Downloading…";
      try {
        if (kind === "upnext") {
          await api("POST", "/api/looks/youtube", { kind: "upnext", tag: n.tag, url: it.url, title: it.title });
          toast(`${n.label} now uses “${it.title}”`);
        } else {
          await api("POST", "/api/youtube", { items: [{ url: it.url, title: it.title, id: it.id }], kind: "ads", network: n.tag });
          toast("Downloading: the bumpers show up here once it's done (follow along on the Commercials page)");
        }
        close();
        reload();
      } catch (x) { toast(x.message, true); results.querySelectorAll("button").forEach((b) => { b.disabled = false; }); btn.textContent = "Use this"; }
    };
    const search = async () => {
      fill(status, "Searching YouTube…");
      fill(results);
      try {
        const r = await api("GET", "/api/youtube/search?q=" + encodeURIComponent(q.value));
        fill(status, r.results.length ? (kind === "upnext" ? `Pick one: Rerun downloads it and uses it as ${n.label}'s “Up next”.`
          : `Pick a video: Rerun downloads it and keeps each bumper in it as one of ${n.label}'s.`) : "Nothing found. Try other words.");
        fill(results, r.results.map((it) => {
          const btn = h("button", { class: "btn small primary", onclick: () => use(it, btn) }, "Use this");
          return h("div", { class: "look-yt" },
            h("img", { src: `/api/youtube/thumb/${it.id}.jpg`, alt: "", loading: "lazy" }),
            h("b", null, it.title), h("small", null, `${it.uploader || ""}${it.duration ? " · " + fmtDur(it.duration) : ""}`),
            h("div", { class: "row" }, btn, h("a", { class: "btn small quiet", href: it.url, target: "_blank", rel: "noopener" }, "Watch")));
        }));
      } catch (x) { fill(status, h("span", { class: "error" }, x.message)); }
    };
    add(box, h("div", { class: "pv-head" },
      h("div", null, h("div", { class: "eyebrow" }, n.label), h("h2", null, kind === "upnext" ? "Find a real “Up next”" : "Find bumpers and idents")),
      h("button", { class: "icon-btn", "aria-label": "Close", onclick: close }, "×")),
    h("p", { class: "lede" }, kind === "upnext"
      ? "Green-screen templates work best: Rerun makes the green see-through and plays the show behind it, squeezing it into the window if there is one."
      : "Bumper compilations work well: Rerun splits a video into its separate bumpers and idents and files them under " + n.label + ". Green-screen videos aren't bumpers (they'd air as a green screen): use those for “Up next” instead."),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); search(); } }, q, h("button", { class: "btn primary" }, "Search")),
    status, results);
    search();
  });
}

function settingRow(title, about, ...controls) {
  return h("div", { class: "setting" }, h("div", { class: "about" }, h("h2", null, title), about ? h("p", null, about) : null), h("div", null, controls));
}

// A Save button that only lights up once its part of the page has been changed.
function saveButton(label, getState, onSave, cls = "btn primary") {
  let saved = JSON.stringify(getState());
  const btn = h("button", {
    class: cls, disabled: true, onclick: async () => {
      btn.disabled = true;
      if (await onSave(btn)) saved = JSON.stringify(getState());
      update();
    },
  }, label);
  const update = () => { btn.disabled = JSON.stringify(getState()) === saved; };
  btn.watch = update;
  return btn;
}

async function viewSettings() {
  const v = shell("#/settings");
  let s;
  try { s = await api("GET", "/api/settings"); } catch (x) { add(v, h("p", { class: "error" }, x.message)); return; }
  add(v, pageHead("Preferences", "Settings", "Switches save as soon as you flip them. Sections with a Save button wait for you to press it; the button lights up when there's something to save."));
  const save = async (patch, msg = "Saved") => {
    try { s = await api("PUT", "/api/settings", patch); toast(msg); return true; }
    catch (x) { toast(x.message, true); return false; }
  };

  const auto = { ...s.auto };
  const st = { ...s.stream };
  const enc = h("div");
  const renderEnc = () => {
    const e = s.encoder;
    fill(enc, h("div", { class: "notice " + (e.ok && !e.message ? "ok" : "") },
      e.ok ? (e.message || `Encoding with ${e.label || e.using} — working.${e.using !== "none" ? " The processor is left free for everything else." : ""}`) : e.message || "The encoder hasn't been tested yet."));
  };
  renderEnc();
  const sec = { ...s.security, extra_networks: [...s.security.extra_networks] };
  const extra = h("textarea", { placeholder: "One per line, for example 203.0.113.0/24", oninput: (e) => { sec.extra_networks = e.target.value.split(/\s+/).filter(Boolean); } });
  extra.value = sec.extra_networks.join("\n");
  const cur = h("input", { type: "password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", autocomplete: "new-password" });
  const maps = s.path_map.map((m) => ({ ...m }));
  const mapBox = h("div");
  const saveMaps = saveButton("Save mappings", () => maps.filter((m) => m.from && m.to), () => save({ path_map: maps.filter((m) => m.from && m.to) }), "btn small primary");
  const renderMaps = () => fill(mapBox,
    maps.map((m, i) => h("div", { class: "row", style: { marginBottom: "8px" } },
      h("input", { value: m.from, placeholder: "Path Plex uses, e.g. /data/tv", oninput: (e) => { m.from = e.target.value; }, style: { flex: "1", width: "auto" } }), "→",
      h("input", { value: m.to, placeholder: "Same folder here, e.g. /mnt/media/tv", oninput: (e) => { m.to = e.target.value; }, style: { flex: "1", width: "auto" } }),
      h("button", { class: "btn small quiet", onclick: () => { maps.splice(i, 1); renderMaps(); saveMaps.watch(); } }, "Remove"))),
    h("div", { class: "row" }, h("button", { class: "btn small", onclick: () => { maps.push({ from: "", to: "" }); renderMaps(); } }, "Add a mapping"), saveMaps));
  renderMaps();
  const saveStream = saveButton("Save & test", () => st, async (btn) => {
    btn.textContent = "Testing the encoder…";
    const ok = await save({ stream: st }, "Saved and tested");
    if (ok) renderEnc();
    btn.textContent = "Save & test";
    return ok;
  });
  const saveNet = saveButton("Save network settings", () => sec, () => save({ security: sec }), "btn");
  const watchers = [saveMaps, saveStream, saveNet];
  ["input", "change"].forEach((ev) => v.addEventListener(ev, () => later(() => watchers.forEach((b) => b.watch()), 0)));
  leaveGuard = () => watchers.some((b) => !b.disabled);

  add(v, h("div", { class: "panel" },
    settingRow("Plex & libraries", s.plex.url ? `Connected to ${s.plex.name || "Plex"} at ${s.plex.url}.` : "Plex isn't connected.",
      h("div", { class: "row" },
        h("a", { class: "btn", href: "#/setup/plex/settings" }, "Plex server"),
        h("a", { class: "btn", href: "#/setup/libraries/settings" }, "Libraries & folders"),
        h("a", { class: "btn", href: "#/setup/extras/settings" }, "Networks & TMDB key"),
        h("button", { class: "btn", onclick: async () => { await api("POST", "/api/scan"); toast("Scanning your library…"); } }, "Rescan now")),
      h("label", null, "Rescan every night at"),
      select(Array.from({ length: 24 }, (_, i) => [i, new Date(2000, 0, 1, i).toLocaleTimeString([], { hour: "numeric" })]), s.rescan_hour,
        (val) => save({ rescan_hour: parseInt(val, 10) }), { class: "inline" })),
    settingRow("Automatic channels", "Channels you remove are never added back by themselves.",
      h("div", { class: "stack" },
        toggle("Add a channel for each new network in my library", auto.networks, (val) => { auto.networks = val; save({ auto }); }),
        toggle("Add Christmas and Halloween channels when I have enough holiday episodes", auto.holidays, (val) => { auto.holidays = val; save({ auto }); }))),
    settingRow("Commercials", "Finding classic commercials, and seeing or removing the ones you have, is on the Commercials page.",
      h("a", { class: "btn", href: "#/commercials" }, "Open Commercials")),
    settingRow("Character cut-outs", "The show's character that pops up on Nickelodeon's 2009 “Up next”, found on fanart.tv. Your fanart.tv keys go here.",
      cutoutsBox()),
    settingRow("Picture & sound", "Changes apply to channels started after saving. If hardware encoding doesn't work, Rerun notices and uses the CPU instead.",
      h("div", { class: "grid2" },
        h("div", null, h("label", null, "Picture size"), select([["480p", "480p — lightest"], ["720p", "720p — recommended"], ["1080p", "1080p"]], st.resolution, (val) => { st.resolution = val; })),
        h("div", null, h("label", null, "Video quality, kbps"), h("input", { type: "number", value: st.video_kbps, min: 1000, max: 20000, step: 500, oninput: (e) => { st.video_kbps = parseInt(e.target.value, 10); } })),
        h("div", null, h("label", null, "Hardware encoding"), select([["auto", "Automatic — recommended"], ["vaapi", "Intel Quick Sync / AMD"], ["nvenc", "NVIDIA"], ["none", "Off — use the processor"]], st.hwaccel, (val) => { st.hwaccel = val; })),
        h("div", null, h("label", null, "Channels at once, max"), h("input", { type: "number", value: st.tuners, min: 1, max: 20, oninput: (e) => { st.tuners = parseInt(e.target.value, 10); } }))),
      checkbox("Even out the volume", st.even_volume, (val) => { st.even_volume = val; }, "Keeps commercials from being louder than the shows."),
      checkbox("Network logo in the corner", st.screen_bug !== false, (val) => { st.screen_bug = val; },
        "A see-through logo during shows, like real channels had: the channel's own on a network's channel, each show's own network on a mixed one, as it looked the year the episode aired (the Nick splat on a 2003 episode). For your own, put it in Rerun's data folder under bugs/custom, named like Nickelodeon.png, or Nickelodeon 1984-2009.png for certain years."),
      checkbox("“Up next” banner", st.up_next !== false, (val) => { st.up_next = val; },
        "Says what's on next, near the end of each show, in the network's own style. For the real thing, put a green-screen “Up next” template in Rerun's data folder under bugs/custom/upnext, named after a show or a network (see the README)."),
      checkbox("Promos and “After the break”", st.promos !== false, (val) => { st.promos = val; },
        "“After the break: more SpongeBob” going into a break, and early in a show a “Tonight at 8” promo for a movie or big show later on the same channel."),

      enc,
      saveStream),
    settingRow("Guide", "How far ahead the schedule and Plex's guide reach, and how Plex apps get channel logos and show pictures.",
      select([[24, "1 day"], [48, "2 days"], [72, "3 days"], [120, "5 days"], [168, "7 days"]], s.guide_hours, (val) => save({ guide_hours: parseInt(val, 10) }), { class: "inline" }),
      h("label", null, "Pictures in Plex"),
      select([["public", "Public picture addresses — work in every Plex app"], ["direct", "Straight from Rerun — only Plex Web opened by its IP address"]],
        s.plex_logos, (val) => save({ plex_logos: val }, "Saved. Delete the Rerun DVR in Plex and set it up again to pick this up.")),
      h("p", { class: "hint" }, "Plex apps on a secure connection (Apple TV, phones, app.plex.tv) only load logos from public https addresses. Network logos then come from TMDB through wsrv.nl, and channels without one get a lettered card from placehold.co. Those services only see which logo or channel name is drawn.")),
    settingRow("Privacy & security", "Rerun only answers devices on your home network (and Tailscale) unless you say otherwise.",
      toggle("Home network only", sec.lan_only, (val) => { sec.lan_only = val; }),
      h("label", null, "Also allow these networks"), extra,
      h("div", { style: { marginTop: "12px" } }, saveNet),
      h("div", { class: "subpanel" },
        h("h3", null, "Password"),
        h("div", { class: "grid2" }, h("div", null, h("label", null, "Current password"), cur), h("div", null, h("label", null, "New password"), nw)),
        h("div", { class: "row", style: { marginTop: "14px" } },
          h("button", {
            class: "btn", onclick: async () => {
              try { await api("POST", "/api/auth/password", { current: cur.value, new: nw.value }); toast("Password changed. Other devices were signed out."); cur.value = nw.value = ""; }
              catch (x) { toast(x.message, true); }
            },
          }, "Change password"),
          h("button", { class: "btn quiet", onclick: async () => { await api("POST", "/api/auth/logout"); location.hash = "#/login"; } }, "Sign out")))),
    settingRow("File locations", "Only needed if Plex sees your files under different paths than this computer does. Without a mapping, Rerun streams those files through Plex instead.", mapBox)));
}

// ================================================================ help & health

async function viewHelp() {
  const v = shell("#/help");
  add(v, pageHead("Help", "Help & health", null));
  const plexCard = h("section", { class: "panel" }, h("div", { class: "eyebrow" }, "Once only"), h("h2", null, "Add Rerun to Plex"),
    h("p", { class: "lede" }, "Plex sees Rerun as a TV tuner."));
  const health = h("div");
  const reportBox = h("textarea", { class: "report", readonly: true, hidden: true, "aria-label": "Diagnostic report" });
  const loadReport = async () => {
    const r = await fetch("/api/report", { headers: { "X-Rerun": "1" } });
    if (!r.ok) throw new Error("Couldn't make the report");
    reportBox.value = await r.text();
    reportBox.hidden = false;
  };
  const reportCard = h("section", { class: "panel" }, h("div", { class: "eyebrow" }, "Asking for help"), h("h2", null, "Diagnostic report"),
    h("p", { class: "lede" }, "What Plex has asked Rerun for, how quickly channels started, how hard the computer is working, and recent problems. It has no passwords or Plex sign-in in it. Watch a channel in Plex for a minute first, then copy this and paste it wherever you're asking for help."),
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: async () => {
        try { await loadReport(); copyText(reportBox.value, reportBox); } catch (x) { toast(x.message, true); }
      } }, "Copy report"),
      h("button", { class: "btn quiet", onclick: () => loadReport().catch((x) => toast(x.message, true)) }, "Show it")),
    h("p", { class: "hint" }, "On the server you can also run ", h("code", null, "rerun report"), "."),
    reportBox);
  add(v, plexCard, reportCard, health);
  addToPlexContent(plexCard).then(() => add(plexCard,
    h("div", { class: "subpanel" }, h("h3", null, "Start fresh in Plex"),
      h("p", { class: "hint" }, "Plex remembers a tuner even after you delete its DVR. This gives Rerun a new tuner ID, so Plex sets it up as a brand-new device. First delete the Rerun DVR in Plex, then press this, then set the DVR up again with the addresses above."),
      h("button", { class: "btn", onclick: (e) => modal((box, close) => {
        fill(box, h("div", { class: "eyebrow" }, "Start fresh in Plex"), h("h2", null, "Show Plex a brand-new tuner?"),
          h("p", { class: "lede" }, "Do this after deleting the Rerun DVR in Plex. Your channels and settings in Rerun don't change."),
          h("div", { class: "row end" }, h("button", { class: "btn quiet", onclick: close }, "Cancel"),
            h("button", { class: "btn primary", onclick: async () => {
              try { const r = await api("POST", "/api/plex/new-tuner"); close(); toast(`Done. Rerun's new tuner ID is ${r.device_id}. Now set the DVR up in Plex.`); }
              catch (x) { toast(x.message, true); }
            } }, "Give Rerun a new tuner ID")));
      }) }, "Give Rerun a new tuner ID"))))
    .catch((x) => add(plexCard, h("p", { class: "error" }, x.message)));
  const load = async () => {
    let s;
    try { s = await api("GET", "/api/status"); } catch (x) { fill(health, h("p", { class: "error" }, x.message)); return; }
    const c = s.catalog;
    const up = s.uptime > 86400 ? plural(Math.floor(s.uptime / 86400), "day") : s.uptime > 3600 ? plural(Math.floor(s.uptime / 3600), "hour") : plural(Math.floor(s.uptime / 60), "minute");
    fill(health,
      h("section", { class: "panel" }, h("div", { class: "eyebrow" }, "Right now"), h("h2", null, "How everything's running"),
        s.warnings.length ? s.warnings.map((w) => h("div", { class: "notice" }, w)) : h("div", { class: "notice ok" }, "All systems normal."),
        h("p", null, `Rerun ${s.version}, on the air for ${up}. Video is encoded by ${s.encoder.label || s.encoder.using}.`),
        h("h3", null, "Being watched"),
        s.streams.length ? h("table", null, h("thead", null, h("tr", null, ["Channel", "Viewers", "Playing", "Running for"].map((t) => h("th", null, t)))),
          h("tbody", null, s.streams.map((x) => h("tr", null, h("td", null, `${x.number} ${x.name}`), h("td", null, x.viewers), h("td", null, x.now_playing || "—"),
            h("td", null, plural(Math.floor(x.uptime / 60), "minute"))))))
          : h("p", { class: "muted" }, "Nobody is watching. Channels only use the processor while someone watches."),
        h("h3", null, "Library"),
        h("div", { class: "stats" }, [["shows", "TV shows"], ["episodes", "Episodes"], ["movies", "Movies"], ["music_videos", "Music videos"], ["commercials", "Commercials"]]
          .map(([k, label]) => h("div", { class: "stat" }, h("b", null, (c[k] || 0).toLocaleString()), h("span", null, label)))),
        h("p", { class: "hint" }, s.scan.running ? `Scanning now: ${s.scan.step}` : `Last scanned ${fmtAgo(c.scanned_at)}.`),
        h("details", { class: "subpanel" }, h("summary", { style: { cursor: "pointer", fontWeight: 600, color: "var(--teal)" } }, "Recent activity"),
          h("div", { class: "logs" }, s.logs.slice().reverse().map((l) => h("div", { class: l.level }, `${new Date(l.t * 1000).toLocaleString()}  ${l.msg}`))))));
  };
  load();
  every(load, 8000);
}

// ================================================================ router

// A page with unsaved work sets leaveGuard; it returns true while there's something to lose.
let leaveGuard = null;
let onLeave = [];
let shownHash = null;
let bounceBack = false;

window.addEventListener("beforeunload", (e) => { if (leaveGuard && leaveGuard()) e.preventDefault(); });

async function route() {
  if (bounceBack) { bounceBack = false; return; }
  if (leaveGuard && location.hash !== shownHash && leaveGuard() && !confirm("You have unsaved changes. Leave without saving?")) {
    bounceBack = true;
    location.hash = shownHash;
    return;
  }
  leaveGuard = null;
  shownHash = location.hash;
  onLeave.forEach((fn) => fn());
  onLeave = [];
  clearTimers();
  document.querySelectorAll(".modal-bg").forEach((m) => m.remove());
  window.scrollTo(0, 0);
  let boot;
  try { boot = await api("GET", "/api/bootstrap"); }
  catch (x) { fill($("#app"), h("p", { class: "loading" }, x.message)); return; }
  const hash = location.hash || "#/";
  if (boot.needs_password) { if (hash !== "#/welcome") { location.hash = "#/welcome"; return; } return viewWelcome(); }
  if (!boot.signed_in) { if (hash !== "#/login") { location.hash = "#/login"; return; } return viewLogin(); }
  const setup = hash.match(/^#\/setup\/(\w+)(\/settings)?$/);
  if (setup) return viewSetup(setup[1], setup[2] ? "#/settings" : null);
  if (!boot.setup_complete) { location.hash = "#/setup/plex"; return; }
  const ch = hash.match(/^#\/channel\/(\w+)$/);
  if (ch) return viewChannel(ch[1]);
  const lineupRoute = hash.match(/^#\/lineup\/([a-z0-9]+)$/);
  if (lineupRoute) return viewLineup(lineupRoute[1]);
  const views = { "#/channels": viewChannels, "#/networks": viewNetworks, "#/holidays": viewHolidays, "#/25days": viewTwentyFive, "#/commercials": viewCommercials, "#/looks": viewLooks, "#/guide": viewGuide,
    "#/settings": viewSettings, "#/help": viewHelp, "#/plex": viewHelp, "#/status": viewHelp };
  if (views[hash]) return views[hash]();
  location.hash = "#/channels";
}

// Tie each caption to the box right after it, so clicking the caption focuses the box and
// screen readers announce what the box is for.
let fieldIds = 0;
new MutationObserver(() => {
  document.querySelectorAll("label:not([for]):not(.check):not(.switch):not(.radio-card):not(.pick)").forEach((lab) => {
    const next = lab.nextElementSibling;
    const box = next && (next.matches("input, select, textarea") ? next : next.querySelector(":scope > input, :scope > select"));
    if (!box) return;
    box.id = box.id || `field-${++fieldIds}`;
    lab.htmlFor = box.id;
  });
}).observe(document.getElementById("app"), { childList: true, subtree: true });

window.addEventListener("hashchange", () => route());
route();
