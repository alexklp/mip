# Relation Gold Set v1 — runbook

Вимірювальний інструмент, не production-алгоритм. Мета — відповісти
«цього сигналу достатньо / недостатньо», а не зафіксувати пороги.

**Нічого не пише в БД. Не викликає Mamay. Не змінює production-семантику.**

```
gold_set/
├── gold_set_common.py             чиста логіка: страти, відбір, порядок, ваги, статистика
├── relation_gold_set_sampler.py   READ-ONLY семплер -> JSONL + CSV + manifest
├── relation_gold_set_labeler.html однофайловий UI розмітки (без сервера, без фреймворків)
├── relation_gold_set_analyze.py   аналіз розмітки -> звіт у stdout
├── ANNOTATION_HANDBOOK.md         пам'ятка анотатора (боундарі + контрприклади)
└── tests/
    ├── test_gold_set.py           63 тести: логіка + інтеграція семплера/аналізатора
    ├── test_labeler_ui.py         11 тестів UI у headless Chromium (Playwright)
    └── stubs/                     фейкові psycopg/pgvector/repo-модулі ТІЛЬКИ для тестів
```

---

## 0. Передумови

- Ті самі, що для `candidate_pair_dual_profile.py`: `psycopg`, `pgvector`, `numpy`.
  Нових залежностей пакет не додає.
- `claim_embeddings` і content `embeddings` для `embedding_model_id=1` мають бути догнані.
- Mamay **не потрібен** і не використовується. Live cron можна не зупиняти:
  семплер читає БД у read-only і не тримає з'єднання під час обчислень.

---

## 1. Крок 1 — dry-run (обов'язково перший)

Показує РОЗМІРИ страт у реальному корпусі, нічого не пишучи.

```bash
cd ~/mip
python3 -m experiments.claim_relations.gold_set.relation_gold_set_sampler \
    --out-dir /tmp/gold_v1 --dry-run
```

Дивитись на колонку `population`:

- якщо `population < requested` для страти — вибірка недобере розмір;
- якщо це **decision-critical** страта (S1, S4, S8), а населення < 35 —
  вона фізично не зможе сертифікувати 90% (див. розділ 5). Тоді або
  знижуємо поріг `--hub-degree-threshold` (для S8), або приймаємо, що
  ця комірка лише скринінгова.

Очікуваний час: десятки секунд (сканування ~189M cross-group пар + same-group
для exploratory страти). Пік RSS має бути того ж порядку, що `candidate_pair_profile`
(~1 GiB), бо тримаються дві матриці векторів (claim + content).

---

## 2. Крок 2 — згенерувати датасет

```bash
python3 -m experiments.claim_relations.gold_set.relation_gold_set_sampler \
    --out-dir /tmp/gold_v1 2>&1 | tee /tmp/gold_v1/sampler.log
```

Артефакти:

| Файл | Призначення |
|---|---|
| `gold_set_v1_dataset.jsonl` | канонічний артефакт: те, що вантажиться в labeler |
| `gold_set_v1_pairs.csv` | той самий зріз для швидкого огляду в БД/таблиці |
| `gold_set_v1_manifest.json` | seed, populations, allocation, версії — потрібен аналізатору для ваг |

`manifest.json` **обов'язковий** для аналізу: без `populations` неможливо
порахувати ваги, і будь-яка «частка» перетвориться на брехню.

Корисні прапорці:

```bash
--seed 20260903              # змінювати лише свідомо: змінює всю вибірку
--n-s1 60                    # збільшити decision-critical комірку (терпить 1 помилку)
--no-exploratory             # не семплювати same-source-group страту
--max-claim-uses 3           # послабити кап на повторне використання claim
--hub-degree-threshold 3     # якщо S8 порожня при дефолтному порозі
```

---

## 3. Крок 3 — розмітка

localStorage на `file://` у частині браузерів обмежений. Надійніший запуск —
через локальний http-сервер (він же дає стабільний origin для автозбереження):

```bash
cd ~/mip/experiments/claim_relations/gold_set
python3 -m http.server 8899
# відкрити http://127.0.0.1:8899/relation_gold_set_labeler.html
```

Далі:

1. `load dataset` → обрати `/tmp/gold_v1/gold_set_v1_dataset.jsonl`.
2. Розмічати (`?` — пам'ятка, клавіші описані там).
3. Кожні ~25 пар тиснути **export annotations** → `gold_set_v1_annotations.jsonl`.
4. Якщо браузер закрився — просто відкрити знову (стан відновиться) або
   `resume from export` з останнього експорту.

Порядок пар prefix-balanced: **зупинятись можна будь-коли**, розмічена частина
лишається збалансованою за стратами.

---

## 4. Крок 4 — аналіз

```bash
python3 -m experiments.claim_relations.gold_set.relation_gold_set_analyze \
    --dataset     /tmp/gold_v1/gold_set_v1_dataset.jsonl \
    --annotations ~/Downloads/gold_set_v1_annotations.jsonl \
    --manifest    /tmp/gold_v1/gold_set_v1_manifest.json \
    | tee /tmp/gold_v1/analysis.txt
```

Розділи звіту:

1. completeness / контракт (+ попередження про псевдореплікацію)
2. self-consistency на прихованих повторах (raw + Cohen's kappa)
3. розподіл міток: **вибірка vs зважена популяція**
4. per-stratum результати + **сертифікація reject-зони**
5. 2D-таблиці claim × content
6. precision/recall простих детермінованих правил (зважені)
7. що саме втрачає поріг claim ≥ 0.80
8. чи є content незалежним сигналом (AUC)
9. exploratory same-group frame — **окремо**, поза основними оцінками

---

## 5. Як читати «сертифіковано / не сертифіковано»

Wilson lower bound на `P(different referent)` у комірці. Бюджет помилок:

| n | максимум помилок, щоб lower bound ≥ 0.90 |
|---|---|
| ≤ 34 | **неможливо навіть при нулі помилок** |
| 35 | 0 |
| 40 | 0 |
| 60 | 1 |
| 80 | 2 |
| 100 | 4 |

Практично: `n=40` розрізняє «комірка бездоганна» від «комірка не бездоганна».
Щоб довести 90% попри 1–2 помилки, потрібно `n≈60–80` — і це свідоме рішення
про додаткові години розмітки, а не щось, що можна доотримати з тих самих даних.

---

## 6. Тести

```bash
cd ~/mip
python3 experiments/claim_relations/gold_set/tests/test_gold_set.py     # 63, без БД
python3 experiments/claim_relations/gold_set/tests/test_labeler_ui.py   # 11, headless Chromium
```

`tests/stubs/` підмінює `psycopg`, `pgvector` і три repo-модулі, тому весь
семплер проганяється end-to-end на синтетичному корпусі без PostgreSQL.
Каталог stubs додається у `sys.path` **тільки** всередині тестів.

UI-тести пропускаються (skip), якщо Playwright не встановлений — це не помилка.

---

## 7. Чого цей інструмент НЕ робить

- не пише в `candidate_pairs` / `relation_judgments` (з'єднання `read_only=True`);
- не викликає Mamay і не потребує GPU;
- не змінює `candidate_version=1` і жодної production-семантики;
- не використовує час як eligibility/stratification/ranking сигнал;
- не тренує класифікатор;
- не пропонує порогів — він дає підстави їх обирати.
