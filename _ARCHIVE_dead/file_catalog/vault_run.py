"""
vault_run.py — run the vault stage headless (apply=True) and report, so we can
tell WHY nothing was copied: empty plan, unreachable paths, or a UI-wiring issue.

    py vault_run.py                                   # defaults below
    py vault_run.py "PERRY\\SQLEXPRESS" DataView_Demo "C:\\Bulk\\Vault"
"""
import sys
from dataview.import_data import pipeline_run as pr
from dataview.file_catalog import worker_core as w
from sqlalchemy import text

SERVER     = sys.argv[1] if len(sys.argv) > 1 else r"PERRY\SQLEXPRESS"
DB         = sys.argv[2] if len(sys.argv) > 2 else "DataView_Demo"
VAULT_ROOT = sys.argv[3] if len(sys.argv) > 3 else r"C:\Bulk\Vault"

e = w.make_engine(SERVER, DB)

with e.connect() as c:
    total = c.execute(text(
        "SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG")).scalar()
    withpath = c.execute(text("""
        SELECT COUNT(*) FROM file_catalog.GLOBAL_FILE_CATALOG
        WHERE NULLIF(LTRIM(RTRIM(FILE_PATH)),'') IS NOT NULL""")).scalar()
    print(f"catalog rows: {total} · with FILE_PATH: {withpath}")

print(f"\nrunning vault stage  root={VAULT_ROOT}  apply=True\n" + "-" * 60)
pr._ensure_catalog_cols(e)
res = pr._stage_vault(e, "file_catalog", VAULT_ROOT, "copy", True, print)
print("-" * 60)
print("result:", res)

with e.connect() as c:
    vaulted = c.execute(text(
        "SELECT COUNT(VAULT_PATH) FROM file_catalog.GLOBAL_FILE_CATALOG")).scalar()
    print(f"\nVAULT_PATH now set on: {vaulted} row(s)")

print("\ndone.")
