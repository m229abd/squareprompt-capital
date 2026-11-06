import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(os.getenv('DATA_DIR', '/data'))

def connect():
    ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ROOT / 'capital.db', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=30000')
    return db

def init():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, body TEXT NOT NULL, updated REAL);
        CREATE INDEX IF NOT EXISTS campaigns_updated ON campaigns(updated DESC);
        CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, campaign TEXT, role TEXT, status TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY, body TEXT);
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT, time REAL, kind TEXT, message TEXT);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, body TEXT, sent INTEGER DEFAULT 0, error TEXT);
        ''')

@contextlib.contextmanager
def transaction():
    db = connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

def load(cid, db=None):
    if db is None:
        with connect() as conn:
            return load(cid, conn)
    row = db.execute('SELECT body FROM campaigns WHERE id=?', (cid,)).fetchone()
    if row is None:
        raise KeyError(cid)
    return json.loads(row['body'])

def save(c, db):
    db.execute('INSERT INTO campaigns VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body, updated=excluded.updated', (c['id'], json.dumps(c, allow_nan=False), time.time()))

def event(cid, kind, message, db):
    db.execute('INSERT INTO events(campaign,time,kind,message) VALUES(?,?,?,?)', (cid,time.time(),kind,message))

def settings():
    with connect() as db:
        row = db.execute('SELECT body FROM settings WHERE id=1').fetchone()
    return json.loads(row['body']) if row else {'base_url':'','api_key':'','models':[]}
