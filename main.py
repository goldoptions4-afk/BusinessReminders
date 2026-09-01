"""
Team Reminder Bot + Dashboard (v4, multi-user)
One Telegram bot, many people: each approved user has their own private task
list, reminders, snooze, daily summary and dashboard link. The admin approves
new members and can see everyone's tasks on their own dashboard.

Reminders: Telegram always; email per user (sent from the shared Gmail, set
with "myemail you@x.com"); WhatsApp per user via CallMeBot (set with
"mywhatsapp +44... APIKEY").

Runs as a single process. Start command: python main.py
"""

import base64
import json
import logging
import os
import secrets
import smtplib
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reminderbot")

# ---------------------------------------------------------------- config

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
TIMEZONE = os.environ.get("TIMEZONE", "Europe/London")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")  # optional; else first user is admin

NAG_MINUTES = int(os.environ.get("NAG_MINUTES", "15"))
NO_DUE_NAG_MINUTES = int(os.environ.get("NO_DUE_NAG_MINUTES", "120"))
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))  # -1 disables
QUIET_START = int(os.environ.get("QUIET_START", "-1"))
QUIET_END = int(os.environ.get("QUIET_END", "7"))

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

PORT = int(os.environ.get("PORT", "8080"))

_default_data = "/data" if os.path.isdir("/data") else "."
DATA_DIR = os.environ.get("DATA_DIR", _default_data)
DB_PATH = os.path.join(DATA_DIR, "tasks.db")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TZ = ZoneInfo(TIMEZONE)

# ---------------------------------------------------------------- storage


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            due_utc TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            stage TEXT NOT NULL,
            next_fire_utc TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            completed_utc TEXT,
            user_id TEXT
        )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            name TEXT,
            approved INTEGER NOT NULL DEFAULT 0,
            is_admin INTEGER NOT NULL DEFAULT 0,
            token TEXT,
            email TEXT,
            wa_phone TEXT,
            wa_key TEXT,
            created_utc TEXT
        )"""
    )
    for stmt in (
        "ALTER TABLE tasks ADD COLUMN completed_utc TEXT",
        "ALTER TABLE tasks ADD COLUMN user_id TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def migrate_legacy():
    """Assign pre-multiuser tasks to the admin and make the admin a user row."""
    conn = db()
    owner = OWNER_CHAT_ID or (
        (conn.execute("SELECT v FROM meta WHERE k='owner_chat_id'").fetchone() or [None])[0]
    )
    if owner:
        owner = str(owner)
        conn.execute("UPDATE tasks SET user_id=? WHERE user_id IS NULL", (owner,))
        row = conn.execute("SELECT chat_id FROM users WHERE chat_id=?", (owner,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (chat_id, name, approved, is_admin, token, created_utc) "
                "VALUES (?, 'Admin', 1, 1, ?, ?)",
                (owner, secrets.token_hex(8), iso(datetime.now(timezone.utc))),
            )
    conn.commit()
    conn.close()


def meta_get(key):
    conn = db()
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def meta_set(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def from_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def fmt_local(dt_utc):
    return dt_utc.astimezone(TZ).strftime("%a %d %b, %H:%M")


# ---------------------------------------------------------------- users


def get_user(chat_id):
    conn = db()
    row = conn.execute(
        "SELECT chat_id, name, approved, is_admin, token, email, wa_phone, wa_key "
        "FROM users WHERE chat_id=?",
        (str(chat_id),),
    ).fetchone()
    conn.close()
    if not row:
        return None
    keys = ["chat_id", "name", "approved", "is_admin", "token", "email", "wa_phone", "wa_key"]
    return dict(zip(keys, row))


def user_by_token(token):
    conn = db()
    row = conn.execute(
        "SELECT chat_id, name, approved, is_admin FROM users WHERE token=? AND approved=1",
        (token,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"chat_id": row[0], "name": row[1], "approved": row[2], "is_admin": row[3]}


def all_users(approved_only=True):
    conn = db()
    q = "SELECT chat_id, name, approved, is_admin, email, wa_phone, wa_key FROM users"
    if approved_only:
        q += " WHERE approved=1"
    rows = conn.execute(q).fetchall()
    conn.close()
    keys = ["chat_id", "name", "approved", "is_admin", "email", "wa_phone", "wa_key"]
    return [dict(zip(keys, r)) for r in rows]


def create_user(chat_id, name, approved=0, is_admin=0):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users (chat_id, name, approved, is_admin, token, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(chat_id), name or "Someone", approved, is_admin, secrets.token_hex(8), iso(utcnow())),
    )
    conn.commit()
    conn.close()


def set_user_field(chat_id, field, value):
    assert field in ("approved", "email", "wa_phone", "wa_key", "name")
    conn = db()
    conn.execute(f"UPDATE users SET {field}=? WHERE chat_id=?", (value, str(chat_id)))
    conn.commit()
    conn.close()


def admin_chat():
    if OWNER_CHAT_ID:
        return str(OWNER_CHAT_ID)
    for u in all_users():
        if u["is_admin"]:
            return u["chat_id"]
    return meta_get("owner_chat_id")


# ---------------------------------------------------------------- scheduling


def initial_schedule(due_utc):
    now = utcnow()
    if due_utc is None:
        return "nodue", now + timedelta(minutes=1)
    if now < due_utc - timedelta(hours=24):
        return "pre24", due_utc - timedelta(hours=24)
    if now < due_utc - timedelta(hours=1):
        return "pre1", due_utc - timedelta(hours=1)
    if now < due_utc:
        return "due", due_utc
    return "nag", now + timedelta(minutes=1)


def next_schedule(stage, due_utc):
    now = utcnow()
    if stage == "nodue":
        return "nodue", now + timedelta(minutes=NO_DUE_NAG_MINUTES)
    if stage == "pre24":
        if due_utc - timedelta(hours=1) > now:
            return "pre1", due_utc - timedelta(hours=1)
        return "due", max(due_utc, now + timedelta(minutes=1))
    if stage == "pre1":
        return "due", max(due_utc, now + timedelta(minutes=1))
    return "nag", now + timedelta(minutes=NAG_MINUTES)


def reminder_text(task_text, due_utc, stage):
    if stage == "pre24":
        return f"Tomorrow: {task_text} ({fmt_local(due_utc)})"
    if stage == "pre1":
        return f"In 1 hour: {task_text} ({fmt_local(due_utc)})"
    if stage == "due":
        return f"NOW: {task_text}"
    if stage == "nag":
        when = f" (was due {fmt_local(due_utc)})" if due_utc else ""
        return f"Still not done: {task_text}{when}"
    return f"Reminder: {task_text}"


def in_quiet_hours(now_local):
    if QUIET_START < 0:
        return False
    h = now_local.hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END


# ---------------------------------------------------------------- channels


def tg(method, payload):
    try:
        r = requests.post(f"{TG_API}/{method}", json=payload, timeout=60)
        return r.json()
    except Exception as e:
        log.error("telegram %s failed: %s", method, e)
        return {}


def send_telegram(chat_id, text, task_id=None, snooze=False, approve_id=None):
    payload = {"chat_id": chat_id, "text": text}
    rows = []
    if task_id is not None:
        rows.append([{"text": "✅ Done", "callback_data": f"done:{task_id}"}])
        if snooze:
            rows.append(
                [
                    {"text": "💤 1h", "callback_data": f"snz:{task_id}:60"},
                    {"text": "💤 3h", "callback_data": f"snz:{task_id}:180"},
                    {"text": "💤 Tomorrow", "callback_data": f"snz:{task_id}:tom"},
                ]
            )
    if approve_id is not None:
        rows.append(
            [
                {"text": "✅ Approve", "callback_data": f"appr:{approve_id}"},
                {"text": "🚫 Reject", "callback_data": f"rej:{approve_id}"},
            ]
        )
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}
    tg("sendMessage", payload)


def send_email(to_addr, subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and to_addr):
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = to_addr
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_USER, [to_addr], msg.as_string())
    except Exception as e:
        log.error("email failed: %s", e)


def send_whatsapp(phone, apikey, text):
    if not (phone and apikey):
        return
    try:
        url = (
            "https://api.callmebot.com/whatsapp.php?phone="
            + urllib.parse.quote(phone)
            + "&apikey=" + urllib.parse.quote(apikey)
            + "&text=" + urllib.parse.quote(text)
        )
        requests.get(url, timeout=30)
    except Exception as e:
        log.error("callmebot failed: %s", e)


def broadcast(user, text, task_id, snooze=True):
    send_telegram(user["chat_id"], text, task_id, snooze=snooze)
    send_email(user.get("email"), f"Reminder: {text[:80]}", text)
    send_whatsapp(user.get("wa_phone"), user.get("wa_key"), text)


# ---------------------------------------------------------------- Claude parsing


PARSE_INSTRUCTIONS = (
    "You extract a reminder task from the user's message or image. "
    "The image may be a screenshot or photo of an appointment letter, booking "
    "confirmation, calendar entry or similar. Current date and time in the user's "
    "timezone ({tz}): {now}. Respond with ONLY a JSON object, no other text: "
    '{{"task": "<short description>", "due": "<YYYY-MM-DD HH:MM in the user\'s '
    'timezone, or null if no date or time can be determined>"}}. '
    "If a date is given without a time, use 09:00. Interpret relative dates "
    "(tomorrow, friday) from the current date. Keep the task under 15 words."
)


def fallback_parse(text):
    try:
        import parsedatetime

        cal = parsedatetime.Calendar()
        now_local = datetime.now(TZ).replace(tzinfo=None)
        found = cal.nlp(text, sourceTime=now_local)
    except Exception as e:
        log.error("parsedatetime failed: %s", e)
        found = None
    if not found:
        return text.strip(), None
    dt, flag, _start, _end, matched = found[0]
    task = text.replace(matched.strip(), "").strip(" ,.-@")
    due = dt.replace(tzinfo=TZ)
    now = datetime.now(TZ)
    if due <= now and flag == 2 and due.hour < 12 and "am" not in matched.lower():
        due = due + timedelta(hours=12)
    while due <= now:
        due = due + timedelta(days=1)
    return (task or text.strip()), due.astimezone(timezone.utc)


def claude_parse(text=None, image_b64=None, image_media_type="image/jpeg"):
    if not ANTHROPIC_API_KEY:
        return fallback_parse(text) if text else (None, None)
    now_local = datetime.now(TZ).strftime("%A %Y-%m-%d %H:%M")
    system = PARSE_INSTRUCTIONS.format(tz=TIMEZONE, now=now_local)
    content = []
    if image_b64:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image_media_type, "data": image_b64},
            }
        )
    content.append({"type": "text", "text": text or "Extract the appointment from this image."})
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 300,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=60,
        )
        raw = r.json()["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        task = (data.get("task") or "").strip()
        due = None
        if data.get("due"):
            due_local = datetime.strptime(data["due"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            due = due_local.astimezone(timezone.utc)
        return (task or None), due
    except Exception as e:
        log.error("claude parse failed: %s", e)
        return fallback_parse(text) if text else (None, None)


# ---------------------------------------------------------------- task ops


def create_task(user_id, task_text, due_utc, notify_chat=None):
    stage, next_fire = initial_schedule(due_utc)
    conn = db()
    cur = conn.execute(
        "INSERT INTO tasks (text, due_utc, status, stage, next_fire_utc, created_utc, user_id) "
        "VALUES (?, ?, 'active', ?, ?, ?, ?)",
        (
            task_text,
            iso(due_utc) if due_utc else None,
            stage,
            iso(next_fire),
            iso(utcnow()),
            str(user_id),
        ),
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    if due_utc:
        plan = (
            f"Saved (#{task_id}): {task_text}\nDue: {fmt_local(due_utc)}\n"
            f"I'll remind you 24h before, 1h before, then every {NAG_MINUTES} min "
            f"from the due time until you say done."
        )
    else:
        plan = (
            f"Saved (#{task_id}): {task_text}\nNo date/time found, so I'll nag you "
            f"every {NO_DUE_NAG_MINUTES // 60}h until you say done."
        )
    if notify_chat:
        send_telegram(notify_chat, plan, task_id)
    return task_id, plan


def active_tasks(user_id=None):
    conn = db()
    if user_id is None:
        rows = conn.execute(
            "SELECT id, text, due_utc, stage, next_fire_utc, user_id FROM tasks "
            "WHERE status='active' ORDER BY COALESCE(due_utc, created_utc)"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, due_utc, stage, next_fire_utc, user_id FROM tasks "
            "WHERE status='active' AND user_id=? ORDER BY COALESCE(due_utc, created_utc)",
            (str(user_id),),
        ).fetchall()
    conn.close()
    return rows


def recent_closed(user_id=None, limit=20):
    conn = db()
    if user_id is None:
        rows = conn.execute(
            "SELECT id, text, status, completed_utc, user_id FROM tasks "
            "WHERE status IN ('done','deleted') "
            "ORDER BY COALESCE(completed_utc, created_utc) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, status, completed_utc, user_id FROM tasks "
            "WHERE status IN ('done','deleted') AND user_id=? "
            "ORDER BY COALESCE(completed_utc, created_utc) DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
    conn.close()
    return rows


def task_owner(task_id):
    conn = db()
    row = conn.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def can_touch(chat_id, task_id):
    u = get_user(chat_id)
    if not u or not u["approved"]:
        return False
    return u["is_admin"] or task_owner(task_id) == str(chat_id)


def complete_task(task_id, new_status="done"):
    conn = db()
    cur = conn.execute(
        "UPDATE tasks SET status=?, completed_utc=? WHERE id=? AND status='active'",
        (new_status, iso(utcnow()), task_id),
    )
    conn.commit()
    row = conn.execute("SELECT text FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return row[0] if (row and cur.rowcount) else None


def get_task(task_id):
    conn = db()
    row = conn.execute(
        "SELECT text, due_utc FROM tasks WHERE id=? AND status='active'", (task_id,)
    ).fetchone()
    conn.close()
    return row


def update_task(task_id, new_text, new_due):
    stage, next_fire = initial_schedule(new_due)
    conn = db()
    cur = conn.execute(
        "UPDATE tasks SET text=?, due_utc=?, stage=?, next_fire_utc=? "
        "WHERE id=? AND status='active'",
        (new_text, iso(new_due) if new_due else None, stage, iso(next_fire), task_id),
    )
    conn.commit()
    conn.close()
    return bool(cur.rowcount)


def parse_edit_payload(raw):
    if " @ " in raw:
        left, right = raw.rsplit(" @ ", 1)
        try:
            due_local = datetime.strptime(right.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            return left.strip(), due_local.astimezone(timezone.utc)
        except ValueError:
            pass
    return claude_parse(text=raw)


def snooze_task(task_id, minutes=None, tomorrow=False):
    if tomorrow:
        tm = (datetime.now(TZ) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        new_fire = tm.astimezone(timezone.utc)
    else:
        new_fire = utcnow() + timedelta(minutes=minutes or 60)
    conn = db()
    cur = conn.execute(
        "UPDATE tasks SET next_fire_utc=?, stage='nag' WHERE id=? AND status='active'",
        (iso(new_fire), task_id),
    )
    conn.commit()
    row = conn.execute("SELECT text FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if row and cur.rowcount:
        return row[0], new_fire
    return None, None


def list_text(user_id):
    rows = active_tasks(user_id)
    if not rows:
        return "Nothing on the list. Send me a task or a screenshot."
    lines = ["Your active tasks:"]
    for tid, text, due, _stage, _nf, _uid in rows:
        when = fmt_local(from_iso(due)) if due else "no set time"
        lines.append(f"#{tid}  {text}  ({when})")
    lines.append('\nReply "done 3" (the # number) or tap Done on a reminder.')
    return "\n".join(lines)


def daily_summary_text(user_id):
    now_local = datetime.now(TZ)
    today = now_local.date()
    todays, overdue, untimed = [], 0, 0
    for _tid, text, due, _stage, _nf, _uid in active_tasks(user_id):
        if due is None:
            untimed += 1
            continue
        d = from_iso(due).astimezone(TZ)
        if d.date() == today:
            todays.append(f"{d.strftime('%H:%M')} {text}")
        elif d < now_local:
            overdue += 1
    if not (todays or overdue or untimed):
        return None
    lines = [
        "Good morning. "
        + (f"Today ({today.strftime('%a %d %b')}):" if todays else "Nothing scheduled today.")
    ]
    lines.extend(todays)
    if overdue:
        lines.append(f"Overdue: {overdue} task{'s' if overdue > 1 else ''}, check the list.")
    if untimed:
        lines.append(f"Open tasks with no time: {untimed}")
    return "\n".join(lines)


# ---------------------------------------------------------------- nag loop


def nag_loop():
    while True:
        try:
            now = utcnow()
            now_local = datetime.now(TZ)
            users = {u["chat_id"]: u for u in all_users()}

            if DAILY_SUMMARY_HOUR >= 0 and now_local.hour == DAILY_SUMMARY_HOUR:
                today_key = now_local.strftime("%Y-%m-%d")
                if meta_get("last_summary") != today_key:
                    meta_set("last_summary", today_key)
                    for uid, u in users.items():
                        summary = daily_summary_text(uid)
                        if summary:
                            send_telegram(uid, summary)
                            send_email(u.get("email"), "Today's tasks", summary)
                            send_whatsapp(u.get("wa_phone"), u.get("wa_key"), summary)

            if not in_quiet_hours(now_local):
                for tid, text, due, stage, next_fire, uid in active_tasks():
                    if from_iso(next_fire) <= now:
                        user = users.get(str(uid))
                        due_dt = from_iso(due) if due else None
                        new_stage, new_fire = next_schedule(stage, due_dt)
                        conn = db()
                        conn.execute(
                            "UPDATE tasks SET stage=?, next_fire_utc=? WHERE id=?",
                            (new_stage, iso(new_fire), tid),
                        )
                        conn.commit()
                        conn.close()
                        if user:
                            msg = reminder_text(text, due_dt, stage)
                            broadcast(user, msg + f"  (#{tid})", tid)
        except Exception as e:
            log.error("nag loop error: %s", e)
        time.sleep(30)


# ---------------------------------------------------------------- telegram handlers


def help_text(user):
    extra = ""
    if user and user["is_admin"]:
        extra = "\nAdmin: \"users\" lists members; you approve new people via the buttons."
    return (
        "I'm your reminder bot. Send me:\n"
        "- a task in plain English: \"dentist friday 2pm\", \"call the bank tomorrow\"\n"
        "- a screenshot or photo of an appointment letter or booking\n\n"
        "I'll confirm it, then remind you until you're done. "
        "Use the snooze buttons to push one back.\n\n"
        "Commands:\n"
        "list - show your tasks\n"
        "done - mark done (or \"done 3\", or tap the Done button)\n"
        "edit 3 - change a task (I'll ask for the new details)\n"
        "delete 3 - remove a task without doing it\n"
        "dashboard - your personal dashboard link\n"
        "myemail you@example.com - also get reminders by email (\"myemail off\" stops)\n"
        "mywhatsapp +447700900123 APIKEY - also get them on WhatsApp via CallMeBot\n"
        "  (save +34 621 331 709, WhatsApp it \"I allow callmebot to send me messages\", "
        "it replies with your APIKEY)\n"
        "help - this message" + extra
    )


def ensure_user(chat_id, message):
    """Return the user row; handle first-contact and pending approval."""
    user = get_user(chat_id)
    if user:
        return user
    frm = message.get("from", {}) if message else {}
    name = (frm.get("first_name") or "") + (" " + frm.get("last_name") if frm.get("last_name") else "")
    name = name.strip() or frm.get("username") or f"User {chat_id}"
    admin = admin_chat()
    if admin is None:
        # very first person becomes the admin
        create_user(chat_id, name, approved=1, is_admin=1)
        meta_set("owner_chat_id", chat_id)
        send_telegram(chat_id, "You're set up as the admin. Send \"help\" to see what I do.")
        return get_user(chat_id)
    create_user(chat_id, name, approved=0)
    send_telegram(
        chat_id,
        "Hi! This is a private team bot. I've asked the admin to approve you, "
        "you'll get a message here when you're in.",
    )
    send_telegram(admin, f"{name} ({chat_id}) wants to join the reminder bot.", approve_id=chat_id)
    return get_user(chat_id)


def handle_done(user, arg):
    chat_id = user["chat_id"]
    rows = active_tasks(chat_id)
    if not rows:
        send_telegram(chat_id, "Nothing to mark done.")
        return
    if arg:
        try:
            tid = int(arg.lstrip("#"))
        except ValueError:
            send_telegram(chat_id, 'Use the # number, e.g. "done 3".')
            return
        if not can_touch(chat_id, tid):
            send_telegram(chat_id, f"Task #{tid} isn't yours.")
            return
    elif len(rows) == 1:
        tid = rows[0][0]
    else:
        send_telegram(chat_id, "Which one?\n\n" + list_text(chat_id))
        return
    text = complete_task(tid)
    if text:
        send_telegram(chat_id, f"Done ✅ {text}\nNo more reminders for that one.")
    else:
        send_telegram(chat_id, f"Couldn't find active task #{tid}.")


def handle_photo(user, message):
    chat_id = user["chat_id"]
    if not ANTHROPIC_API_KEY:
        send_telegram(chat_id, "Screenshot reading isn't enabled. Type the task as text instead.")
        return
    file_id = message["photo"][-1]["file_id"]
    info = tg("getFile", {"file_id": file_id})
    try:
        path = info["result"]["file_path"]
        data = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=60
        ).content
        b64 = base64.b64encode(data).decode()
    except Exception as e:
        log.error("photo download failed: %s", e)
        send_telegram(chat_id, "Couldn't download that image, try again.")
        return
    caption = message.get("caption", "")
    send_telegram(chat_id, "Reading that…")
    task, due = claude_parse(text=caption or None, image_b64=b64)
    if not task:
        send_telegram(chat_id, "Couldn't find an appointment in that image. Type it instead?")
        return
    create_task(chat_id, task, due, notify_chat=chat_id)


def handle_text(user, text):
    chat_id = user["chat_id"]
    lower = text.strip().lower()
    if lower in ("/start", "/help", "help"):
        send_telegram(chat_id, help_text(user))
        return
    if lower in ("list", "/list", "tasks"):
        send_telegram(chat_id, list_text(chat_id))
        return
    if lower in ("dashboard", "/dashboard"):
        base = meta_get("public_url") or ""
        u = get_user(chat_id)
        link = f"{base}/u/{u['token']}" if base else f"/u/{u['token']} (ask the admin for the site address)"
        send_telegram(chat_id, f"Your dashboard:\n{link}\nKeep this link private, it's your login.")
        return
    if lower in ("users", "/users") and user["is_admin"]:
        lines = ["Members:"]
        for u in all_users(approved_only=False):
            state = "admin" if u["is_admin"] else ("ok" if u["approved"] else "waiting approval")
            lines.append(f"- {u['name']} ({u['chat_id']}) {state}")
        send_telegram(chat_id, "\n".join(lines))
        return
    if lower.startswith("myemail"):
        parts = text.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() in ("off", ""):
            set_user_field(chat_id, "email", None)
            send_telegram(chat_id, "Email reminders off.")
        elif "@" in arg and "." in arg:
            set_user_field(chat_id, "email", arg)
            if GMAIL_USER:
                send_telegram(chat_id, f"Email reminders will go to {arg}.")
            else:
                send_telegram(chat_id, f"Saved {arg}, but email isn't configured on the server yet (admin needs to set GMAIL_USER).")
        else:
            send_telegram(chat_id, "Use: myemail you@example.com (or \"myemail off\").")
        return
    if lower.startswith("mywhatsapp"):
        parts = text.split()
        if len(parts) == 2 and parts[1].lower() == "off":
            set_user_field(chat_id, "wa_phone", None)
            set_user_field(chat_id, "wa_key", None)
            send_telegram(chat_id, "WhatsApp reminders off.")
        elif len(parts) == 3 and parts[1].startswith("+"):
            set_user_field(chat_id, "wa_phone", parts[1])
            set_user_field(chat_id, "wa_key", parts[2])
            send_telegram(chat_id, f"WhatsApp reminders will go to {parts[1]}.")
        else:
            send_telegram(
                chat_id,
                "Use: mywhatsapp +447700900123 YOURAPIKEY\n"
                "Get the key by saving +34 621 331 709 and WhatsApping it: "
                "I allow callmebot to send me messages",
            )
        return
    if lower.startswith(("done", "/done")):
        arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
        handle_done(user, arg)
        return
    if lower.startswith(("edit", "/edit", "change")):
        parts = text.split(None, 2)
        if len(parts) >= 2 and parts[1].lstrip("#").isdigit():
            tid = int(parts[1].lstrip("#"))
            if not can_touch(chat_id, tid):
                send_telegram(chat_id, f"Task #{tid} isn't yours.")
                return
            if len(parts) >= 3:
                task, due = claude_parse(text=parts[2])
                if task and update_task(tid, task, due):
                    when = fmt_local(due) if due else "no set time, will nag until done"
                    send_telegram(chat_id, f"Updated (#{tid}): {task}\nDue: {when}", tid)
                else:
                    send_telegram(chat_id, f"Couldn't update task #{tid}.")
            else:
                row = get_task(tid)
                if not row:
                    send_telegram(chat_id, f"Couldn't find active task #{tid}.")
                else:
                    when = fmt_local(from_iso(row[1])) if row[1] else "no set time"
                    meta_set(f"pending_edit:{chat_id}", tid)
                    send_telegram(
                        chat_id,
                        f"Editing #{tid}: {row[0]} ({when})\n"
                        "Send me the new details, e.g. \"dentist thursday 3pm\".\n"
                        'Or send "cancel" to leave it as it is.',
                    )
        else:
            send_telegram(chat_id, 'Use: edit 3 (get the # from "list").')
        return
    if lower.startswith(("delete", "/delete", "remove")):
        parts = text.split(None, 1)
        if len(parts) > 1:
            try:
                tid = int(parts[1].strip().lstrip("#"))
            except ValueError:
                tid = None
            if tid is not None:
                if not can_touch(chat_id, tid):
                    send_telegram(chat_id, f"Task #{tid} isn't yours.")
                    return
                removed = complete_task(tid, "deleted")
                send_telegram(
                    chat_id,
                    f"Deleted: {removed}" if removed else f"Couldn't find active task #{tid}.",
                )
                return
        send_telegram(chat_id, 'Use the # number, e.g. "delete 3".')
        return
    pending = meta_get(f"pending_edit:{chat_id}")
    if pending:
        meta_set(f"pending_edit:{chat_id}", "")
        if lower == "cancel":
            send_telegram(chat_id, "OK, left it as it was.")
            return
        task, due = claude_parse(text=text)
        if task and update_task(int(pending), task, due):
            when = fmt_local(due) if due else "no set time, will nag until done"
            send_telegram(chat_id, f"Updated (#{pending}): {task}\nDue: {when}", int(pending))
        else:
            send_telegram(chat_id, f"Couldn't update task #{pending}, it may have been completed.")
        return
    # anything else = new task
    task, due = claude_parse(text=text)
    if not task:
        send_telegram(chat_id, "Couldn't make sense of that, try rewording it.")
        return
    create_task(chat_id, task, due, notify_chat=chat_id)


def handle_update(update):
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = str(cq["message"]["chat"]["id"])
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        user = get_user(chat_id)
        if not user:
            return
        data = cq.get("data", "")
        if data.startswith(("appr:", "rej:")) and user["is_admin"]:
            target = data.split(":", 1)[1]
            t_user = get_user(target)
            name = t_user["name"] if t_user else target
            if data.startswith("appr:"):
                set_user_field(target, "approved", 1)
                send_telegram(chat_id, f"Approved {name}.")
                send_telegram(
                    target,
                    "You're in ✅ Send me a task like \"dentist friday 2pm\" or a "
                    "screenshot of an appointment. Send \"help\" for everything I do.",
                )
            else:
                send_telegram(chat_id, f"Rejected {name}.")
                send_telegram(target, "Sorry, the admin didn't approve access.")
            return
        if not user["approved"]:
            return
        if data.startswith("done:"):
            tid = int(data.split(":", 1)[1])
            if can_touch(chat_id, tid):
                text = complete_task(tid)
                if text:
                    send_telegram(chat_id, f"Done ✅ {text}\nNo more reminders for that one.")
        elif data.startswith("snz:"):
            _p, tid, what = data.split(":", 2)
            if can_touch(chat_id, int(tid)):
                if what == "tom":
                    text, new_fire = snooze_task(int(tid), tomorrow=True)
                else:
                    text, new_fire = snooze_task(int(tid), minutes=int(what))
                if text:
                    send_telegram(chat_id, f"💤 Snoozed: {text}\nNext reminder {fmt_local(new_fire)}.")
        return

    message = update.get("message")
    if not message:
        return
    if message.get("chat", {}).get("type") != "private":
        return  # ignore groups
    chat_id = str(message["chat"]["id"])
    user = ensure_user(chat_id, message)
    if not user or not user["approved"]:
        return
    if "photo" in message:
        handle_photo(user, message)
    elif "voice" in message or "audio" in message:
        send_telegram(chat_id, "Voice notes aren't wired up yet. Dictate with your keyboard mic instead.")
    elif "text" in message:
        handle_text(user, message["text"])


def poll_loop():
    offset = 0
    log.info("Telegram polling started. DB at %s", DB_PATH)
    while True:
        try:
            r = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 50},
                timeout=60,
            ).json()
            for update in r.get("result", []):
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except Exception as e:
                    log.error("update error: %s", e)
        except Exception as e:
            log.error("poll error: %s", e)
            time.sleep(5)


# ---------------------------------------------------------------- dashboard

app = Flask(__name__)

DASH_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reminders</title>
<style>
:root { --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d21; --mut:#6b7280;
        --line:#e5e7eb; --accent:#2563eb; --danger:#dc2626; --ok:#16a34a; }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system,
       BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:16px; }
.wrap { max-width:760px; margin:0 auto; }
.wrap.wide { max-width:1200px; }
.top { display:flex; align-items:center; justify-content:space-between; margin:4px 0 14px; }
h1 { font-size:20px; }
h1 small { color:var(--mut); font-size:13px; font-weight:400; }
.tabs button { background:var(--card); border:1px solid var(--line); color:var(--mut);
  padding:8px 14px; font-size:14px; cursor:pointer; }
.tabs button:first-child { border-radius:10px 0 0 10px; }
.tabs button:last-child { border-radius:0 10px 10px 0; }
.tabs button.on { background:var(--accent); border-color:var(--accent); color:#fff; }
.stats { display:flex; gap:10px; margin-bottom:14px; }
.stat { flex:1; background:var(--card); border:1px solid var(--line);
        border-radius:10px; padding:10px 12px; }
.stat b { display:block; font-size:22px; }
.stat span { color:var(--mut); font-size:12px; }
.stat.bad b { color:var(--danger); }
form.add { display:flex; gap:8px; margin-bottom:14px; }
form.add input { flex:1; padding:11px 12px; border:1px solid var(--line);
                 border-radius:10px; font-size:15px; background:var(--card); }
button { border:0; border-radius:10px; padding:10px 14px; font-size:14px; cursor:pointer; }
.btn-add { background:var(--accent); color:#fff; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:12px 14px; margin-bottom:8px; display:flex; align-items:center;
        gap:8px; flex-wrap:wrap; }
.card .info { flex:1; min-width:180px; }
.card .task { font-weight:600; overflow-wrap:anywhere; }
.card .who { color:var(--accent); font-weight:400; font-size:12.5px; }
.card .sub { color:var(--mut); font-size:12.5px; margin-top:2px; }
.card.overdue { border-left:4px solid var(--danger); }
.card .sub .od { color:var(--danger); font-weight:600; }
.btn-done { background:var(--ok); color:#fff; }
.btn-snz { background:#eef2ff; color:#3730a3; padding:10px 10px; }
.btn-edit { background:transparent; color:var(--mut); padding:10px 6px; }
.btn-del { background:transparent; color:var(--mut); padding:10px 6px; }
h2 { font-size:13px; color:var(--mut); text-transform:uppercase;
     letter-spacing:.04em; margin:18px 0 8px; }
.hist { color:var(--mut); font-size:13.5px; padding:4px 2px; }
.hist s { opacity:.8; }
.empty { color:var(--mut); padding:18px 4px; }
#msg { color:var(--accent); font-size:13px; margin:-6px 0 10px; min-height:16px; }
.calhead { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
.calhead b { font-size:16px; }
.calhead button { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:6px 12px; }
.calgrid { display:grid; grid-template-columns:repeat(7,1fr); gap:4px; }
.dow { color:var(--mut); font-size:11px; text-align:center; padding:4px 0;
  text-transform:uppercase; }
.day { background:var(--card); border:1px solid var(--line); border-radius:8px;
  height:calc((100vh - 240px)/6); min-height:54px; padding:4px; font-size:12px;
  overflow:hidden; }
.day .num { color:var(--mut); font-size:11px; margin-bottom:2px; }
.day.today { border-color:var(--accent); border-width:2px; }
.day.other { opacity:.4; }
.chip { background:#eef2ff; color:#3730a3; border-radius:6px; padding:2px 5px;
  margin-bottom:3px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
  cursor:pointer; }
.chip.od { background:#fee2e2; color:#991b1b; }
.chip b { font-weight:600; }
.more { color:var(--mut); font-size:10px; }
@media (max-width:560px){ .chip{font-size:10px} }
</style></head><body><div class="wrap">
<div class="top">
  <h1>Reminders <small id="who"></small></h1>
  <div class="tabs">
    <button id="tab-list" class="on" onclick="setView('list')">List</button>
    <button id="tab-cal" onclick="setView('cal')">Calendar</button>
  </div>
</div>
<div class="stats" id="stats-row">
  <div class="stat"><b id="s-active">–</b><span>active</span></div>
  <div class="stat bad"><b id="s-overdue">–</b><span>overdue</span></div>
  <div class="stat"><b id="s-today">–</b><span>done today</span></div>
</div>
<form class="add" id="add-form" onsubmit="addTask(event)">
  <input id="new-task" placeholder='New task, e.g. "dentist friday 2pm"' autocomplete="off">
  <button class="btn-add">Add</button>
</form>
<div id="msg"></div>
<div id="view-list">
  <div id="active"></div>
  <h2>Recently closed</h2>
  <div id="recent"></div>
</div>
<div id="view-cal" style="display:none">
  <div class="calhead">
    <button onclick="calShift(-1)">‹</button>
    <b id="cal-title"></b>
    <button onclick="calShift(1)">›</button>
  </div>
  <div class="calgrid" id="calgrid"></div>
</div>
</div>
<script>
const BASE = location.pathname.replace(/\\/+$/,'');
let DATA = {active:[], recent:[], stats:{active:'–',overdue:'–',done_today:'–'}, me:'', admin:false};
let calBase = new Date(); calBase.setDate(1);
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(path, opts){ const r = await fetch(BASE + path, opts); return r.json(); }
function setView(v){
  document.getElementById('view-list').style.display = v==='list' ? '' : 'none';
  document.getElementById('view-cal').style.display = v==='cal' ? '' : 'none';
  document.getElementById('stats-row').style.display = v==='list' ? '' : 'none';
  document.getElementById('add-form').style.display = v==='list' ? '' : 'none';
  document.getElementById('msg').style.display = v==='list' ? '' : 'none';
  document.querySelector('.wrap').className = v==='cal' ? 'wrap wide' : 'wrap';
  document.getElementById('tab-list').className = v==='list' ? 'on' : '';
  document.getElementById('tab-cal').className = v==='cal' ? 'on' : '';
  if (v==='cal') renderCal();
}
function editTask(id){
  const t = DATA.active.find(x => x.id === id);
  if (!t) return;
  const cur = t.due_date ? `${t.text} @ ${t.due_date} ${t.time}` : t.text;
  const raw = prompt('Edit task (keep "text @ YYYY-MM-DD HH:MM", or just retype it in plain English):', cur);
  if (raw === null || !raw.trim()) return;
  api('/api/edit/'+id, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({raw: raw.trim()})}).then(d => {
      document.getElementById('msg').textContent = d.ok ? ('Updated: ' + d.summary) : (d.error || 'Failed');
      load();
    });
}
function render(){
  const d = DATA;
  document.getElementById('who').textContent = d.admin ? (d.me + ' · admin view, all members') : d.me;
  document.getElementById('s-active').textContent = d.stats.active;
  document.getElementById('s-overdue').textContent = d.stats.overdue;
  document.getElementById('s-today').textContent = d.stats.done_today;
  const act = document.getElementById('active');
  if (!d.active.length){ act.innerHTML = '<div class="empty">Nothing active. Add one above or message the bot.</div>'; }
  else act.innerHTML = d.active.map(t => `
    <div class="card ${t.overdue ? 'overdue' : ''}">
      <div class="info">
        <div class="task">${esc(t.text)} ${d.admin && t.owner ? '<span class="who">· '+esc(t.owner)+'</span>' : ''}</div>
        <div class="sub">${t.overdue ? '<span class="od">OVERDUE · </span>' : ''}${esc(t.when)} · next nag ${esc(t.next)}</div>
      </div>
      <button class="btn-done" onclick="act_(${t.id},'done')">Done</button>
      <button class="btn-snz" onclick="snz(${t.id},60)">1h</button>
      <button class="btn-snz" onclick="snz(${t.id},180)">3h</button>
      <button class="btn-snz" onclick="snz(${t.id},'tom')">Tmrw</button>
      <button class="btn-edit" onclick="editTask(${t.id})" title="Edit">✎</button>
      <button class="btn-del" onclick="act_(${t.id},'delete')">✕</button>
    </div>`).join('');
  const rec = document.getElementById('recent');
  rec.innerHTML = d.recent.length
    ? d.recent.map(t => `<div class="hist"><s>${esc(t.text)}</s>${d.admin && t.owner ? ' ('+esc(t.owner)+')' : ''} · ${t.status} ${esc(t.when)}</div>`).join('')
    : '<div class="empty">Nothing yet.</div>';
  if (document.getElementById('view-cal').style.display !== 'none') renderCal();
}
function renderCal(){
  const y = calBase.getFullYear(), m = calBase.getMonth();
  document.getElementById('cal-title').textContent =
    calBase.toLocaleDateString('en-GB', {month:'long', year:'numeric'});
  const byDay = {};
  for (const t of DATA.active){
    if (!t.due_date) continue;
    (byDay[t.due_date] = byDay[t.due_date] || []).push(t);
  }
  const todayStr = new Date().toLocaleDateString('sv');
  const first = new Date(y, m, 1);
  let start = new Date(first); start.setDate(1 - ((first.getDay() + 6) % 7));
  let html = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(d=>`<div class="dow">${d}</div>`).join('');
  for (let i=0; i<42; i++){
    const d = new Date(start); d.setDate(start.getDate()+i);
    const ds = d.toLocaleDateString('sv');
    const dayTasks = byDay[ds] || [];
    let chips = dayTasks.slice(0, 3).map(t =>
      `<div class="chip ${t.overdue?'od':''}" title="${esc(t.text)} (click to edit)" onclick="editTask(${t.id})"><b>${t.time}</b> ${esc(t.text)}</div>`).join('');
    if (dayTasks.length > 3) chips += `<div class="more">+${dayTasks.length - 3} more</div>`;
    html += `<div class="day ${d.getMonth()!==m?'other':''} ${ds===todayStr?'today':''}">
      <div class="num">${d.getDate()}</div>${chips}</div>`;
  }
  document.getElementById('calgrid').innerHTML = html;
}
function calShift(n){ calBase.setMonth(calBase.getMonth()+n); renderCal(); }
async function load(){ DATA = await api('/api/tasks'); render(); }
async function act_(id, what){ await api('/api/'+what+'/'+id, {method:'POST'}); load(); }
async function snz(id, v){
  await api('/api/snooze/'+id, {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(v==='tom' ? {tomorrow:true} : {minutes:v})});
  document.getElementById('msg').textContent = 'Snoozed.';
  load();
}
async function addTask(e){
  e.preventDefault();
  const inp = document.getElementById('new-task');
  const text = inp.value.trim();
  if (!text) return;
  document.getElementById('msg').textContent = 'Saving…';
  const d = await api('/api/add', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
  document.getElementById('msg').textContent = d.ok ? d.summary : (d.error || 'Failed');
  if (d.ok) inp.value = '';
  load();
}
load();
setInterval(load, 30000);
</script></body></html>"""


def token_user():
    token = request.view_args.get("token", "")
    return user_by_token(token)


@app.route("/")
def index():
    return (
        "<div style='font-family:sans-serif;padding:40px;max-width:480px;margin:0 auto'>"
        "<h2>Team Reminders</h2><p>This bot is private. Message the Telegram bot the word "
        "<b>dashboard</b> and it will send you your personal link.</p></div>"
    )


@app.route("/u/<token>")
def dash(token):
    if not user_by_token(token):
        return "Unknown link. Message the bot \"dashboard\" for your current one.", 404
    return DASH_HTML


@app.route("/u/<token>/api/tasks")
def api_tasks(token):
    u = token_user()
    if not u:
        return jsonify({"error": "auth"}), 401
    scope = None if u["is_admin"] else u["chat_id"]
    names = {x["chat_id"]: x["name"] for x in all_users(approved_only=False)}
    now = utcnow()
    today_local = datetime.now(TZ).date()
    active = []
    overdue_n = 0
    for tid, text, due, stage, next_fire, uid in active_tasks(scope):
        due_dt = from_iso(due) if due else None
        overdue = bool(due_dt and due_dt < now)
        if overdue:
            overdue_n += 1
        local = due_dt.astimezone(TZ) if due_dt else None
        active.append(
            {
                "id": tid,
                "text": text,
                "owner": names.get(str(uid), ""),
                "when": fmt_local(due_dt) if due_dt else "no set time",
                "next": fmt_local(from_iso(next_fire)),
                "overdue": overdue,
                "due_date": local.strftime("%Y-%m-%d") if local else None,
                "time": local.strftime("%H:%M") if local else "",
            }
        )
    recent = []
    done_today = 0
    for tid, text, status, completed, uid in recent_closed(scope):
        when = ""
        if completed:
            c = from_iso(completed)
            when = fmt_local(c)
            if status == "done" and c.astimezone(TZ).date() == today_local:
                done_today += 1
        recent.append({"id": tid, "text": text, "status": status, "when": when,
                       "owner": names.get(str(uid), "")})
    return jsonify(
        {
            "active": active,
            "recent": recent,
            "me": u["name"],
            "admin": bool(u["is_admin"]),
            "stats": {"active": len(active), "overdue": overdue_n, "done_today": done_today},
        }
    )


def _dash_can_touch(u, tid):
    return u["is_admin"] or task_owner(tid) == str(u["chat_id"])


@app.route("/u/<token>/api/done/<int:tid>", methods=["POST"])
def api_done(token, tid):
    u = token_user()
    if not u or not _dash_can_touch(u, tid):
        return jsonify({"error": "auth"}), 401
    owner = task_owner(tid)
    text = complete_task(tid)
    if text and owner:
        send_telegram(owner, f"Done ✅ {text} (ticked off on the dashboard)")
    return jsonify({"ok": bool(text)})


@app.route("/u/<token>/api/delete/<int:tid>", methods=["POST"])
def api_delete(token, tid):
    u = token_user()
    if not u or not _dash_can_touch(u, tid):
        return jsonify({"error": "auth"}), 401
    text = complete_task(tid, "deleted")
    return jsonify({"ok": bool(text)})


@app.route("/u/<token>/api/snooze/<int:tid>", methods=["POST"])
def api_snooze(token, tid):
    u = token_user()
    if not u or not _dash_can_touch(u, tid):
        return jsonify({"error": "auth"}), 401
    body = request.get_json(silent=True) or {}
    if body.get("tomorrow"):
        text, new_fire = snooze_task(tid, tomorrow=True)
    else:
        try:
            mins = int(body.get("minutes", 60))
        except (TypeError, ValueError):
            mins = 60
        text, new_fire = snooze_task(tid, minutes=mins)
    return jsonify({"ok": bool(text), "next": fmt_local(new_fire) if new_fire else None})


@app.route("/u/<token>/api/edit/<int:tid>", methods=["POST"])
def api_edit(token, tid):
    u = token_user()
    if not u or not _dash_can_touch(u, tid):
        return jsonify({"error": "auth"}), 401
    raw = (request.get_json(silent=True) or {}).get("raw", "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "Empty"})
    task, due = parse_edit_payload(raw)
    if not task:
        return jsonify({"ok": False, "error": "Couldn't parse that"})
    if not update_task(tid, task, due):
        return jsonify({"ok": False, "error": "Task not found or not active"})
    when = fmt_local(due) if due else "no set time"
    return jsonify({"ok": True, "summary": f"{task} · {when}"})


@app.route("/u/<token>/api/add", methods=["POST"])
def api_add(token):
    u = token_user()
    if not u:
        return jsonify({"error": "auth"}), 401
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty task"})
    task, due = claude_parse(text=text)
    if not task:
        return jsonify({"ok": False, "error": "Couldn't parse that, try rewording"})
    _tid, plan = create_task(u["chat_id"], task, due, notify_chat=u["chat_id"])
    return jsonify({"ok": True, "summary": plan.split("\n")[0] + (
        f" · due {fmt_local(due)}" if due else " · no time, will nag until done")})


@app.route("/health")
def health():
    return "ok"


if __name__ == "__main__":
    migrate_legacy()
    if os.environ.get("PUBLIC_URL"):
        meta_set("public_url", os.environ["PUBLIC_URL"].rstrip("/"))
    threading.Thread(target=nag_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
