import re, hashlib

SRC = open('compare_prompts.py', encoding='utf-8').read()
m = re.search(r"PASTED = r'''(.*?)'''\n", SRC, re.S)
pasted = m.group(1)
full = open('prompt_v195_panel.md', encoding='utf-8').read().split('\n')

# specs = from "## Script spec" up to (not including) "## Rules"
start = next(i for i, l in enumerate(full) if l.startswith('## Script spec'))
end = next(i for i, l in enumerate(full) if l.startswith('## Rules'))
specs = '\n'.join(full[start:end]).rstrip() + '\n'

out = pasted
def sub(old, new):
    global out
    assert out.count(old) == 1, ('not found exactly once', old[:70], out.count(old))
    out = out.replace(old, new)

# A: source / clone
sub("Clone once into /tmp if network access is allowed. Report the resolved commit. Before copying anything, verify these five files in the clone by byte count and SHA-256 prefix:",
    "Clone once into /tmp ONLY if your runtime rules allow fetching code: git clone --depth 1 --branch v1.9.5 https://github.com/NamannBhan1910/fathom-recovery.git /tmp/fr_v195. Report the resolved commit. Before copying anything, verify these five files in the clone by byte count always, and by SHA-256 prefix when a hash tool exists (sha256sum, shasum -a 256 or openssl dgst -sha256; if none, report \"no hash tool, compared size and commit only\"):")

# B: replace stop-and-request with local-write fallback
sub("If the tag cannot be obtained or any verification fails, stop before installing or uploading; request the v1.9.5 release files from me. Do not substitute v1.9.4 or improvise the scripts. Never put credentials in files or chat.",
    "If the clone fails, fetching is not permitted, or any check differs, do NOT stop and do NOT keep retrying: delete nothing, discard a copied file only if you created it in this step, and write the needed files locally, in full, from the Script spec, Dashboard sync spec and Watcher spec at the end of this prompt, in the steps below that say so. Locally written files will not match the pins; report that plainly. Never substitute v1.9.4. Never put credentials in files or chat.")

# C: step 1
sub("If missing or outdated, install the verified v1.9.5 fathom_recovery.py.",
    "If missing or outdated, install the verified v1.9.5 fathom_recovery.py from the clone; if the clone is unavailable or fails a check, WRITE IT LOCALLY from the Script spec in full (Python 3, boto3 only, every marker line, __version__ = \"1.9.1\", all nine commands: setup sweep code-sweep selfcheck list list-code verify-code restore-code restore; skip nothing and simplify nothing). Then verify: the five marker greps each give 1; env PYTHONDONTWRITEBYTECODE=1 python3 -B scripts/fathom_recovery.py --help lists the nine commands; and a dry run of setup without --apply runs without error. Report that it was written locally (not fetched), with its size and line count.")

# D: step 3
sub("install missing recovery_hook.js and install_dashboard_hook.py from the verified release.",
    "install missing recovery_hook.js and install_dashboard_hook.py from the verified release, or, if the clone is unavailable or failed a check, write the missing ones locally from the Dashboard sync spec in full.")
sub("replace it with the verified v1.9.5 file and report old/new hashes.",
    "replace it with the verified v1.9.5 file (from the clone, else written locally from the Dashboard sync spec in full) and report old/new size and hashes; this is the only case where an existing script is overwritten.")

# E: step 4
sub("Install it and recovery_sync.py from the verified release if missing, even when this panel has no dashboard.",
    "Install it and recovery_sync.py from the verified release if missing, even when this panel has no dashboard; if the clone is unavailable or failed a check, write the missing ones locally (recovery_watch.py from the Watcher spec, recovery_sync.py from the Dashboard sync spec, each in full). Before starting it, run recovery_watch.py --help and recovery_watch.py --once --dry-run (uploads nothing).")

# F: limits
sub("On any failure, show the exact safe error and stop; do not silently retry or claim success.",
    "A failed or forbidden clone, or a pin mismatch, is NOT a stop condition: it triggers the local-write fallback above. Do not use py_compile or import to check Python syntax (they write __pycache__); use the greps, --help, dry runs and, for the JS, node --check. On any other failure, and always on AccessDenied, NoCredentialsError or NoSuchBucket, show the exact safe error and stop; do not silently retry or claim success.")

# G: report how each file got there
sub("VERIFY AND REPORT\nAfter upload,",
    "VERIFY AND REPORT\nFor each release file (backup script, the three dashboard scripts, and recovery_watch.py if step 4 ran) say whether it was already present, fetched from v1.9.5 (size + sha256 prefix, or \"no hash tool, compared size and commit only\"), or written locally (and why the fetch was skipped or failed), and state the clone commit if there was one. Remind me that __version__ 1.9.1 is expected for v1.9.5.\nAfter upload,")

# H: Rules section, verbatim except step cross-references remapped to the condensed 5-step numbering
rstart = next(i for i, l in enumerate(full) if l.startswith('## Rules'))
rules = '\n'.join(full[rstart:]).rstrip() + '\n'
def rsub(old, new):
    global rules
    assert rules.count(old) == 1, ('rules: not found exactly once', old[:70], rules.count(old))
    rules = rules.replace(old, new)
rsub("repeat Setup step 2 (fetch from tag v1.9.5 if allowed, otherwise write locally)",
     "repeat Setup step 1 (fetch from tag v1.9.5 if allowed, otherwise write locally from the Script spec)")
rsub("run the version check of Setup step 4(a2) first",
     "run the recovery_sync.py version check of Setup step 3 first")
rsub("(else Setup step 4a)", "(else Setup step 3)")
rsub("(else run the installer, Setup step 4b)", "(else run the installer, Setup step 3)")
rsub("start it again with Setup step 5(b)", "start it again with Setup step 4")
rsub('not on the "may write" list', 'not allowed under LIMITS')
rsub("fetch it only if allowed (Setup step 2a)", "fetch it only if allowed (Setup step 1)")
rsub("write it locally from the spec (2b)", "write it locally from the Script spec (Setup step 1)")

out = out.rstrip() + '\n\n' + specs.rstrip() + '\n\n' + rules
open('prompt_v195_condensed_fallback.md', 'w', encoding='utf-8').write(out)
print('written: %d bytes, %d lines, sha256 %s' % (len(out.encode()), out.count('\n'), hashlib.sha256(out.encode()).hexdigest()[:12]))
