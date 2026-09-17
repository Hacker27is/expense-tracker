"""Exchange rates: fetched from open.er-api.com (free, no key), cached in SQLite."""
import json
import logging
import threading
import urllib.request
from datetime import datetime, timedelta

import db

log = logging.getLogger("rates")

REFRESH_EVERY = timedelta(hours=6)
# Последний запасной вариант, если интернета нет и кэш пустой (курсы на 14.09.2026).
SEED_PER_USD = {"USD": 1.0, "THB": 33.08, "MYR": 4.07, "SGD": 1.267, "EUR": 0.862, "RUB": 84.24}

_lock = threading.Lock()


class UnknownCurrency(Exception):
    pass


def _fetch():
    req = urllib.request.Request(
        "https://open.er-api.com/v6/latest/USD", headers={"User-Agent": "expense-tracker/1.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    if data.get("result") != "success":
        raise RuntimeError(f"rates API: {data.get('error-type')}")
    return data["rates"]


def refresh(force=False):
    with _lock:
        rates, updated = db.load_rates()
        fresh = updated and datetime.fromisoformat(updated) > datetime.now() - REFRESH_EVERY
        if fresh and not force:
            return rates
        try:
            rates = _fetch()
            db.save_rates(rates)
            log.info("rates refreshed (%d currencies)", len(rates))
        except Exception as e:  # сеть упала — живём на кэше
            log.warning("rates refresh failed: %s", e)
            if not rates:
                rates = dict(SEED_PER_USD)
        return rates


def convert(amount, from_cur, to_cur):
    from_cur, to_cur = (from_cur or "").upper(), (to_cur or "").upper()
    if from_cur == to_cur:
        return round(float(amount), 2)
    rates = refresh()
    if from_cur not in rates or to_cur not in rates:
        raise UnknownCurrency(from_cur if from_cur not in rates else to_cur)
    return round(float(amount) / rates[from_cur] * rates[to_cur], 2)


def rate(from_cur, to_cur):
    """How many to_cur you get for 1 from_cur."""
    rates = refresh()
    return rates[to_cur.upper()] / rates[from_cur.upper()]


def updated_at():
    return db.load_rates()[1]
