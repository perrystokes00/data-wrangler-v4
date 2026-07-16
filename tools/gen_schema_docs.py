#!/usr/bin/env python3
"""
gen_schema_docs.py
==================
Generate schema documentation for the DataView database straight from the
live catalog — no hand-drawn diagrams to drift out of date.

Outputs (into --out, default ./schema_docs):
  • schema_erd.md        – an overview flowchart of the subject areas plus one
                           Mermaid erDiagram per area (renders on GitHub).
  • schema_erd.html      – the same diagrams as a self-contained page; open it
                           in a browser to view, then print-to-PDF to export.
                           Skip with --no-html.
  • data_dictionary.md   – every table grouped by subject area: row count,
                           columns (type / null / key), and relationships.
  • schema_areas.json    – the table→area assignment, so you can review it and
                           feed corrections back in via --areas-in.
  • erd_<name>.svg/png   – only with --images svg|png. Rendered locally via
                           Graphviz (pip install graphviz + the dot engine),
                           or mermaid-cli if that's what's installed.

Relationship lines come from declared FOREIGN KEYs where they exist, and are
otherwise inferred: any table with a `uwi` column links to the well master,
and any `<x>_id` column links to the table whose primary key is `<x>_id`.
Inferred edges are marked "(inf)" so you can tell them apart.

Examples
--------
  python gen_schema_docs.py --database DataView
  python gen_schema_docs.py --server "localhost\\SQLEXPRESS" \\
      --database DataView --schema dataview --out ./schema_docs
  python gen_schema_docs.py --database DataView --areas-in schema_areas.json
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dataview.core import schema_introspect as si


def _human(n: int) -> str:
    return f"{n:,}"


def write_erd(model: dict, out: Path, suffix: str = "") -> Path:
    lines = [
        f"# {model['schema']} — schema map",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M} from the live catalog._",
        "",
        "## Subject areas",
        "",
        "```mermaid",
        si.build_overview_mermaid(model),
        "```",
        "",
    ]
    for area, tabs in model["areas"].items():
        meta = si.AREA_META[area]
        lines += [
            f"## {meta['icon']} {meta['label']}",
            "",
            f"{meta['desc']}",
            "",
            f"*{len(tabs)} tables.*",
            "",
            "```mermaid",
            si.build_area_mermaid(model, area),
            "```",
            "",
        ]
    p = out / f"schema_erd{suffix}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def write_dictionary(model: dict, out: Path, suffix: str = "") -> Path:
    tables = model["tables"]
    # parent/child lookup for the relationships line
    parents: dict[str, list] = {}
    children: dict[str, list] = {}
    for e in model["edges"]:
        parents.setdefault(e["child"], []).append(e)
        children.setdefault(e["parent"], []).append(e)

    lines = [
        f"# {model['schema']} — data dictionary",
        "",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M}._",
        "",
    ]
    total_rows = sum(t["row_count"] for t in tables.values())
    lines += [
        f"**{len(tables)} tables**, "
        f"**{_human(total_rows)} rows** across "
        f"{len(model['areas'])} subject areas.",
        "",
    ]
    for area, tabs in model["areas"].items():
        meta = si.AREA_META[area]
        lines += [f"## {meta['icon']} {meta['label']}", ""]
        for tname in tabs:
            t = tables[tname]
            lines += [
                f"### `{tname}`",
                "",
                f"_{_human(t['row_count'])} rows._",
                "",
                "| Column | Type | Null | Key |",
                "|--------|------|:----:|:---:|",
            ]
            for c in t["columns"]:
                key = "PK" if c["is_pk"] else ("FK" if c["is_fk"] else "")
                nul = "✓" if c["is_nullable"] else ""
                lines.append(
                    f"| {c['name']} | {si.format_type(c)} | {nul} | {key} |")
            rels = []
            for e in parents.get(tname, []):
                tag = " (inferred)" if e["inferred"] else ""
                rels.append(f"→ `{e['parent']}` on `{e['col']}`{tag}")
            for e in children.get(tname, []):
                tag = " (inferred)" if e["inferred"] else ""
                rels.append(f"← `{e['child']}` on `{e['col']}`{tag}")
            if rels:
                lines += ["", "**Relationships:** " + "; ".join(rels)]
            lines.append("")
    p = out / f"data_dictionary{suffix}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def write_areas_json(model: dict, out: Path, suffix: str = "") -> Path:
    mapping = {tname: t["area"] for tname, t in sorted(model["tables"].items())}
    p = out / f"schema_areas{suffix}.json"
    p.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    return p


def _diagrams(model: dict):
    """[(name, title, mermaid_code), …] — overview flowchart + one ER per area."""
    out = [("overview", "Subject areas", si.build_overview_mermaid(model))]
    for area in model["areas"]:
        meta = si.AREA_META[area]
        out.append((area, f"{meta['icon']} {meta['label']}",
                    si.build_area_mermaid(model, area)))
    return out


_PANZOOM_SRC = ('<script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.1/'
                'dist/svg-pan-zoom.min.js"></script>')
_MERMAID_SRC = ('<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/'
                'mermaid.min.js"></script>')

_INIT_PZ_JS = """function initPZ(){
  if(typeof svgPanZoom!=='function'){
    console.error('svg-pan-zoom not loaded (CDN blocked?) - diagrams still '
      +'show, just without zoom/pan'); return; }
  document.querySelectorAll('.diagram-wrap').forEach(function(wrap){
    var svg=wrap.querySelector('svg'); if(!svg) return;
    svg.removeAttribute('style');
    svg.setAttribute('width','100%'); svg.setAttribute('height','100%');
    var inst;
    try{ inst=svgPanZoom(svg,{zoomEnabled:true,controlIconsEnabled:false,
      fit:true,center:true,minZoom:0.2,maxZoom:40,zoomScaleSensitivity:0.3}); }
    catch(e){ console.error('panzoom',e); return; }
    var q=function(s){return wrap.querySelector(s);};
    if(q('.zin'))  q('.zin').addEventListener('click',function(){inst.zoomIn();});
    if(q('.zout')) q('.zout').addEventListener('click',function(){inst.zoomOut();});
    if(q('.zfit')) q('.zfit').addEventListener('click',
        function(){inst.resize();inst.fit();inst.center();});
  });
}"""

_SCRIPTS_INLINE = (
    _PANZOOM_SRC + "\n<script>\n" + _INIT_PZ_JS +
    "\nwindow.addEventListener('load',function(){setTimeout(initPZ,30);});\n"
    "</script>")

_SCRIPTS_MERMAID = (
    _MERMAID_SRC + "\n" + _PANZOOM_SRC + "\n<script>\n"
    "mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'loose',"
    "suppressErrors:false,flowchart:{htmlLabels:true}});\n" + _INIT_PZ_JS +
    "\n(async()=>{try{if(typeof mermaid.run==='function'){"
    "await mermaid.run({querySelector:'.mermaid'});}else{"
    "mermaid.init(undefined,document.querySelectorAll('.mermaid'));}}"
    "catch(e){console.error('mermaid',e);}setTimeout(initPZ,80);})();\n"
    "</script>")

_HTML_TMPL = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{background:#0e1117;color:#e8eef2;font-family:system-ui,Segoe UI,Arial,
      sans-serif;margin:24px}
 h1{font-size:1.4rem}
 h2{font-size:1.05rem;margin-top:28px;border-bottom:1px solid #2a2f3a;
    padding-bottom:4px}
 .meta{color:#9aa0a6;font-size:.85rem;margin-bottom:4px}
 .hint{color:#7a7f87;font-size:.8rem;margin-bottom:16px}
 .diagram-wrap{height:600px;border:1px solid #2a2f3a;border-radius:8px;
    background:#0e1117;margin:8px 0 28px;overflow:hidden;position:relative}
 .svgbox,.mermaid{height:100%;width:100%;display:flex;align-items:center;
    justify-content:center}
 .diagram-wrap svg{max-width:none !important}
 .bar{position:absolute;top:8px;right:8px;z-index:5;display:flex;gap:4px}
 .bar button{background:#1b2230;color:#e8eef2;border:1px solid #3a4151;
    border-radius:5px;padding:3px 10px;cursor:pointer;font-size:14px;
    line-height:1}
 .bar button:hover{background:#283042}
</style></head><body>
<h1>__TITLE__</h1>
<div class="meta">__META__</div>
<div class="hint">Scroll to zoom &middot; drag to pan &middot; or use the
 <b>+ &minus; Fit</b> buttons on each box. Each diagram pans independently.</div>
__SECTIONS__
__SCRIPTS__
</body></html>"""

_DIAGRAM_BAR = ('<div class="bar"><button class="zin" title="Zoom in">+</button>'
                '<button class="zout" title="Zoom out">\u2212</button>'
                '<button class="zfit" title="Fit to box">\u27f2 Fit</button>'
                '</div>')


def _inline_svgs(model: dict):
    """Render every diagram to an inline SVG string via Graphviz. Returns
    {name: svg_markup} or None if Graphviz (package or `dot` engine) is
    unavailable — in which case the caller falls back to Mermaid."""
    try:
        import graphviz
    except ImportError:
        return None
    out = {}
    for name, _title, _mmd in _diagrams(model):
        dot = (si.build_overview_dot(model) if name == "overview"
               else si.build_area_dot(model, name))
        try:
            svg = graphviz.Source(dot).pipe(format="svg").decode("utf-8")
        except Exception:
            return None  # dot engine missing → fall back to Mermaid
        i = svg.find("<svg")
        out[name] = svg[i:] if i >= 0 else svg
    return out


def write_html(model: dict, out: Path, suffix: str = "") -> Path:
    """Self-contained HTML viewer with zoom/pan (svg-pan-zoom) and +/-/Fit
    buttons per diagram. Embeds Graphviz-rendered SVGs directly when Graphviz
    is available (most robust); otherwise renders via Mermaid in the browser."""
    svgs = _inline_svgs(model)
    if svgs is not None:
        sections = "\n".join(
            f'<h2>{title}</h2>\n<div class="diagram-wrap">{_DIAGRAM_BAR}'
            f'<div class="svgbox">{svgs[name]}</div></div>'
            for name, title, _code in _diagrams(model))
        scripts = _SCRIPTS_INLINE
    else:
        sections = "\n".join(
            f'<h2>{title}</h2>\n<div class="diagram-wrap">{_DIAGRAM_BAR}'
            f'<div class="mermaid">\n{code}\n</div></div>'
            for _name, title, code in _diagrams(model))
        scripts = _SCRIPTS_MERMAID

    total = sum(t["row_count"] for t in model["tables"].values())
    meta = (f"schema <code>{model['schema']}</code> &middot; "
            f"{len(model['tables'])} tables &middot; {total:,} rows &middot; "
            f"generated {datetime.now():%Y-%m-%d %H:%M}")
    html = (_HTML_TMPL
            .replace("__TITLE__", f"{model['schema']} — schema map")
            .replace("__META__", meta)
            .replace("__SECTIONS__", sections)
            .replace("__SCRIPTS__", scripts))
    p = out / f"schema_erd{suffix}.html"
    p.write_text(html, encoding="utf-8")
    return p


def _render_graphviz(model: dict, out: Path, suffix: str, fmt: str):
    """Render each diagram to fmt via the `graphviz` package (`dot` engine).
    Returns list of paths. Raises graphviz.ExecutableNotFound if `dot` isn't
    installed; ImportError if the package isn't installed."""
    import graphviz
    paths = []
    for name, _title, _mmd in _diagrams(model):
        dot = (si.build_overview_dot(model) if name == "overview"
               else si.build_area_dot(model, name))
        src = graphviz.Source(dot, filename=f"erd{suffix}_{name}",
                              directory=str(out), format=fmt)
        paths.append(Path(src.render(cleanup=True)))
    return paths


def _render_mmdc(model: dict, out: Path, suffix: str, fmt: str, mmdc: str):
    """Render each diagram via mermaid-cli (mmdc). Returns list of paths."""
    import subprocess
    paths = []
    for name, _title, code in _diagrams(model):
        mmd = out / f"erd{suffix}_{name}.mmd"
        mmd.write_text(code, encoding="utf-8")
        img = out / f"erd{suffix}_{name}.{fmt}"
        try:
            subprocess.run(
                [mmdc, "-i", str(mmd), "-o", str(img),
                 "-t", "dark", "-b", "transparent"],
                check=True, capture_output=True)
            paths.append(img)
        except Exception as e:
            print(f"    ! render failed for {name}: {e}", file=sys.stderr)
    return paths


def write_images(model: dict, out: Path, suffix: str, fmt: str):
    """Render diagrams to fmt. Graphviz first (local, no Node), then
    mermaid-cli. Returns (paths, hint) — hint is a how-to string if nothing
    rendered, else None."""
    import shutil

    # 1) Graphviz
    try:
        import graphviz  # noqa: F401
        try:
            return _render_graphviz(model, out, suffix, fmt), None
        except graphviz.ExecutableNotFound:
            gv_hint = ("Graphviz engine ('dot') not on PATH — install it: "
                       "winget install Graphviz  (then open a fresh shell)")
    except ImportError:
        gv_hint = ("Graphviz not set up — pip install graphviz  and  "
                   "winget install Graphviz")

    # 2) mermaid-cli fallback
    mmdc = shutil.which("mmdc")
    if mmdc:
        return _render_mmdc(model, out, suffix, fmt, mmdc), None

    return [], gv_hint


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate DataView schema docs from the live catalog.")
    ap.add_argument("--server", default=r"localhost\SQLEXPRESS",
                    help=r"SQL Server instance (default localhost\SQLEXPRESS)")
    ap.add_argument("--database", required=True, help="Database name")
    ap.add_argument("--schema", default="dataview",
                    help="Schema name, or comma-separated list "
                         "(e.g. dataview,file_catalog,las_catalog). "
                         "With more than one, output files are suffixed "
                         "per schema.")
    ap.add_argument("--out", default="support/schema_docs", help="Output directory")
    ap.add_argument("--driver", default=None,
                    help="ODBC driver name (auto-detected if omitted)")
    ap.add_argument("--areas-in", default=None,
                    help="JSON file of table→area overrides to apply")
    ap.add_argument("--no-html", action="store_true",
                    help="Skip the self-contained HTML diagram bundle")
    ap.add_argument("--images", choices=["svg", "png"], default=None,
                    help="Also render each diagram to svg/png. Uses Graphviz "
                         "(pip install graphviz + winget install Graphviz); "
                         "falls back to mermaid-cli if present.")
    args = ap.parse_args(argv)

    overrides = None
    if args.areas_in:
        overrides = json.loads(Path(args.areas_in).read_text(encoding="utf-8"))

    try:
        engine = si.make_engine(args.server, args.database, args.driver)
    except Exception as e:
        print(f"ERROR: could not connect: {e}", file=sys.stderr)
        return 2

    schemas = [s.strip() for s in args.schema.split(",") if s.strip()]
    multi = len(schemas) > 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    any_ok = False
    for sch in schemas:
        try:
            model = si.build_model(engine, sch, overrides)
        except Exception as e:
            print(f"ERROR [{sch}]: could not introspect: {e}", file=sys.stderr)
            continue
        if not model["tables"]:
            print(f"WARN [{sch}]: no tables found — skipped.", file=sys.stderr)
            continue

        any_ok = True
        suffix = f"_{sch}" if multi else ""
        p_erd = write_erd(model, out, suffix)
        p_dict = write_dictionary(model, out, suffix)
        p_json = write_areas_json(model, out, suffix)
        written = [p_erd.name, p_dict.name, p_json.name]

        if not args.no_html:
            written.append(write_html(model, out, suffix).name)
        if args.images:
            imgs, hint = write_images(model, out, suffix, args.images)
            if imgs:
                written.append(f"{len(imgs)} .{args.images} diagrams")
            if hint:
                print(f"    ! {hint}", file=sys.stderr)

        _wells = model.get("well_count")
        _wtxt = f", {_wells:,} wells" if _wells is not None else ""
        print(f"✓ [{sch}] {len(model['tables'])} tables across "
              f"{len(model['areas'])} areas{_wtxt}")
        for area, tabs in model["areas"].items():
            print(f"    {si.AREA_META[area]['label']:<28} {len(tabs)}")
        print(f"    wrote {', '.join(written)}")

    if not any_ok:
        print("ERROR: nothing generated.", file=sys.stderr)
        return 2
    print(f"✓ output in {out}/  "
          "(edit schema_areas*.json + pass via --areas-in to reclassify)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
