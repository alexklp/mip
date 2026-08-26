#!/usr/bin/env python3
"""Content Contours persistence worker v1.

v1 persists only calibrated CONFIRMED assignments:
- C1: exact verified object alias;
- C2: deterministic high-precision anchors for sanctions, military aid,
      defence industry;
- C3: deterministic presidential/state-decision anchor.

Semantic-only candidates are intentionally not persisted in v1 because no
accepted candidate threshold/policy has been measured yet.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter

import psycopg


DB_DSN = "dbname=mip_dev"
ASSIGNMENT_VERSION = 1


def norm_text(title: str | None, text: str | None) -> str:
    return re.sub(
        r"\s+",
        " ",
        f"{title or ''} {text or ''}".casefold(),
    ).strip()


def alias_pattern(alias: str) -> re.Pattern:
    escaped = re.escape(norm_text(alias, None))
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.UNICODE)


RULES = (
    (
        2,
        "sanctions",
        "c2_sanctions_rf_v1",
        re.compile(
            r"санкц\w*[^.!?\n]{0,80}(?:проти|против)[^.!?\n]{0,40}"
            r"(?:росі\w*|росс\w*|\bрф\b|кремл\w*|російськ\w*|российск\w*)"
            r"|sanctions?[^.!?\n]{0,80}against[^.!?\n]{0,30}russia",
            re.I,
        ),
    ),
    (
        2,
        "military_aid",
        "c2_military_aid_ukraine_v1",
        re.compile(
            r"(?:військов\w+\s+допомог\w+|военн\w+\s+помощ\w+|military aid)"
            r"[^.!?\n]{0,120}(?:україн\w*|украин\w*|ukrain\w*)"
            r"|(?:україн\w*|украин\w*|ukrain\w*)"
            r"[^.!?\n]{0,120}"
            r"(?:військов\w+\s+допомог\w+|военн\w+\s+помощ\w+|military aid)",
            re.I,
        ),
    ),
    (
        2,
        "defence_industry",
        "c2_defence_industry_explicit_v1",
        re.compile(
            r"\bвпк\b|"
            r"оборонн\w+\s+промисл\w+|"
            r"военн\w+\s+промышлен\w+|"
            r"defen[cs]e industr\w*|"
            r"military industr\w*",
            re.I,
        ),
    ),
    (
        3,
        "state_decisions",
        "c3_state_decision_ua_v1",
        re.compile(
            r"(?:"
            r"указ\w*[^.!?\n]{0,100}"
            r"(?:президент\w+\s+україн\w*|зеленськ\w*)"
            r"|"
            r"(?:президент\w+\s+україн\w*|зеленськ\w*)"
            r"[^.!?\n]{0,100}указ\w*"
            r"|"
            r"ставк\w+[^.!?\n]{0,100}"
            r"верховн\w+[^.!?\n]{0,100}"
            r"головнокомандувач\w*"
            r")",
            re.I,
        ),
    ),
)


def git_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--short=12", "HEAD"],
        text=True,
    ).strip()


def require_clean_git() -> None:
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"],
        text=True,
    ).strip()
    if dirty:
        raise RuntimeError(
            "Refusing --write with dirty working tree. "
            "Commit the worker first."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist assignments. Default is dry-run.",
    )
    args = parser.parse_args()

    if args.write:
        require_clean_git()

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_id, title, text_content
                FROM content_items
                ORDER BY content_id
                """
            )
            content = cur.fetchall()

            cur.execute(
                """
                SELECT
                    e.reference_id,
                    e.object_id,
                    e.reference_text,
                    o.canonical_name
                FROM contour_reference_entries e
                JOIN contour_reference_objects o
                  ON o.object_id = e.object_id
                WHERE e.monitoring_contour_id = 1
                  AND e.entry_type = 'alias'
                  AND e.active
                  AND e.match_mode IN ('exact', 'both')
                ORDER BY e.reference_id
                """
            )
            aliases = cur.fetchall()

            cur.execute(
                """
                SELECT monitoring_contour_id, facet_code
                FROM contour_reference_entries
                WHERE active
                  AND facet_code IS NOT NULL
                """
            )
            registry_facets = set(cur.fetchall())

        compiled_aliases = [
            (
                reference_id,
                object_id,
                canonical_name,
                reference_text,
                alias_pattern(reference_text),
            )
            for reference_id, object_id, reference_text, canonical_name
            in aliases
        ]

        expected_facets = {
            (contour_id, facet_code)
            for contour_id, facet_code, _, _ in RULES
        }

        missing = expected_facets - registry_facets
        if missing:
            raise RuntimeError(
                f"Missing active registry facets: {sorted(missing)}"
            )

        # key:
        # content_id, contour_id, facet_code, object_id
        proposed: dict[tuple, dict] = {}

        for content_id, title, text_content in content:
            full = norm_text(title, text_content)

            # C1 exact object aliases.
            for (
                reference_id,
                object_id,
                canonical_name,
                alias,
                pattern,
            ) in compiled_aliases:
                if not pattern.search(full):
                    continue

                key = (
                    content_id,
                    1,
                    None,
                    object_id,
                )

                row = {
                    "content_id": content_id,
                    "contour_id": 1,
                    "facet_code": None,
                    "object_id": object_id,
                    "status": "confirmed",
                    "evidence_type": "exact_reference",
                    "reference_id": reference_id,
                    "rule_code": None,
                    "reason": f"exact alias: {alias}",
                    "label": f"C1:{canonical_name}",
                }

                # Multiple aliases may identify the same object.
                # Keep deterministic smallest reference_id as winning evidence.
                old = proposed.get(key)
                if old is None or reference_id < old["reference_id"]:
                    proposed[key] = row

            # Calibrated deterministic C2/C3 rules.
            for contour_id, facet_code, rule_code, pattern in RULES:
                if not pattern.search(full):
                    continue

                key = (
                    content_id,
                    contour_id,
                    facet_code,
                    None,
                )

                proposed[key] = {
                    "content_id": content_id,
                    "contour_id": contour_id,
                    "facet_code": facet_code,
                    "object_id": None,
                    "status": "confirmed",
                    "evidence_type": "deterministic_rule",
                    "reference_id": None,
                    "rule_code": rule_code,
                    "reason": f"matched deterministic rule {rule_code}",
                    "label": f"C{contour_id}:{facet_code}",
                }

        counts = Counter(row["label"] for row in proposed.values())

        print(f"content_total={len(content)}")
        print(f"c1_exact_aliases={len(aliases)}")
        print(f"assignment_version={ASSIGNMENT_VERSION}")
        print(f"mode={'WRITE' if args.write else 'DRY-RUN'}")
        print(f"proposed_total={len(proposed)}")

        print("\n===== PROPOSED BY SCOPE =====")
        for label, count in sorted(counts.items()):
            print(f"{count:5d}  {label}")

        if not args.write:
            print("\nNo database writes performed.")
            return 0

        revision = git_revision()

        with conn.cursor() as cur:
            inserted = 0

            for row in proposed.values():
                cur.execute(
                    """
                    INSERT INTO content_contour_assignments (
                        content_id,
                        monitoring_contour_id,
                        assignment_version,
                        status,
                        object_id,
                        facet_code,
                        evidence_type,
                        reference_id,
                        embedding_model_id,
                        rule_code,
                        score,
                        reason,
                        code_revision
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, NULL, %s, NULL, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        row["content_id"],
                        row["contour_id"],
                        ASSIGNMENT_VERSION,
                        row["status"],
                        row["object_id"],
                        row["facet_code"],
                        row["evidence_type"],
                        row["reference_id"],
                        row["rule_code"],
                        row["reason"],
                        revision,
                    ),
                )
                inserted += cur.rowcount

        conn.commit()

        print(f"\ninserted={inserted}")
        print(f"code_revision={revision}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
