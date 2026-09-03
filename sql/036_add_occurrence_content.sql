-- 036_add_occurrence_content.sql
--
-- occurrence_content v1 -- occurrence-level full-text extraction (RSS full-text
-- retrieval + structure-preserving extraction). Строго за
-- claude/26_MIP_OccurrenceContent_v1_LLD (ревізія 2, затверджено 03.09.2026).
--
-- Адитивна міграція: нічого існуючого (content_items, item_occurrences,
-- embeddings, claims, routing, rss_worker.py, tg_web_worker.py) не змінюється
-- і не читається цим DDL. Жодного backfill/scheduler цією міграцією не
-- запускається -- лише схема.
--
-- Ключові інваріанти, зафіксовані constraint'ами:
--   1. Append-only attempt history: UNIQUE на повний identity-тюпл
--      (occurrence_id, extractor, extractor_version, extraction_profile_version,
--      attempt_no) -- ретрай ніколи не перезаписує попередню спробу in place
--      (той самий паттерн, що claim_extraction_runs/relation_judgments).
--   2. Один термінальний status (success/fetch_error/extraction_error) --
--      pending НЕ зберігається рядком; відсутність рядка = ще не робили.
--   3. CHECK на success вимагає ПОВНИЙ успішний бандл разом
--      (text_content + text_hash + structured_content + structured_content_format
--      + final_url) -- не можна отримати success із текстом, але без XML.
--   4. extractor_version (версія пакету) і extraction_profile_version (наш
--      набір kwargs) -- дві незалежні plain-колонки, не одна змішана і не
--      registry-таблиця.

CREATE TABLE occurrence_content (
    occurrence_content_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurrence_id              uuid NOT NULL REFERENCES item_occurrences(occurrence_id),

    extractor                  text NOT NULL DEFAULT 'trafilatura',
    extractor_version          text NOT NULL,
    extraction_profile_version text NOT NULL,
    attempt_no                 smallint NOT NULL DEFAULT 1 CHECK (attempt_no > 0),

    status                     text NOT NULL CHECK (status IN ('success', 'fetch_error', 'extraction_error')),
    fetch_url                  text NOT NULL,   -- те, що ми ЗАПИТУВАЛИ (= item_occurrences.external_ref)
    final_url                  text,            -- те, що реально ВІДПОВІЛО, після редіректів; NULL при fetch_error
    http_status                integer,
    raw_html                   text,

    text_content                text,
    text_hash                   text,
    structured_content          text,
    structured_content_format   text CHECK (structured_content_format IS NULL OR structured_content_format IN ('trafilatura_xml')),

    errors                      jsonb CHECK (errors IS NULL OR jsonb_typeof(errors) = 'array'),
    code_revision                text NOT NULL CHECK (btrim(code_revision) <> ''),
    latency_ms                    integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
    created_at                    timestamptz NOT NULL DEFAULT now(),

    CHECK (
        (status = 'success')
        = (
            text_content IS NOT NULL
            AND text_hash IS NOT NULL
            AND structured_content IS NOT NULL
            AND structured_content_format IS NOT NULL
            AND final_url IS NOT NULL
        )
    ),
    UNIQUE (occurrence_id, extractor, extractor_version, extraction_profile_version, attempt_no)
);

CREATE INDEX idx_occurrence_content_occurrence_id ON occurrence_content(occurrence_id);

-- Підтримує і eligibility (NOT EXISTS/COUNT по occurrence_id+identity+status),
-- і resolver (occurrence_id+identity+status='success' ORDER BY attempt_no DESC).
CREATE INDEX idx_occurrence_content_identity_status
    ON occurrence_content(occurrence_id, extractor, extractor_version, extraction_profile_version, status);
