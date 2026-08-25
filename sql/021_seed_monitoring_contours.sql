-- 021_seed_monitoring_contours.sql
--
-- Чотири стратегічні контури МІП.
-- Це content-level monitoring contours.
--
-- НЕ плутати з legacy sources.contour_id, який залишається
-- source-level provenance metadata.

INSERT INTO monitoring_contours (
    monitoring_contour_id,
    code,
    name,
    description
)
VALUES
    (
        1,
        'dshv_objects',
        'Об''єкти ДШВ ЗСУ',
        'Моніторинг об''єктів Десантно-штурмових військ ЗСУ, їх підрозділів, персоналу, айдентики, переміщень та пов''язаних інформаційних загроз.'
    ),
    (
        2,
        'world_context',
        'Світовий контекст',
        'Моніторинг міжнародної військової підтримки України, рішень партнерів, санкцій, НАТО та світового оборонно-промислового контексту.'
    ),
    (
        3,
        'national_context',
        'Загальнодержавний контекст',
        'Моніторинг подій загальнодержавного значення в Україні, рішень державного і військового керівництва, мобілізації та критичної інфраструктури.'
    ),
    (
        4,
        'enemy_media',
        'Моніторинг ворожих медіа',
        'Моніторинг російського інформаційного простору, офіційних заяв, військової тематики, пропагандних наративів, суспільних настроїв та оборонно-промислового комплексу РФ.'
    )
ON CONFLICT (monitoring_contour_id) DO UPDATE
SET
    code = EXCLUDED.code,
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    active = true;
