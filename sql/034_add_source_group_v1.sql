-- 034_add_source_group_v1.sql
--
-- Registry-level provenance источника.
-- Это НЕ страна, НЕ язык, НЕ content contour и НЕ признак достоверности.
--
-- source_group описывает информационное пространство, к которому источник
-- отнесён при ручной регистрации в МІП.
--
-- Для текущего baseline:
--   7 UA RSS                          -> ua_space
--   7 RU RSS + 72 hostile Telegram  -> ru_space
--
-- При добавлении новых источников source_group задаётся вручную.

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS source_group text NOT NULL DEFAULT 'unknown';

ALTER TABLE sources
    DROP CONSTRAINT IF EXISTS sources_source_group_check;

ALTER TABLE sources
    ADD CONSTRAINT sources_source_group_check
    CHECK (source_group IN ('ua_space', 'ru_space', 'other', 'unknown'));

-- Текущий Telegram registry целиком сформирован из утверждённого списка
-- мониторинга каналов противника.
UPDATE sources
SET source_group = 'ru_space'
WHERE source_type = 'telegram';

-- Текущий RSS baseline:
-- contour_id 2/3 = семь проверенных UA-space RSS;
-- contour_id 4   = семь проверенных RU-space RSS.
-- Это только исторический backfill текущего registry:
-- source_group НЕ выводится из contour_id в дальнейшей работе.
UPDATE sources
SET source_group = 'ua_space'
WHERE source_type = 'rss'
  AND contour_id IN (2, 3);

UPDATE sources
SET source_group = 'ru_space'
WHERE source_type = 'rss'
  AND contour_id = 4;
