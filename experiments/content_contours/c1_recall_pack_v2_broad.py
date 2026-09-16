#!/usr/bin/env python3
"""Broad recall-first C1 alias pack v2.

Adds noisy morphological / ordinal / generic DShV aliases as candidates.
Registry only. Does not create assignments.
"""

from __future__ import annotations

import argparse
import re

import psycopg


DB_DSN = "dbname=mip_dev"
SOURCE_NOTE = "c1_recall_pack_v2_broad"


BROAD_DSHV = {
    "десантники",
    "десантників",
    "десантник",
    "десантника",
    "українські десантники",
    "українських десантників",
    "десантно-штурмова бригада",
    "десантно-штурмової бригади",
    "десантно-штурмовые бригады",
    "десантно-штурмовая бригада",
    "аеромобільна бригада",
    "аеромобільної бригади",
    "аэромобильная бригада",
    "аэромобильной бригады",
    "єгерська бригада",
    "єгерської бригади",
    "егерская бригада",
    "маруновий берет",
    "марунові берети",
    "марунового берета",
    "завжди перший",
    "день дшв",
    "день десантно-штурмових військ",
    "крилата піхота",
}


def key(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def numeric_forms(name: str) -> set[str]:
    match = re.match(r"^(\d+)\b", name)
    if not match:
        return set()

    number = match.group(1)
    forms: set[str] = set()

    if "бригада" in name:
        forms |= {
            f"{number} бригада",
            f"{number} бригади",
            f"{number} бригаді",
            f"{number} бригаду",
            f"{number} бригадою",
            f"{number}-та бригада",
            f"{number}-ї бригади",
            f"{number}-й бригаді",
            f"{number}-ю бригаду",
            f"{number}-я бригада",
            f"{number}-й бригады",
            f"{number}-й бригаде",
            f"{number}-ю бригаду",
        }

    if "батальйон" in name:
        forms |= {
            f"{number} батальйон",
            f"{number} батальйону",
            f"{number} батальйоні",
            f"{number}-й батальйон",
            f"{number} батальон",
            f"{number}-й батальон",
            f"{number}-го батальона",
            f"{number}-м батальоне",
        }

    if "полк" in name:
        forms |= {
            f"{number} полк",
            f"{number} полку",
            f"{number}-й полк",
            f"{number}-го полку",
            f"{number}-го полка",
        }

    if "корпус" in name:
        forms |= {
            f"{number} корпус",
            f"{number} корпусу",
            f"{number}-й корпус",
            f"{number}-го корпусу",
            f"{number}-го корпуса",
        }

    if "центр" in name:
        forms |= {
            f"{number} центр",
            f"{number} центру",
            f"{number}-й центр",
            f"{number}-го центру",
            f"{number}-го центра",
        }

    return forms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT object_id, canonical_name
                FROM contour_reference_objects
                WHERE monitoring_contour_id = 1
                  AND active
                ORDER BY object_id
                """
            )
            objects = dict(cur.fetchall())

            cur.execute(
                """
                SELECT object_id, reference_text
                FROM contour_reference_entries
                WHERE monitoring_contour_id = 1
                  AND object_id IS NOT NULL
                  AND entry_type = 'alias'
                  AND active
                """
            )
            existing = {
                (object_id, key(reference_text))
                for object_id, reference_text in cur.fetchall()
            }

        proposed: dict[int, set[str]] = {
            object_id: numeric_forms(name)
            for object_id, name in objects.items()
        }

        proposed.setdefault(1, set()).update(BROAD_DSHV)

        pending: list[tuple[int, str]] = []

        for object_id, aliases in proposed.items():
            seen = set()

            for alias in sorted(aliases):
                normalized = key(alias)

                if not normalized or normalized in seen:
                    continue

                seen.add(normalized)

                if (object_id, normalized) in existing:
                    continue

                pending.append((object_id, alias))

        total_unique = sum(
            len({key(alias) for alias in aliases})
            for aliases in proposed.values()
        )

        print("pack_unique =", total_unique)
        print("pending =", len(pending))
        print("mode =", "WRITE" if args.write else "DRY-RUN")

        if not args.write:
            return 0

        inserted = 0

        with conn.cursor() as cur:
            for object_id, alias in pending:
                cur.execute(
                    """
                    INSERT INTO contour_reference_entries (
                        monitoring_contour_id,
                        object_id,
                        entry_type,
                        match_mode,
                        facet_code,
                        reference_text,
                        active,
                        reference_version,
                        source_note,
                        verification_status,
                        verified_on
                    )
                    VALUES (
                        1,
                        %s,
                        'alias',
                        'exact',
                        NULL,
                        %s,
                        true,
                        1,
                        %s,
                        'candidate',
                        NULL
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        object_id,
                        alias,
                        SOURCE_NOTE,
                    ),
                )
                inserted += cur.rowcount

        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM contour_reference_entries
                WHERE monitoring_contour_id = 1
                  AND source_note = %s
                  AND verification_status = 'candidate'
                  AND active
                """,
                (SOURCE_NOTE,),
            )
            rows_in_db = cur.fetchone()[0]

        print("inserted =", inserted)
        print("pack_rows_in_db =", rows_in_db)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
