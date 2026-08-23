# -*- coding: utf-8 -*-
"""SQLite 留档: 消息/话题/统计, 线程安全"""
import json
import os
import sqlite3
import sys
import threading
import time

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_FILE = os.path.join(BASE_DIR, "chat_archive.db")
_lock = threading.RLock()
_conn = None


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
        return _conn


def init():
    with _lock:
        c = get_conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT,
            msg_id TEXT,
            raw_type TEXT DEFAULT 'text',
            text_content TEXT DEFAULT '',
            image_urls TEXT DEFAULT '',
            raw_message TEXT DEFAULT '',
            ts INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_msg_gts ON messages(group_id, ts);
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            start_ts INTEGER NOT NULL,
            last_ts INTEGER NOT NULL,
            msg_count INTEGER DEFAULT 0,
            participants TEXT DEFAULT '[]',
            status TEXT DEFAULT 'open',
            title TEXT DEFAULT '',
            points TEXT DEFAULT '[]',
            summary TEXT DEFAULT '',
            style TEXT DEFAULT 'standard',
            interest_score REAL DEFAULT NULL,
            interest_reason TEXT DEFAULT '',
            closed_ts INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_topic_g ON topics(group_id, status);
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL,
            model TEXT DEFAULT '',
            msgs INTEGER DEFAULT 0,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0
        );
        """)
        # 旧库迁移: 补 interest 列(已存在则跳过)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(topics)").fetchall()}
        if "interest_score" not in cols:
            c.execute("ALTER TABLE topics ADD COLUMN interest_score REAL DEFAULT NULL")
        if "interest_reason" not in cols:
            c.execute("ALTER TABLE topics ADD COLUMN interest_reason TEXT DEFAULT ''")
        c.commit()


# ------------------------- 消息 -------------------------
def insert_message(group_id, user_id, nickname, msg_id, raw_type, text, image_urls, raw, ts):
    with _lock:
        c = get_conn()
        c.execute("INSERT INTO messages(group_id,user_id,nickname,msg_id,raw_type,text_content,image_urls,raw_message,ts)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (group_id, user_id, nickname, msg_id, raw_type, text,
                   json.dumps(image_urls or [], ensure_ascii=False), raw, ts))
        c.commit()


def query_messages(group_id, start_ts, end_ts):
    with _lock:
        rows = get_conn().execute(
            "SELECT nickname,text_content,image_urls,ts,user_id FROM messages"
            " WHERE group_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
            (group_id, start_ts, end_ts)).fetchall()
        return [{"nickname": r["nickname"] or "?",
                 "text": (r["text_content"] or "").strip(),
                 "image_urls": json.loads(r["image_urls"] or "[]"),
                 "ts": r["ts"], "user_id": r["user_id"]} for r in rows]


def count_messages_since(ts) -> int:
    with _lock:
        r = get_conn().execute("SELECT COUNT(*) c FROM messages WHERE ts>=?", (ts,)).fetchone()
        return r["c"]


# ------------------------- 话题 -------------------------
def create_topic(group_id, start_ts) -> int:
    with _lock:
        c = get_conn()
        cur = c.execute("INSERT INTO topics(group_id,start_ts,last_ts,msg_count,participants,status)"
                        " VALUES(?,?,?,0,'[]','open')", (group_id, start_ts, start_ts))
        c.commit()
        return cur.lastrowid


def update_topic_open(topic_id, last_ts, msg_count, participants):
    with _lock:
        c = get_conn()
        c.execute("UPDATE topics SET last_ts=?, msg_count=?, participants=? WHERE id=?",
                  (last_ts, msg_count, json.dumps(sorted(participants), ensure_ascii=False), topic_id))
        c.commit()


def close_topic(topic_id, status="closed"):
    with _lock:
        c = get_conn()
        c.execute("UPDATE topics SET status=?, closed_ts=? WHERE id=?",
                  (status, int(time.time()), topic_id))
        c.commit()


def save_summary(topic_id, title, points, summary, style):
    with _lock:
        c = get_conn()
        c.execute("UPDATE topics SET title=?, points=?, summary=?, style=? WHERE id=?",
                  (title, json.dumps(points, ensure_ascii=False), summary, style, topic_id))
        c.commit()


def save_interest(topic_id, score, reason=""):
    """写入兴趣度打分(score 可为负=用户标记不感兴趣)"""
    with _lock:
        c = get_conn()
        c.execute("UPDATE topics SET interest_score=?, interest_reason=? WHERE id=?",
                  (float(score), (reason or "")[:200], topic_id))
        c.commit()


def get_topic(topic_id):
    with _lock:
        r = get_conn().execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        return dict(r) if r else None


def list_topics(group_id=None, status=None, limit=200, offset=0, since=None, sort="time"):
    """话题列表. sort: time=按时间倒序 | score=按兴趣分倒序(未打分垫底)"""
    with _lock:
        sql, args = "SELECT * FROM topics WHERE 1=1", []
        if group_id:
            sql += " AND group_id=?"; args.append(group_id)
        if status:
            sql += " AND status=?"; args.append(status)
        if since:
            sql += " AND closed_ts>=?"; args.append(since)
        if sort == "score":
            sql += " ORDER BY (interest_score IS NULL), interest_score DESC, id DESC"
        else:
            sql += " ORDER BY id DESC"
        sql += " LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in get_conn().execute(sql, args).fetchall()]


def find_open_topics():
    with _lock:
        rows = get_conn().execute("SELECT * FROM topics WHERE status='open'").fetchall()
        return [dict(r) for r in rows]


# ------------------------- 统计 -------------------------
def add_stat(kind, model="", msgs=0, tokens_in=0, tokens_out=0):
    with _lock:
        c = get_conn()
        c.execute("INSERT INTO stats(ts,kind,model,msgs,tokens_in,tokens_out) VALUES(?,?,?,?,?,?)",
                  (int(time.time()), kind, model, msgs, tokens_in, tokens_out))
        c.commit()


def dashboard_stats(since_ts):
    with _lock:
        c = get_conn()
        msgs = c.execute("SELECT COUNT(*) c FROM messages WHERE ts>=?", (since_ts,)).fetchone()["c"]
        imgs = sum(len(json.loads(r["image_urls"] or "[]"))
                   for r in c.execute("SELECT image_urls FROM messages WHERE ts>=?", (since_ts,)).fetchall())
        topics = c.execute("SELECT COUNT(*) c FROM topics WHERE closed_ts>=?", (since_ts,)).fetchone()["c"]
        rows = c.execute("SELECT kind, COUNT(*) n, SUM(tokens_in) ti, SUM(tokens_out) to2 FROM stats"
                         " WHERE ts>=? GROUP BY kind", (since_ts,)).fetchall()
        llm = [{"kind": r["kind"], "calls": r["n"], "tokens_in": r["ti"] or 0,
                "tokens_out": r["to2"] or 0} for r in rows]
        return {"messages": msgs, "images": imgs, "topics": topics, "llm": llm}


# ------------------------- 运维 -------------------------
def backup(dest_path) -> str:
    with _lock:
        c = get_conn()
        c.commit()
        import shutil
        shutil.copyfile(DB_FILE, dest_path)
        return dest_path


def clear_data(group_id=None):
    with _lock:
        c = get_conn()
        if group_id:
            c.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
            c.execute("DELETE FROM topics WHERE group_id=?", (group_id,))
        else:
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM topics")
        c.commit()
