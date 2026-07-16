r"""promote_timing.py — pull the promote/enrich timing from the newest run log.

    py promote_timing.py                     # default C:\Bulk\reports
    py promote_timing.py "C:\Bulk\reports"
"""
import sys, os, glob, re

REPORTS = sys.argv[1] if len(sys.argv) > 1 else r"C:\Bulk\reports"
logs = sorted(glob.glob(os.path.join(REPORTS, "pipeline_*.log")),
              key=os.path.getmtime, reverse=True)
if not logs:
    print(f"no pipeline_*.log in {REPORTS}"); sys.exit(0)
txt = open(logs[0], "r", encoding="utf-8", errors="replace").read()
print(f"reading: {logs[0]}\n")

# the run_promote vs gold-enrich vs commit split (added to _stage_promote)
mp = re.search(r"\[promote-parts\].*", txt)
if mp:
    print(mp.group(0).strip())
else:
    print(">> no '[promote-parts]' line — this log predates the split-timing "
          "pipeline_run.py (deploy it and re-run to see run_promote vs enrich "
          "vs commit).")
print()

# the per-step promote timing line (added to run_promote)
m = re.search(r"-- promote timing \(slowest first\):.*", txt)
if m:
    print(m.group(0))
else:
    print("no 'promote timing' line found — the instrumented promote_catalog.py "
          "isn't deployed yet. Deploy it and re-run promote.")

# enrich pass times (already logged by the enrich stage)
print("\nenrich passes:")
for line in txt.splitlines():
    if re.search(r"\[TIME\s*\].*(resolve missing UWI|fill blank|curate UWI14|"
                 r"survey from file name|reflect schema|total)", line):
        print("  " + line.strip())
for line in txt.splitlines():
    if re.match(r"\s*-- enrich", line):
        print("  " + line.strip())

# the promote mirror table (eligible/moved) so we see row volumes vs time
print("\npromote mirrors (eligible · moved):")
for line in txt.splitlines():
    if re.match(r"\s*(cat_|dv_)\w+\s+\d", line) or "TOTAL" in line[:40]:
        print("  " + line.strip()[:70])
