"""Детермінований контракт кешу LLM-тез для Signals.

Модуль не звертається до БД, Mamay або мережі.

Однаковий фактичний набір матеріалів має однаковий input_hash
незалежно від того, де сигнал показано: Global Signals, C1 або
інший майбутній контур.

Scope, candidate_id і presentation-заголовок навмисно не входять
до hash: вони не є входом LLM-аналізу.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


CACHE_SCHEMA_VERSION = "signal-theses-cache/1"

PROMPT_NAME = "signal_theses"
PROMPT_VERSION = "v1"

MODEL_NAME = (
    "MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf"
)

DEFAULT_STATE_ROOT = (
    Path.home()
    / ".local"
    / "state"
    / "mip"
    / "signal-theses"
)

MAX_CACHE_BYTES = 256 * 1024


class SignalThesesContractError(ValueError):
    """Порушення детермінованого контракту тез."""


def _require_text(
    value: Any,
    *,
    field: str,
    max_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
    ):
        raise SignalThesesContractError(
            f"{field}: некоректний текст"
        )

    return value.strip()


def canonical_materials(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_content_ids: Sequence[str],
) -> list[dict[str, str]]:
    """Звірити DB-матеріали з candidate і дати стабільний порядок."""

    if (
        not isinstance(expected_content_ids, Sequence)
        or isinstance(expected_content_ids, (str, bytes))
        or not expected_content_ids
    ):
        raise SignalThesesContractError(
            "expected_content_ids: порожній або некоректний список"
        )

    expected: list[str] = []

    for value in expected_content_ids:
        if not isinstance(value, str) or not value:
            raise SignalThesesContractError(
                "expected_content_ids: некоректний content_id"
            )
        expected.append(value)

    if len(expected) != len(set(expected)):
        raise SignalThesesContractError(
            "expected_content_ids: дублікати"
        )

    material_by_id: dict[str, dict[str, str]] = {}

    for row in rows:
        content_id = row.get("content_id")

        if not isinstance(content_id, str) or not content_id:
            raise SignalThesesContractError(
                "material: некоректний content_id"
            )

        if content_id in material_by_id:
            raise SignalThesesContractError(
                f"material: duplicate content_id={content_id}"
            )

        title = row.get("title") or ""
        text = row.get("text") or row.get("text_content") or ""

        if not isinstance(title, str):
            raise SignalThesesContractError(
                f"{content_id}: title не str"
            )

        if not isinstance(text, str):
            raise SignalThesesContractError(
                f"{content_id}: text не str"
            )

        material_by_id[content_id] = {
            "content_id": content_id,
            "title": title,
            "text": text,
        }

    expected_set = set(expected)
    actual_set = set(material_by_id)

    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)

        raise SignalThesesContractError(
            "material set mismatch: "
            f"missing={missing}, extra={extra}"
        )

    return [
        material_by_id[content_id]
        for content_id in sorted(expected_set)
    ]


def make_input_hash(
    materials: Sequence[Mapping[str, str]],
    *,
    prompt_version: str = PROMPT_VERSION,
    model_name: str = MODEL_NAME,
) -> str:
    """Hash точного LLM-входу, без presentation/scope metadata."""

    if not materials:
        raise SignalThesesContractError(
            "materials: порожній список"
        )

    canonical = []

    seen: set[str] = set()

    for material in materials:
        content_id = material.get("content_id")
        title = material.get("title")
        text = material.get("text")

        if (
            not isinstance(content_id, str)
            or not content_id
            or content_id in seen
        ):
            raise SignalThesesContractError(
                "materials: некоректний або дубльований content_id"
            )

        if not isinstance(title, str) or not isinstance(text, str):
            raise SignalThesesContractError(
                f"{content_id}: title/text мають бути str"
            )

        seen.add(content_id)

        canonical.append({
            "content_id": content_id,
            "title": title,
            "text": text,
        })

    canonical.sort(
        key=lambda row: row["content_id"]
    )

    payload = {
        "contract": CACHE_SCHEMA_VERSION,
        "prompt_name": PROMPT_NAME,
        "prompt_version": prompt_version,
        "model_name": model_name,
        "materials": canonical,
    }

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def validate_model_result(
    value: Any,
    *,
    material_count: int,
) -> dict[str, Any]:
    """Суворо перевірити сирий JSON Mamay до будь-якого кешування."""

    if (
        type(material_count) is not int
        or material_count < 1
    ):
        raise SignalThesesContractError(
            "material_count: некоректне значення"
        )

    if not isinstance(value, dict):
        raise SignalThesesContractError(
            "result: top-level не object"
        )

    if set(value) != {"summary", "theses"}:
        raise SignalThesesContractError(
            "result: очікуються лише summary і theses"
        )

    summary = _require_text(
        value["summary"],
        field="summary",
        max_length=1200,
    )

    theses = value["theses"]

    if (
        not isinstance(theses, list)
        or not 2 <= len(theses) <= 4
    ):
        raise SignalThesesContractError(
            "theses: очікується 2–4 тези"
        )

    normalized = []

    for index, thesis in enumerate(theses):
        if (
            not isinstance(thesis, dict)
            or set(thesis)
            != {"text", "evidence_material_ids"}
        ):
            raise SignalThesesContractError(
                f"theses[{index}]: некоректна структура"
            )

        text = _require_text(
            thesis["text"],
            field=f"theses[{index}].text",
            max_length=800,
        )

        evidence = thesis[
            "evidence_material_ids"
        ]

        if (
            not isinstance(evidence, list)
            or not 1 <= len(evidence) <= 3
            or len(evidence) != len(set(evidence))
        ):
            raise SignalThesesContractError(
                f"theses[{index}]: evidence має містити 1–3 унікальні ID"
            )

        for material_id in evidence:
            if (
                type(material_id) is not int
                or not 1 <= material_id <= material_count
            ):
                raise SignalThesesContractError(
                    f"theses[{index}]: evidence ID поза межами"
                )

        normalized.append({
            "text": text,
            "evidence_material_ids": list(evidence),
        })

    return {
        "summary": summary,
        "theses": normalized,
    }


def build_cache_record(
    *,
    input_hash: str,
    materials: Sequence[Mapping[str, str]],
    model_result: Any,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Перетворити validated model IDs на стабільні content_id."""

    _validate_hash(input_hash)

    normalized = validate_model_result(
        model_result,
        material_count=len(materials),
    )

    generated_at = (
        generated_at
        or datetime.now(timezone.utc)
    )

    if generated_at.tzinfo is None:
        raise SignalThesesContractError(
            "generated_at: timezone required"
        )

    material_list = list(materials)

    evidence_theses = []

    for thesis in normalized["theses"]:
        evidence_content_ids = [
            material_list[material_id - 1]["content_id"]
            for material_id
            in thesis["evidence_material_ids"]
        ]

        evidence_theses.append({
            "text": thesis["text"],
            "evidence_content_ids": evidence_content_ids,
        })

    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "input_hash": input_hash,
        "prompt_name": PROMPT_NAME,
        "prompt_version": PROMPT_VERSION,
        "model_name": MODEL_NAME,
        "generated_at": generated_at.astimezone(
            timezone.utc
        ).isoformat(),
        "material_count": len(material_list),
        "content_ids": [
            row["content_id"]
            for row in material_list
        ],
        "summary": normalized["summary"],
        "theses": evidence_theses,
    }


def _validate_hash(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(
            char not in "0123456789abcdef"
            for char in value
        )
    ):
        raise SignalThesesContractError(
            "input_hash: очікується sha256 hex"
        )


def validate_cache_record(
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SignalThesesContractError(
            "cache: top-level не object"
        )

    required = {
        "schema_version",
        "input_hash",
        "prompt_name",
        "prompt_version",
        "model_name",
        "generated_at",
        "material_count",
        "content_ids",
        "summary",
        "theses",
    }

    if set(value) != required:
        raise SignalThesesContractError(
            "cache: некоректний набір полів"
        )

    if value["schema_version"] != CACHE_SCHEMA_VERSION:
        raise SignalThesesContractError(
            "cache: unsupported schema"
        )

    if (
        value["prompt_name"] != PROMPT_NAME
        or value["prompt_version"] != PROMPT_VERSION
        or value["model_name"] != MODEL_NAME
    ):
        raise SignalThesesContractError(
            "cache: model/prompt identity mismatch"
        )

    _validate_hash(value["input_hash"])

    try:
        generated_at = datetime.fromisoformat(
            value["generated_at"]
        )
    except (TypeError, ValueError) as exc:
        raise SignalThesesContractError(
            "cache: generated_at invalid"
        ) from exc

    if generated_at.tzinfo is None:
        raise SignalThesesContractError(
            "cache: generated_at timezone required"
        )

    content_ids = value["content_ids"]
    material_count = value["material_count"]

    if (
        type(material_count) is not int
        or material_count < 1
        or not isinstance(content_ids, list)
        or len(content_ids) != material_count
        or len(content_ids) != len(set(content_ids))
        or any(
            not isinstance(content_id, str)
            or not content_id
            for content_id in content_ids
        )
    ):
        raise SignalThesesContractError(
            "cache: content contract invalid"
        )

    _require_text(
        value["summary"],
        field="cache.summary",
        max_length=1200,
    )

    theses = value["theses"]

    if (
        not isinstance(theses, list)
        or not 2 <= len(theses) <= 4
    ):
        raise SignalThesesContractError(
            "cache: theses invalid"
        )

    allowed = set(content_ids)

    for index, thesis in enumerate(theses):
        if (
            not isinstance(thesis, dict)
            or set(thesis)
            != {"text", "evidence_content_ids"}
        ):
            raise SignalThesesContractError(
                f"cache.theses[{index}]: invalid"
            )

        _require_text(
            thesis["text"],
            field=f"cache.theses[{index}].text",
            max_length=800,
        )

        evidence = thesis[
            "evidence_content_ids"
        ]

        if (
            not isinstance(evidence, list)
            or not 1 <= len(evidence) <= 3
            or len(evidence) != len(set(evidence))
            or any(
                content_id not in allowed
                for content_id in evidence
            )
        ):
            raise SignalThesesContractError(
                f"cache.theses[{index}]: evidence invalid"
            )

    return value


def cache_path(
    input_hash: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Path:
    _validate_hash(input_hash)

    return (
        Path(state_root)
        / "cache"
        / f"{input_hash}.json"
    )


def write_cache_record(
    record: Mapping[str, Any],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Path:
    """Атомарно записати один ready-result."""

    validated = validate_cache_record(
        dict(record)
    )

    destination = cache_path(
        validated["input_hash"],
        state_root=state_root,
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = (
        json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

    if len(payload) > MAX_CACHE_BYTES:
        raise SignalThesesContractError(
            "cache: payload too large"
        )

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=".signal-theses-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(
            temporary,
            destination,
        )

    finally:
        if temporary is not None:
            temporary.unlink(
                missing_ok=True
            )

    return destination


def read_cache_record(
    input_hash: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any] | None:
    path = cache_path(
        input_hash,
        state_root=state_root,
    )

    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None

    if len(payload) > MAX_CACHE_BYTES:
        raise SignalThesesContractError(
            "cache: payload too large"
        )

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SignalThesesContractError(
            "cache: invalid JSON"
        ) from exc

    validated = validate_cache_record(
        value
    )

    if validated["input_hash"] != input_hash:
        raise SignalThesesContractError(
            "cache: filename/hash mismatch"
        )

    return validated
