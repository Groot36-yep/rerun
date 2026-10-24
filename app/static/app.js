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

const NAV = [["#/channels", "Channels"], ["#/networks", "Networks"], ["#/holidays", "Holidays"],
  ["#/guide", "TV Guide"], ["#/settings", "Settings"], ["#/help", "Help"]];

function shell(active) {
  const page = NAV.find(([href]) => href === active);
  document.title = page ? `${page[1]} · Rerun` : "Rerun";
  fill($("#app"),
    h("header", { class: "masthead" }, h("div", { class: "masthead-inner" },
      h("a", { class: "brand", href: "#/channels", "aria-label": "Rerun home" }, h("img", { src: "/static/logo.svg", alt: "Rerun" })),
      h("nav", { class: "nav", "aria-label": "Main" }, NAV.map(([href, label]) => h("a", { href, class: href === active ? "on" : null }, label))))),
    h("main", { id: "view", class: "page" }));
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
    const render = async (path) => {
      let d;
      try { d = await api("GET", "/api/folders?path=" + encodeURIComponent(path)); }
      catch (x) { fill(box, h("p", { class: "error" }, x.message)); return; }
      fill(box,
        h("div", { class: "eyebrow" }, "Choose a folder"),
        h("h2", { style: { margin: "6px 0 4px" } }, d.path ? d.path.split(/[\\/]/).filter(Boolean).pop() : "Your media folders"),
        h("p", { class: "crumb" }, d.path || "These are the folders Rerun is allowed to read."),
        h("div", { class: "folder-list" },
          d.parent ? h("button", { onclick: () => render(d.parent) }, icon("up"), "Up a level") : null,
          d.path && !d.parent ? h("button", { onclick: () => render("") }, icon("up"), "All media folders") : null,
          d.dirs.map((x) => h("button", { onclick: () => render(x.path) }, icon("folder"), x.name))),
        !d.dirs.length && !d.path ? h("p", { class: "notice" }, "No media folders are shared with Rerun yet. Run the installer again and add your media folder.") : null,
        d.path ? h("p", { class: "hint" }, d.videos ? `${plural(d.videos, "video")} in this folder, plus everything in its sub-folders.` : "Everything in its sub-folders is included.") : null,
        h("div", { class: "row end", style: { marginTop: "14px" } },
          h("button", { class: "btn quiet", onclick: close }, "Cancel"),
          d.path ? h("button", { class: "btn primary", onclick: () => { onPick(d.path); close(); } }, "Use this folder") : null));
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
      ch.live ? h("span", { class: "on-air" }, "Being watched") : h("span", null, ch.season ? `Holiday channel · ${ch.season_dates}` : describeInfo(ch.info))),
    nowCell, nextCell,
    h("div", { class: "l-actions" },
      onOff,
      h("a", { class: "icon-btn", href: `#/channel/${ch.id}`, title: "Edit this channel", "aria-label": `Edit ${ch.name}` }, icon("edit")),
      h("button", { class: "icon-btn", title: "Copy the stream address (for VLC and other players)", "aria-label": `Copy the stream address for ${ch.name}`, onclick: () => copyText(`${location.origin}/stream/${ch.number}.ts`) }, icon("link"))));
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
  add(v, bar, body);
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
          hd.channel
            ? h("a", { class: "btn small", href: `#/channel/${hd.channel}` }, "Edit channel")
            : total ? h("button", {
              class: "btn small primary", onclick: async () => {
                const res = await api("POST", `/api/holidays/${hd.key}/channel`);
                toast(`Added on channel ${res.number}`);
                load();
              },
            }, "Add channel") : null),
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
  load();
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
        commercials: { enabled: true, break_style: "chapters", sources: [], tags: [] } }) : api("GET", `/api/channels/${cid}`),
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
        ["chapters", "At act breaks", "Uses the chapter marks in DVD and Blu-ray rips, like the original broadcast."],
        ["even", "Evenly spaced", "Splits each show into even parts."],
        ["end", "Between shows", "Never interrupts a show."],
      ], com.break_style, (val) => { com.break_style = val; refresh(); }),
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
    fill(showList,
      info.show_list.length ? h("div", { class: "show-list" }, info.show_list.map((s) => h("div", null,
        h("span", { class: "grow", title: s.title }, s.title), h("span", { class: "muted" }, s.episodes),
        h("button", { class: "btn small quiet", title: "Never play this show here", onclick: () => { names[s.id] = s.title; ch.exclude.push(s.id); refresh(); } }, "Hide")))) : null,
      renderExcluded());
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
              class: "g-block" + (isNow ? " now" : b.movie ? " movie" : ""), title: `${fmtTime(b.s)}–${fmtTime(b.e)}  ${b.title}${b.sub ? " — " + b.sub : ""}`,
              style: { left: ((s - g.start) / 60) * PX + 2 + "px", width: Math.max(4, ((e - s) / 60) * PX - 4) + "px" },
            }, h("b", null, b.title || "—"), b.sub ? h("span", null, b.sub) : null);
          })))));
  };
  load();
  every(load, 60000);
}

// ================================================================ settings

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
    settingRow("Picture & sound", "Changes apply to channels started after saving. If hardware encoding doesn't work, Rerun notices and uses the CPU instead.",
      h("div", { class: "grid2" },
        h("div", null, h("label", null, "Picture size"), select([["480p", "480p — lightest"], ["720p", "720p — recommended"], ["1080p", "1080p"]], st.resolution, (val) => { st.resolution = val; })),
        h("div", null, h("label", null, "Video quality, kbps"), h("input", { type: "number", value: st.video_kbps, min: 1000, max: 20000, step: 500, oninput: (e) => { st.video_kbps = parseInt(e.target.value, 10); } })),
        h("div", null, h("label", null, "Hardware encoding"), select([["auto", "Automatic — recommended"], ["vaapi", "Intel Quick Sync / AMD"], ["nvenc", "NVIDIA"], ["none", "Off — use the processor"]], st.hwaccel, (val) => { st.hwaccel = val; })),
        h("div", null, h("label", null, "Channels at once, max"), h("input", { type: "number", value: st.tuners, min: 1, max: 20, oninput: (e) => { st.tuners = parseInt(e.target.value, 10); } }))),
      checkbox("Even out the volume", st.even_volume, (val) => { st.even_volume = val; }, "Keeps commercials from being louder than the shows."),
      enc,
      saveStream),
    settingRow("Guide", "How far ahead the schedule and Plex's guide reach, and how Plex apps get channel logos and show pictures.",
      select([[24, "1 day"], [48, "2 days"], [72, "3 days"], [120, "5 days"], [168, "7 days"]], s.guide_hours, (val) => save({ guide_hours: parseInt(val, 10) }), { class: "inline" }),
      h("label", null, "Pictures in Plex"),
      select([["through_plex", "Through your Plex server — works in every Plex app"], ["direct", "Straight from Rerun — only when Plex is opened by its IP address"]],
        s.plex_logos, (val) => save({ plex_logos: val }, "Saved. In Plex, refresh the guide (or set the DVR up again) to pick this up.")),
      h("p", { class: "hint" }, "Plex apps over a secure connection (app.plex.tv, Apple TV, phones) won't load pictures straight from Rerun, so by default they go through your Plex server.")),
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
  const views = { "#/channels": viewChannels, "#/networks": viewNetworks, "#/holidays": viewHolidays, "#/guide": viewGuide,
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
