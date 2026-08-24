-- 014_add_shared_referent_gate.sql
-- Referent-gate contract для relation judgments (pilot contract 2 / prompt v3).
--
-- Причина: prompt-only v2 fix (шр.22.08) провалився емпірично — модель
-- показала системну інверсію burden of proof ("немає ознак відмінності" =
-- "підтверджено спільний референт") на 11+ з 20 тестових пар, включно з
-- майже дослівним negative-прикладом, який уже був у v2 промпті. Текстова
-- інструкція сама по собі не працює як механізм — потрібен окремий,
-- валідований крок.
--
-- shared_referent_status / shared_referent_evidence — nullable на рівні DDL,
-- щоб НЕ зламати історичні рядки з prompt_id=1/2 (attempt_no=1), які
-- передують цьому контракту. Обов'язковість цих полів для 'valid' рядків,
-- написаних під новим промптом (v3+), — це application-level вимога
-- (validate_relation_judgment у relation_judgment_worker.py), свідомо НЕ
-- закодована як DB CHECK саме тут, щоб не ретроактивно інвалідувати старі
-- рядки.
--
-- Сам referent gate — DB CHECK (defense in depth, не лише app-логіка): якщо
-- shared_referent_status ЗАПОВНЕНИЙ (тобто рядок написаний під новим
-- контрактом), лейбли same_fact/same_event/contradiction дозволені ТІЛЬКИ
-- при shared_referent_status='confirmed' з непорожнім shared_referent_evidence.
-- Історичні рядки (shared_referent_status IS NULL) звільнені від цієї
-- перевірки — OR коротко замикається на true.

ALTER TABLE relation_judgments
    ADD COLUMN shared_referent_status text
        CHECK (shared_referent_status IS NULL OR shared_referent_status IN ('confirmed', 'not_confirmed', 'insufficient')),
    ADD COLUMN shared_referent_evidence text
        CHECK (shared_referent_evidence IS NULL OR btrim(shared_referent_evidence) <> '');

ALTER TABLE relation_judgments
    ADD CONSTRAINT relation_judgments_referent_gate CHECK (
        shared_referent_status IS NULL
        OR relation_label IS NULL
        OR relation_label NOT IN ('same_fact', 'same_event', 'contradiction')
        OR (shared_referent_status = 'confirmed' AND shared_referent_evidence IS NOT NULL AND btrim(shared_referent_evidence) <> '')
    );
