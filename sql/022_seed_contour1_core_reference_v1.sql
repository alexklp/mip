-- 022_seed_contour1_core_reference_v1.sql
-- Content Contours v2: Contour 1 core reference data v1.
--
-- Это стартовый проверяемый seed, а не полный справочник ДШВ.

INSERT INTO contour_reference_objects (
    monitoring_contour_id,
    object_type,
    canonical_name,
    source_note
)
VALUES
    (1, 'organization',  'Десантно-штурмові війська ЗСУ', 'MIP Contour 1 baseline v1'),
    (1, 'military_unit', '81 оаембр', 'MIP Contour 1 baseline v1'),
    (1, 'military_unit', '95 одшбр',  'MIP Contour 1 baseline v1'),
    (1, 'military_unit', '79 одшбр',  'MIP Contour 1 baseline v1'),
    (1, 'military_unit', '80 одшбр',  'MIP Contour 1 baseline v1'),
    (1, 'military_unit', '46 оаембр', 'MIP Contour 1 baseline v1'),
    (1, 'military_unit', '148 оабр',  'MIP Contour 1 baseline v1')
ON CONFLICT (monitoring_contour_id, object_type, canonical_name)
DO UPDATE SET
    active = true,
    source_note = EXCLUDED.source_note;


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
    1,
    o.object_id,
    'alias',
    'exact',
    x.reference_text,
    1,
    'MIP Contour 1 baseline v1'
FROM contour_reference_objects o
CROSS JOIN (
    VALUES
        ('ДШВ'),
        ('Десантно-штурмові війська'),
        ('Десантно-штурмові війська ЗСУ')
) AS x(reference_text)
WHERE
    o.monitoring_contour_id = 1
    AND o.object_type = 'organization'
    AND o.canonical_name = 'Десантно-штурмові війська ЗСУ'
ON CONFLICT DO NOTHING;


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
    'MIP Contour 1 baseline v1'
FROM contour_reference_objects o
WHERE
    o.monitoring_contour_id = 1
    AND o.object_type = 'military_unit'
ON CONFLICT DO NOTHING;


INSERT INTO contour_reference_entries (
    monitoring_contour_id,
    object_id,
    entry_type,
    match_mode,
    facet_code,
    reference_text,
    reference_version,
    source_note
)
VALUES
    (1, NULL, 'facet', 'semantic', 'command',
     'Командування ДШВ, командири підрозділів, офіцери та пресофіцери',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'identity',
     'Айдентика ДШВ, символіка, емблеми та знаки підрозділів',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'personnel',
     'Особовий склад, військовослужбовці, полонені та зниклі безвісти',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'movement_opsec',
     'Переміщення підрозділів і техніки, колони, ешелони, дислокація та геоприв’язка',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'phishing_malware',
     'Фішинг, шкідливе програмне забезпечення та фейкові чат-боти підрозділів',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'discrediting_ipso',
     'Інформаційно-психологічні операції, дискредитація, вкиди про втрати, оточення та втрату боєздатності',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'internal_negative',
     'Критика командування, внутрішні конфлікти та скарги на забезпечення',
     1, 'MIP Contour 1 baseline v1'),

    (1, NULL, 'facet', 'semantic', 'media_resonance',
     'Резонансні публікації засобів масової інформації про підрозділи ДШВ',
     1, 'MIP Contour 1 baseline v1')
ON CONFLICT DO NOTHING;
