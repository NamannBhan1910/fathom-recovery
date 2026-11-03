// Runs scripts/recovery_hook.js under Node with a mocked window.Panel (no browser, no S3).
const fs = require("fs"), path = require("path"), vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "..", "..", "recovery_hook.js"), "utf8");
let pass = 0, fail = 0;
const check = (n, c) => { c ? pass++ : fail++; console.log((c ? "PASS " : "FAIL ") + n); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

function makeEnv(opts) {
  const writes = [], runs = [];
  const Panel = {
    runFile: async (p, o) => {
      runs.push({ p, o });
      if (p === "scripts/recovery_sync.py") return { success: true, result: { exit_code: 0, stdout: 'log\n{"ok": true}' } };
      if (p === "scripts/fail.py") return { success: true, result: { exit_code: 1, stdout: "{\"x\":1}" } };
      return { success: true, result: { exit_code: 0, stdout: JSON.stringify({ rows: runs.length }) } };
    },
    writeFile: async (p, c, o) => { writes.push({ p, c, o }); return { success: true }; },
  };
  const win = Object.assign({ Panel, console, setTimeout, clearTimeout, Date, JSON }, opts || {});
  win.window = win;
  vm.createContext(win);
  vm.runInContext(src, win);
  return { win, writes, runs, Panel };
}

(async () => {
  let e = makeEnv({ FATHOM_RECOVERY_DEBOUNCE_MS: 50 });
  await sleep(400);
  check("page load triggers one sync run", e.runs.filter(r => r.p === "scripts/recovery_sync.py").length === 1);
  check("sync run uses a long timeout", e.runs[0].o && e.runs[0].o.timeout === 900);

  e = makeEnv({ FATHOM_RECOVERY_DEBOUNCE_MS: 50 });
  await sleep(300);
  const base = e.runs.length;
  await e.win.Panel.runFile("scripts/get_positions.py", { args: "--date 2026-01-01" });
  await e.win.Panel.runFile("scripts/get_positions.py", { args: "--date 2026-01-02" });
  await sleep(400);
  const data = e.writes.filter(w => !w.p.includes("dashboard_responses"));
  check("dashboard data captured to inbox (2 arg variants -> 2 files)",
    data.length === 2 && data.every(w => /^\.fathom_recovery\/inbox\/scripts_get_positions_[a-z0-9]+__\d+\.json$/.test(w.p)));
  check("responses file for the S3 snapshot written once, with both calls",
    e.writes.filter(w => w.p.includes("dashboard_responses")).length === 1 &&
    Object.keys(JSON.parse(e.writes.find(w => w.p.includes("dashboard_responses")).c)).length === 2);
  check("captured content is the script stdout", JSON.parse(e.writes[0].c).rows !== undefined);
  check("burst of runFile calls -> ONE sync", e.runs.slice(base).filter(r => r.p === "scripts/recovery_sync.py").length === 1);
  check("sync script's own runFile is not captured", !e.writes.some(w => w.p.includes("recovery_sync")));

  await e.win.Panel.runFile("scripts/fail.py", {});
  await sleep(300);
  check("failed script (exit 1) is not captured", !e.writes.some(w => w.p.includes("fail")));

  const before = e.writes.length;
  e.win.FathomRecovery.save("manual data", { a: 1 });
  await sleep(300);
  check("manual save sanitizes the name",
    e.writes.length === before + 1 && /inbox\/manual_data__\d+\.json$/.test(e.writes[e.writes.length - 1].p));

  e = makeEnv({ FATHOM_RECOVERY_DEBOUNCE_MS: 50, FATHOM_RECOVERY_AUTO: false });
  await sleep(300);
  await e.win.Panel.runFile("scripts/get_positions.py", {});
  await sleep(300);
  check("FATHOM_RECOVERY_AUTO=false disables capture", e.writes.length === 0);

  e = makeEnv({ FATHOM_RECOVERY_DEBOUNCE_MS: 50, FATHOM_RECOVERY_MAX_BYTES: 10 });
  await sleep(300);
  e.win.FathomRecovery.save("big", "x".repeat(100));
  await sleep(300);
  check("oversize payload skipped", e.writes.length === 0);

  // Panel appears late
  const writes = [], runs = [];
  const win = { console, setTimeout, clearTimeout, Date, JSON, FATHOM_RECOVERY_DEBOUNCE_MS: 50 };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(src, win);
  await sleep(600);
  win.Panel = { runFile: async (p) => { runs.push(p); return { success: true, result: { exit_code: 0, stdout: '{"ok":true}' } }; }, writeFile: async () => ({ success: true }) };
  await sleep(1200);
  check("hook waits for window.Panel and then syncs", runs.includes("scripts/recovery_sync.py"));

  console.log("\nRESULT: " + (pass + fail) + " checks, " + fail + " failed");
  process.exit(fail ? 1 : 0);
})();
