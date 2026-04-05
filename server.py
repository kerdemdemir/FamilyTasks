"""Parent Task Tracker — tracks child tasks, savings, and screen time penalties."""

import asyncio
import hashlib
import sqlite3
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH    = Path(__file__).parent / "data" / "tracker.db"
STATIC_DIR = Path(__file__).parent / "static"

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# SHA-256 of the parent password
PARENT_TOKEN = hashlib.sha256(b"erdemdem").hexdigest()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_parent(authorization: str = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(403, "Parent access required")
    token = authorization.removeprefix("Bearer ").strip()
    if token != PARENT_TOKEN:
        raise HTTPException(403, "Invalid token")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_period(frequency: str, for_date: date | None = None) -> str:
    d = for_date or date.today()
    if frequency == "daily":
        return d.isoformat()
    elif frequency == "weekly":
        iso = d.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return datetime.now().isoformat()


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL,
            type             TEXT    NOT NULL,
            frequency        TEXT    NOT NULL,
            reward_dkk       REAL    NOT NULL DEFAULT 10,
            is_active        INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS completions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER NOT NULL REFERENCES tasks(id),
            period       TEXT    NOT NULL,
            completed_at TEXT    NOT NULL,
            UNIQUE(task_id, period)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            amount        REAL    NOT NULL,
            description   TEXT    NOT NULL,
            completion_id INTEGER REFERENCES completions(id),
            created_at    TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            period  TEXT    NOT NULL,
            type    TEXT    NOT NULL DEFAULT 'missed',
            sent_at TEXT    NOT NULL,
            UNIQUE(task_id, period, type)
        );
        CREATE TABLE IF NOT EXISTS whatsapp_contacts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            label      TEXT    NOT NULL,
            phone      TEXT    NOT NULL,
            apikey     TEXT    NOT NULL,
            is_active  INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL
        );
    """)

    # Migrations: add deadline columns if missing
    for col, default in [("deadline_hour", 20), ("deadline_weekday", 4)]:
        try:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
        except Exception:
            pass

    # Migration: add type column to notifications (recreate table to change UNIQUE constraint)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()]
    if "type" not in cols:
        conn.executescript("""
            ALTER TABLE notifications RENAME TO notifications_old;
            CREATE TABLE notifications (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                period  TEXT    NOT NULL,
                type    TEXT    NOT NULL DEFAULT 'missed',
                sent_at TEXT    NOT NULL,
                UNIQUE(task_id, period, type)
            );
            INSERT INTO notifications (task_id, period, type, sent_at)
            SELECT task_id, period, 'missed', sent_at FROM notifications_old;
            DROP TABLE notifications_old;
        """)

    # Seed default tasks
    if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
        now = datetime.now().isoformat()
        conn.executemany(
            """INSERT INTO tasks
               (name, type, frequency, reward_dkk, is_active, deadline_hour, deadline_weekday, created_at)
               VALUES (?,?,?,?,1,?,?,?)""",
            [
                ("Maths homework",         "mandatory", "weekly", 10, 20, 4, now),
                ("Daily reading",          "mandatory", "daily",  10, 20, 4, now),
                ("Solve 15 chess puzzles", "optional",  "manual", 15, 20, 4, now),
                ("Wash dishes",            "optional",  "manual", 15, 20, 4, now),
            ],
        )

    # Seed default settings
    for key, value in {
        "child_name":              "Ela",
        "goal_name":               "iPad Pen",
        "goal_amount":             "1200",
        "screen_time_penalty_min": "30",
        "whatsapp_phone":          "",
        "whatsapp_apikey":         "",
        "site_url":                "",
        "strike_count":            "0",
        "strike_max":              "3",
        "strike_punishment":       "Sleepover with friend will be cancelled",
    }.items():
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (key, value))

    # Seed opening balance (once only)
    if not conn.execute(
        "SELECT 1 FROM transactions WHERE description='Opening balance'"
    ).fetchone():
        conn.execute(
            "INSERT INTO transactions (amount, description, completion_id, created_at) VALUES (?,?,NULL,?)",
            (700, "Opening balance", datetime.now().isoformat()),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# WhatsApp via CallMeBot
# ---------------------------------------------------------------------------

def send_whatsapp(phone: str, apikey: str, message: str):
    url = (
        "https://api.callmebot.com/whatsapp.php"
        f"?phone={urllib.parse.quote(phone)}"
        f"&text={urllib.parse.quote(message)}"
        f"&apikey={urllib.parse.quote(apikey)}"
    )
    urllib.request.urlopen(url, timeout=10)


def _deadline_dt(task: dict, today: date) -> datetime | None:
    """Return the deadline datetime for a mandatory task."""
    from datetime import time as dtime
    dl_h  = task["deadline_hour"]    if task["deadline_hour"]    is not None else 20
    dl_wd = task["deadline_weekday"] if task["deadline_weekday"] is not None else 4
    t     = dtime(hour=dl_h)
    if task["frequency"] == "daily":
        return datetime.combine(today, t)
    elif task["frequency"] == "weekly":
        days_until = (dl_wd - today.weekday()) % 7
        return datetime.combine(today + timedelta(days=days_until), t)
    return None


def _task_period(task: dict, today: date) -> str:
    if task["frequency"] == "daily":
        return today.isoformat()
    elif task["frequency"] == "weekly":
        iso = today.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return ""


def _broadcast(contacts, msg, label):
    sent_any = False
    for c in contacts:
        try:
            send_whatsapp(c["phone"], c["apikey"], msg)
            sent_any = True
            print(f"[{label}] → {c['label']}")
        except Exception as e:
            print(f"[{label}] failed for {c['label']}: {e}")
    return sent_any


def check_and_notify():
    conn = get_db()
    try:
        contacts = [dict(r) for r in conn.execute(
            "SELECT * FROM whatsapp_contacts WHERE is_active=1"
        )]
        if not contacts:
            return

        s        = _settings(conn)
        today    = date.today()
        now      = datetime.now()
        penalty  = int(s.get("screen_time_penalty_min", 30))
        child    = s.get("child_name", "Ela")
        site_url = s.get("site_url", "").strip()
        link     = f"\n🔗 {site_url}" if site_url else ""
        completions = _current_completions(conn, today)

        tasks = [dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE type='mandatory' AND is_active=1"
        )]

        # (type, threshold_minutes, fires_when minutes_left <= threshold)
        REMINDERS = [
            ("6h",     360),
            ("2h",     120),
            ("missed",   0),
        ]

        for t in tasks:
            if t["id"] in completions:
                continue

            period = _task_period(t, today)
            dl     = _deadline_dt(t, today)
            if not period or not dl:
                continue

            mins_left = (dl - now).total_seconds() / 60

            for notif_type, threshold in REMINDERS:
                if mins_left > threshold:
                    continue

                if conn.execute(
                    "SELECT 1 FROM notifications WHERE task_id=? AND period=? AND type=?",
                    (t["id"], period, notif_type)
                ).fetchone():
                    continue

                h = max(0, int(mins_left // 60))
                m = max(0, int(mins_left % 60))

                if notif_type == "6h":
                    msg = (
                        f"⏰ Reminder for {child}\n"
                        f"{t['name']} is due in {h}h {m:02d}m — don't forget!{link}"
                    )
                elif notif_type == "2h":
                    msg = (
                        f"⚡ Only {h}h {m:02d}m left!\n"
                        f"{child} still needs to: {t['name']}{link}"
                    )
                else:
                    msg = (
                        f"⚠️ {child} missed: {t['name']}\n"
                        f"Reduce screen time by {penalty} min in Family Link.{link}"
                    )

                if _broadcast(contacts, msg, f"{notif_type}:{t['name']}"):
                    conn.execute(
                        "INSERT INTO notifications (task_id, period, type, sent_at) VALUES (?,?,?,?)",
                        (t["id"], period, notif_type, now.isoformat())
                    )
                    conn.commit()
    finally:
        conn.close()


async def _notification_loop():
    while True:
        await asyncio.sleep(60)
        try:
            check_and_notify()
        except Exception as e:
            print(f"Notification loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(_notification_loop())
    yield
    task.cancel()


app = FastAPI(title="Parent Task Tracker", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def _balance(conn) -> float:
    return round(conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions").fetchone()[0], 2)


def _current_completions(conn, today: date) -> dict[int, dict]:
    daily_p  = today.isoformat()
    iso      = today.isocalendar()
    weekly_p = f"{iso.year}-W{iso.week:02d}"
    rows = conn.execute("""
        SELECT c.*, t.frequency FROM completions c
        JOIN tasks t ON t.id = c.task_id
        WHERE (t.frequency='daily'  AND c.period=?)
           OR (t.frequency='weekly' AND c.period=?)
    """, (daily_p, weekly_p)).fetchall()
    return {r["task_id"]: dict(r) for r in rows}


def _mandatory_tasks_status(conn, today: date, now_hour: int) -> dict:
    """
    Return status for each active mandatory task plus the total screen-time penalty.
    Status per task:
      done    – completed this period
      pending – not done but deadline not yet reached
      missed  – not done and deadline has passed → penalty applies
    """
    s           = _settings(conn)
    penalty_min = int(s.get("screen_time_penalty_min", 30))
    completions = _current_completions(conn, today)

    tasks = [dict(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE type='mandatory' AND is_active=1 ORDER BY frequency, name"
    )]

    result        = []
    total_penalty = 0

    for t in tasks:
        dl_h  = t["deadline_hour"]    if t["deadline_hour"]    is not None else 20
        dl_wd = t["deadline_weekday"] if t["deadline_weekday"] is not None else 4

        if t["id"] in completions:
            status   = "done"
            due_label = ""
        elif t["frequency"] == "daily":
            past = now_hour >= dl_h
            if past:
                status = "missed"
                total_penalty += penalty_min
            else:
                status = "pending"
            due_label = f"Daily by {dl_h}:00"
        else:  # weekly
            past = today.weekday() > dl_wd or (today.weekday() == dl_wd and now_hour >= dl_h)
            if past:
                status = "missed"
                total_penalty += penalty_min
            else:
                status = "pending"
            due_label = f"Weekly by {DAYS[dl_wd]} {dl_h}:00"

        result.append({
            "id":             t["id"],
            "name":           t["name"],
            "frequency":      t["frequency"],
            "reward_dkk":     t["reward_dkk"],
            "deadline_hour":  dl_h,
            "deadline_weekday": dl_wd,
            "status":         status,
            "due_label":      due_label,
            "done":           status == "done",
            "period":         (today.isoformat() if t["frequency"] == "daily"
                               else f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"),
        })

    return {
        "tasks":                result,
        "total_penalty_minutes": total_penalty,
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AuthRequest(BaseModel):
    password: str

class TaskCreate(BaseModel):
    name:             str
    type:             str
    frequency:        str
    reward_dkk:       float = 10.0
    deadline_hour:    int   = 20
    deadline_weekday: int   = 4

class TaskUpdate(BaseModel):
    name:             Optional[str]   = None
    reward_dkk:       Optional[float] = None
    is_active:        Optional[bool]  = None
    deadline_hour:    Optional[int]   = None
    deadline_weekday: Optional[int]   = None

class TransactionCreate(BaseModel):
    amount:      float
    description: str

class SettingsUpdate(BaseModel):
    child_name:              Optional[str]   = None
    goal_name:               Optional[str]   = None
    goal_amount:             Optional[float] = None
    screen_time_penalty_min: Optional[int]   = None
    whatsapp_phone:          Optional[str]   = None
    whatsapp_apikey:         Optional[str]   = None
    site_url:                Optional[str]   = None


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/auth")
def auth(body: AuthRequest):
    token = hashlib.sha256(body.password.encode()).hexdigest()
    if token != PARENT_TOKEN:
        raise HTTPException(401, "Wrong password")
    return {"token": PARENT_TOKEN, "role": "parent"}


@app.get("/api/dashboard")
def dashboard():
    conn = get_db()
    try:
        s           = _settings(conn)
        balance     = _balance(conn)
        goal_amount = float(s.get("goal_amount", 1200))
        today       = date.today()
        now_hour    = datetime.now().hour

        mandatory   = _mandatory_tasks_status(conn, today, now_hour)

        optional_tasks = [dict(t) for t in conn.execute(
            "SELECT * FROM tasks WHERE type='optional' AND is_active=1 ORDER BY name"
        )]

        transactions = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions ORDER BY created_at DESC LIMIT 30"
        )]

        return {
            "settings":          s,
            "balance":           balance,
            "goal_amount":       goal_amount,
            "goal_progress":     min(balance / goal_amount * 100, 100) if goal_amount > 0 else 0,
            "mandatory":         mandatory,
            "optional_tasks":    optional_tasks,
            "transactions":      transactions,
            "today":             today.isoformat(),
            "week":              f"{today.isocalendar().year}-W{today.isocalendar().week:02d}",
            "strikes": {
                "count":      int(s.get("strike_count", 0)),
                "max":        int(s.get("strike_max", 3)),
                "punishment": s.get("strike_punishment", ""),
            },
        }
    finally:
        conn.close()


@app.get("/api/tasks")
def list_tasks():
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tasks ORDER BY type, frequency, name"
        )]
    finally:
        conn.close()


@app.post("/api/tasks", status_code=201, dependencies=[Depends(require_parent)])
def create_task(body: TaskCreate):
    if body.type not in ("mandatory", "optional"):
        raise HTTPException(400, "type must be mandatory or optional")
    if body.frequency not in ("daily", "weekly", "manual"):
        raise HTTPException(400, "frequency must be daily, weekly, or manual")
    conn = get_db()
    try:
        now = datetime.now().isoformat()
        cur = conn.execute(
            """INSERT INTO tasks
               (name,type,frequency,reward_dkk,is_active,deadline_hour,deadline_weekday,created_at)
               VALUES (?,?,?,?,1,?,?,?)""",
            (body.name, body.type, body.frequency, body.reward_dkk,
             body.deadline_hour, body.deadline_weekday, now),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.put("/api/tasks/{task_id}", dependencies=[Depends(require_parent)])
def update_task(task_id: int, body: TaskUpdate):
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            raise HTTPException(404, "Task not found")
        updates: dict = {}
        if body.name             is not None: updates["name"]             = body.name
        if body.reward_dkk       is not None: updates["reward_dkk"]       = body.reward_dkk
        if body.is_active        is not None: updates["is_active"]        = 1 if body.is_active else 0
        if body.deadline_hour    is not None: updates["deadline_hour"]    = body.deadline_hour
        if body.deadline_weekday is not None: updates["deadline_weekday"] = body.deadline_weekday
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE id=?", (*updates.values(), task_id))
            conn.commit()
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_parent)])
def delete_task(task_id: int):
    conn = get_db()
    try:
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            raise HTTPException(404, "Task not found")
        conn.execute("DELETE FROM completions WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/complete/{task_id}", dependencies=[Depends(require_parent)])
def complete_task(task_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND is_active=1", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        task = dict(row)
        now  = datetime.now()

        if task["frequency"] == "manual":
            period = now.isoformat()
        else:
            period = get_period(task["frequency"])
            if conn.execute(
                "SELECT 1 FROM completions WHERE task_id=? AND period=?", (task_id, period)
            ).fetchone():
                raise HTTPException(400, "Already completed for this period")

        cur = conn.execute(
            "INSERT INTO completions (task_id, period, completed_at) VALUES (?,?,?)",
            (task_id, period, now.isoformat()),
        )
        conn.execute(
            "INSERT INTO transactions (amount, description, completion_id, created_at) VALUES (?,?,?,?)",
            (task["reward_dkk"], f"Completed: {task['name']}", cur.lastrowid, now.isoformat()),
        )
        conn.commit()
        return {"status": "ok", "earned": task["reward_dkk"], "balance": _balance(conn)}
    finally:
        conn.close()


@app.delete("/api/complete/{task_id}", dependencies=[Depends(require_parent)])
def uncomplete_task(task_id: int):
    conn = get_db()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        task = dict(task)
        if task["frequency"] == "manual":
            raise HTTPException(400, "Cannot undo manual tasks this way")
        period = get_period(task["frequency"])
        comp = conn.execute(
            "SELECT * FROM completions WHERE task_id=? AND period=?", (task_id, period)
        ).fetchone()
        if not comp:
            raise HTTPException(404, "No completion found for this period")
        conn.execute("DELETE FROM transactions WHERE completion_id=?", (comp["id"],))
        conn.execute("DELETE FROM completions  WHERE id=?",            (comp["id"],))
        conn.commit()
        return {"status": "ok", "balance": _balance(conn)}
    finally:
        conn.close()


@app.post("/api/transactions", status_code=201, dependencies=[Depends(require_parent)])
def add_transaction(body: TransactionCreate):
    conn = get_db()
    try:
        now = datetime.now().isoformat()
        cur = conn.execute(
            "INSERT INTO transactions (amount, description, completion_id, created_at) VALUES (?,?,NULL,?)",
            (body.amount, body.description, now),
        )
        conn.commit()
        return {"status": "ok", "id": cur.lastrowid, "balance": _balance(conn)}
    finally:
        conn.close()


@app.get("/api/transactions")
def list_transactions(limit: int = 50):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"transactions": [dict(r) for r in rows], "balance": _balance(conn)}
    finally:
        conn.close()


@app.get("/api/settings")
def get_settings_route():
    conn = get_db()
    try:
        return _settings(conn)
    finally:
        conn.close()


@app.put("/api/settings", dependencies=[Depends(require_parent)])
def update_settings(body: SettingsUpdate):
    conn = get_db()
    try:
        updates: dict[str, str] = {}
        if body.child_name              is not None: updates["child_name"]              = body.child_name
        if body.goal_name               is not None: updates["goal_name"]               = body.goal_name
        if body.goal_amount             is not None: updates["goal_amount"]             = str(body.goal_amount)
        if body.screen_time_penalty_min is not None: updates["screen_time_penalty_min"] = str(body.screen_time_penalty_min)
        if body.whatsapp_phone          is not None: updates["whatsapp_phone"]          = body.whatsapp_phone
        if body.whatsapp_apikey         is not None: updates["whatsapp_apikey"]         = body.whatsapp_apikey
        if body.site_url               is not None: updates["site_url"]               = body.site_url
        for k, v in updates.items():
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, v))
        conn.commit()
        return _settings(conn)
    finally:
        conn.close()


@app.get("/api/whatsapp/contacts")
def list_contacts():
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, label, phone, is_active FROM whatsapp_contacts ORDER BY id"
        )]
    finally:
        conn.close()


class ContactCreate(BaseModel):
    label:  str
    phone:  str
    apikey: str

@app.post("/api/whatsapp/contacts", status_code=201, dependencies=[Depends(require_parent)])
def add_contact(body: ContactCreate):
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO whatsapp_contacts (label, phone, apikey, is_active, created_at) VALUES (?,?,?,1,?)",
            (body.label, body.phone, body.apikey, datetime.now().isoformat())
        )
        conn.commit()
        return {"id": cur.lastrowid, "label": body.label, "phone": body.phone, "is_active": 1}
    finally:
        conn.close()


@app.delete("/api/whatsapp/contacts/{contact_id}", dependencies=[Depends(require_parent)])
def delete_contact(contact_id: int):
    conn = get_db()
    try:
        conn.execute("DELETE FROM whatsapp_contacts WHERE id=?", (contact_id,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


@app.post("/api/whatsapp/test", dependencies=[Depends(require_parent)])
def test_whatsapp():
    conn = get_db()
    try:
        contacts = [dict(r) for r in conn.execute(
            "SELECT * FROM whatsapp_contacts WHERE is_active=1"
        )]
        if not contacts:
            raise HTTPException(400, "No active WhatsApp contacts configured")
        child   = _settings(conn).get("child_name", "Ela")
        msg     = f"✅ Test from {child}'s Task Tracker — notifications are working!"
        results = []
        for c in contacts:
            try:
                send_whatsapp(c["phone"], c["apikey"], msg)
                results.append({"label": c["label"], "ok": True})
            except Exception as e:
                results.append({"label": c["label"], "ok": False, "error": str(e)})
        return {"results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Strikes
# ---------------------------------------------------------------------------

class StrikesUpdate(BaseModel):
    max:        Optional[int] = None
    punishment: Optional[str] = None

def _send_strike_notification(conn, count: int, max_s: int, punishment: str, child: str, s: dict):
    contacts = [dict(r) for r in conn.execute(
        "SELECT * FROM whatsapp_contacts WHERE is_active=1"
    )]
    if not contacts:
        return
    site_url = s.get("site_url", "").strip()
    link     = f"\n🔗 {site_url}" if site_url else ""

    if count >= max_s:
        msg = (
            f"🚨 {child} has reached {count}/{max_s} strikes!\n"
            f"Punishment: {punishment}{link}"
        )
    else:
        remaining = max_s - count
        msg = (
            f"⚡ Strike {count}/{max_s} for {child}!\n"
            f"{remaining} more strike(s) until: {punishment}{link}"
        )
    _broadcast(contacts, msg, f"strike-{count}")


@app.get("/api/strikes")
def get_strikes():
    conn = get_db()
    try:
        s = _settings(conn)
        return {
            "count":      int(s.get("strike_count", 0)),
            "max":        int(s.get("strike_max", 3)),
            "punishment": s.get("strike_punishment", ""),
        }
    finally:
        conn.close()


@app.post("/api/strikes/add", dependencies=[Depends(require_parent)])
def add_strike():
    conn = get_db()
    try:
        s          = _settings(conn)
        count      = int(s.get("strike_count", 0)) + 1
        max_s      = int(s.get("strike_max", 3))
        punishment = s.get("strike_punishment", "")
        child      = s.get("child_name", "Ela")
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('strike_count',?)", (str(count),))
        conn.commit()
        _send_strike_notification(conn, count, max_s, punishment, child, s)
        return {"count": count, "max": max_s, "punishment": punishment}
    finally:
        conn.close()


@app.post("/api/strikes/remove", dependencies=[Depends(require_parent)])
def remove_strike():
    conn = get_db()
    try:
        s     = _settings(conn)
        count = max(0, int(s.get("strike_count", 0)) - 1)
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('strike_count',?)", (str(count),))
        conn.commit()
        return {"count": count, "max": int(s.get("strike_max", 3))}
    finally:
        conn.close()


@app.post("/api/strikes/reset", dependencies=[Depends(require_parent)])
def reset_strikes():
    conn = get_db()
    try:
        s     = _settings(conn)
        child = s.get("child_name", "Ela")
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('strike_count','0')")
        conn.commit()
        contacts = [dict(r) for r in conn.execute("SELECT * FROM whatsapp_contacts WHERE is_active=1")]
        if contacts:
            site_url = s.get("site_url", "").strip()
            link     = f"\n🔗 {site_url}" if site_url else ""
            _broadcast(contacts, f"✅ All strikes cleared for {child}!{link}", "strike-reset")
        return {"count": 0}
    finally:
        conn.close()


@app.put("/api/strikes", dependencies=[Depends(require_parent)])
def update_strikes(body: StrikesUpdate):
    conn = get_db()
    try:
        if body.max        is not None:
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('strike_max',?)", (str(body.max),))
        if body.punishment is not None:
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('strike_punishment',?)", (body.punishment,))
        conn.commit()
        s = _settings(conn)
        return {"count": int(s["strike_count"]), "max": int(s["strike_max"]), "punishment": s["strike_punishment"]}
    finally:
        conn.close()


@app.get("/api/history")
def history(days: int = 30):
    conn = get_db()
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        rows  = conn.execute("""
            SELECT c.*, t.name as task_name, t.type as task_type,
                   t.frequency, t.reward_dkk
            FROM completions c JOIN tasks t ON t.id = c.task_id
            WHERE c.completed_at >= ?
            ORDER BY c.completed_at DESC
        """, (since,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8092, reload=True)
