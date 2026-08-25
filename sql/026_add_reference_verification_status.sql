-- 026_add_reference_verification_status.sql
--
-- Structured provenance / verification state for Content Contours reference data.
--
-- Verification belongs independently to:
--   1) canonical object
--   2) reference entry / alias / facet
--   3) object relation
--
-- A verified object does NOT automatically make every alias or relation verified.

ALTER TABLE contour_reference_objects
ADD COLUMN verification_status text NOT NULL DEFAULT 'candidate'
CHECK (
    verification_status IN (
        'public_official_current',
        'public_official_historical',
        'public_corroborated',
        'internal_verified',
        'candidate',
        'conflict'
    )
);

ALTER TABLE contour_reference_objects
ADD COLUMN verified_on date;


ALTER TABLE contour_reference_entries
ADD COLUMN verification_status text NOT NULL DEFAULT 'candidate'
CHECK (
    verification_status IN (
        'public_official_current',
        'public_official_historical',
        'public_corroborated',
        'internal_verified',
        'candidate',
        'conflict'
    )
);

ALTER TABLE contour_reference_entries
ADD COLUMN verified_on date;


ALTER TABLE contour_reference_object_relations
ADD COLUMN verification_status text NOT NULL DEFAULT 'candidate'
CHECK (
    verification_status IN (
        'public_official_current',
        'public_official_historical',
        'public_corroborated',
        'internal_verified',
        'candidate',
        'conflict'
    )
);

ALTER TABLE contour_reference_object_relations
ADD COLUMN verified_on date;


-- Existing rows explicitly created from official DShV composition.
UPDATE contour_reference_objects
SET
    verification_status = 'public_official_current',
    verified_on = DATE '2026-08-25'
WHERE source_note = 'Official DShV composition, verified 2026-08-25';


UPDATE contour_reference_entries
SET
    verification_status = 'public_official_current',
    verified_on = DATE '2026-08-25'
WHERE source_note = 'Official DShV composition, verified 2026-08-25';


UPDATE contour_reference_object_relations
SET
    verification_status = 'public_official_current',
    verified_on = DATE '2026-08-25'
WHERE source_note = 'Public official DShV sources, verified 2026-08-25';


CREATE INDEX idx_contour_reference_objects_verification
    ON contour_reference_objects (verification_status)
    WHERE active;

CREATE INDEX idx_contour_reference_entries_verification
    ON contour_reference_entries (verification_status)
    WHERE active;

CREATE INDEX idx_contour_reference_relations_verification
    ON contour_reference_object_relations (verification_status)
    WHERE active;
