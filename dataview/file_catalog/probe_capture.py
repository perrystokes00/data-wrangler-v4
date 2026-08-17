r"""
probe_capture.py — call the REAL capture code on one file of each type, print what
it returns. Bypasses the pipeline and any stale running process, so it answers one
question cleanly: does the deployed code capture rows, or not?

Run from repo root with the venv active:
    python probe_capture.py

It writes to file_catalog.cat_* (like a real run). Do it on a freshly wiped DB,
then wipe again after. Adjust ODBC / the three paths if yours differ.
"""
import traceback
import urllib.parse as _up
from sqlalchemy import create_engine

ODBC = (r"DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost\SQLEXPRESS;"
        r"DATABASE=DataView_Demo;Trusted_Connection=yes;Encrypt=no")
ENG = create_engine("mssql+pyodbc:///?odbc_connect=" + _up.quote_plus(ODBC),
                    fast_executemany=True)

BASE = (r"C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler"
        r"\training\training_data")
PDF  = BASE + r"\pdf_and_word_documents\scout_ticket_ANADARKO_MIDL_001.pdf"
DOCX = BASE + r"\pdf_and_word_documents\Final_Well_Report_ANADARKO_MIDL_001.docx"
LAS  = BASE + r"\las_files\ANADARKO_MIDL_001_LOG_42329100010000.las"


def _hr(t):
    print("\n" + "=" * 8, t, "=" * 8)


# 1) do the extractors even produce rows? (no DB — pure parse)
_hr("EXTRACT ONLY (no DB)")
try:
    from dataview.import_data.pdf_document_loader import extract_file as _pdf
    r = _pdf(PDF)
    print("PDF  uwi=%r well=%d srvy_sta=%d casing=%d err=%r"
          % (r.get("uwi"), len(r.get("well", [])), len(r.get("srvy_sta", [])),
             len(r.get("casing", [])), r.get("error")))
except Exception:
    traceback.print_exc()
try:
    from dataview.import_data.docx_document_loader import extract_file as _docx
    r = _docx(DOCX)
    print("DOCX uwi=%r well=%d core=%d srvy_sta=%d err=%r"
          % (r.get("uwi"), len(r.get("well", [])), len(r.get("core", [])),
             len(r.get("srvy_sta", [])), r.get("error")))
except Exception:
    traceback.print_exc()

# 2) does capture_document write rows to cat_* ? (PDF + DOCX)
_hr("CAPTURE_DOCUMENT (writes cat_*)")
try:
    from dataview.file_catalog.catalog_doc_capture import capture_document
    print("PDF  ->", capture_document(ENG, "mssql", PDF, ".pdf", None, None, log=print))
    print("DOCX ->", capture_document(ENG, "mssql", DOCX, ".docx", None, None, log=print))
except Exception:
    traceback.print_exc()

# 3) does the LAS bulk path write rows? (bcp_capture)
_hr("RUN_BCP_CAPTURE (LAS)")
try:
    from dataview.file_catalog.bcp_capture import run_bcp_capture
    print("LAS  ->", run_bcp_capture(
        [{"FILE_PATH": LAS, "MATCHED_UWI": "42329100010000", "INVENTORY_ID": None}],
        conn_str=ODBC, workers=1, log=print))
except Exception:
    traceback.print_exc()

_hr("CAT_* COUNTS NOW")
try:
    from sqlalchemy import text as _t
    with ENG.connect() as c:
        for tbl in ("cat_well", "cat_well_log", "cat_well_log_curve",
                    "cat_well_core", "cat_well_dir_srvy_hdr", "cat_well_dir_srvy_sta"):
            try:
                n = c.execute(_t(f"SELECT COUNT(*) FROM file_catalog.{tbl}")).scalar()
                print(f"  file_catalog.{tbl}: {n}")
            except Exception as e:
                print(f"  file_catalog.{tbl}: (error: {str(e)[:80]})")
except Exception:
    traceback.print_exc()
