-- 027_normalize_contour1_baseline_provenance.sql
--
-- Normalize verification status of the initial Contour 1 baseline.
--
-- Root DShV object / canonical public aliases:
--   public_official_current
--
-- Unit shorthand aliases + semantic facets sourced from the working
-- MIP monitoring dictionary / Technical Assignment:
--   internal_verified

UPDATE contour_reference_objects
SET
    verification_status = 'public_official_current',
    verified_on = DATE '2026-08-25',
    source_note = 'Official DShV public source, verified 2026-08-25'
WHERE monitoring_contour_id = 1
  AND canonical_name = 'Десантно-штурмові війська ЗСУ';


UPDATE contour_reference_entries
SET
    verification_status = 'public_official_current',
    verified_on = DATE '2026-08-25',
    source_note = 'Official DShV public source, verified 2026-08-25'
WHERE monitoring_contour_id = 1
  AND entry_type = 'alias'
  AND reference_text IN (
      'ДШВ',
      'Десантно-штурмові війська',
      'Десантно-штурмові війська ЗСУ'
  );


UPDATE contour_reference_entries
SET
    verification_status = 'internal_verified',
    verified_on = DATE '2026-08-25',
    source_note = 'MIP Technical Assignment monitoring dictionary, Contour 1'
WHERE monitoring_contour_id = 1
  AND verification_status = 'candidate'
  AND source_note = 'MIP Contour 1 baseline v1';
