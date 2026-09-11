-- 042_seed_contour1_verified_fn_aliases_v1.sql
--
-- High-precision C1 aliases confirmed as false negatives during
-- the 2026-09-11 object-layer feasibility audit.
-- Only corpus-observed object-specific forms are added.

WITH alias_data(canonical_name, alias_text, verification_status, source_note) AS (
    VALUES
    (
        '46 окрема аеромобільна Подільська бригада',
        '46-й отдельной аэромобильной бригады',
        'internal_verified',
        'confirmed false negative in MIP corpus during 2026-09-11 object audit'
    ),
    (
        '7 корпус швидкого реагування',
        '7 корпусу швидкого реагування',
        'internal_verified',
        'confirmed false negative in MIP corpus during 2026-09-11 object audit'
    ),
    (
        '77 окрема аеромобільна Наддніпрянська бригада',
        '77-й аэромобильной бригады всу',
        'internal_verified',
        'confirmed false negative in MIP corpus during 2026-09-11 object audit'
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
    DATE '2026-09-11',
    a.source_note
FROM alias_data a
JOIN contour_reference_objects o
  ON o.monitoring_contour_id = 1
 AND o.canonical_name = a.canonical_name
 AND o.active
ON CONFLICT DO NOTHING;
