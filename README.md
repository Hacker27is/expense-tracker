# Expense Tracker

Send photos of receipts to your own Telegram bot. Claude reads the amount, currency, shop and
category, and a dashboard on your Mac shows what you spent, where, and what's left on each account.

- 📸 Receipt photos, screenshots of bank notifications, PDFs, or plain text ("taxi 18 eur")
- 💳 Your own accounts in any currencies, with transfers between them
- 📊 Local dashboard: balances, spending by category and by day, receipt archive, CSV export
- 🔒 Everything stays on your Mac. The bot only answers you.

**Requirements:** macOS, a Telegram account, and a Claude API key (about 1–2 cents per receipt).

## Install

Open **Terminal** (press ⌘ Space, type "Terminal", press Enter), paste this line and press Enter:

```bash
curl -fsSL https://raw.githubusercontent.com/Hacker27is/expense-tracker/main/install.sh | bash
```

The installer puts the app in `~/ExpenseTracker`, sets it to start automatically when you log in,
and opens the dashboard at http://127.0.0.1:8765. The setup page walks you through the rest:

1. Create a bot with [@BotFather](https://t.me/BotFather) and paste its token.
2. Create a key at [console.anthropic.com](https://console.anthropic.com/settings/keys), add credit, paste the key.
3. Add your accounts and the currency for totals.
4. Click the link to open your bot and press **Start**.

## Using the bot

| Send | What happens |
| --- | --- |
| A receipt photo | Recorded as an expense. A caption like "taxi" or "paid from Revolut" overrides what's on the receipt. |
| `coffee 4.50` | Expense without a photo |
| `topped up 2000` | Top-up to your default account |
| `sent 500 to savings, received 480` | Transfer between two accounts |
| A reply to the bot's message | Re-reads the entry with your correction |

Buttons under each entry change its category or account, or delete it.
Commands: `/balance`, `/month`, `/undo`, `/help`.

Expenses are taken from the account in the same currency. Expenses in a currency none of your
accounts hold go to the default account, converted at the current rate
([open.er-api.com](https://open.er-api.com), refreshed every 6 hours).

## Update

Run the install command again. Your data is kept.

## Where things are

- `~/ExpenseTracker/data/tracker.db` — all records (SQLite). Back this folder up now and then.
- `~/ExpenseTracker/data/receipts/` — receipt photos by month
- `~/ExpenseTracker/data/tracker.log` — log file, if something goes wrong

The bot works while your Mac is on. Messages sent while it's asleep are processed when it wakes
(Telegram keeps them for 24 hours).

## Uninstall

```bash
~/ExpenseTracker/uninstall.sh
```

This stops the app and removes it from login items. Your data stays in `~/ExpenseTracker/data`;
drag the `ExpenseTracker` folder to the Trash to remove everything.

## License

MIT
