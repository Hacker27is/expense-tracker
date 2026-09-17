"""Local web UI + JSON API on http://127.0.0.1:8765 (only this computer can open it)."""
import csv
import io
import json
import logging
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bot
import db
import ledger
import rates

log = logging.getLogger("web")
STATIC = os.path.join(db.BASE_DIR, "static")
HOST, PORT = "127.0.0.1", int(os.environ.get("TRACKER_PORT", 8765))


def _num(v, name):
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        raise ledger.LedgerError(f"Invalid number in “{name}”")


def settings_payload():
    token = db.get_setting("telegram_token")
    key = db.get_setting("anthropic_key")
    return {
        "has_token": bool(token),
        "has_key": bool(key),
        "key_hint": f"…{key[-4:]}" if key else None,
        "bot": dict(bot.status),
        "bot_username": db.get_setting("bot_username"),
        "owner": db.get_setting("owner_name") if db.get_setting("owner_chat_id") else None,
        "pair_code": bot.pairing_code(),
        "setup_done": db.get_setting("setup_done") == "1",
        "accounts": db.accounts(),
        "base_currency": db.get_setting("base_currency"),
        "default_account": db.default_account(),
        "currencies": sorted(rates.refresh()),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _same_origin(self):
        # защита от чужих сайтов, которые попытаются дёрнуть localhost из браузера
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def _file(self, path):
        if not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"))
            if url.path.startswith("/receipts/"):
                rel = os.path.normpath(url.path[len("/receipts/"):]).lstrip("/")
                if rel.startswith(".."):
                    return self._send(403, {"error": "forbidden"})
                return self._file(os.path.join(db.RECEIPTS_DIR, rel))
            if url.path == "/api/summary":
                return self._send(200, ledger.summary(q.get("month")))
            if url.path == "/api/transactions":
                return self._send(200, db.list_tx(q.get("month") or None))
            if url.path == "/api/settings":
                return self._send(200, settings_payload())
            if url.path == "/api/export.csv":
                return self._export(q.get("month") or None)
            self._send(404, {"error": "not found"})
        except ledger.LedgerError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            log.exception("GET %s", self.path)
            self._send(500, {"error": str(e)})

    def _export(self, month):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "date", "type", "account", "amount (account currency)", "receipt amount", "receipt currency",
                    db.base_currency(), "to account", "received", "category", "shop", "description", "photo"])
        for t in db.list_tx(month):
            w.writerow([t["id"], t["date"], t["type"], t["account_id"], t["amount"], t["orig_amount"],
                        t["orig_currency"], t["base_amount"], t["to_account_id"] or "", t["to_amount"] or "",
                        t["category"] or "", t["merchant"] or "", t["description"] or "", t["photo"] or ""])
        name = f"expenses-{month or 'all'}.csv"
        self._send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    # ---------- POST / DELETE ----------
    def do_POST(self):
        if not self._same_origin():
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        try:
            data = self._json()
            if url.path == "/api/settings":
                return self._send(200, self._save_settings(data))
            if url.path == "/api/unlink":
                db.set_setting("owner_chat_id", None)
                db.set_setting("owner_name", None)
                db.set_setting("pair_code", None)
                return self._send(200, settings_payload())
            if url.path == "/api/income":
                tx = ledger.add_income(_num(data.get("amount"), "amount"), data.get("currency") or None,
                                       data.get("account_id") or None, date=data.get("date"),
                                       description=data.get("description") or None)
                return self._send(200, db.get_tx(tx))
            if url.path == "/api/expense":
                tx = ledger.add_expense(
                    _num(data.get("amount"), "amount"), data.get("currency"), data.get("account_id") or None,
                    category=data.get("category"), date=data.get("date"),
                    description=data.get("description") or None, merchant=data.get("merchant") or None)
                return self._send(200, db.get_tx(tx))
            if url.path == "/api/transfer":
                to_amount = data.get("to_amount")
                tx = ledger.add_transfer(data.get("from"), data.get("to"), _num(data.get("amount"), "amount"),
                                         _num(to_amount, "received") if to_amount not in (None, "") else None,
                                         date=data.get("date"), description=data.get("description") or None)
                return self._send(200, db.get_tx(tx))
            if url.path == "/api/balance":
                ledger.set_balance(data.get("account_id"), _num(data.get("balance"), "balance"))
                return self._send(200, {"ok": True})
            if url.path == "/api/rates/refresh":
                rates.refresh(force=True)
                return self._send(200, {"updated": rates.updated_at()})
            if url.path == "/api/accounts":
                name = (data.get("name") or "").strip()
                currency = (data.get("currency") or "").strip().upper()
                if not name:
                    raise ledger.LedgerError("Give the account a name.")
                if currency not in rates.refresh():
                    raise ledger.LedgerError(f"Unknown currency: {currency or '—'}")
                aid = db.add_account(name, currency)
                if data.get("balance") not in (None, ""):
                    ledger.set_balance(aid, _num(data["balance"], "starting balance"))
                return self._send(200, settings_payload())
            m = re.fullmatch(r"/api/accounts/([\w-]+)", url.path)
            if m:
                name = (data.get("name") or "").strip()
                if not name:
                    raise ledger.LedgerError("Give the account a name.")
                db.rename_account(m.group(1), name)
                return self._send(200, settings_payload())
            m = re.fullmatch(r"/api/tx/(\d+)", url.path)
            if m:
                changes = {k: data[k] for k in ("date", "category", "merchant", "description", "account_id",
                                                  "to_account_id", "orig_currency") if k in data}
                for k in ("orig_amount", "to_amount"):
                    if data.get(k) not in (None, ""):
                        changes[k] = _num(data[k], "amount")
                return self._send(200, ledger.update_tx(int(m.group(1)), changes))
            self._send(404, {"error": "not found"})
        except (ledger.LedgerError, bot.TelegramError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            log.exception("POST %s", self.path)
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        if not self._same_origin():
            return self._send(403, {"error": "forbidden"})
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/accounts/([\w-]+)", path)
        if m:
            if db.account_in_use(m.group(1)):
                return self._send(400, {"error": "This account has transactions. Delete or move them first."})
            db.delete_account(m.group(1))
            return self._send(200, settings_payload())
        m = re.fullmatch(r"/api/tx/(\d+)", path)
        if not m:
            return self._send(404, {"error": "not found"})
        db.delete_tx(int(m.group(1)))
        self._send(200, {"ok": True})

    def _save_settings(self, data):
        token = (data.get("telegram_token") or "").strip()
        key = (data.get("anthropic_key") or "").strip()
        if token:
            try:
                db.set_setting("bot_username", bot.check_token(token))
            except bot.TelegramError:
                raise ledger.LedgerError("Telegram rejected the bot token. Copy the whole token from @BotFather.")
            except Exception:
                raise ledger.LedgerError("Can’t reach Telegram — check the internet connection.")
            db.set_setting("telegram_token", token)
        if key:
            if not key.startswith("sk-ant-"):
                raise ledger.LedgerError("The Claude key must start with sk-ant-")
            db.set_setting("anthropic_key", key)
        base = (data.get("base_currency") or "").strip().upper()
        if base and base != db.get_setting("base_currency"):
            if base not in rates.refresh():
                raise ledger.LedgerError(f"Unknown currency: {base}")
            db.set_setting("base_currency", base)
            ledger.rebase()
        if data.get("default_account") in db.account_ids():
            db.set_setting("default_account", data["default_account"])
        if data.get("finish"):
            if not db.get_setting("base_currency"):
                raise ledger.LedgerError("Choose the currency for totals.")
            if not db.accounts():
                raise ledger.LedgerError("Add at least one account.")
            db.set_setting("setup_done", "1")
        return settings_payload()


def make_server():
    """Bind the port. Raises OSError if another copy of the tracker already holds it."""
    return ThreadingHTTPServer((HOST, PORT), Handler)


def serve(httpd):
    log.info("UI on http://%s:%d", HOST, PORT)
    httpd.serve_forever()
