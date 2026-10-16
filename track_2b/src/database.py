"""SQLite transactions shared by the legacy modules and new world services."""
import sqlite3
from contextlib import contextmanager


class Connection(sqlite3.Connection):
    atomic_depth = 0

    def commit(self):
        # Legacy helpers may commit. A world operation owns its entire transaction.
        if not self.atomic_depth:
            super().commit()


def connect_db(path: str) -> Connection:
    conn = sqlite3.connect(path, timeout=10, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def atomic(conn: Connection):
    outer = conn.atomic_depth == 0
    if outer and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.atomic_depth += 1
    try:
        yield conn
    except BaseException:
        if outer:
            conn.rollback()
        raise
    else:
        if outer:
            sqlite3.Connection.commit(conn)
    finally:
        conn.atomic_depth -= 1
