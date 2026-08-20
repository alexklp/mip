"""
validator.py — структурна (transport-level) валідація відповіді claim-extraction
проти контракту, описаного в prompt_claim_extractor_v1.txt.

НЕ перевіряє смисл (semantic coverage) — це окремий крок людської side-by-side
перевірки (див. gold_kupiansk_v1.json). Тут — лише контракт: валідний JSON,
рівно ті поля, правильні типи, offset справді відповідає evidence_span у
вихідному тексті evidence.

Invalid response НЕ виправляється мовчки (не ріжуться markdown-огорожі, не
відкидається зайвий текст навколо JSON) — якщо модель порушила контракт, це
має бути видно в transport-метриках, а не замасковано.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

REQUIRED_TOP_KEYS = {"schema_version", "evidence_id", "claims"}
REQUIRED_CLAIM_KEYS = {
    "claim_local_id",
    "claim_text",
    "evidence_span",
    "evidence_start",
    "evidence_end",
    "epistemic_status",
    "claim_time_text",
    "attribution_text",
}
ALLOWED_EPISTEMIC_STATUS = {
    "asserted",
    "reported",
    "alleged",
    "estimated",
    "opinion",
    "uncertain",
    "denied",
    "not_reported",
}
EXPECTED_SCHEMA_VERSION = "claim-extraction/1"


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    claim_count: int = 0

    def add(self, msg: str) -> None:
        self.valid = False
        self.errors.append(msg)


def validate_claim_extraction_response(
    raw_text: str, expected_evidence_id: str, evidence_text: str
) -> ValidationResult:
    """Строга перевірка сирої відповіді моделі проти контракту claim-extraction/1
    для одного evidence. Нічого не "лагодить" — json.loads() як є.
    """
    result = ValidationResult(valid=True)

    try:
        data = json.loads(raw_text if raw_text is not None else "")
    except json.JSONDecodeError as e:
        result.add(f"не валідний JSON: {e}")
        return result

    if not isinstance(data, dict):
        result.add(f"верхній рівень має бути JSON-об'єктом, отримано {type(data).__name__}")
        return result

    top_keys = set(data.keys())
    missing_top = REQUIRED_TOP_KEYS - top_keys
    extra_top = top_keys - REQUIRED_TOP_KEYS
    if missing_top:
        result.add(f"відсутні обов'язкові поля верхнього рівня: {sorted(missing_top)}")
    if extra_top:
        result.add(f"зайві поля верхнього рівня (заборонено контрактом): {sorted(extra_top)}")

    if data.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        result.add(f"schema_version = {data.get('schema_version')!r}, очікується {EXPECTED_SCHEMA_VERSION!r}")

    if data.get("evidence_id") != expected_evidence_id:
        result.add(f"evidence_id = {data.get('evidence_id')!r}, очікується {expected_evidence_id!r}")

    claims = data.get("claims")
    if not isinstance(claims, list):
        result.add(f"claims має бути списком, отримано {type(claims).__name__}")
        return result

    result.claim_count = len(claims)
    seen_local_ids: set[str] = set()

    for idx, claim in enumerate(claims):
        prefix = f"claims[{idx}]"
        if not isinstance(claim, dict):
            result.add(f"{prefix}: очікувався об'єкт, отримано {type(claim).__name__}")
            continue

        claim_keys = set(claim.keys())
        missing = REQUIRED_CLAIM_KEYS - claim_keys
        extra = claim_keys - REQUIRED_CLAIM_KEYS
        if missing:
            result.add(f"{prefix}: відсутні поля {sorted(missing)}")
        if extra:
            result.add(f"{prefix}: зайві поля (заборонено контрактом) {sorted(extra)}")

        local_id = claim.get("claim_local_id")
        if not isinstance(local_id, str) or not local_id:
            result.add(f"{prefix}: claim_local_id має бути непорожнім рядком, отримано {local_id!r}")
        elif local_id in seen_local_ids:
            result.add(f"{prefix}: claim_local_id {local_id!r} дублюється в межах цього evidence")
        else:
            seen_local_ids.add(local_id)

        claim_text = claim.get("claim_text")
        if not isinstance(claim_text, str) or not claim_text.strip():
            result.add(f"{prefix}: claim_text має бути непорожнім рядком")

        status = claim.get("epistemic_status")
        if status not in ALLOWED_EPISTEMIC_STATUS:
            result.add(
                f"{prefix}: epistemic_status {status!r} не входить у дозволений набір {sorted(ALLOWED_EPISTEMIC_STATUS)}"
            )

        for nullable_field in ("claim_time_text", "attribution_text"):
            val = claim.get(nullable_field)
            if val is not None and (
                not isinstance(val, str) or not val.strip() or val.strip().lower() == "null"
            ):
                result.add(
                    f"{prefix}: {nullable_field} має бути JSON null або непорожнім рядком "
                    f"(рядок \"null\" заборонений замість JSON null), отримано {val!r}"
                )

        span = claim.get("evidence_span")
        start = claim.get("evidence_start")
        end = claim.get("evidence_end")

        if not isinstance(span, str) or not span:
            result.add(f"{prefix}: evidence_span має бути непорожнім рядком")
        if not isinstance(start, int) or isinstance(start, bool):
            result.add(f"{prefix}: evidence_start має бути int, отримано {type(start).__name__}")
        if not isinstance(end, int) or isinstance(end, bool):
            result.add(f"{prefix}: evidence_end має бути int, отримано {type(end).__name__}")

        if (
            isinstance(span, str)
            and span
            and isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
        ):
            if not (0 <= start < end <= len(evidence_text)):
                result.add(
                    f"{prefix}: offset [{start}:{end}] поза межами evidence_text (довжина {len(evidence_text)})"
                )
            else:
                actual = evidence_text[start:end]
                if actual != span:
                    result.add(
                        f"{prefix}: evidence_text[{start}:{end}] = {actual!r} НЕ збігається з evidence_span = {span!r}"
                    )

    return result


if __name__ == "__main__":
    # Self-test на прикладі з prompt_claim_extractor_v1.txt — той самий текст
    # і ті самі offset, що вже пройшли ручну перевірку на попередньому кроці.
    example_text = (
        "Місцева влада повідомила про відключення світла у трьох районах. "
        "За словами очевидців, ситуація критична."
    )
    good_response = json.dumps(
        {
            "schema_version": "claim-extraction/1",
            "evidence_id": "EX",
            "claims": [
                {
                    "claim_local_id": "C1",
                    "claim_text": "Місцева влада повідомила про відключення світла у трьох районах",
                    "evidence_span": "Місцева влада повідомила про відключення світла у трьох районах",
                    "evidence_start": 0,
                    "evidence_end": 63,
                    "epistemic_status": "reported",
                    "claim_time_text": None,
                    "attribution_text": None,
                },
                {
                    "claim_local_id": "C2",
                    "claim_text": "Ситуація критична",
                    "evidence_span": "ситуація критична",
                    "evidence_start": 87,
                    "evidence_end": 104,
                    "epistemic_status": "opinion",
                    "claim_time_text": None,
                    "attribution_text": "очевидці",
                },
            ],
        },
        ensure_ascii=False,
    )

    ok = validate_claim_extraction_response(good_response, "EX", example_text)
    print("valid response ->", ok.valid, ok.errors, "claims:", ok.claim_count)
    assert ok.valid, ok.errors

    bad_response = json.dumps(
        {
            "schema_version": "claim-extraction/1",
            "evidence_id": "EX",
            "claims": [
                {
                    "claim_local_id": "C1",
                    "claim_text": "Ситуація критична",
                    "evidence_span": "ситуація критична",
                    "evidence_start": 80,  # свідомо зсунутий offset
                    "evidence_end": 97,
                    "epistemic_status": "made_up_status",  # не з дозволеного набору
                    "claim_time_text": None,
                    "attribution_text": None,
                }
            ],
        },
        ensure_ascii=False,
    )
    bad = validate_claim_extraction_response(bad_response, "EX", example_text)
    print("bad response ->", bad.valid, bad.errors, "claims:", bad.claim_count)
    assert not bad.valid
    assert any("epistemic_status" in e for e in bad.errors)
    assert any("evidence_text[" in e for e in bad.errors)

    print("self-test OK")
