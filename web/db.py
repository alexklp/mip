"""PostgreSQL read-only з'єднання для Web MVP.

Використовує ту саму DSN-конвенцію, що й аналітичний pipeline проєкту
(DB_DSN = "dbname=mip_dev"). З'єднання відкривається лише на час
однієї операції, переводиться у READ ONLY ще до передачі викликаючому
коду і гарантовано закривається через context manager.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DB_DSN = "dbname=mip_dev"
WEB_STATEMENT_TIMEOUT_MS = 15000


@contextmanager
def read_connection() -> Iterator[psycopg.Connection]:
    """Контекстний менеджер для короткоживучого read-only з'єднання.

    З'єднання відкривається на час виконання блоку `with`. Перед тим,
    як потрапити до викликаючого коду, транзакція переводиться у
    READ ONLY. З'єднання гарантовано закривається при виході з блоку,
    незалежно від того, чи сталася помилка.
    """
    conn = psycopg.connect(
        DB_DSN,
        row_factory=dict_row,
        options=f"-c statement_timeout={WEB_STATEMENT_TIMEOUT_MS}",
    )
    try:
        conn.read_only = True
        yield conn
    finally:
        conn.close()
