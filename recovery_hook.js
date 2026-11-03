/* Fathom dashboard -> S3 recovery hook. Generic: works for any dashboard HTML.
 *
 * Installed at the TOP of the dashboard so it is in place before the dashboard's own scripts run.
 *   1. It wraps window.Panel.runFile / readFile / readData (also when Panel is assigned later) and remembers
 *      every successful response ("what the dashboard displayed").
 *   2. After a page load/refresh (and after new responses arrive, debounced) it saves them under
 *      .fathom_recovery/inbox/ and runs scripts/recovery_sync.py, which merges them, builds the dashboard
 *      snapshot and runs the data sweep + code sweep (only changed files upload).
 * Manual API:  FathomRecovery.save("name", dataOrString)   FathomRecovery.sync()   FathomRecovery.state
 * Options (set before this script): FATHOM_RECOVERY_AUTO=false (no capture), FATHOM_RECOVERY_DEBOUNCE_MS,
 * FATHOM_RECOVERY_MAX_BYTES, FATHOM_RECOVERY_NAME_BY_ARGS=false, FATHOM_RECOVERY_SYNC_SCRIPT.
 */
(function () {
  "use strict";
  if (window.FathomRecovery) { return; }
  var SYNC_SCRIPT = window.FATHOM_RECOVERY_SYNC_SCRIPT || "scripts/recovery_sync.py";
  var DEBOUNCE_MS = window.FATHOM_RECOVERY_DEBOUNCE_MS || 4000;
  var MAX_BYTES = window.FATHOM_RECOVERY_MAX_BYTES || 5 * 1024 * 1024;
  var NAME_BY_ARGS = window.FATHOM_RECOVERY_NAME_BY_ARGS !== false;
  var AUTO = window.FATHOM_RECOVERY_AUTO !== false;
  var pending = {}, responses = {}, sigs = {}, respDirty = false;
  var timer = null, running = false, rerun = false, origRun = null;
  var state = { syncs: 0, saved: 0, remembered: 0, lastResult: null, lastError: null };

  function log() {
    try { console.log.apply(console, ["[FathomRecovery]"].concat([].slice.call(arguments))); } catch (e) { /* ignore */ }
  }
  function safeName(s) {
    s = String(s || "data").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[^A-Za-z0-9]+/, "");
    return (s || "data").slice(0, 100);
  }
  function hash(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) { h = ((h << 5) + h + s.charCodeAt(i)) >>> 0; }
    return h.toString(36);
  }
  function argKey(a) {
    if (a === undefined || a === null) { return ""; }
    return typeof a === "string" ? a : JSON.stringify(a);
  }
  function toPayload(data) {
    if (typeof data === "string") {
      var ext = "txt";
      try { JSON.parse(data); ext = "json"; } catch (e) { /* plain text */ }
      return { text: data, ext: ext };
    }
    return { text: JSON.stringify(data), ext: "json" };
  }

  // remember a successful Panel response; this is what the S3 snapshot replays
  function remember(method, path, args, res) {
    if (!path || res === undefined || res === null || res.success === false) { return; }
    var key = method + "|" + path + "|" + argKey(args), s;
    try { s = JSON.stringify(res); } catch (e) { return; }
    if (!s || s.length > MAX_BYTES) { log("response too large, not kept for the snapshot:", key); return; }
    if (sigs[key] === s) { return; }
    sigs[key] = s;
    responses[key] = { t: Date.now(), r: res };
    respDirty = true;
    state.remembered++;
    schedule();
  }
  function capture(path, opts, res) {
    if (!path || path === SYNC_SCRIPT || !res || res.success === false || !res.result) { return; }
    var ec = res.result.exit_code;
    if (ec !== undefined && ec !== null && ec !== 0) { return; }
    remember("runFile", path, opts && opts.args, res);
    var out = res.result.stdout;
    if (typeof out !== "string" || !out.trim()) { return; }
    var base = String(path).replace(/\.[^.\/\\]+$/, "").replace(/[\\\/]+/g, "_");
    var args = opts && opts.args;
    if (NAME_BY_ARGS && args) { base += "_" + hash(String(args)); }
    save(base, out);
  }
  function wrap(P) {
    origRun = P.runFile;
    if (!AUTO) { return; }
    try {
      P.runFile = async function (path, opts) {
        var res = await origRun.apply(this, arguments);
        try { capture(path, opts, res); } catch (e) { log("capture error", String(e)); }
        return res;
      };
      ["readFile", "readData"].forEach(function (m) {
        var o = P[m];
        if (typeof o !== "function") { return; }
        P[m] = async function (path) {
          var res = await o.apply(this, arguments);
          try { remember(m, path, undefined, res); } catch (e) { log("capture error", String(e)); }
          return res;
        };
      });
    } catch (e) { log("could not wrap window.Panel:", String(e)); }
  }
  function install() {
    if (origRun) { return true; }
    var P = window.Panel;
    if (!P || typeof P.runFile !== "function") { return false; }
    wrap(P);
    return true;
  }
  // if the page loads before window.Panel exists, wrap it the moment it is assigned (no early call is missed)
  function trap() {
    try {
      if (window.Panel) { return; }
      var val;
      Object.defineProperty(window, "Panel", {
        configurable: true, enumerable: true,
        get: function () { return val; },
        set: function (v) { val = v; try { install(); } catch (e) { /* polled below */ } }
      });
    } catch (e) { /* not configurable: polling below */ }
  }

  function save(name, data) {
    if (data === undefined || data === null) { return false; }
    var p = toPayload(data);
    if (!p.text || p.text.length > MAX_BYTES) { log("skipped (empty or too large):", name); return false; }
    pending[safeName(name)] = p;
    state.saved++;
    schedule();
    return true;
  }
  function schedule(delay) {
    if (timer) { clearTimeout(timer); }
    timer = setTimeout(function () { timer = null; flush(); }, delay === undefined ? DEBOUNCE_MS : delay);
  }
  async function writeInbox(name, text, ext) {
    var path = ".fathom_recovery/inbox/" + name + "__" + Date.now() + "." + ext;
    var w = await window.Panel.writeFile(path, text,
      { mimeType: ext === "json" ? "application/json" : "text/plain" });
    if (w && w.success === false) { log("writeFile failed for", name, w.error || ""); }
  }
  async function flush() {
    if (!install()) { log("window.Panel not ready; retrying"); schedule(1000); return; }
    if (running) { rerun = true; return; }
    running = true;
    try {
      do {
        rerun = false;
        var batch = pending; pending = {};
        var keys = Object.keys(batch);
        for (var i = 0; i < keys.length; i++) { await writeInbox(keys[i], batch[keys[i]].text, batch[keys[i]].ext); }
        if (respDirty) {
          respDirty = false;
          await writeInbox("dashboard_responses", JSON.stringify(responses), "json");
        }
        var res = await origRun.call(window.Panel, SYNC_SCRIPT, { timeout: 900 });
        var parsed = null;
        try {
          var lines = String((res && res.result && res.result.stdout) || "").trim().split("\n");
          parsed = JSON.parse(lines[lines.length - 1]);
        } catch (e) { /* not json */ }
        state.syncs++;
        state.lastResult = parsed || res;
        state.lastError = (parsed && parsed.ok) ? null : "sync reported a problem";
        log("sync", parsed && parsed.ok ? "ok" : "PROBLEM", parsed || res);
      } while (rerun || respDirty || Object.keys(pending).length);
    } catch (e) {
      state.lastError = String(e);
      log("sync failed", state.lastError);
    } finally {
      running = false;
    }
  }

  window.FathomRecovery = {
    save: save,
    sync: function () { schedule(0); },
    state: state,
    installed: function () { return !!origRun; }
  };

  install() || trap();
  var tries = 0;
  (function boot() {
    if (install()) { schedule(); return; }          // sync once per page load / refresh
    if (++tries < 80) { setTimeout(boot, 250); } else { log("window.Panel never appeared; hook inactive"); }
  })();
})();
