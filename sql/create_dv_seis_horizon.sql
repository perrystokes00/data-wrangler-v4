/* ===========================================================================
   Seismic horizons: the interpretation layer over dv_seis_set / dv_seis_line.
   ---------------------------------------------------------------------------
   THREE TABLES, BECAUSE A HORIZON IS ASKED THREE DIFFERENT QUESTIONS.

     dv_seis_horizon          what the horizon IS         -- one row each
     dv_seis_horizon_grid     the SURFACE, as a grid      -- for sections
     dv_seis_horizon_contour  the surface as CONTOURS     -- for the map

   The grid answers "what time is this horizon at this position", which is what
   a section overlay needs at every trace. The contours answer "draw me the
   structure", which is what a map needs -- and drawing a map from the grid
   would mean shipping tens of thousands of points to the browser to render as
   a picture that twenty polylines already convey. Both are derived from the
   same surface, so they cannot disagree.

   NO dv_r_ REFERENCE TABLE IS CREATED HERE, deliberately. Creating one ARMS A
   GUARD: promote holds any row whose coded value is not registered, and the
   guard fires on dv_r_ names alone. A new domain therefore needs its table AND
   a list covering everything the data says, in the same step -- and horizon
   naming is free text in every source this will ever read. horizon_type is
   documented below and left unconstrained rather than half-registered.

   EXTENT IS NOT DECORATION. A horizon picked over Teapot Dome must not be
   drawn across the North Sea or sampled onto an F3 section. bbox_* is checked
   before a horizon is offered for a line or a map view.
   =========================================================================== */

IF OBJECT_ID('dataview.dv_seis_horizon_contour', 'U') IS NOT NULL
    DROP TABLE dataview.dv_seis_horizon_contour;
GO
IF OBJECT_ID('dataview.dv_seis_horizon_grid', 'U') IS NOT NULL
    DROP TABLE dataview.dv_seis_horizon_grid;
GO
IF OBJECT_ID('dataview.dv_seis_horizon', 'U') IS NOT NULL
    DROP TABLE dataview.dv_seis_horizon;
GO

CREATE TABLE dataview.dv_seis_horizon (
    horizon_id        NVARCHAR(40)   NOT NULL,
    seis_set_id       NVARCHAR(40)   NULL,      -- the survey it was picked on
    horizon_name      NVARCHAR(255)  NOT NULL,
    /* horizon_type vocabulary, free text on purpose (see the note above):
       SEISMIC MARKER | FORMATION TOP | UNCONFORMITY | FAULT | FLUID CONTACT */
    horizon_type      NVARCHAR(40)   NULL,
    strat_unit_name   NVARCHAR(255)  NULL,      -- the geology it stands for
    seq_no            INT            NULL,      -- 1 = shallowest, for ordering
    /* TIME and DEPTH horizons must never be plotted on one axis by accident,
       so the domain travels with the value rather than being inferred from
       the magnitude. */
    pick_domain       NVARCHAR(40)   NULL,      -- TIME | DEPTH
    pick_uom          NVARCHAR(40)   NULL,      -- MS | M | FT
    min_value         NUMERIC(15,4)  NULL,
    max_value         NUMERIC(15,4)  NULL,
    bbox_min_lat      NUMERIC(11,7)  NULL,
    bbox_max_lat      NUMERIC(11,7)  NULL,
    bbox_min_lon      NUMERIC(12,7)  NULL,
    bbox_max_lon      NUMERIC(12,7)  NULL,
    display_colour    NVARCHAR(20)   NULL,      -- hex, used by map AND section
    interpreter       NVARCHAR(255)  NULL,
    interp_date       DATETIME2(7)   NULL,
    active_ind        NVARCHAR(1)    NOT NULL CONSTRAINT df_dv_seis_horizon_act DEFAULT 'Y',
    remark            NVARCHAR(2000) NULL,
    row_created_by    NVARCHAR(40)   NOT NULL,
    row_created_date  DATETIME2(7)   NOT NULL CONSTRAINT df_dv_seis_horizon_crd DEFAULT SYSUTCDATETIME(),
    row_changed_by    NVARCHAR(40)   NULL,
    row_changed_date  DATETIME2(7)   NULL,
    source            NVARCHAR(40)   NULL,
    INVENTORY_ID      NVARCHAR(64)   NULL,
    CONSTRAINT pk_dv_seis_horizon PRIMARY KEY CLUSTERED (horizon_id)
);
GO

/* The surface. One row per grid node -- a regular lat/lon mesh, because the
   section overlay interpolates between nodes and an irregular set would need a
   triangulation to do the same job. */
CREATE TABLE dataview.dv_seis_horizon_grid (
    horizon_id        NVARCHAR(40)   NOT NULL,
    row_no            INT            NOT NULL,
    col_no            INT            NOT NULL,
    latitude          NUMERIC(11,7)  NOT NULL,
    longitude         NUMERIC(12,7)  NOT NULL,
    /* NULL means NO DATA, not zero. A horizon does not exist everywhere its
       bounding box covers, and a zero would plot as a hard reflector at the
       surface. */
    value             NUMERIC(15,4)  NULL,
    active_ind        NVARCHAR(1)    NOT NULL CONSTRAINT df_dv_seis_hgrid_act DEFAULT 'Y',
    row_created_by    NVARCHAR(40)   NOT NULL,
    row_created_date  DATETIME2(7)   NOT NULL CONSTRAINT df_dv_seis_hgrid_crd DEFAULT SYSUTCDATETIME(),
    CONSTRAINT pk_dv_seis_horizon_grid PRIMARY KEY CLUSTERED
        (horizon_id, row_no, col_no),
    CONSTRAINT fk_dv_seis_horizon_grid FOREIGN KEY (horizon_id)
        REFERENCES dataview.dv_seis_horizon(horizon_id)
);
GO

/* Contours for the map. geog is a LINESTRING at one constant value, which is
   exactly what the map's existing line layers already know how to draw. */
CREATE TABLE dataview.dv_seis_horizon_contour (
    horizon_id        NVARCHAR(40)   NOT NULL,
    contour_id        NVARCHAR(40)   NOT NULL,
    contour_value     NUMERIC(15,4)  NOT NULL,
    n_points          INT            NULL,
    geog              GEOGRAPHY      NULL,
    active_ind        NVARCHAR(1)    NOT NULL CONSTRAINT df_dv_seis_hcont_act DEFAULT 'Y',
    row_created_by    NVARCHAR(40)   NOT NULL,
    row_created_date  DATETIME2(7)   NOT NULL CONSTRAINT df_dv_seis_hcont_crd DEFAULT SYSUTCDATETIME(),
    CONSTRAINT pk_dv_seis_horizon_contour PRIMARY KEY CLUSTERED
        (horizon_id, contour_id),
    CONSTRAINT fk_dv_seis_horizon_contour FOREIGN KEY (horizon_id)
        REFERENCES dataview.dv_seis_horizon(horizon_id)
);
GO

/* The map reads contours by horizon and value; the section overlay reads the
   grid by horizon and walks it. Both are covered. */
CREATE INDEX ix_dv_seis_hcont_horizon
    ON dataview.dv_seis_horizon_contour (horizon_id, contour_value);
GO
CREATE INDEX ix_dv_seis_hgrid_pos
    ON dataview.dv_seis_horizon_grid (horizon_id, latitude, longitude)
    INCLUDE (value);
GO
