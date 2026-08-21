#!/usr/bin/env python3
"""
experiments/content_routing/audit_skip_keywords.py — одноразова лексична
підстраховка поверх routing_scan_results.jsonl. Не заміна семантичного
routing, а швидкий keyword grep по SKIP-зоні: якщо туди потрапило щось з
явно військовою лексикою в title — це сигнал, що пороги/prototypes треба
посунути ДО написання sql/009 + routing_worker.py. Одноразовий диагностичний
скрипт, не частина production pipeline.
"""
import json
from pathlib import Path

KEYWORDS = [
    "обстріл", "обстрел", "фронт", "зсу", "окупац", "загибл", "поранен",
    "ракет", "бпла", "дрон", "наступ", "мобіліза", "мобилиза", "атак",
    "війн", "войн", "бойов", "боєзіткнен", "боестолкновен", "генштаб",
    "міноборони", "минобороны", "санкці", "санкц",
]

RESULTS_FILE = Path(__file__).parent / "routing_scan_results.jsonl"


def run() -> None:
    total_skip = 0
    hits = []
    with RESULTS_FILE.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r["decision"] != "skip":
                continue
            total_skip += 1
            title = (r.get("title") or "").lower()
            matched = [k for k in KEYWORDS if k in title]
            if matched:
                hits.append((r, matched))

    print(f"SKIP total: {total_skip}")
    print(f"SKIP items matching war-related keywords: {len(hits)}")
    for r, matched in hits:
        print(f"  score={r['score']:+.4f} content_id={r['content_id']} matched={matched}")
        print(f"    {r['title']}")


if __name__ == "__main__":
    run()
