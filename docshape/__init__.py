"""
docshape — recognise tables in documents by what their columns ARE.

    from docshape import Recogniser, load_pack

    r = Recogniser(load_pack("petroleum"))
    result = r.read_table(header, rows)
    result["shape"]    # 'directional_survey'
    result["columns"]  # {'md': 'Meas Depth (ft)', 'tvd': 'True Vert Dep (ft)', …}

WHY IT EXISTS
-------------
Extractors written against one vendor's layout fail on the next one, and
enumerating layouts is a losing race. Describing what a table IS — a frac
table has a stage number, a top depth and a proppant mass — survives renaming,
reordering, extra columns and missing optional ones.

THREE LAYERS
------------
    engine/    matching, scoring, coercion. Knows no domain.
    packs/     vocabularies. petroleum, legal, … — data, not code.
    readers/   getting tables out of PDF / DOCX / XLSX / LAS / SEG-Y.
    backends/  where captured rows land. DuckDB, SQL Server.

Adding an industry is a file in packs/. Adding a database is a file in
backends/. Neither touches the engine.
"""
from docshape.engine.recognise import Recogniser, to_number, INTERNAL_KEYS
from docshape.packs import (load as load_pack,
                            validate as validate_pack,
                            available as list_packs)

__all__ = ["Recogniser", "to_number", "INTERNAL_KEYS",
           "load_pack", "validate_pack", "list_packs"]
__version__ = "0.1.0"
