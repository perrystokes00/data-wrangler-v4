/* ===========================================================================
   Checkshots: the measured time-depth relationship at a well.
   ---------------------------------------------------------------------------
   WHY THIS IS ITS OWN TABLE. A checkshot is not a directional survey and not a
   log. It is the one measurement that ties a WELL to SEISMIC -- shoot a source
   at surface, record the first arrival at a known depth, and you have a
   time-depth pair that converts a horizon's two-way time into a formation top.
   Without it the seismic and the wells are two datasets that happen to share a
   map.

   Every other time-depth in this database is DERIVED from a velocity model.
   These are the observations that model is supposed to honour, so they belong
   in a table of their own rather than folded into a survey.

   depth_datum and the two uoms travel WITH the rows, not in a note: a
   checkshot referenced to KB and one referenced to ground level differ by the
   kelly bushing height, and a reader that assumes one when the other was meant
   mis-ties the whole well by that amount -- silently, because both numbers are
   plausible.

   The generic promote loop walks whatever discover_tables() returns, so this
   needs (a) the mirror built by build_catalog_mirror, (b) an entry in
   MIRROR_TABLES, and (c) a LINEAGE pair -- all three, or rows stage into a
   mirror nothing walks and are reported as neither moved nor held.
   =========================================================================== */

IF OBJECT_ID('dataview.dv_well_checkshot', 'U') IS NOT NULL
    DROP TABLE dataview.dv_well_checkshot;
GO

CREATE TABLE dataview.dv_well_checkshot (
    uwi               CHAR(14)       NOT NULL,
    checkshot_id      NVARCHAR(40)   NOT NULL,
    station_id        NVARCHAR(40)   NOT NULL,
    survey_date       DATETIME2(7)   NULL,
    contractor_ba_id  NVARCHAR(40)   NULL,
    /* The measurement itself. md is along hole, tvd is vertical, and the two
       differ the moment a well deviates -- which is why both are kept rather
       than one being computed on read from an inclination nobody stored. */
    md                NUMERIC(15,4)  NULL,
    tvd               NUMERIC(15,4)  NULL,
    depth_ouom        NVARCHAR(40)   NULL,
    depth_datum       NVARCHAR(40)   NULL,
    /* ONE-WAY vs TWO-WAY is the classic silent factor of two. Both are stored
       and the column names say which; nothing here infers one from the other. */
    twt_ms            NUMERIC(15,4)  NULL,
    owt_ms            NUMERIC(15,4)  NULL,
    time_ouom         NVARCHAR(40)   NULL,
    avg_velocity      NUMERIC(15,4)  NULL,
    interval_velocity NUMERIC(15,4)  NULL,
    velocity_ouom     NVARCHAR(40)   NULL,
    /* Which seismic this well ties to, when it is known. NULL is honest: a
       checkshot is a well measurement and stands on its own. */
    seis_set_id       NVARCHAR(40)   NULL,
    line_name         NVARCHAR(255)  NULL,
    remark            NVARCHAR(2000) NULL,
    active_ind        NVARCHAR(1)    NOT NULL
        CONSTRAINT df_dv_well_checkshot_act DEFAULT 'Y',
    row_created_by    NVARCHAR(40)   NOT NULL,
    row_created_date  DATETIME2(7)   NOT NULL
        CONSTRAINT df_dv_well_checkshot_crd DEFAULT SYSUTCDATETIME(),
    row_changed_by    NVARCHAR(40)   NULL,
    row_changed_date  DATETIME2(7)   NULL,
    source            NVARCHAR(40)   NULL,
    INVENTORY_ID      NVARCHAR(64)   NULL,
    CONSTRAINT pk_dv_well_checkshot PRIMARY KEY CLUSTERED
        (uwi, checkshot_id, station_id),
    CONSTRAINT fk_dv_well_checkshot_well FOREIGN KEY (uwi)
        REFERENCES dataview.dv_well(uwi)
);
GO

CREATE INDEX ix_dv_well_checkshot_uwi
    ON dataview.dv_well_checkshot (uwi, md) INCLUDE (twt_ms, tvd);
GO
