"""
page_file_workbench.py  —  Shared File Preview Workbench
=========================================================
Used by both the Standard Formats Catalog (catalogers)
and Browse & Export (end users).

Provides:
  render_workbench(file_path, fmt, key)
    - Raw / decoded header viewer
    - Curve / trace plot
    - UWI or survey name display / edit

All viewers read only the minimum data needed:
  LAS   → pre-~A header text + optional curve plot
  DLIS  → decoded origins/channels/parameters + optional curve plot
  LIS   → decoded wellsite data / curve specs + optional curve plot
  SEG-Y → EBCDIC text + binary header fields + optional wiggle traces
  P190  → H-records + shot point table + map plot
"""

import streamlit as st
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Extension → format map
# ─────────────────────────────────────────────────────────────────────────────

EXT_FORMAT = {
    ".las":  "LAS",
    ".dlis": "DLIS", ".dlf": "DLIS", ".dis": "DLIS",
    ".lis":  "LIS",
    ".segy": "SEGY", ".sgy": "SEGY", ".seg": "SEGY",
    ".p190": "P190", ".p90": "P190", ".p1": "P190",
}


def detect_format(file_path: str) -> str:
    """Detect format from file extension."""
    ext = Path(file_path).suffix.lower()
    return EXT_FORMAT.get(ext, "UNKNOWN")


# ─────────────────────────────────────────────────────────────────────────────
# Raw / decoded header viewers
# ─────────────────────────────────────────────────────────────────────────────

def _view_las_header(file_path: str):
    """Raw LAS header — everything before ~A."""
    try:
        with open(file_path, "r", errors="replace") as f:
            text = f.read()
        a_idx = text.upper().find("\n~A")
        header = text[:a_idx].strip() if a_idx > 0 else text
        st.code(header, language=None)
    except Exception as e:
        st.error(f"LAS header: {e}")


def _view_dlis_header(file_path: str):
    """Decoded DLIS origins, channels and parameters."""
    try:
        import warnings
        from dlisio import dlis
        lines = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with dlis.load(file_path) as lfs:
                for lf_idx, lf in enumerate(lfs):
                    lines.append(f"=== Logical File {lf_idx+1} ===")
                    for o in lf.origins:
                        lines.append(f"Origin: {o.name}")
                        for attr in ("well_name","field_name","company","country",
                                     "creation_time","producer_name","run_nr"):
                            v = getattr(o, attr, None)
                            if v:
                                lines.append(f"  {attr}: {v}")
                    ch_list = list(lf.channels)
                    lines.append(f"\nChannels ({len(ch_list)}):")
                    for ch in ch_list:
                        lines.append(
                            f"  {ch.name:<20s} unit={ch.units or '—':<8s} "
                            f"dim={ch.dimension}"
                        )
                    params = list(lf.parameters)
                    if params:
                        lines.append(f"\nParameters ({len(params)}):")
                        for p in params[:50]:
                            lines.append(f"  {p.name:<20s} = {p.values}")
        st.code("\n".join(lines), language=None)
    except Exception as e:
        st.error(f"DLIS decode: {e}")


def _view_lis_header(file_path: str):
    """Decoded LIS wellsite data and curve specs. Falls back to SEG-Y."""
    try:
        import warnings
        from dlisio import lis
        lines = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with lis.load(file_path) as lfs:
                if not lfs:
                    raise ValueError("No logical files")
                lf = lfs[0]
                try:
                    lines.append("=== Wellsite Data ===")
                    for rec in lf.wellsite_data():
                        for c in rec.components():
                            mnem = getattr(c, "mnemonic", "")
                            val  = getattr(c, "component", "")
                            if mnem:
                                lines.append(f"  {str(mnem):<12s} = {val}")
                except Exception:
                    lines.append("  (no wellsite data)")
                try:
                    specs = lf.data_format_specs()
                    if specs:
                        lines.append(f"\n=== Curves ({len(specs)} spec(s)) ===")
                        for spec in specs:
                            for ch in spec.entries:
                                mnem  = str(getattr(ch, "mnemonic", "?"))
                                units = str(getattr(ch, "units", "—"))
                                lines.append(f"  {mnem:<12s} unit={units}")
                except Exception:
                    lines.append("  (could not read curve specs)")
        if lines:
            st.code("\n".join(lines), language=None)
        else:
            raise ValueError("No readable content")
    except Exception as lis_err:
        st.caption(f"⚠️ LIS decode failed ({lis_err}) — trying SEG-Y")
        _view_segy_header(file_path)


def _view_segy_header(file_path: str):
    """EBCDIC text header + binary header fields — auto-detects encoding."""
    def _decode_text_hdr(raw: bytes) -> tuple[str, str]:
        """Try EBCDIC then ASCII. Return (decoded_text, encoding_used)."""
        # Heuristic: valid EBCDIC printable chars are mostly in range 0x40-0xFF
        # ASCII text will have many bytes < 0x40 that map to garbage in EBCDIC
        ebcdic = raw.decode("cp037", errors="replace")
        printable_ebcdic = sum(1 for c in ebcdic if c.isprintable() or c in "\n\r ")
        ascii_ = raw.decode("ascii", errors="replace")
        printable_ascii  = sum(1 for c in ascii_  if c.isprintable() or c in "\n\r ")
        if printable_ascii > printable_ebcdic:
            return ascii_, "ASCII"
        return ebcdic, "EBCDIC"

    try:
        import segyio
        with segyio.open(file_path, ignore_geometry=True, strict=False) as f:
            raw_hdr = f.text[0]
            bin_hdr = {str(k): int(v) for k,v in dict(f.bin).items() if int(v) != 0}

        text, enc = _decode_text_hdr(raw_hdr)
        lines = [text[i:i+80].rstrip() for i in range(0, len(text), 80)]
        lines = [l for l in lines if l.strip()]
        st.caption(f"Encoding: {enc}")
        st.code("\n".join(lines), language=None)

    except Exception as segyio_err:
        try:
            with open(file_path, "rb") as f:
                raw = f.read(3200)
            text, enc = _decode_text_hdr(raw)
            lines = [text[i:i+80].rstrip() for i in range(0, len(text), 80)]
            lines = [l for l in lines if l.strip()]
            st.caption(f"⚠️ segyio failed — raw decode ({enc})")
            st.code("\n".join(lines) if lines else "(empty header)", language=None)
        except Exception as e2:
            st.error(f"SEG-Y header decode failed: {e2}")


def _view_p190_header(file_path: str):
    """P190 H-records + first 500 lines."""
    try:
        with open(file_path, "r", errors="replace") as f:
            lines = f.readlines()
        h_recs = [l.rstrip() for l in lines if l.startswith("H")]
        body   = [l.rstrip() for l in lines[:500]]
        if h_recs:
            st.markdown("**Header (H records):**")
            st.code("\n".join(h_recs), language=None)
            with st.expander("All records (first 500 lines)"):
                st.code("\n".join(body), language=None)
        else:
            st.code("\n".join(body), language=None)
    except Exception as e:
        st.error(f"P190 viewer: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Curve / trace plotters
# ─────────────────────────────────────────────────────────────────────────────

def _plot_las_curves(file_path: str, key: str):
    """Interactive multi-track plotly curve plot for LAS files."""
    try:
        import lasio
        from dataview.file_catalog.las_reader import read_las
        import numpy as np
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        las = read_las(file_path, ignore_header_errors=True)
        all_curves = [c.mnemonic for c in las.curves
                      if c.mnemonic.upper() not in ("DEPT","DEPTH","MD","TVD")]
        if not all_curves:
            st.info("No curves to plot.")
            return

        depth_col = next(
            (c.mnemonic for c in las.curves
             if c.mnemonic.upper() in ("DEPT","DEPTH","MD","TVD")),
            las.curves[0].mnemonic
        )
        depth = las[depth_col]

        sel = st.multiselect(
            "Curves to plot (each gets its own track)",
            all_curves,
            default=all_curves[:min(4, len(all_curves))],
            key=f"las_curves_{key}"
        )
        if not sel:
            return

        n = len(sel)
        fig = make_subplots(
            rows=1, cols=n,
            shared_yaxes=True,
            subplot_titles=sel,
            horizontal_spacing=0.02,
        )

        for i, curve in enumerate(sel, 1):
            try:
                vals = las[curve]
                # Replace null/fill values with NaN
                null_val = getattr(las.well.get("NULL", None), "value", -999.25)
                vals = np.where(np.isclose(vals, null_val, atol=0.1), np.nan, vals)
                fig.add_trace(
                    go.Scatter(
                        x=vals, y=depth, name=curve,
                        mode="lines",
                        line=dict(width=1),
                    ),
                    row=1, col=i
                )
            except Exception:
                pass

        fig.update_yaxes(autorange="reversed", title_text=depth_col, col=1)
        fig.update_layout(
            height=650,
            showlegend=False,
            margin=dict(l=60, r=20, t=40, b=40),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"las_plot_{key}")

    except ImportError:
        st.warning("lasio not installed — cannot plot curves.")
    except Exception as e:
        st.error(f"LAS plot: {e}")


def _plot_dlis_curves(file_path: str, key: str):
    """Interactive plotly curve plot for DLIS files."""
    try:
        import warnings
        import numpy as np
        import plotly.graph_objects as go
        from dlisio import dlis

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with dlis.load(file_path) as lfs:
                lf = lfs[0]
                channels = list(lf.channels)

        if not channels:
            st.info("No channels found.")
            return

        ch_names = [ch.name for ch in channels]
        depth_name = next(
            (n for n in ch_names if n.upper() in ("DEPT","DEPTH","MD","TVD","TIME")),
            ch_names[0]
        )
        data_names = [n for n in ch_names if n != depth_name]

        sel = st.multiselect("Channels to plot", data_names,
                              default=data_names[:min(4, len(data_names))],
                              key=f"dlis_curves_{key}")
        if not sel:
            return

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with dlis.load(file_path) as lfs:
                lf   = lfs[0]
                frs  = list(lf.frames)
                if not frs:
                    st.info("No frames found.")
                    return
                fr    = frs[0]
                frame = fr.curves()
                cols  = frame.dtype.names

        if depth_name not in cols:
            depth_name = cols[0]

        depth = frame[depth_name].flatten()
        fig   = go.Figure()
        for curve in sel:
            if curve in cols:
                vals = frame[curve].flatten()
                if vals.shape == depth.shape:
                    fig.add_trace(go.Scatter(
                        x=vals, y=depth, name=curve, mode="lines"
                    ))

        fig.update_layout(
            height=600,
            yaxis=dict(autorange="reversed", title=depth_name),
            showlegend=True,
            margin=dict(l=50, r=20, t=30, b=40),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"dlis_plot_{key}")

    except Exception as e:
        st.error(f"DLIS plot: {e}")


def _plot_segy_traces(file_path: str, key: str):
    """Wiggle trace plot for SEG-Y — first N traces."""
    try:
        import segyio
        import numpy as np
        import plotly.graph_objects as go

        n_traces = st.slider("Traces to display", 10, 200, 50, 10,
                              key=f"segy_ntrace_{key}")

        with segyio.open(file_path, ignore_geometry=True, strict=False) as f:
            n   = min(n_traces, f.tracecount)
            si  = segyio.tools.dt(f) / 1000.0  # ms
            ns  = f.samples.size
            t   = np.arange(ns) * si
            data = np.stack([f.trace[i] for i in range(n)], axis=1)

        # Normalise
        scale = np.percentile(np.abs(data), 98) or 1.0
        data  = data / scale

        fig = go.Figure()
        for i in range(n):
            x = data[:, i] + i
            fig.add_trace(go.Scatter(
                x=x, y=t, mode="lines",
                line=dict(width=0.7, color="black"),
                showlegend=False,
            ))

        fig.update_layout(
            height=600,
            yaxis=dict(autorange="reversed", title="Time (ms)"),
            xaxis=dict(title="Trace number"),
            margin=dict(l=50, r=20, t=30, b=40),
        )
        st.plotly_chart(fig, use_container_width=True, key=f"segy_plot_{key}")

    except Exception as e:
        st.error(f"SEG-Y trace plot: {e}")


def _plot_p190_map(file_path: str, key: str):
    """Shot point map for P190 files."""
    try:
        import plotly.graph_objects as go

        xs, ys, lats, lons, shots = [], [], [], [], []
        with open(file_path, "r", errors="replace") as f:
            for line in f:
                if not line.startswith("S"):
                    continue
                try:
                    sp  = line[19:25].strip()
                    lat = float(line[46:55].strip())
                    lon = float(line[55:65].strip())
                    x   = float(line[65:75].strip()) if len(line) > 75 else None
                    y   = float(line[75:85].strip()) if len(line) > 85 else None
                    shots.append(sp)
                    lats.append(lat); lons.append(lon)
                    if x: xs.append(x)
                    if y: ys.append(y)
                except Exception:
                    pass

        if not shots:
            st.info("No shot point records (S records) found.")
            return

        use_geo = len(lats) > 0 and any(abs(v) > 0 for v in lats)
        fig = go.Figure()
        if use_geo:
            fig.add_trace(go.Scatter(
                x=lons, y=lats, mode="lines+markers",
                marker=dict(size=3, color="steelblue"),
                line=dict(width=1),
                text=shots, name="Shot points",
            ))
            fig.update_layout(
                xaxis_title="Longitude", yaxis_title="Latitude",
                height=500,
            )
        else:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines+markers",
                marker=dict(size=3, color="steelblue"),
                line=dict(width=1),
                text=shots, name="Shot points",
            ))
            fig.update_layout(
                xaxis_title="Easting", yaxis_title="Northing",
                height=500,
            )

        st.caption(f"{len(shots):,} shot points")
        st.plotly_chart(fig, use_container_width=True, key=f"p190_plot_{key}")

    except Exception as e:
        st.error(f"P190 map: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Main workbench renderer
# ─────────────────────────────────────────────────────────────────────────────

def render_workbench(file_path: str,
                     fmt:       str  = None,
                     key:       str  = "",
                     show_edit: bool = False,
                     uwi:       str  = "",
                     survey:    str  = "",
                     on_save    = None):
    """
    Render the full file workbench for a single file.

    Parameters
    ----------
    file_path : str   — absolute path to the file
    fmt       : str   — format override (auto-detected if None)
    key       : str   — unique key suffix for Streamlit widgets
    show_edit : bool  — show UWI / survey name edit fields
    uwi       : str   — current UWI (for LAS/DLIS/LIS)
    survey    : str   — current survey name (for SEG-Y/P190)
    on_save   : callable(uwi, survey) — called when user saves edits
    """
    path = Path(file_path)
    if fmt is None:
        fmt = detect_format(file_path)

    # ── File summary bar ──────────────────────────────────────────────────────
    size_mb = path.stat().st_size / (1024*1024) if path.exists() else 0
    c1, c2, c3 = st.columns([3, 1, 1])
    c1.markdown(f"**{path.name}**")
    c2.caption(f"{fmt} · {size_mb:.1f} MB")
    if not path.exists():
        st.error(f"⚠️ File not found: `{file_path}`")
        return

    # ── Optional UWI / survey edit ────────────────────────────────────────────
    if show_edit:
        with st.container(border=True):
            if fmt in ("LAS","DLIS","LIS"):
                new_uwi = st.text_input("UWI", value=uwi or "",
                                         key=f"wb_uwi_{key}",
                                         placeholder="e.g. 42-001-00001-0000")
                if on_save and st.button("💾 Save UWI", key=f"wb_save_{key}",
                                          type="primary"):
                    on_save(new_uwi.strip(), "")
            else:
                new_sv = st.text_input("Survey name", value=survey or "",
                                        key=f"wb_survey_{key}",
                                        placeholder="e.g. CENTRAL_AUSTRALIA_3D")
                if on_save and st.button("💾 Save survey", key=f"wb_save_{key}",
                                          type="primary"):
                    on_save("", new_sv.strip())

    # ── Tabs: Header | Decoded | Plot ─────────────────────────────────────────
    if fmt == "LAS":
        tab_hdr, tab_plot = st.tabs(["📄 Raw Header", "📈 Curve Plot"])
        with tab_hdr:
            _view_las_header(file_path)
        with tab_plot:
            _plot_las_curves(file_path, key)

    elif fmt == "DLIS":
        tab_hdr, tab_plot = st.tabs(["📦 Decoded Header", "📈 Curve Plot"])
        with tab_hdr:
            _view_dlis_header(file_path)
        with tab_plot:
            _plot_dlis_curves(file_path, key)

    elif fmt == "LIS":
        tab_hdr, tab_plot = st.tabs(["📋 Decoded Header", "📈 Curve Plot"])
        with tab_hdr:
            _view_lis_header(file_path)
        with tab_plot:
            _plot_dlis_curves(file_path, key)  # LIS uses same frame structure

    elif fmt == "SEGY":
        tab_ebcdic, tab_bin, tab_plot = st.tabs([
            "📡 EBCDIC Header", "🔢 Binary Header", "📊 Wiggle Traces"
        ])
        with tab_ebcdic:
            _view_segy_header(file_path)
        with tab_bin:
            try:
                import segyio, pandas as pd
                with segyio.open(file_path, ignore_geometry=True, strict=False) as f:
                    bin_hdr = {str(k): int(v) for k,v in dict(f.bin).items()}
                # Show as a clean table, non-zero values first
                rows = [{"Field": k, "Value": v}
                        for k,v in bin_hdr.items() if v != 0]
                rows += [{"Field": k, "Value": v}
                         for k,v in bin_hdr.items() if v == 0]
                st.dataframe(pd.DataFrame(rows), hide_index=True,
                             use_container_width=True)
            except Exception as e:
                # Raw struct fallback
                try:
                    import struct, pandas as pd
                    with open(file_path, "rb") as f:
                        raw = f.read(3600)
                    if len(raw) >= 3600:
                        fields = [
                            (0,  ">i", "Job identification number"),
                            (4,  ">i", "Line number"),
                            (8,  ">i", "Reel number"),
                            (12, ">h", "Traces per ensemble"),
                            (14, ">h", "Auxiliary traces per ensemble"),
                            (16, ">h", "Sample interval (µs)"),
                            (18, ">h", "Sample interval (µs) — original"),
                            (20, ">h", "Samples per trace"),
                            (22, ">h", "Samples per trace — original"),
                            (24, ">h", "Data sample format code"),
                            (26, ">h", "Ensemble fold"),
                            (28, ">h", "Trace sorting code"),
                            (30, ">h", "Vertical sum code"),
                            (32, ">h", "Sweep frequency start (Hz)"),
                            (34, ">h", "Sweep frequency end (Hz)"),
                            (36, ">h", "Sweep length (ms)"),
                            (38, ">h", "Sweep type code"),
                            (60, ">h", "SEG-Y format revision number"),
                            (62, ">h", "Fixed length trace flag"),
                        ]
                        rows = []
                        for off, fmt, name in fields:
                            try:
                                sz  = struct.calcsize(fmt)
                                val = struct.unpack(fmt, raw[3200+off:3200+off+sz])[0]
                                rows.append({"Field": name, "Value": val})
                            except Exception:
                                pass
                        st.caption(f"⚠️ segyio failed — raw struct decode")
                        st.dataframe(pd.DataFrame(rows), hide_index=True,
                                     use_container_width=True)
                except Exception as e2:
                    st.error(f"Binary header decode failed: {e2}")
        with tab_plot:
            _plot_segy_traces(file_path, key)

    elif fmt == "P190":
        tab_hdr, tab_map = st.tabs(["📍 H Records", "🗺 Shot Point Map"])
        with tab_hdr:
            _view_p190_header(file_path)
        with tab_map:
            _plot_p190_map(file_path, key)

    else:
        st.warning(f"No viewer available for format: {fmt}")
        try:
            with open(file_path, "r", errors="replace") as f:
                st.code(f.read(4096), language=None)
        except Exception:
            pass
