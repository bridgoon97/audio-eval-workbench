"""数据库、口令与事务支持。"""

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', owner TEXT NOT NULL REFERENCES users(id), created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(task_id TEXT REFERENCES tasks(id), user_id TEXT REFERENCES users(id), PRIMARY KEY(task_id,user_id));
CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), name TEXT NOT NULL, scene TEXT NOT NULL, provenance TEXT NOT NULL, samples INTEGER NOT NULL DEFAULT 0, UNIQUE(task_id,name));
CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, sample_id TEXT NOT NULL REFERENCES samples(id), name TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL, meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(user_id TEXT REFERENCES users(id), sample_id TEXT REFERENCES samples(id), track_id TEXT REFERENCES tracks(id), position INTEGER NOT NULL, PRIMARY KEY(user_id,track_id), UNIQUE(user_id,sample_id,position));
CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY, sample_id TEXT REFERENCES samples(id), user_id TEXT REFERENCES users(id), track_id TEXT REFERENCES tracks(id), start INTEGER NOT NULL, end INTEGER NOT NULL, body TEXT NOT NULL, tag TEXT NOT NULL, parent TEXT REFERENCES comments(id), created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ratings(sample_id TEXT REFERENCES samples(id), user_id TEXT REFERENCES users(id), choice TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(sample_id,user_id));
"""


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def init(path):
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return salt + ":" + digest


def verify(password, stored):
    salt, digest = stored.split(":")
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    ).hex()
    return hmac.compare_digest(actual, digest)
