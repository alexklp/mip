-- 032_seed_contour1_recall_gap_aliases_v1.sql
--
-- High-precision C1 aliases discovered by recall-gap calibration.
-- Only corpus-observed object-specific forms are added.
-- Generic lexical cues are intentionally excluded.

WITH alias_data(canonical_name, alias_text, verification_status, source_note) AS (
    VALUES
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68-й отдельной аэромобильной бригады',
        'internal_verified',
        'observed in MIP corpus during C1 recall-gap calibration'
    ),
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68 ОАЭМБр',
        'internal_verified',
        'observed in MIP corpus during C1 recall-gap calibration'
    )
)
INSERT INTO contour_reference_entries (
    monitoring_contour_id,
    object_id,
    entry_type,
    match_mode,
    reference_text,
    reference_version,
    verification_status,
    verified_on,
    source_note
)
SELECT
    1,
    o.object_id,
    'alias',
    'exact',
    a.alias_text,
    1,
    a.verification_status,
    DATE '2026-08-25',
    a.source_note
FROM alias_data a
JOIN contour_reference_objects o
  ON o.monitoring_contour_id = 1
 AND o.canonical_name = a.canonical_name
 AND o.active
ON CONFLICT DO NOTHING;
