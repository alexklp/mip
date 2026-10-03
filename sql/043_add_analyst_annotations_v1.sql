-- 043_add_analyst_annotations_v1.sql
--
-- Ручна аналітична розмітка v1.
--
-- Контракт:
--   - human annotations не змішуються з автоматичними
--     content_contour_assignments;
--   - "До звіту" не є annotation;
--   - candidate_id сигналу не є сталою identity;
--   - сигнал ідентифікується exact-набором content_id;
--   - ручне рішення для матеріалу має пріоритет над успадкованим
--     рішенням сигналу;
--   - успадкування не дублюється у content-level таблиці:
--     воно відновлюється через signal context + його exact membership.


CREATE TABLE analyst_signal_contexts (
    signal_context_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    fingerprint_version        smallint NOT NULL DEFAULT 1
        CHECK (fingerprint_version = 1),

    -- SHA-256 канонічно відсортованого exact-набору content_id.
    signal_fingerprint         text NOT NULL
        CHECK (signal_fingerprint ~ '^[0-9a-f]{64}$'),

    -- Provenance поточного представника сигналу.
    -- Не використовується як identity.
    representative_content_id  uuid NOT NULL
        REFERENCES content_items(content_id),

    created_at                 timestamptz NOT NULL DEFAULT now(),

    UNIQUE (
        fingerprint_version,
        signal_fingerprint
    )
);


CREATE TABLE analyst_signal_context_members (
    signal_context_id  uuid NOT NULL
        REFERENCES analyst_signal_contexts(signal_context_id)
        ON DELETE CASCADE,

    content_id         uuid NOT NULL
        REFERENCES content_items(content_id),

    PRIMARY KEY (
        signal_context_id,
        content_id
    )
);

CREATE INDEX idx_analyst_signal_context_members_content
    ON analyst_signal_context_members(content_id);


-- Явне рішення аналітика щодо належності сигналу до контуру.
CREATE TABLE analyst_signal_contour_annotations (
    signal_context_id      uuid NOT NULL
        REFERENCES analyst_signal_contexts(signal_context_id)
        ON DELETE CASCADE,

    monitoring_contour_id  smallint NOT NULL
        REFERENCES monitoring_contours(monitoring_contour_id),

    included               boolean NOT NULL,

    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        signal_context_id,
        monitoring_contour_id
    )
);

CREATE INDEX idx_analyst_signal_contours_contour
    ON analyst_signal_contour_annotations(
        monitoring_contour_id,
        included
    );


-- Тільки ЯВНЕ ручне рішення для окремого матеріалу.
--
-- included=true  -> матеріал належить до контуру;
-- included=false -> матеріал явно не належить до контуру.
--
-- Це рішення має пріоритет над:
--   1. contour annotation сигналу, до якого входить матеріал;
--   2. автоматичним content_contour_assignments.
CREATE TABLE analyst_content_contour_annotations (
    content_id              uuid NOT NULL
        REFERENCES content_items(content_id),

    monitoring_contour_id   smallint NOT NULL
        REFERENCES monitoring_contours(monitoring_contour_id),

    included                boolean NOT NULL,

    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        content_id,
        monitoring_contour_id
    )
);

CREATE INDEX idx_analyst_content_contours_contour
    ON analyst_content_contour_annotations(
        monitoring_contour_id,
        included
    );


-- Ручне виправлення membership матеріалу у конкретному
-- exact-контексті сигналу.
--
-- included=false -> "Не належить до сигналу".
-- included=true  -> повернення після ручного виключення.
--
-- Composite FK гарантує, що annotation можна створити лише
-- для матеріалу, який реально входив до зафіксованого signal context.
CREATE TABLE analyst_signal_membership_annotations (
    signal_context_id  uuid NOT NULL,
    content_id         uuid NOT NULL,

    included           boolean NOT NULL,

    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (
        signal_context_id,
        content_id
    ),

    FOREIGN KEY (
        signal_context_id,
        content_id
    )
        REFERENCES analyst_signal_context_members(
            signal_context_id,
            content_id
        )
        ON DELETE CASCADE
);

CREATE INDEX idx_analyst_signal_membership_content
    ON analyst_signal_membership_annotations(
        content_id,
        included
    );
