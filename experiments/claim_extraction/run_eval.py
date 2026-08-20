"""
run_eval.py — прогін claim-extraction контракту на живій моделі.

Для кожного evidence з fixture підставляє промпт (str.replace, не .format —
щоб не конфліктувати з фігурними дужками JSON у шаблоні), стукає в
OpenAI-сумісний /v1/chat/completions живого llama-server, знімає
markdown-огорожу (якщо вона обгортає ВЕСЬ raw_response), перераховує
evidence_start/evidence_end через evidence_text.find(evidence_span) —
бо модель систематично (0/120 на реальному прогоні) не вміє рахувати
character offsets, хоча сам evidence_span копіює майже ідеально (99.2%) —
валідує transport-валідатором (validator.py), пише JSON-звіт.

Semantic coverage (чи збігається з gold_kupiansk_v1.json) тут НЕ рахується —
це окремий крок людської side-by-side перевірки звіту.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from validator import validate_claim_extraction_response

DEFAULT_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_MODEL = "MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf"

# Модель системно ігнорує заборону на markdown-огорожі (8/8 на
# report_kupiansk_v1_run2.json). response_format=json_object на цьому білді
# llama-server + кастомному jinja-шаблоні виявився no-op. Тому знімаємо
# огорожу ЯВНО тут — validator.py лишається строгим і незміненим.
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def strip_markdown_fence(raw_text: str):
    """Якщо raw_text ПОВНІСТЮ обгорнутий у ```/```json ... ``` — знімає
    огорожу, повертає (content, True). Якщо огорожа не покриває весь
    рядок (є текст до/після) — НЕ чіпає, повертає (raw_text, False):
    це вже інша проблема, ховати її не можна."""
    m = FENCE_RE.match(raw_text)
    if m:
        return m.group(1), True
    return raw_text, False


def resolve_offsets(parsed: dict, evidence_text: str):
    """LLM емпірично НЕ вміє рахувати character offsets (0/120 співпадінь
    на реальному прогоні, при 99.2% дослівно правильних evidence_span).
    Тому evidence_start/evidence_end від моделі ІГНОРУЄМО і рахуємо самі
    через evidence_text.find(evidence_span). Якщо span не знайдено —
    лишаємо як є (провалиться на валідації навмисно). Якщо span
    зустрічається >1 раз — беремо перше входження і позначаємо ambiguous
    (не ховаємо невизначеність)."""
    stats = {"resolved": 0, "not_found": 0, "ambiguous": 0}
    claims = parsed.get("claims")
    if not isinstance(claims, list):
        return parsed, stats
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        span = claim.get("evidence_span")
        if not isinstance(span, str) or not span:
            continue
        occurrences = evidence_text.count(span)
        if occurrences == 0:
            stats["not_found"] += 1
            continue
        if occurrences > 1:
            # Неоднозначний span — НЕ обираємо мовчки перше входження як PASS.
            # Лишаємо offset моделі як є (він однаково неправильний у 100%
            # випадків за нашими вимірами) — провалить валідацію чесно.
            stats["ambiguous"] += 1
            continue
        pos = evidence_text.find(span)
        claim["evidence_start"] = pos
        claim["evidence_end"] = pos + len(span)
        stats["resolved"] += 1
    return parsed, stats


def call_model(endpoint: str, model: str, prompt: str, timeout: float, max_tokens: int):
    """Повертає (raw_text, latency_seconds, finish_reason, completion_tokens).
    Transport-помилка (сервер недоступний, timeout, HTTP != 200) НЕ ловиться і
    не ховається — падає нагору, щоб eval зупинився на явній інфраструктурній
    проблемі, а не мовчки записав порожній результат."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST"
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


def build_prompt(template: str, evidence_id: str, evidence_text: str) -> str:
    return template.replace("<<EVIDENCE_ID>>", evidence_id).replace(
        "<<EVIDENCE_TEXT>>", evidence_text
    )


def process_response(raw_text: str, evidence_id: str, evidence_text: str):
    """Fence-strip → resolve offsets (якщо парситься) → валідація.
    Повертає (validation_result, fence_stripped, offset_stats)."""
    normalized_text, fence_stripped = strip_markdown_fence(raw_text)
    offset_stats = None
    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        parsed, offset_stats = resolve_offsets(parsed, evidence_text)
        text_to_validate = json.dumps(parsed, ensure_ascii=False)
    else:
        text_to_validate = normalized_text

    validation = validate_claim_extraction_response(text_to_validate, evidence_id, evidence_text)
    return validation, fence_stripped, offset_stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", default="fixture_kupiansk_v1.json")
    ap.add_argument("--prompt", default="prompt_claim_extractor_v1.txt")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--max-tokens", type=int, default=8192, dest="max_tokens")
    ap.add_argument("--out", default=None, help="куди писати JSON-звіт (за замовчуванням report_<epoch>.json)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="тільки зібрати промпти й вивести їх, без звернення до моделі",
    )
    args = ap.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    template = Path(args.prompt).read_text(encoding="utf-8")

    items = fixture["evidence"] if isinstance(fixture, dict) and "evidence" in fixture else fixture
    if not isinstance(items, list):
        print(f"не розпізнав структуру fixture {args.fixture} — очікував список evidence-об'єктів", file=sys.stderr)
        return 2

    results = []
    n_valid = 0
    n_total = 0

    for item in items:
        evidence_id = item["evidence_id"]
        evidence_text = item["text"]
        prompt = build_prompt(template, evidence_id, evidence_text)

        if args.dry_run:
            print(f"=== {evidence_id} — джерело={item.get('source')!r}, prompt {len(prompt)} символів ===")
            continue

        n_total += 1
        record = {"evidence_id": evidence_id, "source": item.get("source")}
        try:
            raw_text, latency, finish_reason, completion_tokens = call_model(
                args.endpoint, args.model, prompt, args.timeout, args.max_tokens
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError) as e:
            record["transport_error"] = f"{type(e).__name__}: {e}"
            results.append(record)
            print(f"[{evidence_id}] TRANSPORT ERROR: {e}", file=sys.stderr)
            continue

        validation, fence_stripped, offset_stats = process_response(raw_text, evidence_id, evidence_text)
        record.update(
            raw_response=raw_text,
            fence_stripped=fence_stripped,
            offset_stats=offset_stats,
            latency_sec=round(latency, 2),
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            valid=validation.valid,
            claim_count=validation.claim_count,
            errors=validation.errors,
        )
        results.append(record)

        status = "OK" if validation.valid else "FAIL"
        print(
            f"[{evidence_id}] {status} — {validation.claim_count} claims, {latency:.1f}s, "
            f"finish_reason={finish_reason}, completion_tokens={completion_tokens}, "
            f"fence_stripped={fence_stripped}, offset_stats={offset_stats}"
        )
        for err in validation.errors:
            print(f"    - {err}")
        if validation.valid:
            n_valid += 1

    if args.dry_run:
        return 0

    summary = {
        "n_total": n_total,
        "n_valid": n_valid,
        "n_invalid": n_total - n_valid,
        "pass_rate": round(n_valid / n_total, 3) if n_total else None,
        "n_fence_stripped": sum(1 for r in results if r.get("fence_stripped")),
    }
    print("---")
    print(f"SUMMARY: {summary}")

    out_path = Path(args.out) if args.out else Path(f"report_{int(time.time())}.json")
    out_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"звіт записано у {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
