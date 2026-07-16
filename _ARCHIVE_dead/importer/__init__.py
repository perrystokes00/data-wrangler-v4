"""
DataView v3 — Universal Well Data Importer
==========================================
Layer 1: format_detective  — detects file type, encoding, structure
Layer 2: column_mapper     — ML + Claude column → dv_well field mapping

Imports are lazy — heavy deps (chardet, sentence-transformers) only load
when the relevant class/function is actually called.
"""

__all__ = [
    "detect",
    "DetectionResult",
    "ColumnMapper",
    "MappingResult",
    "ColumnMapping",
]


def detect(*args, **kwargs):
    from importer.format_detective import detect as _detect
    return _detect(*args, **kwargs)


def ColumnMapper(*args, **kwargs):
    from importer.column_mapper import ColumnMapper as _CM
    return _CM(*args, **kwargs)
