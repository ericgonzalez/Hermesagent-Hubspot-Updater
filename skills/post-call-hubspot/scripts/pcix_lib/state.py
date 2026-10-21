"""SQLite state. This is a cache/queue only: HubSpot (the pcix-ref marker on the note/meeting)
is the source of truth for 'already written'."""
import json
import os
import sqlite3
import time

TERMINAL = {"committed", "review", "skipped", "failed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(uuid TEXT PRIMARY KEY, status TEXT, work_dir TEXT, attempts INTEGER DEFAULT 0,
                                 detail TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS jobs(uuid TEXT PRIMARY KEY, meeting_id TEXT, host_email TEXT, topic TEXT,
                                status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS reviews(task_id TEXT PRIMARY KEY, uuid TEXT, proposed TEXT, status TEXT, created REAL);
"""


class State:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # items -------------------------------------------------------------
    def get_item(self, uuid):
        r = self.db.execute("SELECT * FROM items WHERE uuid=?", (uuid,)).fetchone()
        return dict(r) if r else None

    def set_item(self, uuid, status, work_dir=None, detail="", attempts=None):
        cur = self.get_item(uuid) or {}
        self.db.execute(
            "INSERT OR REPLACE INTO items(uuid,status,work_dir,attempts,detail,updated) VALUES(?,?,?,?,?,?)",
            (uuid, status, work_dir or cur.get("work_dir"),
             cur.get("attempts", 0) if attempts is None else attempts, detail, time.time()))
        self.db.commit()

    def list_items(self, status):
        return [dict(r) for r in self.db.execute("SELECT * FROM items WHERE status=? ORDER BY updated", (status,))]

    def counts(self):
        return {r[0]: r[1] for r in self.db.execute("SELECT status, COUNT(*) FROM items GROUP BY status")}

    # webhook jobs ------------------------------------------------------
    def enqueue_job(self, uuid, meeting_id, host_email, topic):
        self.db.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)",
                        (uuid, str(meeting_id), (host_email or "").lower(), topic or "", "pending", time.time()))
        self.db.commit()

    def pending_jobs(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created")]

    def finish_job(self, uuid, status):
        self.db.execute("UPDATE jobs SET status=? WHERE uuid=?", (status, uuid))
        self.db.commit()

    def purge_old(self, days):
        """Delete finished rows older than `days`. Open reviews and pending jobs are kept."""
        cutoff = time.time() - days * 86400
        c1 = self.db.execute("DELETE FROM items WHERE status IN ('committed','review','skipped','failed') AND updated<?", (cutoff,)).rowcount
        c2 = self.db.execute("DELETE FROM reviews WHERE status!='open' AND created<?", (cutoff,)).rowcount
        c3 = self.db.execute("DELETE FROM jobs WHERE status!='pending' AND created<?", (cutoff,)).rowcount
        self.db.commit()
        return {"items": c1, "reviews": c2, "jobs": c3}

    # reviews -----------------------------------------------------------
    def add_review(self, task_id, uuid, proposed):
        self.db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                        (str(task_id), uuid, json.dumps(proposed), "open", time.time()))
        self.db.commit()

    def open_reviews(self):
        rows = [dict(r) for r in self.db.execute("SELECT * FROM reviews WHERE status='open'")]
        for r in rows:
            r["proposed"] = json.loads(r["proposed"])
        return rows

    def close_review(self, task_id, status):
        self.db.execute("UPDATE reviews SET status=? WHERE task_id=?", (status, str(task_id)))
        self.db.commit()
