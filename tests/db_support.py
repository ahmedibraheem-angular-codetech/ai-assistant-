"""Close SQLite connections opened by tests.

``with sqlite3.connect(path)`` commits or rolls back the transaction. It does
not close the connection. Python then warns when that connection is collected.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def open_sqlite(path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()
