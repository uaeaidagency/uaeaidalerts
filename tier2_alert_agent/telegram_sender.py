"""
UAE Aid Agency — Tier-2+ Alert Agent
Telegram sender.

Sends alerts via the Telegram Bot API. For each configured chat_id:
  - If the PDF exists, calls sendDocument with the alert text as caption.
  - Otherwise calls sendMessage.

DEPENDENCIES: Standard library only (urllib, json, uuid, mimetypes).

CONFIG:
  telegram.env (this folder):
      TELEGRAM_BOT_TOKEN=123456789:ABC-DEF...
  config.json:
      "telegram": {
          "enabled": true,
          "chat_ids": [123456789, -1001234567890]
      }

HOW TO GET A BOT TOKEN:
  1. Open Telegram, search for @BotFather, send /newbot
  2. Pick a name (e.g. "UAE Aid Alerts") and username (must end in 'bot')
  3. BotFather replies with a token like: 7234567890:AAFxyz...

HOW TO GET A CHAT_ID:
  1. Start a chat with your new bot and send it any message
     (OR add it to a group / channel and send a message there)
  2. Visit in browser:
        https://api.telegram.org/bot<TOKEN>/getUpdates
  3. Find "chat":{"id": ...} in the JSON. That number is your chat_id.
     - Personal chats: positive number
     - Groups: negative number
     - Channels: -100... prefixed
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import urllib.parse
import urllib.request
import uuid
from typing import Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
TELEGRAM_ENV_FILE = os.path.join(HERE, "telegram.env")
API_TIMEOUT_SEC = 30
TELEGRAM_CAPTION_MAX = 1024     # Telegram limit for sendDocument captions
TELEGRAM_MESSAGE_MAX = 4096     # Telegram limit for sendMessage text


def _read_token() -> Optional[str]:
    # Cloud deployment (Railway): token supplied as environment variable.
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_token:
        return env_token
    # Local: read from telegram.env file.
    if not os.path.exists(TELEGRAM_ENV_FILE):
        return None
    with open(TELEGRAM_ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip().upper() == "TELEGRAM_BOT_TOKEN":
                return v.strip().strip('"').strip("'")
    return None


def _post(url: str, body: bytes, content_type: str) -> dict:
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=API_TIMEOUT_SEC) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "raw": raw[:500].decode("utf-8", "replace")}


def _build_multipart(
    fields: List[Tuple[str, str]],
    file_field: str,
    file_path: str,
) -> Tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: List[bytes] = []
    sep = f"--{boundary}\r\n".encode()
    end = f"--{boundary}--\r\n".encode()

    for name, value in fields:
        if value is None:
            continue
        parts.append(sep)
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(str(value).encode("utf-8"))
        parts.append(b"\r\n")

    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    parts.append(sep)
    parts.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'
        ).encode()
    )
    with open(file_path, "rb") as f:
        parts.append(f.read())
    parts.append(b"\r\n")
    parts.append(end)

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _send_message(token: str, chat_id, text: str, parse_mode: str = "HTML") -> dict:
    if len(text) > TELEGRAM_MESSAGE_MAX:
        text = text[: TELEGRAM_MESSAGE_MAX - 200] + "\n…(truncated)"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    ).encode("utf-8")
    return _post(url, body, "application/x-www-form-urlencoded")


def _send_document(
    token: str,
    chat_id,
    file_path: str,
    caption: Optional[str] = None,
    parse_mode: str = "HTML",
) -> dict:
    if caption and len(caption) > TELEGRAM_CAPTION_MAX:
        caption = caption[: TELEGRAM_CAPTION_MAX - 200] + "\n…(see attached PDF)"
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    body, content_type = _build_multipart(
        fields=[
            ("chat_id", str(chat_id)),
            ("caption", caption or ""),
            ("parse_mode", parse_mode),
        ],
        file_field="document",
        file_path=file_path,
    )
    return _post(url, body, content_type)


def send_alert(
    chat_ids: Iterable,
    text_html: str,
    attachment_path: Optional[str] = None,
    attachment_paths=None,
) -> bool:
    """Send the alert to every chat_id. Returns True iff ALL sends succeed.

    If multiple attachments are provided, the text message goes as the
    caption on the FIRST document, and the remaining documents are sent
    as standalone follow-up messages so all PDFs land in the chat.
    """
    token = _read_token()
    if not token:
        print(
            "[telegram] TELEGRAM_BOT_TOKEN not set in telegram.env; skipping Telegram send.",
            file=sys.stderr,
        )
        return False

    chat_ids = list(chat_ids)
    if not chat_ids:
        print("[telegram] no chat_ids configured; skipping Telegram send.", file=sys.stderr)
        return False

    # Normalize attachments to a list.
    if attachment_paths is None:
        attachment_paths = [attachment_path] if attachment_path else []
    elif isinstance(attachment_paths, (str, bytes, os.PathLike)):
        attachment_paths = [attachment_paths]
    files = [str(p) for p in attachment_paths if p and os.path.exists(str(p))]

    all_ok = True
    for cid in chat_ids:
        try:
            if not files:
                # No attachments — single text message.
                result = _send_message(token, cid, text_html)
                if not result.get("ok"):
                    all_ok = False
                    print(
                        f"[telegram] API returned not-ok for chat {cid}: "
                        f"{result.get('description') or result}",
                        file=sys.stderr,
                    )
                continue

            # First attachment carries the full HTML text as its caption.
            first = files[0]
            result = _send_document(token, cid, first, caption=text_html)
            if not result.get("ok"):
                all_ok = False
                print(
                    f"[telegram] API not-ok (first doc) for chat {cid}: "
                    f"{result.get('description') or result}",
                    file=sys.stderr,
                )
            # Remaining attachments go as plain follow-ups (no caption).
            for extra in files[1:]:
                result = _send_document(token, cid, extra, caption="")
                if not result.get("ok"):
                    all_ok = False
                    print(
                        f"[telegram] API not-ok (extra doc {os.path.basename(extra)}) "
                        f"for chat {cid}: {result.get('description') or result}",
                        file=sys.stderr,
                    )
        except Exception as e:
            all_ok = False
            print(f"[telegram] send to chat {cid} failed: {e}", file=sys.stderr)
    return all_ok


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Send a test Telegram message.")
    p.add_argument("chat_id", help="Telegram chat_id (run getUpdates to find yours).")
    p.add_argument("--text", default="Telegram test from UAE Aid Tier-2+ Alert Agent.")
    p.add_argument("--file", default=None, help="Optional file path to attach.")
    args = p.parse_args()
    ok = send_alert([args.chat_id], args.text, attachment_path=args.file)
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
