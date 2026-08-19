-- 005_add_changed_status.sql
-- Дозволяємо позначати raw_items, що прийшли з відомим (source_id, external_ref),
-- але зі зміненим текстом — щоб уникнути orphan content_items (див. process_entry/
-- process_post policy у rss_worker.py / tg_web_worker.py). Повну історію edits
-- зробимо пізніше, зараз тільки явна відмітка "прийшло, але не оброблено".
ALTER TABLE raw_items DROP CONSTRAINT raw_items_status_check;
ALTER TABLE raw_items ADD CONSTRAINT raw_items_status_check
    CHECK (status IN ('pending', 'normalized', 'rejected', 'changed_unprocessed'));
