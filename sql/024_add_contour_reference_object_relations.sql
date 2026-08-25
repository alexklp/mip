-- 024_add_contour_reference_object_relations.sql
--
-- Hierarchy / relations between monitoring reference objects.
-- Kept separately from contour_reference_objects because organizational
-- structure may change over time.

CREATE TABLE contour_reference_object_relations (
    relation_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    subject_object_id  bigint NOT NULL
        REFERENCES contour_reference_objects(object_id),

    relation_type      text NOT NULL
        CHECK (relation_type IN ('subordinate_to')),

    object_object_id   bigint NOT NULL
        REFERENCES contour_reference_objects(object_id),

    active             boolean NOT NULL DEFAULT true,
    reference_version  integer NOT NULL DEFAULT 1
        CHECK (reference_version > 0),

    source_note        text,
    created_at         timestamptz NOT NULL DEFAULT now(),

    CHECK (subject_object_id <> object_object_id),

    UNIQUE (
        subject_object_id,
        relation_type,
        object_object_id,
        reference_version
    )
);

CREATE INDEX idx_contour_reference_object_relations_subject
    ON contour_reference_object_relations (subject_object_id)
    WHERE active;

CREATE INDEX idx_contour_reference_object_relations_object
    ON contour_reference_object_relations (object_object_id)
    WHERE active;
