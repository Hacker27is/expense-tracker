"""SQLite storage: settings, accounts, transactions, cached exchange rates."""
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("TRACKER_DATA") or os.path.join(BASE_DIR, "data")
RECEIPTS_DIR = os.path.join(DATA_DIR, "receipts")
DB_PATH = os.path.join(DATA_DIR, "tracker.db")

os.makedirs(RECEIPTS_DIR, exist_ok=True)

CATEGORIES = [
    ("Groceries", "🛒"),
    ("Cafes & restaurants", "🍜"),
    ("Transport", "🚕"),
    ("Housing", "🏠"),
    ("Utilities & phone", "📱"),
    ("Subscriptions", "💻"),
    ("Health", "💊"),
    ("Clothes & shopping", "🛍"),
    ("Entertainment", "🎉"),
    ("Travel", "✈️"),
    ("Business", "💼"),
    ("Other", "📦"),
]
CATEGORY_NAMES = [c[0] for c in CATEGORIES]
CATEGORY_EMOJI = dict(CATEGORIES)
OTHER = "Other"

# Categories were stored in Russian before the English release; migrated on startup.
_OLD_CATEGORIES = {
    "Продукты": "Groceries", "Кафе и рестораны": "Cafes & restaurants", "Транспорт": "Transport",
    "Жильё": "Housing", "Коммуналка и связь": "Utilities & phone", "Подписки и сервисы": "Subscriptions",
    "Здоровье": "Health", "Одежда и покупки": "Clothes & shopping", "Развлечения": "Entertainment",
    "Путешествия": "Travel", "Бизнес": "Business", "Другое": "Other",
}

_lock = threading.Lock()


@contextmanager
def connect():
    """Transaction + guaranteed close. `with sqlite3.connect()` alone only commits and
    leaks the file handle, which exhausted the 256-file limit after a few hours."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init():
    with _lock, connect() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, name TEXT, currency TEXT, flag TEXT, sort INTEGER);
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,            -- expense | income | transfer | adjust
                date TEXT NOT NULL,            -- YYYY-MM-DD
                created_at TEXT NOT NULL,
                account_id TEXT NOT NULL,      -- paid from / received on
                amount REAL NOT NULL,          -- in the account's currency (adjust may be < 0)
                orig_amount REAL,              -- amount in the receipt's currency
                orig_currency TEXT,
                to_account_id TEXT,            -- transfers only
                to_amount REAL,                -- amount that arrived on to_account
                base_amount REAL,              -- value in the base currency when recorded
                category TEXT,
                merchant TEXT,
                description TEXT,
                items TEXT,                    -- JSON line items from the receipt
                photo TEXT,                    -- path inside data/receipts
                caption TEXT,
                tg_chat_id INTEGER,
                tg_message_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS tx_date ON transactions(date);
            CREATE TABLE IF NOT EXISTS rates (
                currency TEXT PRIMARY KEY, per_usd REAL, updated_at TEXT);
            -- which bot message belongs to which transaction (for reply corrections)
            CREATE TABLE IF NOT EXISTS tg_links (
                chat_id INTEGER, bot_message_id INTEGER, tx_id INTEGER,
                photo TEXT, caption TEXT,
                PRIMARY KEY (chat_id, bot_message_id));
            """
        )
        for old, new in _OLD_CATEGORIES.items():
            c.execute("UPDATE transactions SET category=? WHERE category=?", (new, old))
        # installs from before accounts were configurable had base THB and top-ups on "thai"
        has_base = c.execute("SELECT 1 FROM settings WHERE key='base_currency'").fetchone()
        if not has_base and c.execute("SELECT 1 FROM accounts WHERE id='thai'").fetchone():
            c.execute("INSERT INTO settings(key,value) VALUES('base_currency','THB')")
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('default_account','thai')")


# ---------- settings ----------

def get_setting(key, default=None):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    with _lock, connect() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, None if value is None else str(value)),
        )


def base_currency():
    """Currency all totals and charts are reported in."""
    return get_setting("base_currency", "USD")


def default_account():
    """Account used for top-ups and for expenses in a currency no account holds."""
    ids = account_ids()
    aid = get_setting("default_account")
    return aid if aid in ids else (ids[0] if ids else None)


# ---------- accounts ----------

def flag_for(currency):
    """Country flag from the currency code (THB -> 🇹🇭, EUR -> 🇪🇺); 💱 when there is none."""
    code = (currency or "").upper()
    if len(code) != 3 or not code.isalpha() or code[0] == "X":
        return "💱"
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code[:2])


def accounts():
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM accounts ORDER BY sort, rowid")]


def account_ids():
    return [a["id"] for a in accounts()]


def account(aid):
    with connect() as c:
        r = c.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def add_account(name, currency):
    currency = currency.upper()
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or currency.lower()
    with _lock, connect() as c:
        taken = {r[0] for r in c.execute("SELECT id FROM accounts")}
        aid, n = base, 2
        while aid in taken:
            aid, n = f"{base}-{n}", n + 1
        sort = c.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM accounts").fetchone()[0]
        c.execute("INSERT INTO accounts(id,name,currency,flag,sort) VALUES(?,?,?,?,?)",
                  (aid, name, currency, flag_for(currency), sort))
    return aid


def rename_account(aid, name):
    with _lock, connect() as c:
        c.execute("UPDATE accounts SET name=? WHERE id=?", (name, aid))


def account_in_use(aid):
    with connect() as c:
        return bool(c.execute(
            "SELECT 1 FROM transactions WHERE account_id=? OR to_account_id=? LIMIT 1", (aid, aid)).fetchone())


def delete_account(aid):
    with _lock, connect() as c:
        c.execute("DELETE FROM accounts WHERE id=?", (aid,))


def balances():
    """Balance of every account in its own currency."""
    out = {a["id"]: 0.0 for a in accounts()}
    with connect() as c:
        for r in c.execute("SELECT type, account_id, amount, to_account_id, to_amount FROM transactions"):
            if r["account_id"] not in out:
                continue
            if r["type"] in ("income", "adjust"):
                out[r["account_id"]] += r["amount"]
            elif r["type"] == "expense":
                out[r["account_id"]] -= r["amount"]
            elif r["type"] == "transfer":
                out[r["account_id"]] -= r["amount"]
                if r["to_account_id"] in out:
                    out[r["to_account_id"]] += r["to_amount"] or 0
    return {k: round(v, 2) for k, v in out.items()}


# ---------- transactions ----------

TX_FIELDS = [
    "type", "date", "account_id", "amount", "orig_amount", "orig_currency",
    "to_account_id", "to_amount", "base_amount", "category", "merchant",
    "description", "items", "photo", "caption", "tg_chat_id", "tg_message_id",
]


def _clean(fields):
    data = {k: v for k, v in fields.items() if k in TX_FIELDS}
    if isinstance(data.get("items"), (list, dict)):
        data["items"] = json.dumps(data["items"], ensure_ascii=False)
    return data


def add_tx(**fields):
    data = _clean(fields)
    data.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    data["created_at"] = datetime.now().isoformat(timespec="seconds")
    cols = ",".join(data)
    with _lock, connect() as c:
        cur = c.execute(
            f"INSERT INTO transactions({cols}) VALUES({','.join('?' * len(data))})",
            list(data.values()),
        )
        return cur.lastrowid


def update_tx(tx_id, **fields):
    data = _clean(fields)
    if not data:
        return
    sets = ",".join(f"{k}=?" for k in data)
    with _lock, connect() as c:
        c.execute(f"UPDATE transactions SET {sets} WHERE id=?", [*data.values(), tx_id])


def delete_tx(tx_id):
    with _lock, connect() as c:
        c.execute("DELETE FROM transactions WHERE id=?", (tx_id,))


def get_tx(tx_id):
    with connect() as c:
        r = c.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    return _row(r) if r else None


def last_tx():
    with connect() as c:
        r = c.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 1").fetchone()
    return _row(r) if r else None


def list_tx(month=None):
    q, args = "SELECT * FROM transactions", []
    if month:
        q += " WHERE substr(date,1,7)=?"
        args.append(month)
    q += " ORDER BY date DESC, id DESC"
    with connect() as c:
        return [_row(r) for r in c.execute(q, args)]


def months():
    with connect() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT substr(date,1,7) m FROM transactions ORDER BY m DESC")]


def _row(r):
    d = dict(r)
    try:
        d["items"] = json.loads(d["items"]) if d.get("items") else []
    except ValueError:
        d["items"] = []
    return d


# ---------- telegram message links ----------

def save_link(chat_id, bot_message_id, tx_id, photo, caption):
    with _lock, connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO tg_links(chat_id,bot_message_id,tx_id,photo,caption) VALUES(?,?,?,?,?)",
            (chat_id, bot_message_id, tx_id, photo, caption),
        )


def get_link(chat_id, bot_message_id):
    with connect() as c:
        r = c.execute("SELECT * FROM tg_links WHERE chat_id=? AND bot_message_id=?",
                      (chat_id, bot_message_id)).fetchone()
    return dict(r) if r else None


# ---------- rates ----------

def save_rates(per_usd: dict):
    now = datetime.now().isoformat(timespec="seconds")
    with _lock, connect() as c:
        c.executemany(
            "INSERT INTO rates(currency,per_usd,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(currency) DO UPDATE SET per_usd=excluded.per_usd, updated_at=excluded.updated_at",
            [(k, v, now) for k, v in per_usd.items()],
        )


def load_rates():
    with connect() as c:
        rows = c.execute("SELECT * FROM rates").fetchall()
    if not rows:
        return {}, None
    return {r["currency"]: r["per_usd"] for r in rows}, max(r["updated_at"] for r in rows)
