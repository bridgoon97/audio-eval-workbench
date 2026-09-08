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
CREATE TABLE IF NOT EXISTS deleted_tasks(task_id TEXT PRIMARY KEY REFERENCES tasks(id), deleted_by TEXT NOT NULL REFERENCES users(id), deleted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(task_id TEXT REFERENCES tasks(id), user_id TEXT REFERENCES users(id), PRIMARY KEY(task_id,user_id));
CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), name TEXT NOT NULL, scene TEXT NOT NULL, provenance TEXT NOT NULL, samples INTEGER NOT NULL DEFAULT 0, UNIQUE(task_id,name));
CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, sample_id TEXT NOT NULL REFERENCES samples(id), name TEXT NOT NULL, version TEXT NOT NULL, path TEXT NOT NULL, meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(user_id TEXT REFERENCES users(id), sample_id TEXT REFERENCES samples(id), track_id TEXT REFERENCES tracks(id), position INTEGER NOT NULL, PRIMARY KEY(user_id,track_id), UNIQUE(user_id,sample_id,position));
CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY, sample_id TEXT REFERENCES samples(id), user_id TEXT REFERENCES users(id), track_id TEXT REFERENCES tracks(id), start INTEGER NOT NULL, end INTEGER NOT NULL, body TEXT NOT NULL, tag TEXT NOT NULL, parent TEXT REFERENCES comments(id), created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ratings(sample_id TEXT REFERENCES samples(id), user_id TEXT REFERENCES users(id), choice TEXT NOT NULL, reason TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(sample_id,user_id));
CREATE TABLE IF NOT EXISTS review_assignments(task_id TEXT NOT NULL REFERENCES tasks(id), user_id TEXT NOT NULL REFERENCES users(id), PRIMARY KEY(task_id,user_id));
CREATE TABLE IF NOT EXISTS track_processing(track_id TEXT PRIMARY KEY REFERENCES tracks(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS track_analysis(track_id TEXT PRIMARY KEY REFERENCES tracks(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purge_tokens(token TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), created TEXT NOT NULL);
-- 自助申请：设备令牌、邀请凭证与申请记录。所有凭证只保存哈希；
-- 邀请哈希内嵌独立盐（password_hash 格式 salt:digest），列表与备份均无法恢复明文。
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), token_hash TEXT UNIQUE NOT NULL, created TEXT NOT NULL, claimed_at REAL NOT NULL, last_used REAL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, first_ip TEXT NOT NULL DEFAULT '', last_ip TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS invites(id TEXT PRIMARY KEY, purpose TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, task_id TEXT REFERENCES tasks(id), token_key TEXT UNIQUE NOT NULL, token_hash TEXT NOT NULL, expires TEXT NOT NULL, max_uses INTEGER NOT NULL, used_count INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, created_by TEXT REFERENCES users(id));
CREATE TABLE IF NOT EXISTS applications(id TEXT PRIMARY KEY, claim_hash TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL, employee_id TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', invite_id TEXT NOT NULL REFERENCES invites(id), status TEXT NOT NULL DEFAULT 'pending', ip TEXT NOT NULL DEFAULT '', device TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, decided TEXT, decided_by TEXT REFERENCES users(id), decision TEXT NOT NULL DEFAULT '', user_id TEXT REFERENCES users(id), device_id TEXT REFERENCES devices(id));
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
        # 无损迁移：旧数据中的非 owner 成员是当时被分配的评测者，迁入受邀名单；
        # owner 的自动成员行只是访问便利，不构成评测义务。凡已产生评分的用户
        # （含 owner/admin）同样迁入受邀名单，历史有效证据不丢失。
        # 两条迁移均幂等，可重复执行，不改变任何访问权限。
        db.execute(
            "INSERT OR IGNORE INTO review_assignments(task_id,user_id) "
            "SELECT m.task_id,m.user_id FROM members m JOIN tasks t ON t.id=m.task_id "
            "WHERE m.user_id != t.owner"
        )
        db.execute(
            "INSERT OR IGNORE INTO review_assignments(task_id,user_id) "
            "SELECT s.task_id,r.user_id FROM ratings r JOIN samples s ON s.id=r.sample_id"
        )


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
