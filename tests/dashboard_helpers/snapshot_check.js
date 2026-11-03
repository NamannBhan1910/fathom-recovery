// Opens a snapshot HTML the way a browser OUTSIDE Fathom would (no window.Panel): runs only the snapshot shim and asks
// it for the data the dashboard displayed. Usage: node snapshot_check.js <snapshot.html>
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const shims = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(s => s.includes("FATHOM_SNAPSHOT"));
const win = { console, JSON, Date };
win.window = win;
vm.createContext(win);
shims.forEach(s => vm.runInContext(s, win));
(async () => {
  const exact = await win.Panel.runFile("scripts/get_positions.py", { args: "--book A" });
  const otherArgs = await win.Panel.runFile("scripts/get_positions.py", { args: "--book Z" });
  const missing = await win.Panel.runFile("scripts/not_captured.py", {});
  const read = await win.Panel.readFile("app/notes.txt");
  console.log(JSON.stringify({ shims: shims.length, exact, otherArgs, missing, read, meta: win.FATHOM_SNAPSHOT }));
})();
