"""
WranglerView configuration.
Connection strings, paths, and defaults.
"""
import os

# Load .env so SNOWFLAKE_* (including the private-key path) is available to
# every module that imports config. Best-effort: ignore if python-dotenv
# isn't installed or there's no .env file.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ── Database connections ──────────────────────────────────────────

# SQL Server (prototype / local development)
SQLSERVER_CONN = (
    "mssql+pyodbc://127.0.0.1\\SQLEXPRESS/WranglerView"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
    "&TrustServerCertificate=yes"
)

# ── Snowflake (production federation) ─────────────────────────────
# Snowflake now blocks password-only logins, so the connection uses
# KEY-PAIR authentication. Build the engine via get_snowflake_engine();
# do NOT build a username/password URL anymore.
#
# Required in .env:
#   SNOWFLAKE_ACCOUNT=YDWXNCV-VL88062
#   SNOWFLAKE_USER=PMSTOKES00
#   SNOWFLAKE_PRIVATE_KEY_PATH=C:\path\to\rsa_key.p8
#   SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=        # blank if key made with -nocrypt
#
# Optional overrides (defaults below preserve the previous connection):
SNOWFLAKE_DATABASE  = os.environ.get("SNOWFLAKE_DATABASE",  "WELL_FEDERATION")
SNOWFLAKE_SCHEMA    = os.environ.get("SNOWFLAKE_SCHEMA",    "CURATED")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "WV_WH")
SNOWFLAKE_ROLE      = os.environ.get("SNOWFLAKE_ROLE",      "WV_ROLE")

# DEPRECATED: kept only so old `from config import SNOWFLAKE_CONN` imports
# don't break. It no longer works (password auth is blocked). Anything still
# using it should switch to get_snowflake_engine().
SNOWFLAKE_CONN = None

_SF_ENGINE = None  # module-level singleton — survives Streamlit reruns
_PK_DER = None     # cached private-key bytes


def get_snowflake_private_key():
    """DER/PKCS8 private-key bytes for key-pair auth (cached).

    The single source every raw connect() call should use:
        snowflake.connector.connect(..., private_key=config.get_snowflake_private_key())
    instead of password=os.environ.get("SNOWFLAKE_PASSWORD", "").
    """
    global _PK_DER
    if _PK_DER is None:
        _PK_DER = _load_private_key_der()
    return _PK_DER


def _load_private_key_der() -> bytes:
    """Read the PEM private key and return DER/PKCS8 bytes for the driver."""
    from cryptography.hazmat.primitives import serialization

    path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    if not path:
        raise RuntimeError("SNOWFLAKE_PRIVATE_KEY_PATH is not set in your .env")
    if not os.path.exists(path):
        raise RuntimeError(f"Snowflake private key not found at: {path}")

    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
    with open(path, "rb") as fh:
        key = serialization.load_pem_private_key(
            fh.read(),
            password=passphrase.encode() if passphrase else None,
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def get_snowflake_engine():
    """Key-pair-authenticated Snowflake engine (cached singleton).
    Use this everywhere in place of create_engine(SNOWFLAKE_CONN).
    Fails fast: config/auth problems raise here, not on first query."""
    global _SF_ENGINE
    if _SF_ENGINE is not None:
        return _SF_ENGINE

    from sqlalchemy import create_engine
    from snowflake.sqlalchemy import URL

    account = os.environ.get("SNOWFLAKE_ACCOUNT", "")
    user    = os.environ.get("SNOWFLAKE_USER", "")
    if not account or not user:
        raise RuntimeError(
            "SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER must be set in your .env")

    pkb = _load_private_key_der()
    engine = create_engine(
        URL(
            account=account,
            user=user,
            database=SNOWFLAKE_DATABASE,
            schema=SNOWFLAKE_SCHEMA,
            warehouse=SNOWFLAKE_WAREHOUSE,
            role=SNOWFLAKE_ROLE,
        ),
        connect_args={"private_key": pkb},
        pool_pre_ping=True,
    )
    with engine.connect() as conn:        # prove it now
        conn.exec_driver_sql("SELECT 1")
    _SF_ENGINE = engine
    return _SF_ENGINE


def get_snowflake_connection(**overrides):
    """Raw snowflake.connector connection via KEY-PAIR auth, for code that
    uses cursors rather than a SQLAlchemy engine (e.g. the federation loader
    and scout tickets).

    Defaults mirror the app's previous raw connections (role ACCOUNTADMIN,
    WELL_FEDERATION / WV_WH). Pass overrides such as role=... or
    autocommit=True to change or add connect() parameters.
    """
    import snowflake.connector

    params = dict(
        account=os.environ.get("SNOWFLAKE_ACCOUNT", "YDWXNCV-VL88062"),
        user=os.environ.get("SNOWFLAKE_USER", "PMSTOKES00"),
        private_key=_load_private_key_der(),
        database=SNOWFLAKE_DATABASE,
        warehouse=SNOWFLAKE_WAREHOUSE,
        role="ACCOUNTADMIN",
    )
    params.update(overrides)
    return snowflake.connector.connect(**params)


# Active connection — switch between SQL Server and Snowflake
DB_DIALECT = os.environ.get("WV_DIALECT", "sqlserver")  # "sqlserver" or "snowflake"

# ── Paths ─────────────────────────────────────────────────────────
GEOJSON_PATH = "wells.geojson"


# ── Scratch: where bulk-load staging files are written ────────────
# ONE ROOT, BECAUSE THERE WERE FOUR. C:\bcp_tmp (bcp_capture), C:\Bulk
# (las_loader, file_inventory, enrich_from_dbf) and %LOCALAPPDATA%\Temp\
# dw_wells_bcp (the map) were each hardcoded separately, so nothing could be
# redirected without finding all of them -- and a distribution has to be able
# to.
#
# LOCALAPPDATA, NOT THE REPO. Two reasons, and the second is the one that
# bites: the repo lives inside OneDrive, which does not read .gitignore and
# would sync every staging CSV mid-load; and a packaged install may land
# somewhere the app cannot write at all. The map already resolved this the
# right way and the rest now follows it.
#
# THESE FILES ARE READ BY SQL SERVER, NOT JUST WRITTEN BY US. Every one of
# them ends up in a BULK INSERT ... FROM '<path>', which the SERVER opens --
# so the path has to be readable by the service account, and that is not a
# given for a user-profile directory. Checked 4 Sep rather than assumed:
# SQL Server (SQLEXPRESS) runs as NT Service\MSSQL$SQLEXPRESS here and reads
# LOCALAPPDATA, the repo and C:\bcp_tmp equally.
#
# It will NOT hold everywhere. A hardened service account, or a SQL Server on
# another host, breaks every local path -- which is exactly what DW_SCRATCH
# is for. Point it at a share both sides can see.
DW_SCRATCH = os.environ.get("DW_SCRATCH", "")


# ── The customer's roots: their data, and what they generate ──────
# TWO DIFFERENT QUESTIONS. Scratch above is OURS -- staging CSVs nobody asked
# for, safe to put in a user-profile directory and safe to delete. The vault
# and the reports are THEIRS: curated documents and output they will go
# looking for, so they belong where the customer points, not in AppData where
# a profile reset takes them.
#
# DEFAULT IS C:\Bulk\Vault, NOT C:\Bulk. Checked on disk 4 Sep rather than
# taken from the code, because the code disagreed with itself: three places
# said C:\Bulk\Vault (collect_final_documents, page_monitor, page_file_catalog)
# and two said C:\Bulk (dv_catalog_adapter, page_dv_catalog). The disk is
# unambiguous -- C:\Bulk\Vault\curated exists and holds files, while
# C:\Bulk\raw, C:\Bulk\curated and C:\Bulk\enriched do not exist at all. So
# ensure_vault() at its old default would have created the vault one level
# too high, scattering raw/curated/enriched across a staging area that
# already holds twenty other things.
DW_VAULT = os.environ.get("DW_VAULT", r"C:\Bulk\Vault")

# REPORTS ARE OUTSIDE THE VAULT, and that is not a mistake to tidy up: 1,801
# files live in C:\Bulk\reports today and CLAUDE.md documents that location.
# Made configurable without moving anything -- redirecting it is a decision
# with a migration attached, not a default to change quietly.
DW_REPORTS = os.environ.get("DW_REPORTS", r"C:\Bulk\reports")


def vault_dir(sub: str = "") -> str:
    """The customer's vault root (created), optionally a subfolder."""
    path = os.path.join(DW_VAULT, sub) if sub else DW_VAULT
    os.makedirs(path, exist_ok=True)
    return path


def reports_dir(sub: str = "") -> str:
    """Where customer-facing reports are written (created)."""
    path = os.path.join(DW_REPORTS, sub) if sub else DW_REPORTS
    os.makedirs(path, exist_ok=True)
    return path


def scratch_dir(sub: str = "") -> str:
    """Absolute path to the scratch root (created), optionally a subfolder.

    Callers should not build their own: a second literal is how the four
    above happened.
    """
    root = DW_SCRATCH or os.path.join(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("TMPDIR")
        or os.path.expanduser("~"),
        "DataWrangler", "scratch")
    path = os.path.join(root, sub) if sub else root
    os.makedirs(path, exist_ok=True)
    return path

# ── Mapbox ────────────────────────────────────────────────────────
MAPBOX_TOKEN = os.environ.get("MAPBOX_API_KEY", "")
