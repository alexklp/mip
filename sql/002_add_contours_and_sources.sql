-- 002_add_contours_and_sources.sql

-- 1. Contour classification (грубий дефолт на рівні джерела, не фінальна класифікація контенту)
ALTER TABLE sources ADD COLUMN IF NOT EXISTS contour_id smallint CHECK (contour_id BETWEEN 1 AND 4);

-- 2. Без UNIQUE на url_or_handle ON CONFLICT нижче не спрацює
ALTER TABLE sources ADD CONSTRAINT sources_url_or_handle_key UNIQUE (url_or_handle);

-- 3. Перевірені RSS-джерела. ON CONFLICT ловить і вже існуючий рядок ТСН —
--    доставляє йому contour_id, не створюючи дубль.
INSERT INTO sources (name, url_or_handle, source_type, contour_id) VALUES
    ('ТСН',               'https://tsn.ua/rss/full.rss',                              'rss', 3),
    ('Цензор.НЕТ',        'https://assets.censor.net/rss/censor.net/rss_uk_news.xml', 'rss', 3),
    ('УНІАН',             'https://rss.unian.net/site/news_ukr.rss',                    'rss', 3),
    ('Zaxid.net',         'https://zaxid.net/rss/all.xml',                            'rss', 3),
    ('24 канал',          'https://24tv.ua/rss/all.xml',                              'rss', 3),
    ('Еспресо',           'https://espreso.tv/rss',                                   'rss', 3),
    ('BBC News Україна',  'https://feeds.bbci.co.uk/ukrainian/rss.xml',               'rss', 2)
ON CONFLICT (url_or_handle) DO UPDATE SET contour_id = EXCLUDED.contour_id;