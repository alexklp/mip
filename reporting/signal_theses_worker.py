"""Один bounded Mamay job для Signals theses.

Один запуск:
1. читає поточні ready snapshots;
2. знаходить pending input_hash;
3. обирає РІВНО одну задачу;
4. повторно читає повні матеріали;
5. звіряє input_hash;
6. викликає Mamay;
7. суворо валідовує JSON;
8. атомарно записує ready cache.

Важливо:
- БД лише READ ONLY;
- worker сам НЕ керує mamay.lock;
- live запуск має виконуватися під спільним
  ~/.local/state/mip/locks/mamay.lock;
- --dry-run не звертається до Mamay.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request

from reporting.signal_theses import (
    DEFAULT_STATE_ROOT,
    MODEL_NAME,
    build_cache_record,
    canonical_materials,
    make_input_hash,
    validate_model_result,
    write_cache_record,
)
from reporting.signal_theses_queue import (
    SignalTask,
    fetch_material_rows,
    scan_queue,
)


ENDPOINT = (
    "http://127.0.0.1:8080"
    "/v1/chat/completions"
)

MAX_TOKENS = 384
TIMEOUT_SECONDS = 150.0

FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$",
    re.DOTALL,
)

FAILURE_SCHEMA_VERSION = "signal-theses-failure/1"
FAILURE_RETRY_SECONDS = (
    3600,
    6 * 3600,
    24 * 3600,
)


def _failure_path(
    input_hash: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Path:
    return (
        Path(state_root)
        / "failures"
        / f"{input_hash}.json"
    )


def _read_failure(
    input_hash: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict | None:
    path = _failure_path(
        input_hash,
        state_root=state_root,
    )

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except (
        FileNotFoundError,
        OSError,
        json.JSONDecodeError,
    ):
        return None

    if (
        not isinstance(value, dict)
        or value.get("schema_version")
            != FAILURE_SCHEMA_VERSION
        or value.get("input_hash")
            != input_hash
        or type(value.get("attempts"))
            is not int
        or value["attempts"] < 1
        or not isinstance(
            value.get("retry_after"),
            str,
        )
    ):
        return None

    try:
        retry_after = datetime.fromisoformat(
            value["retry_after"]
        )
    except ValueError:
        return None

    if retry_after.tzinfo is None:
        return None

    return value


def record_failure(
    input_hash: str,
    error: Exception,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
) -> dict:
    now = (
        now
        or datetime.now(timezone.utc)
    ).astimezone(timezone.utc)

    previous = _read_failure(
        input_hash,
        state_root=state_root,
    )

    attempts = (
        previous["attempts"] + 1
        if previous is not None
        else 1
    )

    delay = FAILURE_RETRY_SECONDS[
        min(
            attempts - 1,
            len(FAILURE_RETRY_SECONDS) - 1,
        )
    ]

    record = {
        "schema_version":
            FAILURE_SCHEMA_VERSION,
        "input_hash":
            input_hash,
        "attempts":
            attempts,
        "failed_at":
            now.isoformat(),
        "retry_after":
            (
                now
                + timedelta(seconds=delay)
            ).isoformat(),
        "error_type":
            type(error).__name__,
        "error":
            str(error)[:500],
    }

    path = _failure_path(
        input_hash,
        state_root=state_root,
    )

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".signal-theses-failure-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(
                handle.name
            )
            json.dump(
                record,
                handle,
                ensure_ascii=False,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(
                handle.fileno()
            )

        os.replace(
            temporary,
            path,
        )

    finally:
        if temporary is not None:
            temporary.unlink(
                missing_ok=True
            )

    return record


def clear_failure(
    input_hash: str,
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> None:
    _failure_path(
        input_hash,
        state_root=state_root,
    ).unlink(
        missing_ok=True
    )


def deferred_hashes(
    tasks: list[SignalTask],
    *,
    state_root: Path = DEFAULT_STATE_ROOT,
    now: datetime | None = None,
) -> set[str]:
    now = (
        now
        or datetime.now(timezone.utc)
    ).astimezone(timezone.utc)

    result = set()

    for task in tasks:
        if task.ready:
            continue

        failure = _read_failure(
            task.input_hash,
            state_root=state_root,
        )

        if failure is None:
            continue

        retry_after = datetime.fromisoformat(
            failure["retry_after"]
        ).astimezone(timezone.utc)

        if retry_after > now:
            result.add(
                task.input_hash
            )

    return result


def task_priority(
    task: SignalTask,
) -> tuple[int, int, int, str]:
    """Максимум UI-користі за один дорогий inference.

    1. input, який використовується у більшій кількості views;
    2. найвищий rank серед його views;
    3. менший input як дешевший tie-break;
    4. hash для повної детермінованості.
    """

    return (
        -len(task.references),
        min(
            ref.rank
            for ref in task.references
        ),
        task.chars,
        task.input_hash,
    )


def select_pending_task(
    tasks: list[SignalTask],
    *,
    input_hash_prefix: str | None = None,
    excluded_hashes: set[str] | None = None,
) -> SignalTask | None:
    excluded_hashes = (
        excluded_hashes
        or set()
    )

    pending = [
        task
        for task in tasks
        if not task.ready
    ]

    if input_hash_prefix is not None:
        prefix = input_hash_prefix.strip().lower()

        if not prefix:
            raise ValueError(
                "input_hash prefix порожній"
            )

        matches = [
            task
            for task in pending
            if task.input_hash.startswith(
                prefix
            )
        ]

        if not matches:
            raise ValueError(
                "pending task з таким hash "
                "не знайдено"
            )

        if len(matches) != 1:
            raise ValueError(
                "hash prefix неоднозначний"
            )

        return matches[0]

    eligible = [
        task
        for task in pending
        if task.input_hash
            not in excluded_hashes
    ]

    if not eligible:
        return None

    return min(
        eligible,
        key=task_priority,
    )


def load_task_materials(
    task: SignalTask,
) -> list[dict[str, str]]:
    rows = fetch_material_rows(
        task.content_ids
    )

    materials = canonical_materials(
        [
            rows[content_id]
            for content_id
            in task.content_ids
        ],
        expected_content_ids=list(
            task.content_ids
        ),
    )

    actual_hash = make_input_hash(
        materials
    )

    if actual_hash != task.input_hash:
        raise RuntimeError(
            "input змінився між scan і worker; "
            "cache не записано"
        )

    return materials


def build_prompt(
    materials: list[dict[str, str]],
) -> str:
    """Prompt залежить лише від hashed materials + versioned contract."""

    blocks = []

    for material_id, row in enumerate(
        materials,
        1,
    ):
        blocks.append(
            f"### material_id={material_id}\n"
            f"Заголовок: "
            f"{row['title'] or '(без заголовка)'}\n"
            f"Текст:\n"
            f"{row['text']}"
        )

    materials_block = "\n\n".join(
        blocks
    )

    return f"""Ти аналізуєш один інформаційний сигнал МІП.

Нижче наведено ВСІ унікальні матеріали,
які система віднесла до одного сигналу.

ЗАВДАННЯ:
1. Дай коротке узагальнення сигналу у 1–2 реченнях.
2. Виділи 2–4 основні змістовні тези.

СУВОРІ ПРАВИЛА:
- Використовуй виключно надані матеріали.
- Не додавай зовнішніх знань.
- Не встановлюй самостійно істинність тверджень.
- Не називай матеріали ІПСО, дезінформацією,
  координованою кампанією тощо без прямої
  підстави у наданих текстах.
- Не приписуй мету, мотив або намір, якщо вони
  прямо не сформульовані у матеріалах.
- Якщо матеріали суперечать один одному,
  відобрази цю розбіжність, а не обирай одну версію.
- Не переказуй кожен матеріал окремо.
- Об'єднуй повторювані формулювання в одну тезу.
- Тези мають бути змістовно різними.
- Для кожної тези вибери ЛИШЕ 1–3 найбільш
  показові material_id, які безпосередньо
  підтверджують саме цю тезу.
- Не перелічуй усі матеріали лише тому, що вони
  належать до того самого сюжету.
- evidence_material_ids можуть містити тільки
  реальні material_id зі списку нижче.
- Мова відповіді — українська.

ПОВЕРНИ СУВОРО ОДИН JSON-ОБ'ЄКТ:

{{
  "summary": "коротке узагальнення",
  "theses": [
    {{
      "text": "основна теза",
      "evidence_material_ids": [1, 2]
    }}
  ]
}}

Без markdown, коментарів або тексту поза JSON.

МАТЕРІАЛИ:

{materials_block}
"""


def strip_markdown_fence(
    raw_text: str,
) -> tuple[str, bool]:
    match = FENCE_RE.match(
        raw_text
    )

    if match:
        return match.group(1), True

    return raw_text, False


def parse_model_response(
    raw_text: str,
    *,
    material_count: int,
) -> tuple[dict, bool]:
    normalized, fenced = (
        strip_markdown_fence(
            raw_text
        )
    )

    try:
        parsed = json.loads(
            normalized
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Mamay повернув невалідний JSON"
        ) from exc

    validated = validate_model_result(
        parsed,
        material_count=material_count,
    )

    return validated, fenced


def call_mamay(
    prompt: str,
) -> tuple[str, float, str | None, int | None]:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "max_tokens": MAX_TOKENS,
        "seed": 42,
        "top_k": 1,
        "samplers": ["top_k"],
    }

    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(
            payload,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Content-Type":
                "application/json",
        },
        method="POST",
    )

    started = time.monotonic()

    with urllib.request.urlopen(
        request,
        timeout=TIMEOUT_SECONDS,
    ) as response:
        body = json.loads(
            response.read().decode(
                "utf-8"
            )
        )

    latency = (
        time.monotonic()
        - started
    )

    choice = body["choices"][0]

    return (
        choice["message"]["content"],
        latency,
        choice.get("finish_reason"),
        body.get(
            "usage",
            {},
        ).get(
            "completion_tokens"
        ),
    )


def run_task(
    task: SignalTask,
    *,
    dry_run: bool,
) -> int:
    materials = load_task_materials(
        task
    )

    prompt = build_prompt(
        materials
    )

    scopes = sorted({
        ref.scope
        for ref in task.references
    })

    print("=== SELECTED TASK ===")
    print(
        "input_hash:",
        task.input_hash,
    )
    print(
        "contents:",
        task.content_count,
    )
    print(
        "chars:",
        task.chars,
    )
    print(
        "references:",
        len(task.references),
    )
    print(
        "scopes:",
        ", ".join(scopes),
    )
    print(
        "prompt_chars:",
        len(prompt),
    )

    if dry_run:
        print(
            "DRY RUN: Mamay не викликався"
        )
        return 0

    (
        raw_text,
        latency,
        finish_reason,
        completion_tokens,
    ) = call_mamay(
        prompt
    )

    if finish_reason != "stop":
        raise RuntimeError(
            "Mamay completion incomplete: "
            f"finish_reason={finish_reason!r}"
        )

    (
        model_result,
        fenced,
    ) = parse_model_response(
        raw_text,
        material_count=len(
            materials
        ),
    )

    record = build_cache_record(
        input_hash=task.input_hash,
        materials=materials,
        model_result=model_result,
    )

    path = write_cache_record(
        record
    )

    print()
    print("=== RESULT ===")
    print(
        json.dumps(
            model_result,
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("=== META ===")
    print(
        "latency_s:",
        round(latency, 2),
    )
    print(
        "completion_tokens:",
        completion_tokens,
    )
    print(
        "finish_reason:",
        finish_reason,
    )
    print(
        "fence_stripped:",
        fenced,
    )
    print(
        "cache:",
        path,
    )
    print("VALIDATION: OK")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )
    parser.add_argument(
        "--input-hash",
        default=None,
        help=(
            "Опційний унікальний prefix "
            "pending input_hash"
        ),
    )

    args = parser.parse_args()

    _states, _references, tasks = (
        scan_queue()
    )

    blocked = deferred_hashes(
        tasks
    )

    task = select_pending_task(
        tasks,
        input_hash_prefix=args.input_hash,
        excluded_hashes=blocked,
    )

    if task is None:
        pending_count = sum(
            not item.ready
            for item in tasks
        )

        if pending_count:
            print(
                "QUEUE DEFERRED: "
                f"pending={pending_count}, "
                f"cooldown={len(blocked)}"
            )
        else:
            print(
                "QUEUE EMPTY: "
                "pending thesis tasks відсутні"
            )

        return 0

    try:
        result = run_task(
            task,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        if not args.dry_run:
            failure = record_failure(
                task.input_hash,
                exc,
            )

            print()
            print(
                "FAILURE DEFERRED:",
                f"attempts={failure['attempts']}",
                f"retry_after={failure['retry_after']}",
            )

        raise

    if not args.dry_run:
        clear_failure(
            task.input_hash
        )

    return result


if __name__ == "__main__":
    raise SystemExit(main())
