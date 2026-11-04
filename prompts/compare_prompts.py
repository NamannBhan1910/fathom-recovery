import re, difflib, hashlib, os

PASTED = r'''Panel name: "Trader Positions"

Set up and maintain Fathom recovery for this panel. If the panel name above is empty or still a placeholder, ask me for the name and stop. Use the panel's existing recovery_config.json if present; do not silently change its panel_name. Do not change business logic, broker feeds, dashboard Refresh behavior, schedules, dependencies, or paths outside recovery setup.

SOURCE AND VERSION
Use only https://github.com/NamannBhan1910/fathom-recovery.git at tag v1.9.5. Clone once into /tmp if network access is allowed. Report the resolved commit. Before copying anything, verify these five files in the clone by byte count and SHA-256 prefix:
  fathom_recovery.py          33513  bc5a3e61126a
  recovery_sync.py             9864  15901eb71631
  recovery_hook.js             7686  3be4cb910313
  install_dashboard_hook.py    6884  907ff8ca221d
  recovery_watch.py           13003  feba323c4ba7
The backup script's internal __version__ of 1.9.1 is expected. If the tag cannot be obtained or any verification fails, stop before installing or uploading; request the v1.9.5 release files from me. Do not substitute v1.9.4 or improvise the scripts. Never put credentials in files or chat.

DESTINATION AND BEHAVIOR
Use bucket mosaic-derivatives-margining, prefix 6_Recovery, and outputs/ for generated data. Resolve the authenticated user and panel folder using the release's folder-matching rules; never invent RECOVERY_USER or accept unknown-user for a watcher. Destination: 6_Recovery/<user>/<Panel>/code/ for one current workspace copy, and 6_Recovery/<user>/<Panel>/<YYYYMMDD>_current_data/ for that UTC day's changed data. Preserve older folders. A changed file may replace today's copy; never delete S3 objects or modify a prior day's folder.
The release's sweep decides eligible data types and exclusions; code-sweep backs up eligible workspace files, including hidden and empty files/folders, while excluding secrets, oversized files, symlinks, and data files already covered by sweep. Do not override its safety rules. After a dashboard refresh, the hook captures successful Panel responses. recovery_sync.py stores them in outputs/dashboard_data/, builds a standalone outputs/dashboard_snapshot/<dashboard>.html with embedded responses and a .panel-body wrapper for fragment dashboards, and sweeps changed data and code to S3. An existing downloaded HTML file must be downloaded again to see a later snapshot.

SETUP OR SESSION START
1. Inspect recovery_config.json, outputs/, scripts/, and dashboard HTML candidates without changing them. Check that scripts/fathom_recovery.py has the five release markers: LAYOUT="user/panel", CODE_BACKUP="1", CODE_FLAT="1", CODE_SCOPE="all", PROBE_DIR="data-day". If it has neither LAYOUT nor __version__, stop; never overwrite it. If missing or outdated, install the verified v1.9.5 fathom_recovery.py. Keep existing recovery_config.json; create it only if missing. Use the real panel name.
2. Run setup with RECOVERY_DESTINATION_BUCKET=mosaic-derivatives-margining and RECOVERY_DESTINATION_PREFIX=6_Recovery, Python -B and PYTHONDONTWRITEBYTECODE=1: python3 scripts/fathom_recovery.py setup --panel-name "Trader Positions" --output-dir outputs --apply. Require PASS for config, output_dir, destination read, folder resolution, and write probe. Its probe must be <YYYYMMDD>_current_data/selfcheck_probe.txt. Report the resolved user/panel folders and sweep results. Stop on authentication, authorization, bucket, signature, or other setup failure.
3. If a dashboard HTML exists, install missing recovery_hook.js and install_dashboard_hook.py from the verified release. Check scripts/recovery_sync.py: if it is the older v1.9.4 file (9379 bytes / SHA prefix 0ed7df8072be), lacks the panel-body wrapper, or is missing, replace it with the verified v1.9.5 file and report old/new hashes. Leave an existing different file with the wrapper in place and report that it differs from the pin. Run node --check on the hook; run installer and recovery_sync.py --dry-run first. Then run install_dashboard_hook.py --apply and require exactly one marked hook block. It may change only that block in the dashboard. If discovery is ambiguous or HTML is not UTF-8, stop and ask which dashboard file to use. Run recovery_sync.py once without --dry-run; require ok=true. For a fragment dashboard, verify the generated snapshot begins with <!doctype html> and has exactly one <div class="panel-body">. If no dashboard HTML exists, report dashboard sync skipped; do not create one.
4. Install or start recovery_watch.py ONLY if I have asked for automatic backup in this panel. Install it and recovery_sync.py from the verified release if missing, even when this panel has no dashboard. Apply the same recovery_sync.py version check from step 3. Check watcher --status first; never start a second watcher. A runtime restart stops it, so restart it at future session starts only when previously requested. Keep its log outside the workspace, such as /tmp/fathom_watch.log. Require running=true, last_ok=true, the resolved user folder, and a sweep ok log line before reporting success. If it loaded an old recovery_sync.py, stop and restart it after the replacement. Report any growing failure count. Do not use a made-up user to bypass refusal.
5. At every later session start, repeat version checks, restore a missing dashboard hook, check a previously requested watcher, and run one data sweep and one code sweep. After creating/changing an outputs/ data file, run `python3 -B scripts/fathom_recovery.py sweep --config recovery_config.json --state-file .fathom_recovery/state.json --apply` as the last step. After changing any other allowed workspace file, run `python3 -B scripts/fathom_recovery.py code-sweep --config recovery_config.json --state-file .fathom_recovery/code_state.json --apply` as the last step. Set the same destination and no-bytecode environment variables as in setup. Do not run either sweep twice just to report setup results.

VERIFY AND REPORT
After upload, run verify-code --config recovery_config.json and list-code --config recovery_config.json --run live. Require mismatch=0, missing_in_s3=0, and S3_FILE_COUNT=MANIFEST_FILE_COUNT; report any S3_ONLY entries. Report every changed local file, all uploaded/unchanged/failed counts and skipped-sensitive/large/symlink paths, the UTC day folder, whether scripts/ and knowledge/ are present, and the dashboard snapshot key. If no upload occurred, say so and skip remote verification. Do not claim the browser refresh path is tested until a real dashboard refresh returns new data, [FathomRecovery] sync ok appears, and S3's embedded revision/records match that refresh. A snapshot replays captured Panel responses; it is not a hosted live dashboard. A refresh while an agent holds the workspace lock may need a later refresh to catch up.

LIMITS
Write only the release recovery files under scripts/, recovery_config.json if missing, .fathom_recovery/ state, outputs/ and its dashboard_data/ and dashboard_snapshot/ subfolders, data files I explicitly request, and the installer's single marked dashboard hook block. Do not create scratch or diagnostic files in the panel workspace, delete anything in S3, alter the dashboard's business logic, or move files written outside outputs/; report those paths instead. Restore only when I ask, into a new local restored_code/ folder first. Never claim a check ran unless you saw its result. On any failure, show the exact safe error and stop; do not silently retry or claim success.
'''

FILE = open('prompt_v195_panel.md', encoding='utf-8').read()
os.makedirs('/tmp/cmp', exist_ok=True)
open('/tmp/cmp/pasted_prompt.txt', 'w', encoding='utf-8').write(PASTED)

print("pasted:  %d bytes, %d lines, sha256 %s" % (len(PASTED.encode()), PASTED.count('\n'), hashlib.sha256(PASTED.encode()).hexdigest()[:12]))
print("file:    %d bytes, %d lines, sha256 %s" % (len(FILE.encode()), FILE.count('\n'), hashlib.sha256(FILE.encode()).hexdigest()[:12]))
print("difflib quick ratio over lines: %.3f" % difflib.SequenceMatcher(None, PASTED.splitlines(), FILE.splitlines()).ratio())

checks = [
 ("pin fathom_recovery.py 33513/bc5a3e61126a", r"33513.*bc5a3e61126a"),
 ("pin recovery_sync.py 9864/15901eb71631", r"9864.*15901eb71631"),
 ("pin recovery_hook.js 7686/3be4cb910313", r"7686.*3be4cb910313"),
 ("pin install_dashboard_hook.py 6884/907ff8ca221d", r"6884.*907ff8ca221d"),
 ("pin recovery_watch.py 13003/feba323c4ba7", r"13003.*feba323c4ba7"),
 ("old v1.9.4 pin 9379/0ed7df8072be", r"9379.*0ed7df8072be|0ed7df8072be"),
 ("tag v1.9.5", r"v1\.9\.5"),
 ("expected __version__ 1.9.1", r"__version__.*1\.9\.1"),
 ("STOP + request release files if tag/verification fails", r"request the v1\.9\.5 release files"),
 ("forbids improvising scripts", r"improvise the scripts"),
 ("local-write fallback from specs", r"WRITE IT LOCALLY|write the files locally|written locally"),
 ("sha256 only 'if a hash tool exists'", r"if a hash tool exists"),
 ("'no hash tool' reporting", r"no hash tool"),
 ("embedded Script spec", r"## Script spec"),
 ("embedded Dashboard sync spec", r"## Dashboard sync spec"),
 ("embedded Watcher spec", r"## Watcher spec"),
 ("watcher --help / --once --dry-run pre-check", r"--once --dry-run"),
 ("dashboard size+sha before/after hook", r"size and sha256 prefix"),
 ("installer --remove restores bytes", r"--remove"),
 ("py_compile / import prohibition", r"py_compile"),
 ("stray file / __pycache__ reporting", r"__pycache__"),
 ("never unknown-user watcher", r"unknown-user"),
 ("never invent RECOVERY_USER", r"RECOVERY_USER"),
 ("rule: list --legacy", r"--legacy"),
 ("rule: future-date question", r"future date"),
 ("rule: regenerate dashboard -> reinstall hook", r"regenerate or edit the dashboard"),
 ("ambiguous folder: exact spelling", r"exact spelling"),
 ("STOP on AccessDenied/NoCredentials/NoSuchBucket", r"AccessDenied"),
 ("panel-body wrapper check", r'panel-body'),
 ("node --check", r"node --check"),
 ("/tmp/fathom_watch.log", r"/tmp/fathom_watch\.log"),
 ("S3_ONLY mention", r"S3_ONLY"),
 ("failures growing", r"growing"),
 ("forbids installing anything", r"install anything"),
 ("forbids local delete/move", r"delete anything locally|move or delete"),
 ("restored_code restore rule", r"restored_code"),
 ("browser-not-tested caveat", r"FathomRecovery\] sync ok"),
 ("lock-held refresh caveat", r"workspace lock|holds the workspace lock"),
 ("downloaded copy doesn't self-update", r"does not update itself|downloaded again|download it again"),
 ("one sweep each at session start / not twice", r"twice|do not run them again"),
 ("never put credentials in files/chat", r"credential|secret, password, token"),
]
print("\n%-58s %-7s %-7s" % ("check", "pasted", "file"))
print("-"*74)
diffs = 0
for label, rx in checks:
    p = bool(re.search(rx, PASTED, re.I | re.S))
    f = bool(re.search(rx, FILE, re.I | re.S))
    flag = "" if p == f else "  <-- DIFFERS"
    diffs += (p != f)
    print("%-58s %-7s %-7s%s" % (label, "yes" if p else "no", "yes" if f else "no", flag))
print("\ndiffering checks: %d of %d" % (diffs, len(checks)))

# Locate the stop-vs-fallback wording in the real file
print("\n--- file lines mentioning fallback/stop on clone failure ---")
for i, l in enumerate(FILE.splitlines(), 1):
    if re.search(r"do NOT stop|Never stop just because|If the clone fails", l):
        print(i, l[:200])
print("\n--- pasted lines mentioning stop on verification failure ---")
for i, l in enumerate(PASTED.splitlines(), 1):
    if re.search(r"stop before installing|improvise", l):
        print(i, l[:200])
