-- 029_seed_contours_2_4_semantic_reference_v1.sql
--
-- Semantic reference baseline for strategic monitoring contours 2-4.
-- Migrated from the previously calibrated Content Contours v1 prototype set.
--
-- These are contour-level semantic references, not canonical objects.

INSERT INTO contour_reference_entries (
    monitoring_contour_id,
    object_id,
    entry_type,
    match_mode,
    facet_code,
    reference_text,
    reference_version,
    verification_status,
    verified_on,
    source_note
)
VALUES

-- ---------------------------------------------------------------------------
-- Contour 2: Світовий контекст
-- ---------------------------------------------------------------------------

(2, NULL, 'semantic_prototype', 'semantic', 'military_aid',
 'Пакети військової допомоги для України від країн-партнерів',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 2'),

(2, NULL, 'semantic_prototype', 'semantic', 'ramstein',
 'Рішення саміту Рамштайн щодо підтримки України',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 2'),

(2, NULL, 'semantic_prototype', 'semantic', 'nato_statements',
 'Заяви лідерів країн НАТО щодо війни в Україні',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 2'),

(2, NULL, 'semantic_prototype', 'semantic', 'sanctions',
 'Міжнародні санкції проти Росії',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 2'),

(2, NULL, 'semantic_prototype', 'semantic', 'defence_industry',
 'Тенденції та новини світового військово-промислового комплексу',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 2'),


-- ---------------------------------------------------------------------------
-- Contour 3: Загальнодержавний контекст
-- ---------------------------------------------------------------------------

(3, NULL, 'semantic_prototype', 'semantic', 'national_events',
 'Поточні події в Україні загальнодержавного значення',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 3'),

(3, NULL, 'semantic_prototype', 'semantic', 'state_decisions',
 'Рішення Ставки верховного головнокомандувача, укази президента України',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 3'),

(3, NULL, 'semantic_prototype', 'semantic', 'mobilization',
 'Зміни в процесах мобілізації в Україні',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 3'),

(3, NULL, 'semantic_prototype', 'semantic', 'general_staff',
 'Стратегічні зведення Генерального штабу ЗСУ',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 3'),

(3, NULL, 'semantic_prototype', 'semantic', 'mod_decisions',
 'Заяви та рішення Міністерства оборони України',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 3'),

(3, NULL, 'semantic_prototype', 'semantic', 'critical_infrastructure',
 'Удари по критичній інфраструктурі України',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 3'),


-- ---------------------------------------------------------------------------
-- Contour 4: Моніторинг ворожих медіа
-- ---------------------------------------------------------------------------

(4, NULL, 'semantic_prototype', 'semantic', 'rf_official_statements',
 'Тези з інтерв''ю Путіна та посадовців РФ щодо війни в Україні',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'rf_strikes',
 'Ураження, завдані Росією Україні за добу — російська подача',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'rf_mobilization',
 'Мобілізація в Росії',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'rf_international_cooperation',
 'Міжнародна співпраця Росії',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'occupied_territories',
 'Захоплені Росією території',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'ukrainian_strikes_rf',
 'Ураження, завдані Україною по об''єктах РФ — військова техніка, логістика, склади, особовий склад',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'rf_military_analysis',
 'Виступи російських військових аналітиків про ситуацію на фронті',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'propaganda_ipso',
 'Інформаційно-психологічні операції (ІПСО) та нові наративи російської пропаганди',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'western_aid_reaction',
 'Реакція російського медіапростору на західну військову допомогу Україні',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'elite_conflicts',
 'Внутрішні конфлікти серед політичних еліт, військового командування та воєнкорів РФ',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'rf_social_mood',
 'Суспільні настрої в Росії: протести, реакція на атаки у прикордонних областях',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'rf_defence_industry',
 'Стан військово-промислового комплексу РФ: виробництво ракет, дронів, техніки',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'sabotage_partisans',
 'Партизанська діяльність, диверсії та саботаж на об''єктах критичної інфраструктури РФ',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4'),

(4, NULL, 'semantic_prototype', 'semantic', 'foreign_support_rf',
 'Іноземні найманці та військова допомога Росії від країн-союзників',
 1, 'internal_verified', DATE '2026-08-25',
 'MIP Technical Assignment monitoring dictionary, Contour 4')

ON CONFLICT DO NOTHING;
