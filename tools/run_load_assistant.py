"""Standalone launcher for the Load Assistant — run from the REPO ROOT:

    py -m streamlit run run_load_assistant.py

No nav wiring needed; the page can be added to app_v3's NAVIGATION later
with the same two lines this file uses.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataview.import_data.page_load_assistant import run

run()
