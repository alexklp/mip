-- 030_enable_semantic_object_references.sql
--
-- Canonical object names participate in both deterministic exact lookup
-- and semantic reference retrieval.
--
-- Short/operational aliases remain exact-only.

UPDATE contour_reference_entries e
SET match_mode = 'both'
FROM contour_reference_objects o
WHERE e.object_id = o.object_id
  AND e.monitoring_contour_id = 1
  AND e.entry_type = 'alias'
  AND e.reference_text = o.canonical_name
  AND e.active
  AND o.active
  AND e.match_mode = 'exact';
