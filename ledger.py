"""Business rules shared by the bot and the web UI: which account pays, conversions, summaries."""
import html
from collections import defaultdict
from datetime import date, datetime

import db
import rates

SYMBOLS = {"THB": "฿", "MYR": "RM", "SGD": "S$", "USD": "$", "EUR": "€", "RUB": "₽", "GBP": "£",
           "JPY": "¥", "INR": "₹", "IDR": "Rp", "VND": "₫", "PHP": "₱", "KRW": "₩", "AUD": "A$"}
PREFIX_SYMBOLS = ("$", "€", "£", "S$", "¥", "₹", "₱", "₩", "A$", "Rp")


class LedgerError(Exception):
    pass


def fmt(amount, cur):
    amount = amount or 0
    s = f"{abs(amount):,.2f}".removesuffix(".00")
    sign = "−" if amount < 0 else ""
    sym = SYMBOLS.get(cur, cur)
    return f"{sign}{sym}{s}" if sym in PREFIX_SYMBOLS else f"{sign}{s} {sym}"


def _date(value):
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date.today().isoformat()
    # чек из будущего или очень старый — скорее ошибка распознавания
    if d > date.today() or (date.today() - d).days > 400:
        return date.today().isoformat()
    return d.isoformat()


def _account_or_fail(aid):
    acc = db.account(aid)
    if not acc:
        raise LedgerError(f"No such account: {aid}")
    return acc


def pick_account(currency, hint=None):
    """Hinted account, else the first account in that currency, else the default account."""
    accs = db.accounts()
    if not accs:
        raise LedgerError("Add an account in the dashboard first.")
    if hint in {a["id"] for a in accs}:
        return hint
    for a in accs:
        if a["currency"] == (currency or "").upper():
            return a["id"]
    return db.default_account()


def _convert(amount, cur, to_cur):
    try:
        return rates.convert(amount, cur, to_cur)
    except rates.UnknownCurrency as e:
        raise LedgerError(f"Unknown currency: {e}")


def expense_fields(amount, currency, account_id=None):
    """Amount in account currency + THB equivalent for an expense paid in `currency`."""
    currency = (currency or "").upper() or None
    account_id = pick_account(currency, account_id)
    acc = _account_or_fail(account_id)
    currency = currency or acc["currency"]
    return {
        "account_id": account_id,
        "orig_amount": round(float(amount), 2),
        "orig_currency": currency,
        "amount": _convert(amount, currency, acc["currency"]),
        "base_amount": _convert(amount, currency, db.base_currency()),
    }


def add_expense(amount, currency, account_id=None, **extra):
    if not amount or amount <= 0:
        raise LedgerError("Amount must be greater than zero")
    fields = expense_fields(amount, currency, account_id)
    fields["date"] = _date(extra.pop("date", None))
    fields["category"] = extra.pop("category", None) if extra.get("category") in db.CATEGORY_NAMES else db.OTHER
    return db.add_tx(type="expense", **fields, **extra)


def add_income(amount, currency=None, account_id=None, **extra):
    if not amount or amount <= 0:
        raise LedgerError("Amount must be greater than zero")
    acc = _account_or_fail(account_id or pick_account(None))
    currency = (currency or acc["currency"]).upper()
    return db.add_tx(
        type="income", account_id=acc["id"], date=_date(extra.pop("date", None)),
        orig_amount=amount, orig_currency=currency,
        amount=_convert(amount, currency, acc["currency"]),
        base_amount=_convert(amount, currency, db.base_currency()),
        category=None, **extra,
    )


def add_transfer(from_id, to_id, amount, to_amount=None, **extra):
    if from_id == to_id:
        raise LedgerError("Can’t transfer to the same account")
    if not amount or amount <= 0:
        raise LedgerError("Amount must be greater than zero")
    src, dst = _account_or_fail(from_id), _account_or_fail(to_id)
    if not to_amount or to_amount <= 0:
        to_amount = _convert(amount, src["currency"], dst["currency"])
    return db.add_tx(
        type="transfer", account_id=from_id, to_account_id=to_id, date=_date(extra.pop("date", None)),
        amount=round(float(amount), 2), orig_amount=round(float(amount), 2), orig_currency=src["currency"],
        to_amount=round(float(to_amount), 2),
        base_amount=_convert(amount, src["currency"], db.base_currency()), **extra,
    )


def set_balance(account_id, actual):
    """Reconcile an account with the real bank balance by adding an adjustment."""
    acc = _account_or_fail(account_id)
    diff = round(float(actual) - db.balances()[account_id], 2)
    if abs(diff) < 0.005:
        return None
    return db.add_tx(
        type="adjust", account_id=account_id, amount=diff, orig_amount=diff,
        orig_currency=acc["currency"], base_amount=_convert(diff, acc["currency"], db.base_currency()),
        description="Balance reconciliation",
    )


def update_tx(tx_id, changes):
    """Edit a transaction; recalculates the amounts when money fields change."""
    tx = db.get_tx(tx_id)
    if not tx:
        raise LedgerError("Transaction not found")
    merged = {**tx, **{k: v for k, v in changes.items() if v is not None}}
    fields = {k: merged.get(k) for k in ("date", "category", "merchant", "description")}
    fields["date"] = _date(fields["date"])
    if tx["type"] == "expense":
        fields.update(expense_fields(float(merged["orig_amount"]), merged["orig_currency"], merged["account_id"]))
        if fields["category"] not in db.CATEGORY_NAMES:
            fields["category"] = db.OTHER
    elif tx["type"] == "income":
        acc = _account_or_fail(merged["account_id"])
        cur = merged["orig_currency"] or acc["currency"]
        fields.update(account_id=acc["id"], orig_amount=float(merged["orig_amount"]), orig_currency=cur,
                      amount=_convert(float(merged["orig_amount"]), cur, acc["currency"]),
                      base_amount=_convert(float(merged["orig_amount"]), cur, db.base_currency()))
    elif tx["type"] == "transfer":
        src, dst = _account_or_fail(merged["account_id"]), _account_or_fail(merged["to_account_id"])
        if src["id"] == dst["id"]:
            raise LedgerError("Can’t transfer to the same account")
        amount = float(merged["orig_amount"] or merged["amount"])
        moved = (src["id"], dst["id"], amount) != (tx["account_id"], tx["to_account_id"], tx["amount"])
        if changes.get("to_amount"):
            to_amount = float(changes["to_amount"])        # сколько реально пришло — вписано руками
        elif moved:
            to_amount = _convert(amount, src["currency"], dst["currency"])
        else:
            to_amount = tx["to_amount"]
        fields.update(account_id=src["id"], to_account_id=dst["id"], amount=amount, orig_amount=amount,
                      orig_currency=src["currency"], to_amount=to_amount,
                      base_amount=_convert(amount, src["currency"], db.base_currency()))
    db.update_tx(tx_id, **fields)
    return db.get_tx(tx_id)


def rebase():
    """Recalculate every transaction's base-currency value after the base currency changes."""
    base = db.base_currency()
    accs = {a["id"]: a for a in db.accounts()}
    for t in db.list_tx():
        cur = t["orig_currency"] or accs.get(t["account_id"], {}).get("currency")
        amount = t["orig_amount"] if t["orig_amount"] is not None else t["amount"]
        try:
            db.update_tx(t["id"], base_amount=_convert(amount, cur, base))
        except LedgerError:
            pass


# ---------- parsed AI record -> transaction ----------

def apply_parsed(p, **extra):
    """Create a transaction from ai.parse() output. Returns tx id, or raises LedgerError."""
    kind = p.get("kind")
    amount = float(p.get("amount") or 0)
    if kind == "unclear":
        raise LedgerError(p.get("comment") or "I didn’t understand what to record.")
    if amount <= 0:
        raise LedgerError(p.get("comment") or "I couldn’t read the amount.")
    common = dict(
        date=p.get("date"), merchant=p.get("merchant") or None,
        description=p.get("description") or None, **extra,
    )
    hint = p.get("account") if p.get("account") in db.account_ids() else None
    if kind == "income":
        return add_income(amount, p.get("currency") or None, hint, **common)
    if kind == "transfer":
        cur = (p.get("currency") or "").upper()
        src = pick_account(cur, hint)
        dst = p.get("to_account")
        if dst not in db.account_ids():
            raise LedgerError("I didn’t understand which account the transfer goes to.")
        src_cur = db.account(src)["currency"]
        if cur and cur != src_cur:  # «перевёл 2000 ринггит с тайского» — пересчитаем в валюту счёта
            amount = _convert(amount, cur, src_cur)
        return add_transfer(src, dst, amount, p.get("to_amount") or None, **common)
    return add_expense(amount, p.get("currency"), hint, category=p.get("category"),
                       items=p.get("items") or [], **common)


# ---------- summaries ----------

def summary(month=None):
    month = month or date.today().strftime("%Y-%m")
    bal = db.balances()
    accs = []
    total_base = 0.0
    for a in db.accounts():
        b = bal[a["id"]]
        base = _convert(b, a["currency"], db.base_currency())
        total_base += base
        accs.append({**a, "balance": b, "balance_base": base})

    txs = db.list_tx(month)
    spent = income = 0.0
    by_cat = defaultdict(float)
    by_day = defaultdict(float)
    by_acc = defaultdict(float)
    for t in txs:
        if t["type"] == "expense":
            v = t["base_amount"] or 0
            spent += v
            by_cat[t["category"] or db.OTHER] += v
            by_day[t["date"]] += v
            by_acc[t["account_id"]] += t["amount"]
        elif t["type"] == "income":
            income += t["base_amount"] or 0

    cats = sorted(
        ({"name": k, "emoji": db.CATEGORY_EMOJI.get(k, "📦"), "total": round(v, 2)} for k, v in by_cat.items()),
        key=lambda c: -c["total"],
    )
    return {
        "month": month,
        "base_currency": db.base_currency(),
        "accounts": accs,
        "total_base": round(total_base, 2),
        "spent": round(spent, 2),
        "income": round(income, 2),
        "by_category": cats,
        "by_day": {k: round(v, 2) for k, v in sorted(by_day.items())},
        "spent_by_account": {k: round(v, 2) for k, v in by_acc.items()},
        "months": sorted(set(db.months()) | {date.today().strftime("%Y-%m")}, reverse=True),
        "rates_updated": rates.updated_at(),
        "categories": [{"name": n, "emoji": e} for n, e in db.CATEGORIES],
    }


# ---------- telegram text ----------

def describe(tx):
    """Short HTML description of a transaction for Telegram."""
    e = html.escape
    acc = db.account(tx["account_id"])
    if tx["type"] == "expense":
        emoji = db.CATEGORY_EMOJI.get(tx["category"], "📦")
        lines = [f"✅ <b>{fmt(tx['orig_amount'], tx['orig_currency'])}</b> — {emoji} {e(tx['category'] or db.OTHER)}"]
        what = tx["description"] or tx["merchant"]
        if what:
            lines.append(e(what))
        pay = f"{acc['flag']} {acc['name']}"
        if tx["orig_currency"] != acc["currency"]:
            pay += f" (charged ≈ {fmt(tx['amount'], acc['currency'])})"
        lines.append(pay)
    elif tx["type"] == "income":
        lines = [f"💰 Top-up <b>{fmt(tx['amount'], acc['currency'])}</b>", f"{acc['flag']} {acc['name']}"]
    elif tx["type"] == "transfer":
        dst = db.account(tx["to_account_id"])
        r = tx["to_amount"] / tx["amount"] if tx["amount"] else 0
        lines = [
            f"🔁 Transfer <b>{fmt(tx['amount'], acc['currency'])}</b> → <b>{fmt(tx['to_amount'], dst['currency'])}</b>",
            f"{acc['flag']} {acc['name']} → {dst['flag']} {dst['name']}",
            f"Rate: 1 {acc['currency']} = {r:.4f} {dst['currency']}",
        ]
    else:
        lines = [f"⚖️ Reconciliation: {fmt(tx['amount'], acc['currency'])} on {acc['flag']} {acc['name']}"]
    if tx["date"] != date.today().isoformat():
        lines.append(f"📅 {datetime.strptime(tx['date'], '%Y-%m-%d').strftime('%d %b %Y')}")
    bal = db.balances()
    lines.append(f"Balance {acc['flag']}: {fmt(bal[acc['id']], acc['currency'])}")
    return "\n".join(lines)


def balance_text():
    s = summary()
    lines = ["<b>Balances</b>"]
    for a in s["accounts"]:
        extra = "" if a["currency"] == db.base_currency() else f"  (≈ {fmt(a['balance_base'], db.base_currency())})"
        lines.append(f"{a['flag']} {a['name']}: <b>{fmt(a['balance'], a['currency'])}</b>{extra}")
    lines.append(f"\nTotal ≈ <b>{fmt(s['total_base'], db.base_currency())}</b>")
    return "\n".join(lines)


def month_text(month=None):
    s = summary(month)
    name = datetime.strptime(s["month"], "%Y-%m").strftime("%B %Y")
    lines = [f"<b>Expenses in {name}</b>: ≈ {fmt(s['spent'], db.base_currency())}"]
    for c in s["by_category"]:
        share = c["total"] / s["spent"] * 100 if s["spent"] else 0
        lines.append(f"{c['emoji']} {html.escape(c['name'])}: {fmt(c['total'], db.base_currency())} · {share:.0f}%")
    if s["income"]:
        lines.append(f"\n💰 Top-ups: {fmt(s['income'], db.base_currency())}")
    return "\n".join(lines)
