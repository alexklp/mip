#!/usr/bin/env python3
"""
Pure logic shared by the Relation Gold Set v1 toolkit.

Депендесі: тільки stdlib (numpy НЕ обов'язковий тут). Жодного psycopg,
жодного DB-доступу, жодних LLM-викликів. Це зроблено навмисно: увесь
методологічно критичний код (стратифікація, детермінований відбір, порядок
анотації, приховані повтори, ваги, довірчі інтервали) має бути тестованим
на синтетичних фікстурах без сервера.

Ключові інваріанти, які тут закодовані:

1. СТРАТИ Є РОЗБИТТЯМ (partition). assign_stratum() повертає рівно одну
   страту або None. Перекриття страт зламало б ваги (одна пара порахувалась
   би двічі), тому пріоритет фіксований і покритий тестом.

2. ВІДБІР ДЕТЕРМІНОВАНИЙ І НЕ ЗАЛЕЖИТЬ ВІД ПОРЯДКУ ОБХОДУ. Замість
   класичного reservoir sampling (який залежить від порядку блоків матриці)
   використовується "top-n за стабільним хешем": для кожної страти беруться
   n пар з найменшим sha256(seed|stratum|pair_key). Той самий corpus і той
   самий seed дають той самий результат незалежно від chunk_size і порядку
   груп.

3. ПОРЯДОК АНОТАЦІЇ PREFIX-BALANCED. Пари видаються round-robin по стратах,
   тому БУДЬ-ЯКИЙ префікс послідовності приблизно пропорційний за стратами.
   Практичний наслідок: анотатор може зупинитися на 60% і датасет усе одно
   аналізовний, просто з ширшими інтервалами.

4. ЧАС НЕ ВИКОРИСТОВУЄТЬСЯ. Ні для eligibility, ні для стратифікації, ні для
   ranking. first_seen експортується лише як diagnostic-поле і НЕ показується
   анотатору (див. ANNOTATION_HANDBOOK.md, anti-leakage rule 3).
"""
from __future__ import annotations

import hashlib
import math
from typing import Iterable, Sequence

SCHEMA_VERSION = "relation-gold-set/1"

# ---------------------------------------------------------------------------
# Стратифікація
# ---------------------------------------------------------------------------

# Пороги. Свідомо НЕ production thresholds — це межі вимірювальних комірок.
CLAIM_HIGH = 0.80          # поточний retrieval floor candidate v1
CLAIM_MID = 0.65
CLAIM_FLOOR = 0.50         # нижче цього не семплюємо взагалі
NEAR_DUP = 0.97
CONTENT_LOW = 0.50
CONTENT_MID = 0.65
CONTENT_DEEP = 0.70        # вищий поріг для найглибшої recall-страти

MAIN_FRAME = "main"                # cross-source-group: поточний pilot scope
EXPLORATORY_FRAME = "exploratory"  # same-source-group: окремий діагностичний frame

# stratum_id -> (frame, human label, чи є комірка вирішальною для рішення)
STRATA: dict[str, dict] = {
    "S1_HIGH_CLAIM_LOW_CONTENT": {
        "frame": MAIN_FRAME,
        "label": "claim>=0.80, content<0.50",
        "purpose": "очікувана generic false-positive зона; кандидат на deterministic reject",
        "decision_critical": True,
    },
    "S2_HIGH_CLAIM_MID_CONTENT": {
        "frame": MAIN_FRAME,
        "label": "claim>=0.80, content 0.50-0.65",
        "purpose": "очікувано неоднозначна зона; кандидат на Mamay",
        "decision_critical": False,
    },
    "S3_HIGH_CLAIM_HIGH_CONTENT": {
        "frame": MAIN_FRAME,
        "label": "claim>=0.80, content>=0.65",
        "purpose": "очікувано сильні кандидати",
        "decision_critical": False,
    },
    "S4_MID_CLAIM_HIGH_CONTENT": {
        "frame": MAIN_FRAME,
        "label": "claim 0.65-0.80, content>=0.65",
        "purpose": "RECALL-страта: те, чого поточний retrieval >=0.80 не бачить взагалі",
        "decision_critical": True,
    },
    "S5_MID_CLAIM_MID_CONTENT": {
        "frame": MAIN_FRAME,
        "label": "claim 0.65-0.80, content 0.50-0.65",
        "purpose": "контроль для S4",
        "decision_critical": False,
    },
    "S6_LOW_CLAIM_HIGH_CONTENT": {
        "frame": MAIN_FRAME,
        "label": "claim 0.50-0.65, content>=0.70",
        "purpose": "глибокий recall-зонд: чи рятує content там, де claim давно здався",
        "decision_critical": False,
    },
    "S7_NEAR_DUP": {
        "frame": MAIN_FRAME,
        "label": "claim>=0.97 (будь-який content)",
        "purpose": "near-duplicate смуга: справжній same_fact чи шаблон?",
        "decision_critical": False,
    },
    "S8_HUB_X_HUB": {
        "frame": MAIN_FRAME,
        "label": "claim>=0.80, обидва claims — hubs",
        "purpose": "чи можна безпечно дропати hub x hub",
        "decision_critical": True,
    },
    "S9_SAME_GROUP_EXPLORATORY": {
        "frame": EXPLORATORY_FRAME,
        "label": "той самий source-group, різний content, claim>=0.80",
        "purpose": "діагностика дірки cross-source-only eligibility; НЕ входить у main population",
        "decision_critical": False,
    },
}

MAIN_STRATA = tuple(s for s, cfg in STRATA.items() if cfg["frame"] == MAIN_FRAME)
EXPLORATORY_STRATA = tuple(s for s, cfg in STRATA.items() if cfg["frame"] == EXPLORATORY_FRAME)

# Дефолтні розміри вибірки. Нерівномірні НАВМИСНО.
#
# Числа взяті не зі стелі, а з error budget (див. max_errors_for_lower_bound):
#   n=20..34 -> НЕ здатні сертифікувати 90% навіть з бездоганною коміркою
#   n=35     -> сертифікує лише при 0 помилок
#   n=40     -> сертифікує при 0 помилок (із запасом на 1-2 unusable)
#   n=60     -> терпить 1 помилку
#   n=100    -> терпить 4 помилки
# Тому КОЖНА decision_critical страта має >= 40, інакше вона фізично не може
# дати відповідь "так, це безпечна reject-зона" — лише "схоже на те".
# Не-критичні страти лишаються меншими: вони описують, а не сертифікують.
DEFAULT_ALLOCATION: dict[str, int] = {
    "S1_HIGH_CLAIM_LOW_CONTENT": 40,   # decision-critical
    "S2_HIGH_CLAIM_MID_CONTENT": 32,
    "S3_HIGH_CLAIM_HIGH_CONTENT": 32,
    "S4_MID_CLAIM_HIGH_CONTENT": 40,   # decision-critical
    "S5_MID_CLAIM_MID_CONTENT": 16,
    "S6_LOW_CLAIM_HIGH_CONTENT": 16,
    "S7_NEAR_DUP": 16,
    "S8_HUB_X_HUB": 40,                # decision-critical
    "S9_SAME_GROUP_EXPLORATORY": 16,
}

# Мінімальний n, за якого Wilson lower bound може досягти 0.90 хоча б теоретично.
MIN_N_TO_CERTIFY_90 = 35


def assign_stratum(
    claim_score: float,
    content_score: float,
    hub_a: bool,
    hub_b: bool,
    same_group: bool,
) -> str | None:
    """Повертає РІВНО ОДНУ страту або None (пара не семплюється).

    Пріоритет фіксований і важливий: страти мають бути неперетинними,
    інакше ваги populations/sample зламаються (одна пара порахувалась би у
    двох стратах). Порядок:

      0. same_group -> S9 (окремий exploratory frame, ніколи не змішується з main)
      1. claim >= 0.97 -> S7 (near-dup питання окреме; має пріоритет над hub)
      2. hub x hub AND claim >= 0.80 -> S8
      3. claim >= 0.80 -> S1 / S2 / S3 за content
      4. claim 0.65-0.80 -> S4 / S5 за content (content < 0.50 не семплюємо)
      5. claim 0.50-0.65 -> S6 лише при content >= 0.70
      6. інакше None
    """
    if claim_score < CLAIM_FLOOR:
        return None

    if same_group:
        # Exploratory frame свідомо вужчий: тільки висока claim-схожість.
        if claim_score >= CLAIM_HIGH:
            return "S9_SAME_GROUP_EXPLORATORY"
        return None

    if claim_score >= NEAR_DUP:
        return "S7_NEAR_DUP"

    if claim_score >= CLAIM_HIGH:
        if hub_a and hub_b:
            return "S8_HUB_X_HUB"
        if content_score < CONTENT_LOW:
            return "S1_HIGH_CLAIM_LOW_CONTENT"
        if content_score < CONTENT_MID:
            return "S2_HIGH_CLAIM_MID_CONTENT"
        return "S3_HIGH_CLAIM_HIGH_CONTENT"

    if claim_score >= CLAIM_MID:
        if content_score >= CONTENT_MID:
            return "S4_MID_CLAIM_HIGH_CONTENT"
        if content_score >= CONTENT_LOW:
            return "S5_MID_CLAIM_MID_CONTENT"
        return None

    # claim у [0.50, 0.65)
    if content_score >= CONTENT_DEEP:
        return "S6_LOW_CLAIM_HIGH_CONTENT"
    return None


# ---------------------------------------------------------------------------
# Детермінований відбір
# ---------------------------------------------------------------------------

def pair_key(claim_id_a, claim_id_b) -> str:
    """Канонічний ключ пари, незалежний від порядку сторін.

    Той самий інваріант, що CHECK (claim_id_a < claim_id_b) у candidate_pairs:
    пара — це невпорядкована множина. Використовується і для дедуплікації, і
    для звірки прихованих повторів (де сторони МІНЯЮТЬСЯ місцями навмисно).
    """
    a, b = str(claim_id_a), str(claim_id_b)
    if a > b:
        a, b = b, a
    return f"{a}|{b}"


def stable_hash(*parts: object) -> int:
    """Детермінований 64-бітний хеш від рядкового представлення частин.

    sha256 замість hash() навмисно: вбудований hash() рандомізований між
    процесами (PYTHONHASHSEED), що зламало б відтворюваність.
    """
    payload = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def selection_rank(seed: int, stratum: str, key: str) -> int:
    """Ранг пари всередині страти. Менший = раніше потрапляє у вибірку."""
    return stable_hash(seed, stratum, key)


class StratumSelector:
    """Тримає n пар з найменшим selection_rank для однієї страти.

    Чому не reservoir: reservoir з rng залежить від порядку, у якому пари
    приходять (а він залежить від chunk_size і порядку груп). Тут результат
    залежить ЛИШЕ від (seed, stratum, pair_key), тому будь-який порядок
    обходу і будь-який chunk_size дають ідентичну вибірку.

    Також рахує population (скільки пар СТРАТИ побачено загалом) — це
    потрібно для ваг у аналізі; без нього стратифікована вибірка не
    конвертується в population-level оцінки.
    """

    __slots__ = ("stratum", "capacity", "seed", "population", "_heap", "_max_claim_uses")

    def __init__(self, stratum: str, capacity: int, seed: int):
        self.stratum = stratum
        self.capacity = capacity
        self.seed = seed
        self.population = 0
        self._heap: list[tuple[int, str, dict]] = []  # max-heap за rank через -rank

    def offer(self, key: str, item: dict) -> None:
        """Пропонує пару. population інкрементується ЗАВЖДИ, навіть якщо пара
        не потрапила у вибірку — це і є точний розмір страти."""
        import heapq

        self.population += 1
        if self.capacity <= 0:
            return
        rank = selection_rank(self.seed, self.stratum, key)
        entry = (-rank, key, item)
        if len(self._heap) < self.capacity:
            heapq.heappush(self._heap, entry)
        elif -rank > self._heap[0][0]:
            heapq.heapreplace(self._heap, entry)

    def selected(self) -> list[dict]:
        """Відсортовано за rank зростанням — детерміновано і стабільно."""
        ordered = sorted(self._heap, key=lambda e: (-e[0], e[1]))
        return [item for _neg, _key, item in ordered]


def fill_allocation(
    ranked_by_stratum: dict[str, list[dict]],
    allocation: dict[str, int],
    max_uses: int,
    exempt_strata: Iterable[str] = (),
) -> tuple[list[dict], dict]:
    """Набирає кожну страту до її allocation, ПОВАЖАЮЧИ кап на claim.

    Чому не "відібрати top-n, потім відсіяти за капом": тоді страта просто
    недобирає розмір (у щільних кластерах кап зрізає половину), і
    decision_critical комірка тихо опускається нижче n=35, після чого вже
    нічого не сертифікує. Тут навпаки: беремо наступного за рангом кандидата,
    доки не набрано allocation.

    Порядок обходу страт фіксований і детермінований: спершу
    decision_critical (їм потрібен гарантований розмір), потім решта за
    іменем. У межах страти — за selection_rank.
    """
    exempt = set(exempt_strata)
    uses: dict[str, int] = {}
    chosen: list[dict] = []
    stats: dict[str, dict] = {}

    def stratum_order(name: str) -> tuple[int, str]:
        critical = STRATA.get(name, {}).get("decision_critical", False)
        return (0 if critical else 1, name)

    for stratum in sorted(ranked_by_stratum, key=stratum_order):
        want = allocation.get(stratum, 0)
        candidates = ranked_by_stratum[stratum]
        taken = 0
        skipped = 0
        for item in candidates:
            if taken >= want:
                break
            a, b = str(item["claim_id_a"]), str(item["claim_id_b"])
            if stratum not in exempt:
                if uses.get(a, 0) >= max_uses or uses.get(b, 0) >= max_uses:
                    skipped += 1
                    continue
            chosen.append(item)
            uses[a] = uses.get(a, 0) + 1
            uses[b] = uses.get(b, 0) + 1
            taken += 1
        stats[stratum] = {
            "requested": want,
            "candidates": len(candidates),
            "taken": taken,
            "skipped_by_cap": skipped,
            "short_by": max(0, want - taken),
        }
    return chosen, stats


def enforce_claim_use_cap(
    items: Sequence[dict],
    max_uses: int,
    exempt_strata: Iterable[str] = (),
) -> tuple[list[dict], list[dict]]:
    """Обмежує, скільки разів один claim_id може зустрітися у вибірці.

    Навіщо: без цього hub-claim ("Є постраждалі", degree=82) може окупувати
    десятки пар вибірки. Тоді 40 "незалежних" міток насправді описують 3-4
    claims — псевдореплікація, і будь-який довірчий інтервал бреше.

    exempt_strata: страти, де повторення claims — це і Є предмет вимірювання
    (S8_HUB_X_HUB), тому там кап не застосовується. Це свідомий компроміс;
    аналізатор окремо звітує ефективну кількість унікальних claims.

    Порядок обходу — вхідний (тобто за selection_rank), тому результат
    детермінований. Повертає (kept, dropped).
    """
    exempt = set(exempt_strata)
    uses: dict[str, int] = {}
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in items:
        if item["stratum"] in exempt:
            kept.append(item)
            for cid in (str(item["claim_id_a"]), str(item["claim_id_b"])):
                uses[cid] = uses.get(cid, 0) + 1
            continue
        a, b = str(item["claim_id_a"]), str(item["claim_id_b"])
        if uses.get(a, 0) >= max_uses or uses.get(b, 0) >= max_uses:
            dropped.append(item)
            continue
        kept.append(item)
        uses[a] = uses.get(a, 0) + 1
        uses[b] = uses.get(b, 0) + 1
    return kept, dropped


# ---------------------------------------------------------------------------
# Порядок анотації + приховані повтори
# ---------------------------------------------------------------------------

def build_annotation_order(items: Sequence[dict], seed: int) -> list[dict]:
    """Prefix-balanced детермінований порядок.

    Дві вимоги одночасно:
      (a) сусідні елементи мають бути з РІЗНИХ страт — інакше анотатор
          відчуває "зараз пішла легка смуга" і це leakage через порядок;
      (b) будь-який ПРЕФІКС має бути приблизно пропорційним за стратами —
          щоб зупинка на 60% лишала аналізовний датасет.

    Round-robin по стратах (кожна страта віддає по одному елементу за коло,
    у порядку свого stable-hash перемішування) задовольняє обидві.
    """
    by_stratum: dict[str, list[dict]] = {}
    for item in items:
        by_stratum.setdefault(item["stratum"], []).append(item)

    for stratum, group in by_stratum.items():
        group.sort(key=lambda it: stable_hash(seed, "order", stratum, it["pair_key"]))

    # Страти з більшою кількістю елементів мають віддавати частіше, інакше
    # маленькі страти закінчаться рано і хвіст стане незбалансованим.
    # Розкладаємо кожну страту рівномірно по [0,1) і сортуємо глобально.
    scheduled: list[tuple[float, int, dict]] = []
    for stratum, group in sorted(by_stratum.items()):
        n = len(group)
        for pos, item in enumerate(group):
            # (pos + 0.5)/n — рівномірна сітка; страта з n=40 дає 40 точок,
            # страта з n=16 дає 16, і глобальне сортування їх чергує.
            fraction = (pos + 0.5) / n
            tiebreak = stable_hash(seed, "tiebreak", item["pair_key"])
            scheduled.append((fraction, tiebreak, item))

    scheduled.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _f, _t, item in scheduled]


def plan_hidden_repeats(
    ordered: Sequence[dict],
    repeat_rate: float,
    seed: int,
    min_separation: int,
) -> list[dict]:
    """Вставляє приховані повтори у послідовність анотації.

    Кожен повтор:
      * бере ту саму пару, але з ПЕРЕВЕРНУТИМИ сторонами (A<->B). Це різко
        знижує впізнаваність (людина пам'ятає "зліва було про Київ") і
        додатково перевіряє, що судження не залежить від порядку сторін;
      * ставиться не ближче ніж min_separation інтеракцій від оригіналу;
      * вибирається пропорційно стратам (а не тільки з легких комірок),
        інакше оцінка self-consistency буде оптимістично зміщена.

    Повертає повний список інтеракцій. Кожен елемент має:
      interaction_id, pair_key, is_repeat, repeat_of (interaction_id), presented_swapped.
    """
    n = len(ordered)
    if n == 0:
        return []
    n_repeats = max(0, min(int(round(n * repeat_rate)), n))
    if n_repeats == 0:
        return _number(_as_originals(ordered))

    # Кандидатом на повтор може бути ЛИШЕ пара з достатньо ранньої позиції:
    # повтор пари, що стоїть в кінці, фізично не може відстояти на
    # min_separation, і будь-яка спроба це зробити тихо ламає гарантію.
    # Порядок prefix-balanced, тому обрізання "хвоста" не зміщує страти.
    cutoff = max(1, n - min_separation)
    pool = ordered[:cutoff]
    n_repeats = min(n_repeats, len(pool))

    # Пропорційний по стратах відбір усередині пулу: інакше self-consistency
    # рахувалась би переважно на легких комірках і була б завищена.
    by_stratum: dict[str, list[dict]] = {}
    for item in pool:
        by_stratum.setdefault(item["stratum"], []).append(item)

    chosen: list[dict] = []
    remainder: list[tuple[float, str, dict]] = []
    for stratum, group in sorted(by_stratum.items()):
        share = n_repeats * len(group) / len(pool)
        take = int(math.floor(share))
        ranked = sorted(group, key=lambda it: stable_hash(seed, "repeat", stratum, it["pair_key"]))
        chosen.extend(ranked[:take])
        if take < len(ranked):
            remainder.append((share - take, stratum, ranked[take]))
    remainder.sort(key=lambda e: (-e[0], e[1]))
    for _frac, _stratum, item in remainder:
        if len(chosen) >= n_repeats:
            break
        chosen.append(item)

    interactions = _as_originals(ordered)
    origin_pos = {it["pair_key"]: i for i, it in enumerate(interactions)}

    # Повтори РОЗПОРОШУЮТЬСЯ по другій половині, а не звалюються в кінець:
    # суцільний блок повторів наприкінці сам по собі є підказкою.
    total = n + len(chosen)
    start = max(min_separation + 1, int(0.40 * total))
    pending = sorted(chosen, key=lambda it: (origin_pos[it["pair_key"]],
                                             stable_hash(seed, "place", it["pair_key"])))
    result: list[dict] = []
    next_orig = 0
    queue = list(pending)
    # ФАКТИЧНА позиція оригіналу у фінальній послідовності. Використовувати
    # доіндексний origin_pos не можна: вставлені раніше повтори зсувають
    # оригінали вправо, і реальна дистанція виявляється меншою за розраховану.
    final_origin_pos: dict[str, int] = {}

    def _ready(candidate: dict, pos: int) -> bool:
        f = final_origin_pos.get(candidate["pair_key"])
        return f is not None and (pos - f) >= min_separation

    while len(result) < total:
        pos = len(result)
        placed = False
        if queue and pos >= start:
            # скільки повторів мали б уже стояти, якщо розкладати рівномірно
            span = max(1, total - start)
            due = math.ceil((pos - start + 1) * len(pending) / span)
            if (len(pending) - len(queue)) < due:
                for candidate in queue:
                    if _ready(candidate, pos):
                        entry = dict(candidate)
                        entry["is_repeat"] = True
                        entry["presented_swapped"] = True
                        entry["repeat_of"] = None  # проставимо після нумерації
                        result.append(entry)
                        queue.remove(candidate)
                        placed = True
                        break
        if placed:
            continue
        if next_orig < n:
            entry = interactions[next_orig]
            final_origin_pos.setdefault(entry["pair_key"], len(result))
            result.append(entry)
            next_orig += 1
            continue
        # Оригіналів більше немає. Беремо найстаріший готовий повтор; якщо
        # готових немає (теоретично неможливо через cutoff, але хай буде
        # явно) — беремо найстаріший і фіксуємо факт у полі separation_short.
        ready = [c for c in queue if _ready(c, len(result))]
        candidate = ready[0] if ready else queue[0]
        queue.remove(candidate)
        entry = dict(candidate)
        entry["is_repeat"] = True
        entry["presented_swapped"] = True
        entry["repeat_of"] = None
        if not ready:
            entry["separation_short"] = True
        result.append(entry)

    numbered = _number(result)
    first_id: dict[str, str] = {}
    for entry in numbered:
        if not entry["is_repeat"]:
            first_id.setdefault(entry["pair_key"], entry["interaction_id"])
    for entry in numbered:
        if entry["is_repeat"]:
            entry["repeat_of"] = first_id.get(entry["pair_key"])
    return numbered


def _as_originals(ordered: Sequence[dict]) -> list[dict]:
    out = []
    for item in ordered:
        entry = dict(item)
        entry["is_repeat"] = False
        entry["repeat_of"] = None
        entry["presented_swapped"] = False
        out.append(entry)
    return out


def _number(entries: list[dict]) -> list[dict]:
    for idx, entry in enumerate(entries):
        entry["interaction_id"] = f"i{idx:04d}"
    return entries


def repeat_separation(interactions: Sequence[dict]) -> dict[str, int]:
    """pair_key -> дистанція між оригіналом і повтором (у інтеракціях)."""
    pos = {entry["interaction_id"]: i for i, entry in enumerate(interactions)}
    out: dict[str, int] = {}
    for i, entry in enumerate(interactions):
        if entry["is_repeat"] and entry["repeat_of"] in pos:
            out[entry["pair_key"]] = i - pos[entry["repeat_of"]]
    return out


def side_swap_for_presentation(seed: int, pair_key_value: str, is_repeat: bool) -> bool:
    """Чи показувати claim B зліва.

    Навіщо: якщо A завжди з групи {4} (ворожі медіа), а B завжди з {3},
    анотатор швидко вивчає "ліворуч завжди РФ" і починає судити за
    провенансом, а не за змістом. Детермінований, але непередбачуваний для
    людини свап прибирає цей канал.

    Для прихованих повторів свап ЗАВЖДИ протилежний до оригіналу — це і
    маскує повтор, і перевіряє незалежність судження від порядку сторін.
    """
    base = bool(stable_hash(seed, "side", pair_key_value) & 1)
    return (not base) if is_repeat else base


# ---------------------------------------------------------------------------
# Ваги та статистика
# ---------------------------------------------------------------------------

def stratum_weights(populations: dict[str, int], sampled: dict[str, int]) -> dict[str, float]:
    """w_s = N_s / n_s — скільки пар популяції представляє одна розмічена пара.

    Без цього будь-яка "частка" з стратифікованої вибірки бреше: S1 має
    ~1900 пар популяції і 40 міток, S7 має 61 пару і 16 міток. Проста
    частка по вибірці переоцінила б near-dup смугу у ~30 разів.
    """
    weights: dict[str, float] = {}
    for stratum, n_pop in populations.items():
        n_s = sampled.get(stratum, 0)
        weights[stratum] = (n_pop / n_s) if n_s > 0 else 0.0
    return weights


def weighted_proportion(
    rows: Sequence[dict],
    predicate,
    weights: dict[str, float],
    frame: str = MAIN_FRAME,
) -> tuple[float, float, float]:
    """Оцінка частки на рівні популяції (Horvitz-Thompson).

    Повертає (estimate, weighted_numerator, weighted_denominator).
    Рядки з інших frame'ів (exploratory) ігноруються — вони не є частиною
    основної популяції і не мають туди підмішуватись.
    """
    num = 0.0
    den = 0.0
    for row in rows:
        stratum = row["stratum"]
        if STRATA.get(stratum, {}).get("frame") != frame:
            continue
        w = weights.get(stratum, 0.0)
        if w <= 0:
            continue
        den += w
        if predicate(row):
            num += w
    est = (num / den) if den > 0 else float("nan")
    return est, num, den


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval. Для малих n він набагато чесніший за нормальний.

    Використовується для per-cell precision: саме за цим інтервалом
    вирішується, чи можна СЕРТИФІКУВАТИ reject-зону (див. success criteria).
    """
    if total <= 0:
        return (float("nan"), float("nan"))
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    return (max(0.0, center - half), min(1.0, center + half))


def max_errors_for_lower_bound(total: int, bound: float, z: float = 1.96) -> int:
    """Скільки помилок комірка може мати, щоб Wilson lower bound лишався >= bound.

    Це і є практична відповідь на питання "чи вистачить n": якщо для n=40
    відповідь 0, то комірка сертифікує 90% ТІЛЬКИ бездоганною, і одна
    помилка вже знімає сертифікацію.
    """
    if total <= 0:
        return -1
    best = -1
    for errors in range(total + 1):
        lo, _hi = wilson_interval(total - errors, total, z)
        if lo >= bound:
            best = errors
        else:
            break
    return best


def bootstrap_weighted_ci(
    rows: Sequence[dict],
    predicate,
    weights: dict[str, float],
    seed: int,
    iterations: int = 2000,
    frame: str = MAIN_FRAME,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Довірчий інтервал для зваженої частки — bootstrap ВСЕРЕДИНІ страт.

    Ресемплюємо всередині кожної страти окремо (stratified bootstrap), бо
    саме така структура вибірки. Наївний bootstrap по всіх рядках разом
    занизив би інтервал, ігноруючи дизайн.
    """
    import random

    by_stratum: dict[str, list[dict]] = {}
    for row in rows:
        if STRATA.get(row["stratum"], {}).get("frame") != frame:
            continue
        by_stratum.setdefault(row["stratum"], []).append(row)
    if not by_stratum:
        return (float("nan"), float("nan"))

    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(iterations):
        num = 0.0
        den = 0.0
        for stratum, group in by_stratum.items():
            w = weights.get(stratum, 0.0)
            if w <= 0 or not group:
                continue
            k = len(group)
            hits = 0
            for _i in range(k):
                if predicate(group[rng.randrange(k)]):
                    hits += 1
            num += w * hits
            den += w * k
        if den > 0:
            estimates.append(num / den)
    if not estimates:
        return (float("nan"), float("nan"))
    estimates.sort()
    lo_idx = int(math.floor((alpha / 2) * (len(estimates) - 1)))
    hi_idx = int(math.ceil((1 - alpha / 2) * (len(estimates) - 1)))
    return (estimates[lo_idx], estimates[hi_idx])


# ---------------------------------------------------------------------------
# Контракт анотації
# ---------------------------------------------------------------------------

RELATION_LABELS = ("same_fact", "same_event", "contradiction", "related", "unrelated")
REFERENT_VALUES = ("yes", "no", "uncertain")
CONFLICT_TYPES = ("location", "person_entity", "object_facility", "chronology", "other", "not_obvious")
CONFIDENCE_VALUES = ("high", "medium", "low")

# Які relation labels дозволені при якому same_referent.
# Дзеркалить production referent gate (sql/014 + validate_relation_judgment):
# same_fact/same_event/contradiction вимагають підтвердженого спільного референта.
ALLOWED_RELATIONS: dict[str, tuple[str, ...]] = {
    "yes": ("same_fact", "same_event", "contradiction", "related"),
    "no": ("related", "unrelated"),
    "uncertain": ("related", "unrelated"),
}


def validate_annotation(record: dict) -> list[str]:
    """Перевіряє один запис розмітки. Повертає список помилок (порожній = ок)."""
    errors: list[str] = []

    if record.get("status") == "unusable":
        if not record.get("unusable_reason"):
            errors.append("status=unusable requires unusable_reason")
        return errors

    referent = record.get("same_referent")
    if referent not in REFERENT_VALUES:
        errors.append(f"same_referent must be one of {REFERENT_VALUES}, got {referent!r}")
        return errors

    relation = record.get("relation_label")
    allowed = ALLOWED_RELATIONS[referent]
    if relation not in allowed:
        errors.append(
            f"relation_label={relation!r} not allowed when same_referent={referent!r}; allowed={allowed}"
        )

    confidence = record.get("confidence")
    if confidence not in CONFIDENCE_VALUES:
        errors.append(f"confidence must be one of {CONFIDENCE_VALUES}, got {confidence!r}")

    if referent == "no":
        conflict = record.get("conflict_type")
        if conflict not in CONFLICT_TYPES:
            errors.append(f"conflict_type must be one of {CONFLICT_TYPES} when same_referent=no, got {conflict!r}")
    return errors


def is_positive_relation(record: dict) -> bool:
    """"Корисне для evidence graph" — саме ті labels, що стають qualifying edge
    у event_candidate_builder (same_fact/same_event/contradiction)."""
    return record.get("relation_label") in ("same_fact", "same_event", "contradiction")


def is_different_referent(record: dict) -> bool:
    """Ціль reject-зони: пари, які точно не про той самий референт."""
    return record.get("same_referent") == "no"
