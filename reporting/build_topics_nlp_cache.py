from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import psycopg

import topics_snapshot as ts


CACHE_VERSION = "topics-nlp-cache/1"
CACHE = (
    Path.home()
    / ".local"
    / "state"
    / "mip"
    / "topics_nlp_cache_v1.jsonl"
)

_RULES = None
_STOPWORDS = None


def make_input_hash(
    text: str,
    source_names: list[str],
) -> str:
    payload = json.dumps(
        {
            "cache_version": CACHE_VERSION,
            "text": text,
            "source_names": sorted(source_names),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def load_done() -> set[tuple[str, str]]:
    if not CACHE.exists():
        return set()

    result = set()

    with CACHE.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)

                if (
                    row.get("cache_version")
                    == CACHE_VERSION
                    and row.get("input_hash")
                ):
                    result.add(
                        (
                            row["content_id"],
                            row["input_hash"],
                        )
                    )
            except Exception:
                continue

    return result


def init_worker() -> None:
    global _RULES
    global _STOPWORDS

    try:
        import torch
        torch.set_num_threads(1)
    except Exception:
        pass

    _RULES = ts.load_rules()
    _STOPWORDS = ts.load_stopwords()


def process_one(
    row: tuple[str, str, list[str], str],
) -> dict[str, Any]:
    content_id, text, source_names, fingerprint = row

    try:
        cleaned = ts.clean_text(
            ts.strip_feed_boilerplate(text),
            sorted(source_names),
            _RULES,
        )

        lang = ts.detect_language(cleaned)

        tokens, surfaces = (
            ts.analysis_tokens_with_surfaces(
                cleaned,
                lang,
                _STOPWORDS,
            )
        )

        if len(tokens) != len(surfaces):
            raise RuntimeError(
                "tokens/surfaces length mismatch"
            )

        return {
            "content_id": content_id,
            "cache_version": CACHE_VERSION,
            "input_hash": fingerprint,
            "lang": lang,
            "tokens": tokens,
            "surfaces": surfaces,
        }

    except Exception as exc:
        return {
            "content_id": content_id,
            "cache_version": CACHE_VERSION,
            "input_hash": fingerprint,
            "lang": "error",
            "tokens": [],
            "surfaces": [],
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }


def fetch_documents(
    since_hours: int,
) -> list[tuple[str, str, list[str], str]]:
    sql = ts.SQL.replace(
        "WHERE r.decision = 'analyze'",
        "WHERE r.decision IN ('analyze', 'maybe')",
        1,
    )

    if sql == ts.SQL:
        raise RuntimeError(
            "routing gate replacement failed"
        )

    with psycopg.connect(ts.DB_DSN) as conn:
        conn.execute(
            "SET TRANSACTION ISOLATION LEVEL "
            "REPEATABLE READ, READ ONLY"
        )

        observed_now = conn.execute(
            "SELECT now()"
        ).fetchone()[0]

        as_of = observed_now.replace(
            minute=0,
            second=0,
            microsecond=0,
        )

        start_at = as_of - timedelta(
            hours=since_hours
        )

        raw = conn.execute(
            sql,
            (
                as_of,
                start_at,
                as_of,
                as_of,
            ),
        ).fetchall()

    texts: dict[str, str] = {}
    sources: dict[str, set[str]] = defaultdict(set)

    for row in raw:
        content_id = str(row[1])
        source_name = row[3]
        text_content = row[11] or ""

        texts[content_id] = text_content
        sources[content_id].add(source_name)

    result = []

    for content_id, text in texts.items():
        names = sorted(sources[content_id])

        result.append(
            (
                content_id,
                text,
                names,
                make_input_hash(
                    text,
                    names,
                ),
            )
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--since-hours",
        type=int,
        default=60,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=25,
    )

    args = parser.parse_args()

    CACHE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    done = load_done()

    rows = fetch_documents(
        args.since_hours
    )

    todo = [
        row
        for row in rows
        if (
            row[0],
            row[3],
        ) not in done
    ]

    if args.limit is not None:
        todo = todo[:args.limit]

    print(
        "monitored documents =",
        len(rows),
    )
    print(
        "valid cached =",
        len(done),
    )
    print(
        "todo =",
        len(todo),
    )
    print(
        "workers =",
        args.workers,
    )

    if not todo:
        return 0

    started = time.perf_counter()
    written = 0
    errors = 0

    with CACHE.open(
        "a",
        encoding="utf-8",
    ) as output:
        with mp.Pool(
            args.workers,
            initializer=init_worker,
        ) as pool:
            for rec in pool.imap_unordered(
                process_one,
                todo,
                chunksize=args.chunk,
            ):
                output.write(
                    json.dumps(
                        rec,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

                written += 1

                if rec["lang"] == "error":
                    errors += 1

                if (
                    written % 25 == 0
                    or written == len(todo)
                ):
                    output.flush()

                    elapsed = (
                        time.perf_counter()
                        - started
                    )

                    print(
                        f"{written}/{len(todo)} "
                        f"errors={errors} "
                        f"rate="
                        f"{written / elapsed:.2f} docs/s",
                        flush=True,
                    )

    elapsed = time.perf_counter() - started

    print(
        f"written={written}"
    )
    print(
        f"errors={errors}"
    )
    print(
        f"elapsed={elapsed:.1f}s"
    )
    print(
        f"rate={written / elapsed:.2f} docs/s"
    )
    print(
        f"cache={CACHE}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
