-- 003_seed_ru_rss_sources.sql
-- Контур 4 (вороже медіа) — RSS-джерела .ru. Доступ — через ProtonVPN-проксі,
-- див. rss_worker.py needs_proxy()/get_proxy_url() і MIP_RSS_PROXY_URL.
INSERT INTO sources (name, url_or_handle, source_type, contour_id) VALUES
    ('РИА Новости',           'https://ria.ru/export/rss2/archive/index.xml', 'rss', 4),
    ('ТАСС',                  'https://tass.ru/rss/v2.xml',                   'rss', 4),
    ('Лента.ру',              'https://lenta.ru/rss',                         'rss', 4),
    ('Газета.Ru',             'https://www.gazeta.ru/export/rss/first.xml',   'rss', 4),
    ('Комсомольская правда',  'https://www.kp.ru/rss/allsections.xml',        'rss', 4),
    ('Военное обозрение',     'https://topwar.ru/rss.xml',                    'rss', 4),
    ('ИноСМИ',                'https://inosmi.ru/export/rss2/index.xml',      'rss', 4)
ON CONFLICT (url_or_handle) DO UPDATE SET contour_id = EXCLUDED.contour_id;
