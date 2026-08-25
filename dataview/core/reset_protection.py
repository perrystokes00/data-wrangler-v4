"""What a reset must NOT destroy, and what it must reach — in one place.

THERE ARE TWO RESET PATHS AND THEY KEPT DISAGREEING. demo_reset clears loaded
data; clear_catalog resets the document pipeline. Both decide what to spare,
and each carried its own copy of the answer, so "protected" meant something
slightly different depending on which button was pressed. demo_reset's own
comment said they must protect the same names and they still did not:
clear_catalog named dv_global_file_catalog, demo_reset did not.

That is the same shape as MIRROR_TABLES vs LINEAGE, and it fails the same way
-- silently, in the direction of losing something. So the answer lives here
once and both import it. selftest pins that they agree, without a database.

PROTECTED is learned state: decisions a person made, one file at a time. A
reload regenerates the DATA and cannot regenerate these. REFERENCE is seeded
standards -- re-seedable from source, but losing them ARMS THE dv_r_* GUARD
and promote starts holding every coded value it cannot resolve, so they are
spared too, for a weaker reason.
"""

# Learned state. Never cleared by any path, and not gated on keep_reference --
# somebody turning reference seeds off wants the seeds gone, not the approved
# mappings.
PROTECTED = {
    "dv_column_map",          # every approved column mapping + fingerprint recall
    "dv_column_synonym",      # the column-level half of the same memory
    "dv_target_attribute",    # schema metadata the fit pre-flight reads
    "dv_global_file_catalog", # the loader's own ledger: which file made which rows
}

# Seeded standards the pipeline FKs into. dv_r_* is matched by PREFIX as well.
REFERENCE_EXACT = {"dv_country", "dv_province_state", "dv_county"}
REFERENCE_PREFIXES = ("dv_r_",)

# What a TARGETED (full=False) reset must reach. Extend these when a new data
# domain arrives -- and the test is not "is it new", it is "does it FK into
# something already cleared". dv_prod_*, dv_seis_* and dv_strat_interval all
# did, and all survived: a targeted reset either failed on dv_well's foreign
# keys or left 18,169 production rows pointing at wells that no longer existed.
CLEAR_PREFIXES = ("dv_well", "dv_seis", "dv_prod")
CLEAR_EXACT = {
    "dv_business_associate",
    "dv_field",
    "dv_land_tract",
    "dv_boundary",
    "dv_pipeline",
    "dv_strat_interval",
}


def is_protected(name):
    """Learned state — spared by every path."""
    return str(name or "").lower() in PROTECTED


def is_reference(name):
    """Seeded standards — spared unless a caller explicitly drops them."""
    low = str(name or "").lower()
    return low in REFERENCE_EXACT or low.startswith(REFERENCE_PREFIXES)


def should_clear(name):
    """Does a TARGETED reset reach this table? Protection wins, always."""
    low = str(name or "").lower()
    if low in PROTECTED:
        return False
    return low in CLEAR_EXACT or any(low.startswith(p) for p in CLEAR_PREFIXES)
