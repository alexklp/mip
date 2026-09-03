"""ТЕСТОВИЙ STUB для candidate_pair_profile.

Використовується ЛИШЕ тестами: test_gold_set.py тимчасово додає каталог
tests/stubs у sys.path[0], щоб relation_gold_set_sampler можна було
імпортувати й прогнати повністю без PostgreSQL і без pgvector.

На робочому сервері цей файл ніколи не потрапляє в sys.path — жоден
production-скрипт його не імпортує.

Синтетичний корпус будується детерміновано: кілька "тем", навколо кожної
розкидані claims з різним рівнем шуму, тому cosine між ними лягає в різні
смуги. Групи розподілені так, щоб існували cross-group пари у високих смугах.
"""
from __future__ import annotations

import numpy as np

DB_DSN = "dbname=fake_test_db"
EMBEDDING_MODEL_ID = 1
DIMENSION = 32
MODEL_NAME = "BAAI/bge-m3"

_N_TOPICS = 18
_PER_TOPIC = 12
_N_GENERIC = 14   # штучний "generic hub" кластер (аналог «Є постраждалі»)
_SEED = 7


def get_code_revision() -> str:
    return "stubrev"


def verify_registered_model(conn) -> None:
    return None


def _unit(v: np.ndarray) -> np.ndarray:
    return (v / np.linalg.norm(v)).astype(np.float32)


def _build_synthetic():
    rng = np.random.default_rng(_SEED)
    topics = [_unit(rng.normal(size=DIMENSION)) for _ in range(_N_TOPICS)]
    content_topics = [_unit(rng.normal(size=DIMENSION)) for _ in range(_N_TOPICS)]

    claim_ids, texts, run_ids, content_ids, group_ids = [], [], [], [], []
    claim_vecs, content_vecs = [], []

    idx = 0
    for t in range(_N_TOPICS):
        for k in range(_PER_TOPIC):
            # noise керує тим, у яку смугу cosine потрапить пара.
            # У dim=32 cos ~ 1/(1+32*noise^2): 0.02 -> ~0.99, 0.17 -> ~0.52.
            noise = 0.02 + 0.014 * k
            cvec = _unit(topics[t] + noise * rng.normal(size=DIMENSION))
            # content отримує НЕЗАЛЕЖНИЙ шум -> content_score != claim_score,
            # інакше два сигнали були б вироджено скорельовані і тест
            # "content — незалежний сигнал" нічого б не перевіряв
            cnoise = 0.03 + 0.018 * ((k * 5 + t) % 12)
            dvec = _unit(content_topics[t] + cnoise * rng.normal(size=DIMENSION))

            # групи чергуються так, щоб у кожній темі були представники
            # різних source-groups (інакше cross-group пар не буде)
            group = (2, 3, 4)[k % 3]
            if t % 4 == 0 and k % 3 == 0:
                group = 4

            claim_ids.append(f"c{idx:04d}-0000-0000-0000-{t:04d}{k:04d}")
            texts.append(f"topic{t} claim{k}")
            run_ids.append(f"run{idx:04d}")
            content_ids.append(f"content{idx:04d}")
            group_ids.append(group)
            claim_vecs.append(cvec)
            content_vecs.append(dvec)
            idx += 1

    # Generic hub-кластер: багато claims, майже однакових за claim-вектором,
    # але з РІЗНИМ контентом. Це синтетичний аналог «Є постраждалі» /
    # «Есть пострадавшие» і саме він створює hub x hub пари.
    generic = _unit(rng.normal(size=DIMENSION))
    for g in range(_N_GENERIC):
        cvec = _unit(generic + 0.025 * rng.normal(size=DIMENSION))
        dvec = _unit(rng.normal(size=DIMENSION))  # контексти незалежні
        claim_ids.append(f"g{idx:04d}-0000-0000-0000-{g:08d}")
        texts.append(f"generic casualty claim {g}")
        run_ids.append(f"run{idx:04d}")
        content_ids.append(f"content{idx:04d}")
        group_ids.append(3 if g % 2 else 4)
        claim_vecs.append(cvec)
        content_vecs.append(dvec)
        idx += 1

    order = sorted(range(len(claim_ids)), key=lambda i: claim_ids[i])
    return {
        "claim_ids": [claim_ids[i] for i in order],
        "texts": [texts[i] for i in order],
        "run_ids": [run_ids[i] for i in order],
        "content_ids": [content_ids[i] for i in order],
        "groups": [group_ids[i] for i in order],
        "claim_vecs": [claim_vecs[i] for i in order],
        "content_vecs": [content_vecs[i] for i in order],
    }


SYNTHETIC = _build_synthetic()


class _FakeVector:
    """Імітує pgvector-об'єкт, який має .to_numpy()."""

    def __init__(self, arr):
        self._arr = arr

    def to_numpy(self):
        return self._arr


def fetch_claims(conn):
    """(claim_id, claim_text, run_id, content_id, embedding), ORDER BY claim_id."""
    s = SYNTHETIC
    return [
        (s["claim_ids"][i], s["texts"][i], s["run_ids"][i], s["content_ids"][i],
         _FakeVector(s["claim_vecs"][i]))
        for i in range(len(s["claim_ids"]))
    ]


def fetch_occurrences(conn, content_ids):
    import datetime
    s = SYNTHETIC
    out = {}
    base_time = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
    for i, cid in enumerate(s["content_ids"]):
        out[cid] = [(s["groups"][i], base_time + datetime.timedelta(hours=i))]
    return out


def build_corpus(claim_rows, occ_by_content):
    claim_ids, claim_texts, run_ids, groups, first_seen, vectors = [], [], [], [], [], []
    skipped_no_group = 0
    skipped_no_occurrence = 0
    for claim_id, claim_text, run_id, content_id, embedding in claim_rows:
        occs = occ_by_content.get(content_id)
        if not occs:
            skipped_no_occurrence += 1
            continue
        group_set = frozenset(g for g, _ in occs if g is not None)
        if not group_set:
            skipped_no_group += 1
            continue
        claim_ids.append(claim_id)
        claim_texts.append(claim_text)
        run_ids.append(run_id)
        groups.append(group_set)
        first_seen.append(int(min(t for _, t in occs).timestamp()))
        vectors.append(embedding.to_numpy())
    matrix = (np.vstack(vectors).astype(np.float32, copy=False)
              if vectors else np.empty((0, DIMENSION), dtype=np.float32))
    return {
        "claim_ids": claim_ids,
        "claim_texts": claim_texts,
        "run_ids": run_ids,
        "groups": groups,
        "first_seen": np.asarray(first_seen, dtype=np.int64),
        "vectors": matrix,
        "skipped_no_group": skipped_no_group,
        "skipped_no_occurrence": skipped_no_occurrence,
    }


def group_indices(groups):
    from collections import defaultdict
    grouped = defaultdict(list)
    for idx, group in enumerate(groups):
        grouped[group].append(idx)
    return {g: np.asarray(v, dtype=np.int64) for g, v in grouped.items()}


def group_label(group) -> str:
    return "{" + ",".join(str(v) for v in sorted(group)) + "}"


def preview(text: str, limit: int = 180) -> str:
    clean = " ".join(str(text).split())
    return clean[:limit]
