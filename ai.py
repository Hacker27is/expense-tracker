"""Reads receipts and free-text messages with Claude and returns a structured record."""
import base64
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime

import anthropic

import db

log = logging.getLogger("ai")

MODEL = "claude-opus-5"
MAX_IMAGE_BYTES = 4_500_000  # лимит API на картинку ~5 МБ, держим запас

def _schema(account_ids):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "amount", "currency", "category", "merchant", "description",
                     "date", "account", "to_account", "to_amount", "items", "comment"],
        "properties": {
            "kind": {"type": "string", "enum": ["expense", "income", "transfer", "unclear"]},
            "amount": {"type": "number"},
            "currency": {"type": "string"},
            "category": {"type": "string", "enum": db.CATEGORY_NAMES},
            "merchant": {"type": "string"},
            "description": {"type": "string"},
            "date": {"type": "string"},
            "account": {"type": "string", "enum": [*account_ids, "auto"]},
            "to_account": {"type": "string", "enum": [*account_ids, "none"]},
            "to_amount": {"type": "number"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "price"],
                    "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
                },
            },
            "comment": {"type": "string"},
        },
    }


SYSTEM = """You are the parser behind a personal expense-tracker Telegram bot. The owner sends
photos of receipts or bank notifications (usually with a short caption) or short text messages,
in any language. Turn each message into exactly one record.

Accounts (the owner's own bank accounts, id: name, currency):
{accounts}
Top-ups go to "{default_account}" unless the message names another account.

kind:
- expense: a purchase or payment (every receipt photo is an expense unless the caption says otherwise).
- income: money added to the balance ("topped up 50000", "+50000", "salary", "пополнил").
- transfer: moving money between the owner's own accounts ("sent 20000 to my savings account").
- unclear: you cannot tell what the owner means. Explain briefly in comment.

Fields:
- amount: the final total actually paid, after discounts and including tax, VAT and service
  charge. For a transfer, the amount that left the source account. 0 if unknown.
- currency: ISO 4217 code of amount. Map symbols and local words (฿/baht -> THB, RM/ringgit -> MYR,
  S$ -> SGD, € -> EUR, £ -> GBP, ₽/руб -> RUB, and so on). When no symbol is printed, use the receipt's
  language, address and tax format; for a caption with a bare number, use the currency of the
  account it names, else "".
- category: the best fit from the enum.
- merchant: shop or service name as printed, "" if none.
- description: 2-6 words in English saying what was bought, e.g. "Groceries at Big C", "Grab taxi".
- date: YYYY-MM-DD from the receipt if printed, otherwise "". Receipts printed in the Thai
  Buddhist calendar (year 2569 etc.) must be converted: subtract 543.
- account: the account the money left (or arrived on, for income), only if the caption names
  or clearly implies it. Otherwise "auto".
- to_account / to_amount: only for transfers - destination account and the amount that arrived,
  if stated (to_amount 0 if not stated). "none" and 0 otherwise.
- items: line items from the receipt with their prices (max 30, skip them for text messages).
- comment: one short English sentence only if something needs the owner's attention (total
  unreadable, blurry photo, not a receipt). Otherwise "".

The caption is the owner's own words and overrides what you read on the receipt (amount,
category, currency). Never invent a total you cannot see - use 0 and say so in comment."""


class AIError(Exception):
    pass


_client = None
_client_key = None


def _get_client():
    global _client, _client_key
    key = db.get_setting("anthropic_key")
    if not key:
        raise AIError("No Claude API key set — open the dashboard on your computer and add it in Settings.")
    if _client is None or key != _client_key:
        _client = anthropic.Anthropic(api_key=key, timeout=120, max_retries=3)
        _client_key = key
    return _client


def _image_block(path):
    """Build an image or PDF content block, converting HEIC and shrinking large photos via macOS sips."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}

    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext)
    src = path
    tmp = None
    if media is None or os.path.getsize(path) > MAX_IMAGE_BYTES:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "2400", path, "--out", tmp],
                       check=True, capture_output=True)
        src, media = tmp, "image/jpeg"
    try:
        with open(src, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
    finally:
        if tmp:
            os.unlink(tmp)
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def parse(text="", file_path=None, today=None):
    """Return the parsed record as a dict. Raises AIError with a user-facing message.
    `today` is the day the message was sent (a delayed message must not get the processing date)."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    content = []
    if file_path:
        content.append(_image_block(file_path))
    prompt = f"Today is {today}.\n"
    prompt += f"Caption from the owner: {text}" if file_path else f"Message from the owner: {text}"
    if file_path and not text:
        prompt += "(no caption)"
    content.append({"type": "text", "text": prompt})

    accs = db.accounts()
    if not accs:
        raise AIError("Add at least one account in the dashboard first.")
    ids = [a["id"] for a in accs]
    system = SYSTEM.format(
        accounts="\n".join(f"- {a['id']}: {a['name']}, {a['currency']}" for a in accs),
        default_account=db.default_account(),
    )
    try:
        resp = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _schema(ids)}},
            system=system,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        raise AIError("The Claude API key was rejected. Check it in the dashboard settings.")
    except anthropic.PermissionDeniedError:
        raise AIError("The Claude API key has no access. Check your account at console.anthropic.com.")
    except anthropic.RateLimitError:
        raise AIError("Claude is overloaded or rate-limited. Try again in a minute.")
    except anthropic.BadRequestError as e:
        if "credit" in str(e).lower():
            raise AIError("Your Claude API credit has run out — top it up at console.anthropic.com.")
        log.exception("bad request")
        raise AIError("Claude couldn’t process this message.")
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude returned an error ({e.status_code}). Try again later.")
    except anthropic.APIConnectionError:
        raise AIError("Can’t reach Claude. Check the computer’s internet connection.")

    if resp.stop_reason == "refusal":
        raise AIError("Claude declined to read this message.")
    if resp.stop_reason == "max_tokens":
        raise AIError("The receipt is too long for Claude to finish reading.")
    text_out = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text_out)
    except ValueError:
        log.error("non-JSON reply: %r", text_out[:500])
        raise AIError("Claude returned an unreadable answer. Try again.")
