-- 031_seed_contour1_observed_aliases_v1.sql
--
-- High-precision Contour 1 aliases observed in the current MIP corpus.
-- Numeric-only and overly generic brigade aliases are intentionally excluded.
--
-- Also preserves verified historical naming for the 68th brigade.

WITH alias_data(canonical_name, alias_text, verification_status, source_note) AS (
    VALUES

    -- 77 окрема аеромобільна Наддніпрянська бригада
    (
        '77 окрема аеромобільна Наддніпрянська бригада',
        '77-ї окремої аеромобільної бригади',
        'internal_verified',
        'observed in MIP corpus'
    ),

    -- 79 окрема десантно-штурмова Таврійська бригада
    (
        '79 окрема десантно-штурмова Таврійська бригада',
        '79-ї окремої десантно-штурмової бригади',
        'internal_verified',
        'observed in MIP corpus'
    ),
    (
        '79 окрема десантно-штурмова Таврійська бригада',
        '79-ї десантно-штурмової бригади',
        'internal_verified',
        'observed in MIP corpus'
    ),

    -- 68 окрема аеромобільна бригада імені Олекси Довбуша
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68-ї окремої аеромобільної бригади',
        'internal_verified',
        'observed in MIP corpus'
    ),
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68 ОАЕМБр',
        'internal_verified',
        'observed in MIP corpus'
    ),

    -- Historical official naming of the same 68th brigade
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68 оєбр',
        'public_official_historical',
        'official historical unit naming before transition to airmobile brigade in June 2026'
    ),
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68 окрема єгерська бригада ім. Олекси Довбуша',
        'public_official_historical',
        'official historical unit naming before transition to airmobile brigade in June 2026'
    ),
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68 окрема єгерська бригада імені Олекси Довбуша',
        'public_official_historical',
        'official historical unit naming before transition to airmobile brigade in June 2026'
    ),
    (
        '68 окрема аеромобільна бригада імені Олекси Довбуша',
        '68-ї окремої єгерської бригади',
        'public_official_historical',
        'official historical unit naming before transition to airmobile brigade in June 2026'
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
