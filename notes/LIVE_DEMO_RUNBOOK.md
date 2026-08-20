# Live demo runbook — attended, no screenshots

Attended means you drive and talk; the software is live, not a recording. That
removes the "is this real?" objection the videos can't answer, and it puts every
failure in front of the prospect. So the whole point of the prep below is that
nothing on the demo box is doing anything for the first time.

## The bundle

Two databases, built by `tools/build_demo_bundle.py`:

| Restore as | Contents | Size |
|---|---|---|
| `DataView_Demo` | the product database, catalog and all | ~328 MB data |
| `WELL_REF` | `well_master_gold` trimmed to states 15/30/35/42, plus `WELL_MASTER_MINI` | ~1.1 GB data |

**Restore the trimmed one under the name `WELL_REF`.** The app reaches the master
by three-part name (`WELL_REF.well_ref.well_master_gold`), so the database has to
carry that name on the demo box or every federated view fails at once.

1.78M of 4.03M header rows survive the trim — 44%. The four states are where
every `dv_well` row lives, so nothing on the map goes hollow. If demo data
changes, `--dry-run` counts demo wells that would lose their header and refuses
to build rather than producing a map with a hole in it.

    python tools/build_demo_bundle.py --dry-run
    python tools/build_demo_bundle.py --backup-to C:\Bulk\demo_bundle

Built and verified 20 Aug 2026 — `C:\Bulk\demo_bundle`:

| File | Size | RESTORE VERIFYONLY |
|---|---|---|
| `DataView_Demo.bak` | 66 MB | OK |
| `WELL_REF_DEMO.bak` | 1,794 MB | OK |

1.86 GB total, which fits on anything. On the demo box, restore the trimmed
master **under the name `WELL_REF`** — it was backed up as `WELL_REF_DEMO`, so
the logical files have to be moved explicitly or the restore fails on a name
collision with nothing to explain it:

```sql
RESTORE DATABASE [DataView_Demo]
  FROM DISK = 'C:\Bulk\demo_bundle\DataView_Demo.bak' WITH RECOVERY;

RESTORE DATABASE [WELL_REF]
  FROM DISK = 'C:\Bulk\demo_bundle\WELL_REF_DEMO.bak'
  WITH MOVE 'WELL_REF_DEMO'     TO 'C:\Data\WELL_REF.mdf',
       MOVE 'WELL_REF_DEMO_log' TO 'C:\Data\WELL_REF_log.ldf',
       RECOVERY;
```

Then confirm the federation actually reaches it — this is the one query that
proves the three-part names resolve on the new box:

```sql
SELECT COUNT(*) FROM dataview_federation.v_well_density_r4;   -- expect 3,100-ish
```

## Before the call

- **`SELECT 1` on the demo box.** 0.5 ms healthy, 80 ms+ means ODBC tracing is
  on, which slows every call ~165x and will make the whole product look slow.
  `HKCU\SOFTWARE\ODBC\ODBC.INI\ODBC\Trace` must be `0`.
- **Run one query on each database first.** SQLEXPRESS has AUTO_CLOSE on, so the
  first query in a fresh process pays ~0.5 s — right when you're saying "watch
  how fast this is."
- **Walk the exact path once, end to end,** on the demo box, that morning. Not a
  similar path.
- **Have the backlog non-empty.** Status & Backlog with nothing held is a screen
  that proves nothing. Held rows with named reasons are the argument.

## The spine

1. **Catalog a folder.** Point the pipeline at documents it has never seen.
2. **Show what came back held, and why** — Status & Backlog names the gate per
   file, not a count.
3. **Drain it live.** Supply a UWI or a lat/long, Preview, Apply. This is the
   moment: the product says what is wrong and then lets you fix it in place.
4. **Map it.** The wells you just promoted, against 1.78M agency headers, no GIS
   in the stack.
5. **Drill a well to its documents.** Ends where the data came from.

## When something breaks

Say what it is and keep going. The audience for this product has watched data
load quietly and wrongly for years; a system that stops and names the reason is
the pitch, and demonstrating that live is not a recovery from a failure — it is
the failure mode working.
