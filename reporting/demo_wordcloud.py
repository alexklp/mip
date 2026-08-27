#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from wordcloud import WordCloud


HERE = Path(__file__).resolve().parent
OUTPUT_DIR = HERE / "output"
TOPICS_FILE = OUTPUT_DIR / "demo_topics.json"

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

GROUPS = ("ua_space", "ru_space")


def main() -> int:
    topics = json.loads(
        TOPICS_FILE.read_text(encoding="utf-8")
    )

    for group in GROUPS:
        rows = topics["groups"][group]["top_unigrams"]

        freq = {
            row["term"]: row["documents"]
            for row in rows
            if row["documents"] > 0
        }

        if not freq:
            raise RuntimeError(f"no frequencies for {group}")

        cloud = WordCloud(
            width=1600,
            height=900,
            background_color="white",
            font_path=FONT_PATH,
            max_words=60,
            collocations=False,
            prefer_horizontal=0.9,
            random_state=42,
        ).generate_from_frequencies(freq)

        out = OUTPUT_DIR / f"wordcloud_{group}_demo.png"
        cloud.to_file(str(out))

        print(
            f"{group}: terms={len(freq)} saved={out}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
