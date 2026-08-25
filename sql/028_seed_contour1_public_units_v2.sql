-- 028_seed_contour1_public_units_v2.sql
-- Additional publicly verified Contour 1 objects and aliases.

INSERT INTO contour_reference_objects (
    monitoring_contour_id,
    object_type,
    canonical_name,
    verification_status,
    verified_on,
    source_note
)
VALUES
    (
        1,
        'military_unit',
        '90 окремий аеромобільний батальйон імені Героя України старшого лейтенанта Івана Зубкова',
        'public_official_current',
        DATE '2026-08-25',
        'Official 81 DShV brigade / DShV public sources'
    ),
    (
        1,
        'military_unit',
        '237 окремий батальйон безпілотних систем',
        'public_official_current',
        DATE '2026-08-25',
        'Official 7th Rapid Response Corps DShV public source'
    ),
    (
        1,
        'military_unit',
        '87 окремий батальйон управління',
        'public_official_current',
        DATE '2026-08-25',
        'Official 7th Rapid Response Corps DShV public source'
    ),
    (
        1,
        'military_unit',
        '231 окремий батальйон логістики',
        'public_official_current',
        DATE '2026-08-25',
        'Official 7th Rapid Response Corps DShV public source'
    )
ON CONFLICT (monitoring_contour_id, object_type, canonical_name)
DO UPDATE SET
    active = true,
    verification_status = EXCLUDED.verification_status,
    verified_on = EXCLUDED.verified_on,
    source_note = EXCLUDED.source_note;


-- Short operational aliases from the reference list.
WITH aliases(canonical_name, alias) AS (
    VALUES
        (
            '90 окремий аеромобільний батальйон імені Героя України старшого лейтенанта Івана Зубкова',
            '90 оаемб'
        ),
        (
            '237 окремий батальйон безпілотних систем',
            '237 обБпС'
        ),
        (
            '87 окремий батальйон управління',
            '87 обу'
        ),
        (
            '231 окремий батальйон логістики',
            '231 обл'
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
    a.alias,
    1,
    'internal_verified',
    DATE '2026-08-25',
    'MIP working reference list'
FROM aliases a
JOIN contour_reference_objects o
  ON o.monitoring_contour_id = 1
 AND o.canonical_name = a.canonical_name
ON CONFLICT DO NOTHING;


-- Canonical names also become exact aliases.
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
    o.canonical_name,
    1,
    'public_official_current',
    DATE '2026-08-25',
    o.source_note
FROM contour_reference_objects o
WHERE o.monitoring_contour_id = 1
  AND o.canonical_name IN (
      '90 окремий аеромобільний батальйон імені Героя України старшого лейтенанта Івана Зубкова',
      '237 окремий батальйон безпілотних систем',
      '87 окремий батальйон управління',
      '231 окремий батальйон логістики'
  )
ON CONFLICT DO NOTHING;


-- Publicly verified hierarchy.
WITH pairs(child_name, parent_name) AS (
    VALUES
        (
            '90 окремий аеромобільний батальйон імені Героя України старшого лейтенанта Івана Зубкова',
            '81 окрема аеромобільна Слобожанська бригада'
        ),
        (
            '237 окремий батальйон безпілотних систем',
            '7 корпус швидкого реагування'
        ),
        (
            '87 окремий батальйон управління',
            '7 корпус швидкого реагування'
        ),
        (
            '231 окремий батальйон логістики',
            '7 корпус швидкого реагування'
        )
)
INSERT INTO contour_reference_object_relations (
    subject_object_id,
    relation_type,
    object_object_id,
    reference_version,
    verification_status,
    verified_on,
    source_note
)
SELECT
    child.object_id,
    'subordinate_to',
    parent.object_id,
    1,
    'public_official_current',
    DATE '2026-08-25',
    'Official DShV public sources'
FROM pairs p
JOIN contour_reference_objects child
  ON child.monitoring_contour_id = 1
 AND child.canonical_name = p.child_name
JOIN contour_reference_objects parent
  ON parent.monitoring_contour_id = 1
 AND parent.canonical_name = p.parent_name
ON CONFLICT DO NOTHING;
