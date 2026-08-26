#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import psycopg


DB_DSN = "dbname=mip_dev"

HERE = Path(__file__).resolve().parent
STOPWORDS_FILE = HERE / "stopwords_v1.txt"
RULES_FILE = HERE / "boilerplate_rules_v1.json"

TOKEN_RE = re.compile(
    r"[a-zа-яёіїєґ0-9]+(?:'[a-zа-яёіїєґ0-9]+)*",
    re.IGNORECASE,
)


def load_stopwords() -> set[str]:
    result = set()
    for line in STOPWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if line and not line.startswith("#"):
            result.add(line)
    return result


def load_rules() -> list[dict]:
    raw = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    rules = []
    for item in raw:
        rules.append(
            {
                **item,
                "regex": re.compile(
                    item["pattern"],
                    re.IGNORECASE | re.DOTALL,
                ),
            }
        )
    return rules


def fetch_documents(
    conn,
    source_group: str | None,
    since_hours: int | None,
) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ci.content_id,
                s.source_group,
                ci.text_content,
                array_agg(DISTINCT s.name ORDER BY s.name) AS source_names
            FROM content_items ci
            JOIN item_occurrences io
              ON io.content_id = ci.content_id
            JOIN sources s
              ON s.source_id = io.source_id
            WHERE s.source_group IN ('ua_space', 'ru_space')
              AND (%s::text IS NULL OR s.source_group = %s::text)
              AND (
                    %s::int IS NULL
                    OR ci.first_seen_at >= now() - (%s::int * interval '1 hour')
                  )
            GROUP BY
                ci.content_id,
                s.source_group,
                ci.text_content
            ORDER BY
                s.source_group,
                ci.content_id
            """,
            (
                source_group,
                source_group,
                since_hours,
                since_hours,
            ),
        )
        return cur.fetchall()


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = (
        text.replace("’", "'")
        .replace("ʼ", "'")
        .replace("`", "'")
    )
    return text


def clean_text(
    text: str,
    source_names: list[str],
    rules: list[dict],
) -> str:
    text = normalize_text(text)

    source_set = set(source_names or [])

    ordered_rules = sorted(
        rules,
        key=lambda rule: 0 if rule.get("source_names") else 1,
    )

    for rule in ordered_rules:
        restricted = rule.get("source_names")
        if restricted and not source_set.intersection(restricted):
            continue
        text = rule["regex"].sub(" ", text)

    # Reporting-only self-reference suppression.
    # Убираем название самого источника из его собственных материалов,
    # чтобы footer/promo не становились тематическими терминами.
    for source_name in source_set:
        variants = {source_name}
        variants.add(re.sub(r"\s*\([^)]*\)\s*$", "", source_name))

        for variant in variants:
            variant = variant.strip()
            if len(variant) < 3:
                continue
            text = re.sub(
                rf"(?<!\w){re.escape(variant)}(?!\w)",
                " ",
                text,
                flags=re.IGNORECASE,
            )

    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


def is_content_token(token: str, stopwords: set[str]) -> bool:
    bare = token.replace("'", "")
    if len(bare) < 2:
        return False
    if token in stopwords:
        return False
    return True


def document_terms(
    tokens: list[str],
    ngram: int,
    stopwords: set[str],
) -> Counter:
    result = Counter()

    if ngram == 1:
        for token in tokens:
            if is_content_token(token, stopwords):
                result[token] += 1
        return result

    for i in range(len(tokens) - ngram + 1):
        gram = tokens[i : i + ngram]

        # Не склеиваем слова через удалённые stopwords.
        # Для n-gram сохраняется исходная последовательность.
        # Stopword допустим внутри trigram, но не на краях.
        if not is_content_token(gram[0], stopwords):
            continue
        if not is_content_token(gram[-1], stopwords):
            continue

        content_count = sum(
            is_content_token(token, stopwords)
            for token in gram
        )
        if content_count < 2:
            continue

        result[" ".join(gram)] += 1

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only reporting NLP preparation for MIP"
    )
    parser.add_argument(
        "--source-group",
        choices=("ua_space", "ru_space"),
        default=None,
        help="default: both groups",
    )
    parser.add_argument(
        "--ngram",
        type=int,
        choices=(1, 2, 3),
        default=1,
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
    )
    parser.add_argument(
        "--min-docs",
        type=int,
        default=2,
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="filter by MIP first_seen_at; this is observation time, not publication/origin time",
    )
    parser.add_argument(
        "--lemmatize",
        action="store_true",
        help="detect document language and use RU/UA Stanza lemmas; other languages keep surface tokens",
    )
    args = parser.parse_args()

    stopwords = load_stopwords()
    rules = load_rules()

    if args.lemmatize:
        from morphology import detect_language, normalized_tokens

    tf: dict[str, Counter] = defaultdict(Counter)
    df: dict[str, Counter] = defaultdict(Counter)
    doc_counts = Counter()

    with psycopg.connect(DB_DSN) as conn:
        rows = fetch_documents(
            conn,
            args.source_group,
            args.since_hours,
        )

    for content_id, source_group, text, source_names in rows:
        cleaned = clean_text(
            text,
            list(source_names or []),
            rules,
        )
        if args.lemmatize:
            lang = detect_language(cleaned)

            if lang in ("uk", "ru"):
                tokens = normalized_tokens(
                    cleaned,
                    lang,
                    stopwords,
                )
            else:
                # Unsupported language: preserve existing surface-token behavior.
                tokens = tokenize(cleaned)
        else:
            tokens = tokenize(cleaned)

        per_doc = document_terms(
            tokens,
            args.ngram,
            stopwords,
        )

        doc_counts[source_group] += 1
        tf[source_group].update(per_doc)
        df[source_group].update(per_doc.keys())

    for group in sorted(doc_counts):
        print()
        print(f"===== {group} =====")
        print(
            f"documents={doc_counts[group]} "
            f"ngram={args.ngram} "
            f"lemmatize={args.lemmatize} "
            f"since_hours={args.since_hours}"
        )
        print(
            f"{'term':40} "
            f"{'occurrences':>11} "
            f"{'documents':>10} "
            f"{'doc_pct':>8}"
        )

        ranked = [
            term
            for term, docs in df[group].items()
            if docs >= args.min_docs
        ]
        ranked.sort(
            key=lambda term: (
                -df[group][term],
                -tf[group][term],
                term,
            )
        )

        for term in ranked[: args.top]:
            docs = df[group][term]
            occurrences = tf[group][term]
            pct = 100.0 * docs / doc_counts[group]
            print(
                f"{term[:40]:40} "
                f"{occurrences:11d} "
                f"{docs:10d} "
                f"{pct:7.1f}%"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
