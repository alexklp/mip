#!/usr/bin/env python3
"""Content Contours persistence worker v1.

v1 persistence contract:
- C1 exact object aliases:
  verified registry entries -> CONFIRMED;
  candidate registry entries -> CANDIDATE;
  conflict / unsupported verification states -> ignored;
- C2: deterministic high-precision CONFIRMED anchors for sanctions,
      military aid, defence industry;
- C3: deterministic presidential/state-decision CONFIRMED anchor.

Semantic-only candidates are intentionally not persisted by this worker.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
from collections import Counter

import psycopg


DB_DSN = "dbname=mip_dev"
ASSIGNMENT_VERSION = 1


def norm_text(title: str | None, text: str | None) -> str:
    value = f"{title or ''} {text or ''}".casefold()

    value = value.translate(
        str.maketrans(
            {
                "’": "'",
                "ʼ": "'",
                "`": "'",
                "‐": "-",
                "-": "-",
                "‒": "-",
                "–": "-",
                "—": "-",
                "−": "-",
                "ё": "е",
            }
        )
    )

    return re.sub(r"\s+", " ", value).strip()


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
    worker_path = Path(__file__).resolve()
    repo_root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
        ).strip()
    ).resolve()
    relative_path = worker_path.relative_to(repo_root)

    dirty = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            str(relative_path),
        ],
        text=True,
    ).strip()

    if dirty:
        raise RuntimeError(
            "Refusing --write with dirty contour assignment worker. "
            "Commit the worker first."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist assignments. Default is dry-run.",
    )
    parser.add_argument(
        "--c1-only",
        action="store_true",
        help="Process only C1 exact object aliases.",
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
                    o.canonical_name,
                    e.verification_status
                FROM contour_reference_entries e
                JOIN contour_reference_objects o
                  ON o.object_id = e.object_id
                WHERE e.monitoring_contour_id = 1
                  AND e.entry_type = 'alias'
                  AND e.active
                  AND e.match_mode IN ('exact', 'both')
                  AND e.verification_status IN (
                      'public_official_current',
                      'public_official_historical',
                      'public_corroborated',
                      'internal_verified',
                      'candidate'
                  )
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
                verification_status,
                alias_pattern(reference_text),
            )
            for (
                reference_id,
                object_id,
                reference_text,
                canonical_name,
                verification_status,
            )
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
                verification_status,
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
                    "status": (
                        "candidate"
                        if verification_status == "candidate"
                        else "confirmed"
                    ),
                    "evidence_type": "exact_reference",
                    "reference_id": reference_id,
                    "rule_code": None,
                    "reason": (
                        f"exact alias: {alias}; "
                        f"reference_status={verification_status}"
                    ),
                    "label": f"C1:{canonical_name}",
                }

                # Multiple aliases may identify the same object.
                # Verified evidence always wins over candidate evidence;
                # reference_id provides deterministic tie-breaking.
                old = proposed.get(key)

                row_priority = (
                    0 if row["status"] == "confirmed" else 1,
                    reference_id,
                )

                old_priority = (
                    (
                        0 if old["status"] == "confirmed" else 1,
                        old["reference_id"],
                    )
                    if old is not None
                    else None
                )

                if old is None or row_priority < old_priority:
                    proposed[key] = row

            # Calibrated deterministic C2/C3 rules.
            if args.c1_only:
                continue

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
        status_counts = Counter(row["status"] for row in proposed.values())

        print(f"content_total={len(content)}")
        print(f"c1_exact_aliases={len(aliases)}")
        print(f"assignment_version={ASSIGNMENT_VERSION}")
        print(f"mode={'WRITE' if args.write else 'DRY-RUN'}")
        print(f"scope={'C1' if args.c1_only else 'ALL'}")
        print(f"proposed_total={len(proposed)}")
        print(
            "proposed_confirmed="
            f"{status_counts.get('confirmed', 0)} "
            "proposed_candidate="
            f"{status_counts.get('candidate', 0)}"
        )

        print("\n===== PROPOSED BY SCOPE =====")
        for label, count in sorted(counts.items()):
            print(f"{count:5d}  {label}")

        if not args.write:
            print("\nNo database writes performed.")
            return 0

        revision = git_revision()

        with conn.cursor() as cur:
            written = 0

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
                    ON CONFLICT (
                        content_id,
                        monitoring_contour_id,
                        assignment_version,
                        facet_code,
                        object_id
                    )
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        evidence_type = EXCLUDED.evidence_type,
                        reference_id = EXCLUDED.reference_id,
                        embedding_model_id = EXCLUDED.embedding_model_id,
                        rule_code = EXCLUDED.rule_code,
                        score = EXCLUDED.score,
                        reason = EXCLUDED.reason,
                        code_revision = EXCLUDED.code_revision
                    WHERE
                        content_contour_assignments.status = 'candidate'
                        AND EXCLUDED.status = 'confirmed'
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
                written += cur.rowcount

        conn.commit()

        print(f"\ninserted={inserted}")
        print(f"code_revision={revision}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
