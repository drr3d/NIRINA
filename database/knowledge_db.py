import sqlite3

# 1. Mengambil path secara terpusat dari config
from core_agent.config import sqlite_db_path

# [NEW UPGRADE]: Inisialisasi tabel hr_knowledge di SQLite
def init_knowledge_db():
    with sqlite3.connect(sqlite_db_path, timeout=30.0) as conn:
        cursor = conn.cursor()

        # AKTIFKAN MODE WAL (Write-Ahead Logging) AGAR MULTI-PROCESS AMAN
        cursor.execute("PRAGMA journal_mode=WAL;")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE,
                upload_date TEXT
            )
        ''')
        conn.commit()