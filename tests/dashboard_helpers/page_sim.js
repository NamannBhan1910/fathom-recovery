// Simulates ONE page load / refresh of a dashboard HTML under Node: runs its <script> blocks in order with a mocked
// window.Panel. runFile of scripts/recovery_sync.py really spawns python (against the fake S3 from sitecustomize);
// writeFile really writes into the temp workspace. Usage: node page_sim.js <workspace> <rows>
const fs = require("fs"), path = require("path"), vm = require("vm"), cp = require("child_process");
const [ws, rows] = process.argv.slice(2);
const html = fs.readFileSync(path.join(ws, "app", "dashboard.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const Panel = {
  runFile: async (p, o) => {
    if (p === "scripts/recovery_sync.py") {
      const r = cp.spawnSync("python3", ["-B", p], { cwd: ws, env: process.env, encoding: "utf8" });
      return { success: true, result: { exit_code: r.status, stdout: r.stdout, stderr: r.stderr } };
    }
    if (p === "scripts/get_positions.py") {
      return { success: true, result: { exit_code: 0, stderr: "", stdout: JSON.stringify({ rows: Number(rows), args: o && o.args }) } };
    }
    return { success: false, error: "unknown script " + p };
  },
  readFile: async (p) => ({ success: true, content: "notes for " + p }),
  writeFile: async (p, c) => {
    const f = path.join(ws, p);
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, c);
    return { success: true };
  },
};
const win = { Panel, console: { log() {} }, setTimeout, clearTimeout, Date, JSON, FATHOM_RECOVERY_DEBOUNCE_MS: 100 };
win.window = win;
vm.createContext(win);
for (const s of scripts) vm.runInContext(s, win);
let last = -1, changed = Date.now();
const t0 = Date.now();
(function wait() {
  const st = win.FathomRecovery && win.FathomRecovery.state;
  const n = st ? st.syncs : 0;
  if (n !== last) { last = n; changed = Date.now(); }
  if ((n >= 1 && Date.now() - changed > 700) || Date.now() - t0 > 20000) {
    console.log(JSON.stringify({ syncs: n, rendered_rows: win.__render, lastResult: st && st.lastResult, lastError: st && st.lastError }));
    process.exit(0);
  }
  setTimeout(wait, 100);
})();
