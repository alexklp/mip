#!/usr/bin/env python3
"""
collectors/embed_worker.py — мінімальний batch-воркер embeddings для МІП.

Бере eligible content_items (є ≥1 occurrence, ще немає embedding під поточним
embedding_model_id), кодує BGE-M3 (sentence-transformers, зафіксована revision,
CPU, local_files_only=True), пише в embeddings пакетами.

sql/006_add_embeddings.sql МАЄ бути застосований ДО запуску — цей скрипт модель
в embedding_models НЕ реєструє, тільки звіряє hardcoded константи з тим, що вже
є в БД. Розбіжність -> RuntimeError, без UPDATE (fail-fast, не мовчазне оновлення).

Помилка на batch = стоп всього прогону, без retry-loop і без переходу до наступного
batch. Вже закомічені batches лишаються — повторний запуск ідемпотентно продовжує
(SELECT з NOT EXISTS природньо пропускає вже оброблене).

Потребує: uv pip install pgvector sentence-transformers.
"""

import logging
import sys

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

DB_DSN = "dbname=mip_dev"

EMBEDDING_MODEL_ID = 1
MODEL_NAME = "BAAI/bge-m3"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
FRAMEWORK = "sentence-transformers"
FRAMEWORK_VERSION = "5.7.0"
DIMENSION = 1024
METRIC = "cosine"
ENCODING_PARAMS = {"normalize_embeddings": True, "input_field": "text_content"}

BATCH_SIZE = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("embed_worker")


def verify_registered_model(conn):
    """Звіряє hardcoded конфіг з embedding_models. Розбіжність -> RuntimeError, БЕЗ update."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT model_name, model_revision, framework, framework_version,
                   dimension, metric, encoding_params
            FROM embedding_models WHERE embedding_model_id = %s
            """,
            (EMBEDDING_MODEL_ID,),
        )
        row = cur.fetchone()

    if row is None:
        raise RuntimeError(
            f"embedding_model_id={EMBEDDING_MODEL_ID} не зареєстровано в embedding_models. "
            f"Спочатку застосуй sql/006_add_embeddings.sql."
        )

    db_name, db_revision, db_framework, db_fw_version, db_dim, db_metric, db_params = row
    expected = (MODEL_NAME, MODEL_REVISION, FRAMEWORK, FRAMEWORK_VERSION, DIMENSION, METRIC, ENCODING_PARAMS)
    actual = (db_name, db_revision, db_framework, db_fw_version, db_dim, db_metric, db_params)

    if actual != expected:
        raise RuntimeError(
            "Конфігурація моделі в БД НЕ збігається з константами в цьому скрипті.\n"
            f"  БД:     {actual}\n"
            f"  Скрипт: {expected}\n"
            "Fail-fast навмисний — скрипт ніколи не оновлює embedding_models мовчки."
        )
    log.info(f"model config verified: {MODEL_NAME}@{MODEL_REVISION[:12]} dim={DIMENSION}")


def load_model():
    log.info(f"loading {MODEL_NAME} @ {MODEL_REVISION} (local_files_only=True, CPU)")
    return SentenceTransformer(
        MODEL_NAME,
        revision=MODEL_REVISION,
        device="cpu",
        local_files_only=True,
    )


def fetch_batch(conn, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ci.content_id, ci.text_content
            FROM content_items ci
            WHERE EXISTS (
                SELECT 1 FROM item_occurrences io WHERE io.content_id = ci.content_id
            )
            AND NOT EXISTS (
                SELECT 1 FROM embeddings e
                WHERE e.content_id = ci.content_id AND e.embedding_model_id = %s
            )
            ORDER BY ci.first_seen_at
            LIMIT %s
            """,
            (EMBEDDING_MODEL_ID, limit),
        )
        return cur.fetchall()


def process_batch(conn, model, batch):
    """Кодує весь batch одним викликом, валідує розмірність, пише і комітить одразу весь batch.
    Будь-яка помилка -> rollback, лог batch content_id's, виняток передається нагору (стоп прогону)."""
    content_ids = [row[0] for row in batch]
    texts = [row[1] for row in batch]

    vectors = model.encode(texts, normalize_embeddings=ENCODING_PARAMS["normalize_embeddings"])

    for content_id, vector in zip(content_ids, vectors):
        if len(vector) != DIMENSION:
            raise RuntimeError(
                f"dimension mismatch: content_id={content_id} got {len(vector)}, expected {DIMENSION} "
                f"(batch content_ids={content_ids})"
            )

    with conn.cursor() as cur:
        for content_id, vector in zip(content_ids, vectors):
            cur.execute(
                """
                INSERT INTO embeddings (content_id, embedding_model_id, embedding)
                VALUES (%s, %s, %s)
                ON CONFLICT (content_id, embedding_model_id) DO NOTHING
                """,
                (content_id, EMBEDDING_MODEL_ID, vector),
            )
    conn.commit()
    log.info(f"batch committed: {len(content_ids)} content_ids")


def run():
    with psycopg.connect(DB_DSN) as conn:
        register_vector(conn)
        verify_registered_model(conn)
        model = load_model()

        total = 0
        while True:
            batch = fetch_batch(conn, BATCH_SIZE)
            if not batch:
                break
            try:
                process_batch(conn, model, batch)
            except Exception as exc:
                conn.rollback()
                content_ids = [row[0] for row in batch]
                log.error(f"BATCH FAILED, content_ids={content_ids}: {exc}")
                log.error("STOPPING run — no retry loop, no skip-and-continue. "
                          "Fix the issue, re-run to resume idempotently.")
                sys.exit(1)
            total += len(batch)

        log.info(f"done: {total} embeddings written this run, backlog exhausted")


if __name__ == "__main__":
    run()
