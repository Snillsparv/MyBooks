import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "foldly.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            is_subscriber INTEGER DEFAULT 0,
            stripe_customer_id TEXT,
            daily_folds_used INTEGER DEFAULT 0,
            daily_folds_date TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS folds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            video_id TEXT NOT NULL,
            video_title TEXT DEFAULT '',
            video_url TEXT DEFAULT '',
            summary_json TEXT,
            segments_json TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_sek REAL DEFAULT 0.0,
            language TEXT DEFAULT 'svenska',
            detail_level TEXT DEFAULT 'medium',
            share_token TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_folds_user_id ON folds(user_id);
        CREATE INDEX IF NOT EXISTS idx_folds_share ON folds(share_token);
    """)
    conn.commit()
    conn.close()
