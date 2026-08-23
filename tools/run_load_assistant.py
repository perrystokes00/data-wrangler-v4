"""Standalone launcher for the Load Assistant — run from the REPO ROOT:

    py -m streamlit run tools/run_load_assistant.py

No nav wiring needed; the page can be added to app_v3's NAVIGATION later
with the same two lines this file uses.
"""
import os
import sys


# The REPO ROOT, not tools/. Python puts the SCRIPT's own directory on
# sys.path[0], so `python tools/<name>.py` cannot import dataview without
# this. app_v4.py does the same insert; see tools/reconcile_orphans.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataview.import_data.page_load_assistant import run

run()
