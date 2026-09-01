#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.request
from pathlib import Path

import psycopg

REPO = Path.home() / "mip"
sys.path.insert(0, str(REPO / "experiments" / "claim_extraction"))

from run_eval import strip_markdown_fence  # noqa: E402


def call_model(endpoint: str, model: str, prompt: str, timeout: float, max_tokens: int):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "seed": 42,
        "top_k": 1,
        "samplers": ["top_k"],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    latency = time.monotonic() - start

    choice = body["choices"][0]
    raw_text = choice["message"]["content"]
    finish_reason = choice.get("finish_reason")
    completion_tokens = body.get("usage", {}).get("completion_tokens")

    return raw_text, latency, finish_reason, completion_tokens


DB_DSN = "dbname=mip_dev"
FIXTURE = Path(__file__).with_name("fixture_v1.json")
GOLD = Path(__file__).with_name("gold_v1.json")
PROMPT = Path(__file__).with_name("prompt_v1.txt")

ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf"


def load_catalog() -> list[dict]:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    monitoring_contour_id,
                    code,
                    name,
                    description
                FROM monitoring_contours
                WHERE active
                ORDER BY monitoring_contour_id
            """)
            contours = cur.fetchall()

            cur.execute("""
                SELECT
                    monitoring_contour_id,
                    facet_code,
                    reference_text
                FROM contour_reference_entries
                WHERE active
                  AND facet_code IS NOT NULL
                ORDER BY monitoring_contour_id, facet_code, reference_id
            """)
            refs = cur.fetchall()

    facets: dict[int, dict[str, list[str]]] = {}

    for contour_id, facet_code, reference_text in refs:
        facets.setdefault(contour_id, {}).setdefault(
            facet_code, []
        ).append(reference_text)

    return [
        {
            "contour_id": contour_id,
            "code": code,
            "name": name,
            "description": description,
            "facets": facets.get(contour_id, {}),
        }
        for contour_id, code, name, description in contours
    ]


def validate_result(
    parsed: object,
    evidence_id: str,
    source_text: str,
    allowed_facets: dict[int, set[str]],
) -> list[str]:
    errors: list[str] = []

    if not isinstance(parsed, dict):
        return ["response is not JSON object"]

    if parsed.get("schema_version") != "contour-classification/1":
        errors.append("bad schema_version")

    if parsed.get("evidence_id") != evidence_id:
        errors.append("wrong evidence_id")

    decisions = parsed.get("decisions")

    if not isinstance(decisions, list) or len(decisions) != 4:
        return errors + ["decisions must contain exactly 4 items"]

    seen = set()

    for d in decisions:
        if not isinstance(d, dict):
            errors.append("decision item is not object")
            continue

        contour_id = d.get("contour_id")

        if contour_id not in (1, 2, 3, 4):
            errors.append(f"invalid contour_id={contour_id!r}")
            continue

        if contour_id in seen:
            errors.append(f"duplicate contour_id={contour_id}")

        seen.add(contour_id)

        decision = d.get("decision")

        if decision not in ("yes", "no", "insufficient"):
            errors.append(
                f"C{contour_id}: invalid decision={decision!r}"
            )

        facet_codes = d.get("facet_codes")

        if not isinstance(facet_codes, list):
            errors.append(f"C{contour_id}: facet_codes not list")
            facet_codes = []

        bad_facets = (
            set(facet_codes)
            - allowed_facets.get(contour_id, set())
        )

        if bad_facets:
            errors.append(
                f"C{contour_id}: unknown facets={sorted(bad_facets)}"
            )

        evidence_span = d.get("evidence_span")

        if decision == "yes":
            if not isinstance(evidence_span, str) or not evidence_span:
                errors.append(
                    f"C{contour_id}: yes requires evidence_span"
                )
            elif evidence_span not in source_text:
                errors.append(
                    f"C{contour_id}: evidence_span is not exact substring"
                )
        else:
            if evidence_span is not None:
                errors.append(
                    f"C{contour_id}: {decision} requires evidence_span=null"
                )

            if facet_codes:
                errors.append(
                    f"C{contour_id}: {decision} requires empty facet_codes"
                )

        reason = d.get("reason")

        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"C{contour_id}: empty reason")

    if seen != {1, 2, 3, 4}:
        errors.append(f"missing contour decisions: {sorted({1,2,3,4} - seen)}")

    return errors


def predicted_positive(parsed: dict) -> dict[int, set[str]]:
    result = {}

    for d in parsed["decisions"]:
        if d["decision"] == "yes":
            result[d["contour_id"]] = set(d["facet_codes"])

    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="+")
    ap.add_argument(
        "--out",
        default="/tmp/contour_llm_pilot_run.json",
    )
    args = ap.parse_args()

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    gold_data = json.loads(GOLD.read_text(encoding="utf-8"))
    template = PROMPT.read_text(encoding="utf-8")
    catalog = load_catalog()

    allowed_facets = {
        c["contour_id"]: set(c["facets"])
        for c in catalog
    }

    gold = {
        x["evidence_id"]: x
        for x in gold_data["items"]
    }

    items = fixture["items"]

    if args.ids:
        wanted = set(args.ids)
        items = [
            x for x in items
            if x["evidence_id"] in wanted
        ]

    c4_eligible: set[str] = set()

    if items:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT io.content_id
                    FROM item_occurrences io
                    JOIN sources s ON s.source_id = io.source_id
                    WHERE s.contour_id = 4
                      AND io.content_id = ANY(%s)
                    """,
                    ([x["content_id"] for x in items],),
                )
                c4_eligible = {
                    str(row[0])
                    for row in cur.fetchall()
                }

    results = []
    contour_exact_n = 0
    contour_scoreable_n = 0
    facet_exact_n = 0
    facet_scoreable_n = 0

    catalog_json = json.dumps(
        catalog,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    for item in items:
        evidence_id = item["evidence_id"]

        payload = {
            "evidence_id": evidence_id,
            "source_groups": item["source_groups"],
            "title": item["title"],
            "text": item["text"],
        }

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        prompt = (
            template
            .replace("<<CONTOUR_CATALOG_JSON>>", catalog_json)
            .replace("<<PAYLOAD_JSON>>", payload_json)
        )

        raw, latency, finish_reason, completion_tokens = call_model(
            ENDPOINT,
            MODEL,
            prompt,
            600.0,
            2048,
        )

        normalized, fence_stripped = strip_markdown_fence(raw)

        parsed = None
        parse_error = None

        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError as exc:
            parse_error = str(exc)

        source_text = (
            (item.get("title") or "")
            + "\n"
            + (item.get("text") or "")
        )

        if parsed is None:
            model_errors = [f"JSON parse error: {parse_error}"]
        else:
            model_errors = validate_result(
                parsed,
                evidence_id,
                source_text,
                allowed_facets,
            )

        c4_is_eligible = item["content_id"] in c4_eligible
        effective_parsed = copy.deepcopy(parsed)
        c4_overridden = False

        if isinstance(effective_parsed, dict) and not c4_is_eligible:
            decisions = effective_parsed.get("decisions")

            if isinstance(decisions, list):
                for decision in decisions:
                    if (
                        isinstance(decision, dict)
                        and decision.get("contour_id") == 4
                    ):
                        c4_overridden = (
                            decision.get("decision") != "no"
                            or bool(decision.get("facet_codes"))
                            or decision.get("evidence_span") is not None
                        )

                        decision["decision"] = "no"
                        decision["facet_codes"] = []
                        decision["evidence_span"] = None
                        decision["reason"] = (
                            "C4 заблоковано deterministic provenance gate: "
                            "матеріал не має occurrence з джерела "
                            "sources.contour_id=4."
                        )
                        break

        if effective_parsed is None:
            errors = [f"JSON parse error: {parse_error}"]
        else:
            errors = validate_result(
                effective_parsed,
                evidence_id,
                source_text,
                allowed_facets,
            )

        rec = {
            "evidence_id": evidence_id,
            "latency_sec": round(latency, 2),
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "fence_stripped": fence_stripped,
            "valid": not errors,
            "errors": errors,
            "model_valid": not model_errors,
            "model_errors": model_errors,
            "raw_response": raw,
            "parsed": parsed,
            "effective_parsed": effective_parsed,
            "c4_eligible": c4_is_eligible,
            "c4_overridden": c4_overridden,
        }

        g = gold[evidence_id]

        if effective_parsed is not None and not errors and g["scoreable"]:
            pred = predicted_positive(effective_parsed)
            pred_contours = set(pred)
            gold_contours = set(g["contours"])

            contour_exact = pred_contours == gold_contours

            contour_scoreable_n += 1
            contour_exact_n += int(contour_exact)

            facet_checks = []

            for contour_key, expected in g["facets"].items():
                contour_id = int(contour_key)
                facet_checks.append(
                    pred.get(contour_id, set())
                    == set(expected)
                )

            facet_exact = (
                contour_exact and all(facet_checks)
                if facet_checks
                else None
            )

            if facet_exact is not None:
                facet_scoreable_n += 1
                facet_exact_n += int(facet_exact)

            rec["gold"] = {
                "contours": sorted(gold_contours),
                "predicted_contours": sorted(pred_contours),
                "contour_exact": contour_exact,
                "facet_exact": facet_exact,
            }

        results.append(rec)

        yes = []

        if parsed is not None and not errors:
            yes = sorted(predicted_positive(parsed))

        print(
            f"[{evidence_id}] "
            f"{'OK' if not errors else 'FAIL'} "
            f"{latency:.1f}s "
            f"yes={yes} "
            f"finish={finish_reason} "
            f"tokens={completion_tokens}"
        )

        if errors:
            for err in errors:
                print(f"    - {err}")
            print(f"    RAW: {raw[:700]}")
        else:
            print(
                json.dumps(
                    parsed,
                    ensure_ascii=False,
                    indent=2,
                )
            )

            if "gold" in rec:
                print(f"    GOLD: {rec['gold']}")

    report = {
        "schema_version": "contour-pilot-report/1",
        "items": results,
        "summary": {
            "processed": len(results),
            "valid": sum(r["valid"] for r in results),
            "contour_exact": (
                f"{contour_exact_n}/{contour_scoreable_n}"
            ),
            "facet_exact": (
                f"{facet_exact_n}/{facet_scoreable_n}"
            ),
        },
    }

    Path(args.out).write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("=== SUMMARY ===")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"saved={args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
