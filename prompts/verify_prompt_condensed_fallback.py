import re
T = open('prompt_v195_condensed_fallback.md', encoding='utf-8').read()
checks = [
 ("pin fathom_recovery.py", r"33513\s+bc5a3e61126a", True),
 ("pin recovery_sync.py", r"9864\s+15901eb71631", True),
 ("pin recovery_hook.js", r"7686\s+3be4cb910313", True),
 ("pin install_dashboard_hook.py", r"6884\s+907ff8ca221d", True),
 ("pin recovery_watch.py", r"13003\s+feba323c4ba7", True),
 ("old stop-and-request wording removed", r"request the v1\.9\.5 release files", False),
 ("old 'improvise' prohibition removed", r"improvise the scripts", False),
 ("local-write fallback present", r"do NOT stop and do NOT keep retrying", True),
 ("clone only if allowed", r"ONLY if your runtime rules allow fetching code", True),
 ("sha256 only if hash tool exists", r"when a hash tool exists", True),
 ("no hash tool reporting", r"no hash tool, compared size and commit only", True),
 ("step1 local write from Script spec", r"WRITE IT LOCALLY from the Script spec", True),
 ("nine commands listed", r"setup sweep code-sweep selfcheck list list-code verify-code restore-code restore", True),
 ("step3 local write Dashboard sync spec", r"write the missing ones locally from the Dashboard sync spec", True),
 ("recovery_sync replacement fallback", r"else written locally from the Dashboard sync spec", True),
 ("watcher local write", r"recovery_watch\.py from the Watcher spec", True),
 ("watcher --help + --once --dry-run", r"--once --dry-run", True),
 ("clone failure not a stop condition", r"is NOT a stop condition", True),
 ("still stops on AccessDenied etc", r"always on AccessDenied, NoCredentialsError or NoSuchBucket", True),
 ("py_compile ban", r"py_compile", True),
 ("report how each file got there", r"written locally \(and why the fetch was skipped or failed\)", True),
 ("embedded Script spec", r"^## Script spec", True),
 ("embedded Dashboard sync spec", r"^## Dashboard sync spec", True),
 ("embedded Watcher spec", r"^## Watcher spec", True),
 ("Rules section present", r"^## Rules \(every session, without being asked\)", True),
 ("rule 1 outputs/ convention", r"^1\. New data files with no existing panel convention", True),
 ("rule 8 legacy data", r"^8\. Data written before v1\.6\.0", True),
 ("rule 10 future date", r"^10\. If I ask where new data will land", True),
 ("rule 12 regenerate dashboard", r"^12\. If I regenerate or edit the dashboard HTML", True),
 ("rule 15 watcher honesty", r"^15\. Be honest about the limits of the watcher", True),
 ("rule 15 full wording", r"report the `--status` output and the `sweep ok` log lines you actually saw after a change", True),
 ("no stale 'Setup step 2a/2b/4a/4b/4(a2)/5(b)' refs in Rules", r"Setup step (2a|4a|4b|4\(a2\)|5\(b\))|spec \(2b\)", False),
 ("no 'may write' list ref", r"\"may write\" list", False),
 ("never invent RECOVERY_USER kept", r"never invent RECOVERY_USER", True),
 ("restored_code rule kept", r"restored_code/ folder", True),
 ("never delete S3 kept", r"delete anything in S3", True),
]
bad = 0
for label, rx, want in checks:
    got = bool(re.search(rx, T, re.M))
    ok = got == want
    bad += (not ok)
    print(("OK   " if ok else "FAIL ") + label)
print("failures:", bad)
for h in ("Script spec", "Dashboard sync spec", "Watcher spec"):
    i = T.find("## " + h)
    print(h, "section chars:", len(T[i:].split("\n## ")[0]))
