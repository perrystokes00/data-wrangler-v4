"""
column_mapper.py
================
Layer 2 of the Universal Well Data Importer.

Maps source file columns → dataview.dv_well target fields using:
  1. Fingerprint cache  — exact match from a previously confirmed mapping
  2. sentence-transformers — semantic similarity (column name + sample values)
  3. Claude API          — resolves low-confidence / ambiguous mappings

Confidence thresholds:
  >= AUTO_THRESHOLD   → auto-mapped, no review needed
  >= FLAG_THRESHOLD   → mapped but flagged for user review
  <  FLAG_THRESHOLD   → unmapped, user must assign manually

Usage:
    from importer.column_mapper import ColumnMapper

    mapper = ColumnMapper(schema_path="schema_registry/dataview_schema_domain.json")
    result = mapper.map(detection_result)

    for m in result.mappings:
        print(m)        # ColumnMapping dataclass

    mapper.confirm(result)   # saves fingerprint to cache
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ── Thresholds ────────────────────────────────────────────────────────
AUTO_THRESHOLD = 0.82   # auto-map, no review
FLAG_THRESHOLD = 0.55   # map but flag for review
# Below FLAG_THRESHOLD → unmapped

CACHE_PATH = Path(__file__).parent / "fingerprint_cache.json"

# ── Target field definitions loaded from schema ───────────────────────
# These descriptions are used to build embeddings for dv_well columns.
# Supplement with domain knowledge so the ML has rich signal.
DV_WELL_FIELD_HINTS: dict[str, str] = {
    "uwi":              "unique well identifier UWI API number well id",
    "well_name":        "well name lease name well label title",
    "well_type":        "well type oil gas water injection dry hole",
    "well_status":      "well status active plugged abandoned cancelled drilling",
    "province_state":   "state province region territory",
    "country":          "country nation",
    "county":           "county parish district borough",
    "surface_latitude":  "latitude lat surface location coordinate degrees north",
    "surface_longitude": "longitude lon long surface location coordinate degrees west",
    "final_td":         "total depth TD KB final depth feet meters",
    "depth_datum":      "depth datum KB rotary table ground level",
    "spud_date":        "spud date drilling start date commenced",
    "completion_date":  "completion date completed finished",
    "api_num":          "API number american petroleum institute well number",
    "operator_name":    "operator company name working interest owner",
    "field_name":       "field name oil field gas field producing area",
    "formation":        "formation producing zone pay zone reservoir",
    "active_ind":       "active indicator flag Y N yes no",
    "source":           "data source origin provider",
    "remark":           "remark comment note description",
    "row_created_by":   "created by user loader system",
    "row_created_date": "created date insert date load date",
    "row_changed_by":   "changed by updated by modified by",
    "row_changed_date": "changed date updated date modified date",
}


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class ColumnMapping:
    source_col:   str
    target_field: str | None   # None = unmapped
    confidence:   float        # 0.0 – 1.0
    method:       str          # 'cache', 'ml', 'claude', 'manual'
    auto:         bool         # True = no review needed
    flagged:      bool         # True = needs review
    sample_values: list[str] = field(default_factory=list)
    note:         str = ""

    def __str__(self) -> str:
        conf  = f"{self.confidence:.0%}"
        flag  = "⚠" if self.flagged else ("✓" if self.auto else "✗")
        tgt   = self.target_field or "UNMAPPED"
        return (f"  {flag} {self.source_col:30} → {tgt:25} "
                f"{conf:5}  [{self.method}]")


@dataclass
class MappingResult:
    file_path:    str
    file_type:    str
    mappings:     list[ColumnMapping]
    fingerprint:  str   # SHA256 of source column set
    confirmed:    bool = False

    @property
    def auto_mapped(self) -> list[ColumnMapping]:
        return [m for m in self.mappings if m.auto]

    @property
    def flagged(self) -> list[ColumnMapping]:
        return [m for m in self.mappings if m.flagged]

    @property
    def unmapped(self) -> list[ColumnMapping]:
        return [m for m in self.mappings if not m.target_field]

    def summary(self) -> str:
        lines = [
            f"File       : {self.file_path}",
            f"Fingerprint: {self.fingerprint[:16]}...",
            f"Auto-mapped: {len(self.auto_mapped)} / {len(self.mappings)}",
            f"Flagged    : {len(self.flagged)}",
            f"Unmapped   : {len(self.unmapped)}",
            "",
        ]
        for m in self.mappings:
            lines.append(str(m))
        return "\n".join(lines)


# ── Main class ────────────────────────────────────────────────────────

class ColumnMapper:
    """
    Maps source columns to dv_well target fields.

    Parameters
    ----------
    schema_path : str
        Path to dataview_schema_domain.json
    cache_path : str | Path
        Path to fingerprint cache JSON (default: importer/fingerprint_cache.json)
    use_claude : bool
        Whether to call Claude API for low-confidence mappings (default: True)
    anthropic_api_key : str | None
        API key — reads ANTHROPIC_API_KEY env var if not supplied
    """

    def __init__(
        self,
        schema_path: str = "schema_registry/dataview_schema_domain.json",
        cache_path: str | Path = CACHE_PATH,
        use_claude: bool = True,
        anthropic_api_key: str | None = None,
    ):
        self.schema_path = Path(schema_path)
        self.cache_path  = Path(cache_path)
        self.use_claude  = use_claude
        self.api_key     = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model      = None   # lazy-loaded sentence-transformer
        self._target_embeddings: dict[str, np.ndarray] = {}
        self._cache: dict[str, dict] = self._load_cache()
        self._field_hints = self._build_field_hints()

    # ── Public ────────────────────────────────────────────────────────

    def map(self, detection_result: Any) -> MappingResult:
        """
        Map columns from a DetectionResult to dv_well fields.
        Tries cache → ML → Claude in that order.
        """
        from importer.format_detective import DetectionResult
        dr: DetectionResult = detection_result

        source_cols   = dr.raw_columns
        sample_rows   = dr.sample_rows
        fingerprint   = self._fingerprint(source_cols)

        # Layer 1: cache hit?
        if fingerprint in self._cache:
            return self._map_from_cache(dr, fingerprint)

        # Layer 2: ML mapping
        mappings = self._map_ml(source_cols, sample_rows)

        # Layer 3: Claude for anything still below threshold
        if self.use_claude and self.api_key:
            mappings = self._map_claude(mappings, sample_rows)

        return MappingResult(
            file_path=dr.file_path,
            file_type=dr.file_type,
            mappings=mappings,
            fingerprint=fingerprint,
        )

    def confirm(self, result: MappingResult) -> None:
        """
        Save a confirmed mapping to the fingerprint cache.
        Call this after the user has reviewed and approved mappings in the UI.
        """
        entry = {
            "file_type": result.file_type,
            "mappings": [
                {"source_col": m.source_col, "target_field": m.target_field}
                for m in result.mappings
            ],
        }
        self._cache[result.fingerprint] = entry
        self._save_cache()
        result.confirmed = True
        print(f"Fingerprint saved: {result.fingerprint[:16]}...")

    def known_formats(self) -> list[dict]:
        """Return list of all cached format fingerprints."""
        return [
            {"fingerprint": fp[:16] + "...", **v}
            for fp, v in self._cache.items()
        ]

    # ── Fingerprint ───────────────────────────────────────────────────

    def _fingerprint(self, columns: list[str]) -> str:
        """SHA256 of sorted, normalised column names."""
        key = "|".join(sorted(c.lower().strip() for c in columns))
        return hashlib.sha256(key.encode()).hexdigest()

    # ── Cache ─────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text())
            except Exception:
                pass
        return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=2))

    def _map_from_cache(self, dr: Any, fingerprint: str) -> MappingResult:
        entry    = self._cache[fingerprint]
        mappings = []
        for m in entry["mappings"]:
            mappings.append(ColumnMapping(
                source_col=m["source_col"],
                target_field=m["target_field"],
                confidence=1.0,
                method="cache",
                auto=True,
                flagged=False,
            ))
        return MappingResult(
            file_path=dr.file_path,
            file_type=dr.file_type,
            mappings=mappings,
            fingerprint=fingerprint,
            confirmed=True,
        )

    # ── ML mapping ────────────────────────────────────────────────────

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            print("Loading sentence-transformer model (first run only)...")
            self._model = SentenceTransformer("all-MiniLM-L6-v2")
            # Pre-embed all target fields
            texts = list(self._field_hints.values())
            keys  = list(self._field_hints.keys())
            vecs  = self._model.encode(texts, normalize_embeddings=True)
            self._target_embeddings = dict(zip(keys, vecs))
        return self._model

    def _build_field_hints(self) -> dict[str, str]:
        """
        Merge DV_WELL_FIELD_HINTS with any extra fields found in the schema JSON.
        Schema fields not in hints get a hint built from their field name.
        """
        hints = dict(DV_WELL_FIELD_HINTS)
        if self.schema_path.exists():
            try:
                schema = json.loads(self.schema_path.read_text())
                # Support both list-of-tables and flat field list structures
                tables = schema if isinstance(schema, list) else schema.get("tables", [])
                for tbl in tables:
                    if isinstance(tbl, dict):
                        tbl_name = tbl.get("table_name", "")
                        if "dv_well" not in tbl_name.lower():
                            continue
                        for col in tbl.get("columns", []):
                            fname = col.get("column_name", "")
                            if fname and fname not in hints:
                                # Build hint from column name + description
                                desc = col.get("description", "")
                                hints[fname] = f"{fname.replace('_',' ')} {desc}".strip()
            except Exception as e:
                print(f"  Schema load warning: {e}")
        return hints

    def _source_text(self, col: str, samples: list[str]) -> str:
        """Combine column name + sample values into a single embedding string."""
        clean_col = col.replace("_", " ").replace("-", " ").lower().strip()
        sample_str = " ".join(str(s) for s in samples[:5] if s and str(s).strip())
        return f"{clean_col} {sample_str}".strip()

    def _map_ml(
        self,
        source_cols: list[str],
        sample_rows: list[dict],
    ) -> list[ColumnMapping]:
        model = self._get_model()
        mappings = []

        for col in source_cols:
            samples = [str(row.get(col, "")) for row in sample_rows if row.get(col)]
            src_text = self._source_text(col, samples)
            src_vec  = model.encode(src_text, normalize_embeddings=True)

            # Cosine similarity against all target fields
            best_field = None
            best_score = 0.0
            for tgt_field, tgt_vec in self._target_embeddings.items():
                score = float(np.dot(src_vec, tgt_vec))
                if score > best_score:
                    best_score = score
                    best_field = tgt_field

            auto    = best_score >= AUTO_THRESHOLD
            flagged = FLAG_THRESHOLD <= best_score < AUTO_THRESHOLD
            if best_score < FLAG_THRESHOLD:
                best_field = None

            mappings.append(ColumnMapping(
                source_col=col,
                target_field=best_field,
                confidence=best_score,
                method="ml",
                auto=auto,
                flagged=flagged,
                sample_values=samples[:5],
            ))

        return mappings

    # ── Claude resolution ─────────────────────────────────────────────

    def _map_claude(
        self,
        mappings: list[ColumnMapping],
        sample_rows: list[dict],
    ) -> list[ColumnMapping]:
        """
        Send unmapped + flagged columns to Claude API for resolution.
        Returns updated mappings list.
        """
        candidates = [m for m in mappings if not m.auto]
        if not candidates:
            return mappings

        target_fields = list(self._field_hints.keys())
        col_descriptions = "\n".join(
            f"  {f}: {h}" for f, h in self._field_hints.items()
        )

        cols_block = []
        for m in candidates:
            samples_str = ", ".join(m.sample_values[:3]) if m.sample_values else "no samples"
            cols_block.append(
                f'  - source_col: "{m.source_col}" | samples: [{samples_str}] | '
                f'ml_suggestion: "{m.target_field or "none"}" | ml_confidence: {m.confidence:.2f}'
            )

        prompt = f"""You are a petroleum data expert mapping source file columns to the dataview.dv_well schema.

Target fields and their meanings:
{col_descriptions}

Columns to map (with sample values and ML suggestions):
{chr(10).join(cols_block)}

Return ONLY a JSON array. Each element must have:
  "source_col": exact source column name
  "target_field": best matching target field name, or null if no good match
  "confidence": float 0.0-1.0
  "note": brief reason

No preamble, no markdown, just the JSON array."""

        try:
            import urllib.request
            body = json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())

            text = data["content"][0]["text"].strip()
            # Strip markdown fences if present
            text = re.sub(r"^```json\s*|```\s*$", "", text, flags=re.MULTILINE).strip()
            suggestions = json.loads(text)

            # Apply Claude suggestions back to mappings
            sug_map = {s["source_col"]: s for s in suggestions}
            updated = []
            for m in mappings:
                if m.source_col in sug_map and not m.auto:
                    s = sug_map[m.source_col]
                    conf   = float(s.get("confidence", 0))
                    tgt    = s.get("target_field")
                    auto   = conf >= AUTO_THRESHOLD
                    flagged = FLAG_THRESHOLD <= conf < AUTO_THRESHOLD
                    if conf < FLAG_THRESHOLD:
                        tgt = None
                    updated.append(ColumnMapping(
                        source_col=m.source_col,
                        target_field=tgt,
                        confidence=conf,
                        method="claude",
                        auto=auto,
                        flagged=flagged,
                        sample_values=m.sample_values,
                        note=s.get("note", ""),
                    ))
                else:
                    updated.append(m)
            return updated

        except Exception as e:
            print(f"  Claude API warning: {e} — using ML mappings only")
            return mappings


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from importer.format_detective import detect

    if len(sys.argv) < 2:
        print("Usage: python -m importer.column_mapper <file_path>")
        sys.exit(1)

    dr     = detect(sys.argv[1])
    mapper = ColumnMapper()
    result = mapper.map(dr)
    print(result.summary())
