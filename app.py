"""Entry point: starts the Telegram bot and the local web UI."""
import logging
import os
import socket
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler

import db

# В этой сети IPv6 не работает: Python без «happy eyeballs» ждёт его до таймаута
# (20–70 с на каждый запрос к Telegram). Пробуем IPv4 первым.
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **kw: sorted(_getaddrinfo(*a, **kw), key=lambda r: r[0] != socket.AF_INET)

LOG_PATH = os.path.join(db.DATA_DIR, "tracker.log")

_handlers = [RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
if sys.stdout.isatty():  # в фоне (автозапуск) в консоль не пишем, только в файл
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    handlers=_handlers)
logging.getLogger("httpx").setLevel(logging.WARNING)

import bot  # noqa: E402
import rates  # noqa: E402
import web  # noqa: E402


def main():
    url = f"http://{web.HOST}:{web.PORT}"
    try:
        httpd = web.make_server()
    except OSError as e:
        if e.errno == 48:  # порт занят — трекер уже запущен, второй бот не нужен
            print("Expense Tracker is already running, opening the dashboard.")
            webbrowser.open(url)
            return
        raise
    db.init()
    threading.Thread(target=rates.refresh, daemon=True).start()
    bot.start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    web.serve(httpd)


if __name__ == "__main__":
    main()
