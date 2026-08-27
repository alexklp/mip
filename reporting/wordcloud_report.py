#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt
import psycopg
from wordcloud import WordCloud

from morphology import detect_language, normalized_tokens
from nlp_prepare import (
    DB_DSN,
    clean_text,
    document_terms,
    fetch_documents,
    load_rules,
    load_stopwords,
    tokenize,
)


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
NOISE_FILE = Path(__file__).resolve().parent / "wordcloud_noise_v1.txt"

GROUPS = {
    "ua_space": "UA-space",
    "ru_space": "RU-space",
}


def build_df(source_group: str, since_hours: int) -> tuple[Counter, int]:
    rules = load_rules()
    stopwords = load_stopwords()

    with psycopg.connect(DB_DSN) as conn:
        rows = fetch_documents(
            conn,
            source_group=source_group,
            since_hours=since_hours,
        )

    df = Counter()
    docs = 0

    for i, (content_id, group, text, source_names) in enumerate(rows, 1):
        cleaned = clean_text(
            text,
            list(source_names or []),
            rules,
        )

        lang = detect_language(cleaned)

        if lang in ("uk", "ru"):
            tokens = normalized_tokens(
                cleaned,
                lang,
                stopwords,
            )
        else:
            tokens = tokenize(cleaned)

        terms = document_terms(
            tokens,
            ngram=1,
            stopwords=stopwords,
        )

        # Word-cloud weight = number of documents containing term.
        df.update(terms.keys())
        docs += 1

        if i % 100 == 0:
            print(
                f"{source_group}: processed={i}/{len(rows)}",
                flush=True,
            )

    return df, docs


def load_visual_noise() -> set[str]:
    result = set()

    for line in NOISE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()

        if line and not line.startswith("#"):
            result.add(line)

    return result


def make_cloud(freq: Counter) -> WordCloud:
    noise = load_visual_noise()

    # Visualization-only filtering.
    # Base NLP statistics remain unchanged.
    visual_freq = {
        term: weight
        for term, weight in freq.items()
        if not term.isdigit()
        and term.lower() not in noise
    }

    return WordCloud(
        width=1600,
        height=900,
        background_color="white",
        font_path=FONT_PATH,
        max_words=100,
        collocations=False,
        prefer_horizontal=0.9,
        random_state=42,
    ).generate_from_frequencies(visual_freq)


def main() -> int:
    since_hours = 24
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    clouds = {}

    for group, title in GROUPS.items():
        print(f"\n===== {group} =====")

        freq, docs = build_df(
            source_group=group,
            since_hours=since_hours,
        )

        cloud = make_cloud(freq)
        clouds[group] = (cloud, docs)

        out = OUTPUT_DIR / f"wordcloud_{group}_24h.png"
        cloud.to_file(str(out))

        print(f"documents={docs}")
        print(f"saved={out}")

    fig, axes = plt.subplots(
        1,
        2,
        figsize=(20, 7),
        constrained_layout=True,
    )

    for ax, (group, title) in zip(axes, GROUPS.items()):
        cloud, docs = clouds[group]

        ax.imshow(
            cloud,
            interpolation="bilinear",
        )
        ax.axis("off")
        ax.set_title(
            f"{title} | останні 24 год | n={docs}",
            fontsize=16,
        )

    pair = OUTPUT_DIR / "wordcloud_ua_vs_ru_24h.png"

    fig.savefig(
        pair,
        dpi=160,
        bbox_inches="tight",
    )

    plt.close(fig)

    print(f"\nsaved={pair}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
