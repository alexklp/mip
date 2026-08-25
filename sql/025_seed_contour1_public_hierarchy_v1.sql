-- 025_seed_contour1_public_hierarchy_v1.sql
-- Publicly verified DShV hierarchy only.
-- No military unit identifiers / internal reference data.

WITH pairs(child_name, parent_name) AS (
    VALUES
        ('8 корпус', 'Десантно-штурмові війська ЗСУ'),
        ('7 корпус швидкого реагування', 'Десантно-штурмові війська ЗСУ'),

        ('95 окрема десантно-штурмова Поліська бригада', '8 корпус'),
        ('82 окрема десантно-штурмова Буковинська бригада', '8 корпус'),
        ('80 окрема десантно-штурмова Галицька бригада', '8 корпус'),
        ('71 окрема аеромобільна бригада', '8 корпус'),
        ('46 окрема аеромобільна Подільська бригада', '8 корпус'),
        ('148 окрема артилерійська Житомирська бригада', '8 корпус'),

        ('25 окрема повітрянодесантна Січеславська бригада', '7 корпус швидкого реагування'),
        ('79 окрема десантно-штурмова Таврійська бригада', '7 корпус швидкого реагування'),
        ('81 окрема аеромобільна Слобожанська бригада', '7 корпус швидкого реагування'),
        ('77 окрема аеромобільна Наддніпрянська бригада', '7 корпус швидкого реагування'),
        ('78 окрема десантно-штурмова бригада', '7 корпус швидкого реагування')
)
INSERT INTO contour_reference_object_relations (
    subject_object_id,
    relation_type,
    object_object_id,
    reference_version,
    source_note
)
SELECT
    child.object_id,
    'subordinate_to',
    parent.object_id,
    1,
    'Public official DShV sources, verified 2026-08-25'
FROM pairs p
JOIN contour_reference_objects child
  ON child.monitoring_contour_id = 1
 AND child.canonical_name = p.child_name
JOIN contour_reference_objects parent
  ON parent.monitoring_contour_id = 1
 AND parent.canonical_name = p.parent_name
ON CONFLICT DO NOTHING;
