"""Telegram bot: long polling, receipt photos -> Claude -> ledger, inline buttons for fixes."""
import html
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import ai
import db
import ledger

log = logging.getLogger("bot")

status = {"state": "stopped", "error": None, "username": None, "last_update": None}
_pool = ThreadPoolExecutor(max_workers=3)

HELP = """<b>How to use</b>

📸 Send a photo of a receipt or a bank notification — I'll read the amount, currency and category. Add a caption to clarify: "taxi", "paid from my savings account", "1350".
✍️ No photo needed: "coffee 4.50", "taxi 18 eur".
💰 Top-up: "topped up 2000".
🔁 Transfer: "sent 500 to savings, received 480".
↩️ Made a mistake? Reply to my message with a correction and I'll redo it. Or use the buttons under the entry.

/balance — account balances
/month — this month's expenses by category
/undo — delete the last entry"""


class TelegramError(Exception):
    pass


def _token():
    return db.get_setting("telegram_token")


def api(method, token=None, timeout=70, **params):
    token = token or _token()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
        except ValueError:
            raise TelegramError(f"HTTP {e.code}")
    if not data.get("ok"):
        raise TelegramError(data.get("description", "unknown error"))
    return data["result"]


def check_token(token):
    """Returns bot username or raises TelegramError."""
    return api("getMe", token=token, timeout=15)["username"]


def pairing_code():
    code = db.get_setting("pair_code")
    if not code:
        code = f"{secrets.randbelow(900000) + 100000}"
        db.set_setting("pair_code", code)
    return code


def send(chat_id, text, reply_to=None, markup=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if markup:
        params["reply_markup"] = markup
    return api("sendMessage", **params)


def edit(chat_id, message_id, text, markup=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    params["reply_markup"] = markup or {"inline_keyboard": []}
    try:
        api("editMessageText", **params)
    except TelegramError as e:
        if "not modified" not in str(e):
            raise


# ---------- keyboards ----------

def main_kb(tx):
    if not tx:
        return None
    rows = []
    if tx["type"] == "expense":
        rows.append([
            {"text": "🏷 Category", "callback_data": f"cat:{tx['id']}"},
            {"text": "💳 Account", "callback_data": f"acc:{tx['id']}"},
        ])
    rows.append([{"text": "🗑 Delete", "callback_data": f"del:{tx['id']}"}])
    return {"inline_keyboard": rows}


def cat_kb(tx_id):
    btns = [{"text": f"{e} {n}", "callback_data": f"setcat:{tx_id}:{i}"} for i, (n, e) in enumerate(db.CATEGORIES)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([{"text": "← Back", "callback_data": f"back:{tx_id}"}])
    return {"inline_keyboard": rows}


def acc_kb(tx_id):
    rows = [[{"text": f"{a['flag']} {a['name']}", "callback_data": f"setacc:{tx_id}:{a['id']}"}] for a in db.accounts()]
    rows.append([{"text": "← Back", "callback_data": f"back:{tx_id}"}])
    return {"inline_keyboard": rows}


# ---------- files ----------

def download(file_id, suffix):
    info = api("getFile", file_id=file_id)
    month_dir = datetime.now().strftime("%Y-%m")
    os.makedirs(os.path.join(db.RECEIPTS_DIR, month_dir), exist_ok=True)
    rel = os.path.join(month_dir, f"{datetime.now():%Y%m%d-%H%M%S}-{file_id[-8:]}{suffix}")
    url = f"https://api.telegram.org/file/bot{_token()}/{info['file_path']}"
    with urllib.request.urlopen(url, timeout=60) as r, open(os.path.join(db.RECEIPTS_DIR, rel), "wb") as f:
        f.write(r.read())
    return rel


def _attachment(msg):
    """(file_id, suffix) of a receipt image/PDF in the message, or None."""
    if msg.get("photo"):
        return msg["photo"][-1]["file_id"], ".jpg"
    doc = msg.get("document")
    if doc:
        mime = doc.get("mime_type", "")
        name = doc.get("file_name", "").lower()
        if mime == "application/pdf" or name.endswith(".pdf"):
            return doc["file_id"], ".pdf"
        if mime.startswith("image/"):
            ext = os.path.splitext(name)[1] or "." + mime.split("/")[1]
            return doc["file_id"], ext
    return None


# ---------- handlers ----------

def process(chat_id, user_msg_id, text, photo_rel, replace_tx=None, status_msg_id=None, sent=None, member_id=None):
    """Parse with Claude, record, and show the result. Runs in the worker pool.
    `sent` is the YYYY-MM-DD the owner sent the message."""
    if status_msg_id is None:
        status_msg_id = send(chat_id, "⏳ Reading the receipt…" if photo_rel else "⏳ Recording…", reply_to=user_msg_id)["message_id"]
    else:
        edit(chat_id, status_msg_id, "⏳ Re-reading…")
    try:
        parsed = ai.parse(text or "", os.path.join(db.RECEIPTS_DIR, photo_rel) if photo_rel else None, today=sent)
        log.info("parsed: %s", parsed)
        if not parsed.get("date") and sent:
            parsed["date"] = sent
        tx_id = ledger.apply_parsed(parsed, photo=photo_rel, caption=text or None,
                                    tg_chat_id=chat_id, tg_message_id=user_msg_id,
                                    member_id=member_id or chat_id)
        if replace_tx and db.get_tx(replace_tx):
            db.delete_tx(replace_tx)
        tx = db.get_tx(tx_id)
        body = ledger.describe(tx)
        if parsed.get("comment"):
            body += f"\n\n💬 {html.escape(parsed['comment'])}"
        edit(chat_id, status_msg_id, body, main_kb(tx))
        db.save_link(chat_id, status_msg_id, tx_id, photo_rel, text)
    except (ai.AIError, ledger.LedgerError) as e:
        hint = "\n\nReply to this message with a correction, e.g. \"1250 baht, groceries\"." if photo_rel else ""
        edit(chat_id, status_msg_id, f"⚠️ {html.escape(str(e))}{hint}")
        db.save_link(chat_id, status_msg_id, replace_tx, photo_rel, text)
    except Exception:
        log.exception("processing failed")
        edit(chat_id, status_msg_id, "⚠️ Something went wrong while saving. Please try again.")


def _submit(*args, **kw):
    fut = _pool.submit(process, *args, **kw)
    fut.add_done_callback(lambda f: f.exception() and log.error("worker: %s", f.exception()))


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    owner = db.owner_chat_id()
    who = msg["chat"].get("first_name") or msg["chat"].get("username") or "Someone"
    sent = datetime.fromtimestamp(msg.get("date") or time.time()).strftime("%Y-%m-%d")

    if text.startswith("/start"):
        code = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if db.is_member(chat_id):
            send(chat_id, "I'm here 👌\n\n" + HELP)
        elif owner is None and code and code == db.get_setting("pair_code"):
            db.add_member(chat_id, who, is_owner=True)
            db.set_setting("owner_chat_id", chat_id)      # kept for older dashboards
            db.set_setting("owner_name", who)
            send(chat_id, "Done — the bot is now linked to you ✅\n\n" + HELP)
        elif code and db.use_invite(code, chat_id):
            db.add_member(chat_id, who)
            send(chat_id, f"You're in, {html.escape(who)} ✅\nYour expenses go into the shared budget.\n\n" + HELP)
            if owner:
                send(owner, f"👋 <b>{html.escape(who)}</b> joined the expense tracker.")
        else:
            send(chat_id, "This is a private bot. Ask its owner for an invite link.")
        return

    if not db.is_member(chat_id):
        return  # messages from anyone else are ignored

    cmd = text.split("@")[0].lower() if text.startswith("/") else ""
    if cmd == "/help" or text.lower() in ("помощь", "help"):
        send(chat_id, HELP)
    elif cmd == "/balance" or text.lower() in ("баланс", "остаток", "сколько осталось", "balance", "balances"):
        send(chat_id, ledger.balance_text())
    elif cmd == "/month" or text.lower() in ("месяц", "расходы", "month", "expenses"):
        send(chat_id, ledger.month_text())
    elif cmd == "/undo":
        tx = db.last_tx(member_id=chat_id)
        if not tx:
            send(chat_id, "No entries yet.")
        else:
            db.delete_tx(tx["id"])
            send(chat_id, "🗑 Deleted the last entry:\n" + ledger.describe(tx).split("\nBalance")[0])
    elif cmd:
        send(chat_id, "I don't know that command.\n\n" + HELP)
    else:
        reply = msg.get("reply_to_message")
        link = reply and db.get_link(chat_id, reply["message_id"])
        att = _attachment(msg)
        if link and text and not att:
            # уточнение к уже разобранному чеку / сообщению
            base = link["caption"] or ""
            combined = f"{base}\nOwner's correction: {text}".strip()
            orig_sent = datetime.fromtimestamp(reply.get("date") or time.time()).strftime("%Y-%m-%d")
            _submit(chat_id, msg["message_id"], combined, link["photo"],
                    replace_tx=link["tx_id"], status_msg_id=reply["message_id"], sent=orig_sent,
                    member_id=(db.get_tx(link["tx_id"]) or {}).get("member_id") or chat_id)
        elif att:
            try:
                rel = download(*att)
            except TelegramError as e:
                send(chat_id, f"⚠️ Couldn't download the file: {html.escape(str(e))}", reply_to=msg["message_id"])
                return
            _submit(chat_id, msg["message_id"], text, rel, sent=sent)
        elif text:
            _submit(chat_id, msg["message_id"], text, None, sent=sent)
        else:
            send(chat_id, "Send a receipt photo or type the expense as text.", reply_to=msg["message_id"])


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    msg_id = cq["message"]["message_id"]
    if not db.is_member(chat_id):
        return api("answerCallbackQuery", callback_query_id=cq["id"])
    action, _, rest = cq.get("data", "").partition(":")
    parts = rest.split(":")
    tx = db.get_tx(int(parts[0])) if parts[0].isdigit() else None
    note = None
    if not tx:
        edit(chat_id, msg_id, "This entry has already been deleted.")
    elif action == "cat":
        edit(chat_id, msg_id, ledger.describe(tx) + "\n\nChoose a category:", cat_kb(tx["id"]))
    elif action == "acc":
        edit(chat_id, msg_id, ledger.describe(tx) + "\n\nWhich account paid?", acc_kb(tx["id"]))
    elif action == "setcat":
        tx = ledger.update_tx(tx["id"], {"category": db.CATEGORIES[int(parts[1])][0]})
        edit(chat_id, msg_id, ledger.describe(tx), main_kb(tx))
        note = "Category changed"
    elif action == "setacc":
        tx = ledger.update_tx(tx["id"], {"account_id": parts[1]})
        edit(chat_id, msg_id, ledger.describe(tx), main_kb(tx))
        note = "Account changed"
    elif action == "back":
        edit(chat_id, msg_id, ledger.describe(tx), main_kb(tx))
    elif action == "del":
        text = ledger.describe(tx).split("\nBalance")[0]
        db.delete_tx(tx["id"])
        edit(chat_id, msg_id, f"<s>{text}</s>\n🗑 Deleted")
        note = "Deleted"
    api("answerCallbackQuery", callback_query_id=cq["id"], text=note or "")


# ---------- loop ----------

def _loop():
    last_token = None
    while True:
        try:
            token = _token()
            if not token:
                status.update(state="no_token", error=None)
                time.sleep(2)
                continue
            if token != last_token:
                status["username"] = check_token(token)
                api("deleteWebhook")
                api("setMyCommands", commands=[
                    {"command": "balance", "description": "Account balances"},
                    {"command": "month", "description": "This month's expenses"},
                    {"command": "undo", "description": "Delete the last entry"},
                    {"command": "help", "description": "How to use"},
                ])
                last_token = token
            status.update(state="running", error=None)
            offset = int(db.get_setting("tg_offset", "0"))
            updates = api("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
            for u in updates:
                db.set_setting("tg_offset", u["update_id"] + 1)
                status["last_update"] = datetime.now().isoformat(timespec="seconds")
                try:
                    if "message" in u:
                        handle_message(u["message"])
                    elif "callback_query" in u:
                        handle_callback(u["callback_query"])
                except Exception:
                    log.exception("update %s failed", u.get("update_id"))
        except TelegramError as e:
            msg = str(e)
            if "Unauthorized" in msg or "Not Found" in msg:
                status.update(state="bad_token", error="Invalid bot token")
                last_token = None
                time.sleep(10)
            else:
                status.update(state="error", error=msg)
                log.warning("telegram: %s", msg)
                time.sleep(5)
        except Exception as e:  # сеть, таймауты
            status.update(state="error", error=f"Can't reach Telegram: {e}")
            log.warning("telegram connection: %s", e)
            time.sleep(5)


def start():
    threading.Thread(target=_loop, name="telegram", daemon=True).start()
