-- 023_sync_contour1_official_objects_v1.sql
--
-- Contour 1 canonical objects synchronized with the current
-- public official DShV composition.
-- Verified: 2026-08-25.
--
-- Existing short aliases remain attached to their object_id.

-- Upgrade shorthand canonical names created by the initial pilot seed.
UPDATE contour_reference_objects
SET canonical_name = CASE canonical_name
        WHEN '95 одшбр'  THEN '95 окрема десантно-штурмова Поліська бригада'
        WHEN '81 оаембр' THEN '81 окрема аеромобільна Слобожанська бригада'
        WHEN '80 одшбр'  THEN '80 окрема десантно-штурмова Галицька бригада'
        WHEN '79 одшбр'  THEN '79 окрема десантно-штурмова Таврійська бригада'
        WHEN '46 оаембр' THEN '46 окрема аеромобільна Подільська бригада'
        WHEN '148 оабр'  THEN '148 окрема артилерійська Житомирська бригада'
    END,
    source_note = 'Official DShV composition, verified 2026-08-25'
WHERE monitoring_contour_id = 1
  AND canonical_name IN (
      '95 одшбр',
      '81 оаембр',
      '80 одшбр',
      '79 одшбр',
      '46 оаембр',
      '148 оабр'
  );


INSERT INTO contour_reference_objects (
    monitoring_contour_id,
    object_type,
    canonical_name,
    source_note
)
VALUES
    (1, 'corps', '8 корпус',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '95 окрема десантно-штурмова Поліська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '82 окрема десантно-штурмова Буковинська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '81 окрема аеромобільна Слобожанська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '80 окрема десантно-штурмова Галицька бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'corps', '7 корпус швидкого реагування',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '79 окрема десантно-штурмова Таврійська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '78 окрема десантно-штурмова бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '77 окрема аеромобільна Наддніпрянська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '71 окрема аеромобільна бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '68 окрема аеромобільна бригада імені Олекси Довбуша',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '46 окрема аеромобільна Подільська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '421 окремий батальйон безпілотних систем',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '25 окрема повітрянодесантна Січеславська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'training_unit', '199 навчальний центр',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '170 окремий ремонтно-відновлювальний полк',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '148 окрема артилерійська Житомирська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '147 окрема артилерійська бригада',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '132 окремий розвідувальний батальйон',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'organization', 'Командування Десантно-штурмових військ',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '135 окремий батальйон управління',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit',
        '13 окремий десантно-штурмовий батальйон імені Героя України полковника Тараса Сенюка 95 окремої десантно-штурмової Поліської бригади',
        'Official DShV composition, verified 2026-08-25'),

    (1, 'military_unit', '33 окремий інженерний полк',
        'Official DShV composition, verified 2026-08-25')
ON CONFLICT (monitoring_contour_id, object_type, canonical_name)
DO UPDATE SET
    active = true,
    source_note = EXCLUDED.source_note;


-- Every canonical object name is also a conservative exact alias.
INSERT INTO contour_reference_entries (
    monitoring_contour_id,
    object_id,
    entry_type,
    match_mode,
    reference_text,
    reference_version,
    source_note
)
SELECT
    o.monitoring_contour_id,
    o.object_id,
    'alias',
    'exact',
    o.canonical_name,
    1,
    'Official DShV composition, verified 2026-08-25'
FROM contour_reference_objects o
WHERE o.monitoring_contour_id = 1
  AND o.active
ON CONFLICT DO NOTHING;
