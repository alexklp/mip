"""ТЕСТОВИЙ STUB psycopg. Тільки для tests/, ніколи не на production-шляху."""
from __future__ import annotations


class _Cursor:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): raise AssertionError("stub cursor must not be used")
    def fetchall(self): return []
    def fetchone(self): return None


class Connection:
    def __init__(self, dsn):
        self.dsn = dsn
        self.read_only = False
        self.closed = False
        self.writes_attempted = 0

    def __enter__(self): return self
    def __exit__(self, *a):
        self.closed = True
        return False

    def cursor(self): return _Cursor()
    def commit(self): self.writes_attempted += 1
    def close(self): self.closed = True


LAST_CONNECTIONS: list[Connection] = []


def connect(dsn, *a, **k):
    conn = Connection(dsn)
    LAST_CONNECTIONS.append(conn)
    return conn
