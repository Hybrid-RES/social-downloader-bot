from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    database = Path(os.getenv("DATABASE_PATH", "/config/social-downloader.sqlite3"))
    if not database.is_file():
        return 1
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as connection:
            connection.execute("SELECT 1").fetchone()
    except sqlite3.Error:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
