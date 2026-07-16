# Supported File Types — What DataView Extracts From a File Share

When the cataloger crawls a file share, it inventories **every** file, then runs
format-specific extraction on the types below to pull a well/seismic identity and
key metadata. Files whose type isn't listed are inventoried but carry no extracted
identity (they fall to the LOW/triage queue for manual handling).

"Identity" means the fields that let a file be matched to a well or survey —
primarily **UWI/API** and **well name**, plus operator, field, county/state, and
location where available. Identity is what drives triage tiering and promotion.

---

## Unstructured documents

| Type | Extensions | What's extracted | Engine |
|---|---|---|---|
| PDF | `.pdf` | Report-type classification (directional survey, formation tops, DST, completion, scout ticket, pressure, petrophysical, mud log, …), UWI/API, well name, operator, field, county/state, lat/lon, total depth, spud/rig-release dates; scout-ticket sections | `pdf_survey_catalog` |
| Word | `.docx` `.docm` `.doc` | Paragraph + heading text, document-type classification, UWI/API and well name via pattern match | python-docx |
| Excel | `.xlsx` `.xlsm` `.xls` | Header located anywhere in the first 25 rows **or** a label:value form; UWI/API and well name from the matched column/label; sheet-schema classification | openpyxl |
| PowerPoint | `.pptx` `.ppt` | Slide and table text → UWI/API and well name; slide count and deck title | python-pptx |
| Tabular | `.csv` `.tsv` `.txt` | Header or label:value scan → UWI/API and well name; column structure | csv/pandas |
| Email | `.eml` `.msg` | Subject + body → UWI/API; sender and subject | stdlib email / extract-msg |
| OpenDocument | `.odt` `.ods` `.odp` | Document text → UWI/API and well name | content.xml |
| Rich text | `.rtf` | Document text → UWI/API and well name | striprtf |
| Images / scans | `.tif` `.tiff` `.png` `.jpg` `.jpeg` | Dimensions, mode, band count. **Identity requires OCR**, which is flagged but not run — a scanned log/map is inventoried with a note, not auto-identified | rasterio / Pillow |

## Structured well data

| Type | Extensions | What's extracted | Engine |
|---|---|---|---|
| LAS logs | `.las` | Well header (UWI/API, well, operator, field, state/county, lat/lon, TD, spud, service co.) + curve mnemonics/units/depth interval | lasio (+ curve registry) |
| DLIS / LIS | `.dlis` `.dlf` `.lis` | Well/field/operator from origins; frames and channel (curve) list | dlisio |
| ASCII / deviation | `.asc` `.prn` `.dev` | Header UWI/API and well name; data-column count (deviation = MD/INC/AZI) | text scan |
| WITSML | `.xml` `.wml` | well / wellbore / trajectory / log / mudLog objects → UWI, well name, operator, field, curves | `witsml_catalog` |
| OSDU / JSON well log | `.json` | WellLog / Well / WellboreMarkerSet / PressureData / Trajectory / SeismicAcquisitionSurvey → identity, curves, bbox | `json_well_log_catalog` |

## Seismic

| Type | Extensions | What's extracted | Engine |
|---|---|---|---|
| SEG-Y | `.segy` `.sgy` `.seg` | Survey/line name, sample interval, trace count, 2D/3D, inline/crossline range, bbox, EPSG, survey footprint (WKT) | segyio |
| Navigation | `.p190` `.p90` `.p1` `.p2` `.p3` | UKOOA/SEG positioning — survey/line, coordinates, CRS | P190 parser |

*2D vs 3D is metadata (`SEIS_SET_TYPE`), not an extension difference — both are SEG-Y.*

## Spatial / GIS

| Type | Extensions | What's extracted | Engine |
|---|---|---|---|
| Esri shapefile | `.shp` (+ `.shx` `.dbf` `.prj` `.cpg` sidecars) | Feature count, geometry type, CRS, attributes; sample UWIs / well names / operators / fields / dates from matched columns | GeoPandas + `shapefile_catalog` |
| GeoJSON | `.geojson` | Same as shapefile (feature properties) | GeoPandas |
| GeoPackage | `.gpkg` | Same as shapefile (layer features) | GeoPandas |
| MapInfo / geodatabase | `.tab` `.mif` `.gdb` | Feature count, geometry, CRS, attributes; UWI / well name from matched columns | GeoPandas / OGR |
| Google Earth | `.kml` `.kmz` | Placemark names + ExtendedData → UWI and well name; placemark count | XML parse |
| GeoTIFF | `.tif` `.tiff` | CRS, bounds, raster size, band count (georeferenced raster — no well identity) | rasterio |

---

## Not auto-extracted (inventoried only / manual)

- **Application projects** — Petrel (`.pet`/`.zgy`), GeoGraphix, Geolog, Techlog, Kingdom (`.tks`): these are folders or databases, not single files. They are detected and registered as a project/container, not parsed file-by-file. Reading their contents is a database/connector job (Kingdom = SQL Server, GeoGraphix = SQL Anywhere), built per-project against the real schema.
- **Legacy binary** — old `.doc`/`.xls` (pre-2007 binary) and `.xlsb` need separate readers; **`.cgm`** vector log images and **SEG-D** (`.segd`) need dedicated parsers.
- **CAD** — `.dwg`/`.dxf` and **Access** `.mdb`/`.accdb`.
- **Scanned rasters** — `.tif`/images are inventoried with dimensions but need OCR for identity.

---

### How extraction routes (for maintainers)

The workbench's `_extract_fields` has dedicated branches for the high-value
formats (PDF, LAS, DLIS/LIS, SEG-Y, shapefile, WITSML, OSDU JSON, Office). Every
other supported extension falls through to a catch-all that calls
`modules.file_summarizer.summarize()`, gated on `file_summarizer.SUPPORTED_EXTS`
(currently 49 extensions) so unknown files (`.exe`, `.ini`, …) are never opened.
Adding a new format = add a `_summarize_*` handler + a dispatch entry; the
catch-all wires it into the pipeline automatically.
