#!/usr/bin/env python3
"""
Safe STEX range-menu Telegram bot.

Features:
- /start with SELECT RANGE
- Owner-only ADMIN menu
- Add/delete/display range labels and range IDs
- Configure STEX API base URL and API key (mauthapi)
- Test API by calling the documented /liveaccess endpoint

This safe version does NOT allocate real phone numbers or forward OTPs.
Set BOT_TOKEN and OWNER_ID as environment variables before running.
"""

import json
import os
import time
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_TEXT = os.environ.get("OWNER_ID", "").strip()
DATA_FILE = Path(os.environ.get("BOT_DATA_FILE", "stex_bot_data.json"))
DEFAULT_API_BASE = "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN environment variable.")
if not OWNER_ID_TEXT.isdigit():
    raise SystemExit("Missing/invalid OWNER_ID environment variable (your numeric Telegram user ID).")

OWNER_ID = int(OWNER_ID_TEXT)
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_DATA = {
    "api_base": DEFAULT_API_BASE,
    "api_key": "",
    "ranges": [
        {"label": "UK (example)", "rid": "26134"},
        {"label": "India (example)", "rid": "0"},
        {"label": "USA (example)", "rid": "0"},
        {"label": "Bangladesh (example)", "rid": "0"},
    ],
}
user_states = {}


def load_data():
    if DATA_FILE.exists():
        try:
            obj = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                merged = dict(DEFAULT_DATA)
                merged.update(obj)
                if not isinstance(merged.get("ranges"), list):
                    merged["ranges"] = []
                return merged
        except Exception as exc:
            logging.warning("Could not read data file: %s", exc)
    return json.loads(json.dumps(DEFAULT_DATA))


data = load_data()


def save_data():
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def tg(method, payload=None, timeout=35):
    response = requests.post(TG_API + method, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result.get("result")


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    return tg("sendMessage", payload)


def edit(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    try:
        return tg("editMessageText", payload)
    except Exception:
        return send(chat_id, text, keyboard)


def answer_callback(callback_id, text=""):
    try:
        tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
    except Exception:
        pass


def ik(rows):
    return {"inline_keyboard": rows}


def main_keyboard(uid):
    rows = [[{"text": "📱 SELECT RANGE", "callback_data": "ranges"}]]
    if uid == OWNER_ID:
        rows.append([{"text": "⚙️ ADMIN", "callback_data": "admin"}])
    return ik(rows)


def ranges_keyboard():
    rows = []
    for i, item in enumerate(data["ranges"]):
        rows.append([{
            "text": item.get("label", f"Range {i+1}"),
            "callback_data": f"range_{i}"
        }])
    rows.append([{"text": "🏠 MAIN MENU", "callback_data": "home"}])
    return ik(rows)


def admin_keyboard():
    return ik([
        [{"text": "➕ ADD RANGE", "callback_data": "admin_add_range"}],
        [{"text": "🗑 DELETE RANGE", "callback_data": "admin_delete_range"}],
        [{"text": "🔌 TEST API", "callback_data": "admin_test_api"}],
        [{"text": "🌐 SET API URL", "callback_data": "admin_set_url"}],
        [{"text": "🔑 SET API KEY", "callback_data": "admin_set_key"}],
        [{"text": "📋 SHOW SETTINGS", "callback_data": "admin_show"}],
        [{"text": "🏠 MAIN MENU", "callback_data": "home"}],
    ])


def is_owner(uid):
    return int(uid) == OWNER_ID


def api_request(path):
    base = data.get("api_base", "").strip().rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    headers = {}
    key = data.get("api_key", "").strip()
    if key:
        headers["mauthapi"] = key
    return requests.get(url, headers=headers, timeout=20)


def start_text():
    return "Welcome! 👋\n\nChoose an option below."


def handle_message(msg):
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return
    uid = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not uid:
        return

    if text.startswith("/cancel"):
        user_states.pop(uid, None)
        send(uid, "Cancelled.", main_keyboard(uid))
        return

    if text.startswith("/start"):
        user_states.pop(uid, None)
        send(uid, start_text(), main_keyboard(uid))
        return

    state = user_states.get(uid)
    if not state:
        send(uid, "Use /start to open the menu.", main_keyboard(uid))
        return

    if state == "add_range":
        parts = text.split("|", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            send(uid, "Format:\nRange Name | Range ID\n\nExample: UK | 26134\nUse /cancel to stop.")
            return
        label, rid = parts[0].strip(), parts[1].strip()
        if not rid.isdigit():
            send(uid, "Range ID must contain digits only. Try again or /cancel.")
            return
        data["ranges"].append({"label": label, "rid": rid})
        save_data()
        user_states.pop(uid, None)
        send(uid, f"✅ Range saved: {label}\nRange ID: {rid}", admin_keyboard())
        return

    if state == "set_url":
        if not text.startswith("https://"):
            send(uid, "Please send a complete HTTPS API URL, or /cancel.")
            return
        data["api_base"] = text.rstrip("/")
        save_data()
        user_states.pop(uid, None)
        send(uid, "✅ API URL saved.", admin_keyboard())
        return

    if state == "set_key":
        data["api_key"] = text
        save_data()
        user_states.pop(uid, None)
        send(uid, "✅ API key saved. It will not be displayed in the settings summary.", admin_keyboard())
        return

    send(uid, "I didn't understand that. Send /cancel or /start.")


def handle_callback(call):
    callback_id = call.get("id")
    message = call.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    msg_id = message.get("message_id")
    uid = call.get("from", {}).get("id")
    action = call.get("data", "")
    if chat_id is None or uid is None:
        answer_callback(callback_id)
        return

    if action == "home":
        user_states.pop(uid, None)
        edit(chat_id, msg_id, start_text(), main_keyboard(uid))
    elif action == "ranges":
        if not data["ranges"]:
            edit(chat_id, msg_id, "No ranges configured yet.", main_keyboard(uid))
        else:
            lines = ["🌍 SELECT YOUR RANGE\n"]
            for i, item in enumerate(data["ranges"], 1):
                lines.append(f"{i}. {item.get('label', 'Range')} — ID {item.get('rid', '?')}")
            lines.append("\nThis safe menu displays configured ranges only; it does not allocate numbers.")
            edit(chat_id, msg_id, "\n".join(lines), ranges_keyboard())
    elif action.startswith("range_"):
        try:
            idx = int(action.split("_", 1)[1])
            item = data["ranges"][idx]
            edit(
                chat_id, msg_id,
                f"✅ Range selected: {item.get('label', 'Range')}\n"
                f"Range ID: {item.get('rid', '?')}\n\n"
                "Number allocation and OTP forwarding are not enabled in this safe build.",
                ik([[{"text": "🔙 BACK TO RANGES", "callback_data": "ranges"}],
                    [{"text": "🏠 MAIN MENU", "callback_data": "home"}]])
            )
        except (ValueError, IndexError):
            edit(chat_id, msg_id, "Range not found.", ranges_keyboard())
    elif action == "admin":
        if not is_owner(uid):
            answer_callback(callback_id, "Admin access denied.")
            return
        edit(chat_id, msg_id, "⚙️ ADMIN PANEL\nChoose an action:", admin_keyboard())
    elif action.startswith("admin_"):
        if not is_owner(uid):
            answer_callback(callback_id, "Admin access denied.")
            return
        if action == "admin_add_range":
            user_states[uid] = "add_range"
            edit(chat_id, msg_id, "Send range as:\nRange Name | Range ID\nExample: UK | 26134\n\n/cancel to stop.")
        elif action == "admin_delete_range":
            rows = []
            for i, item in enumerate(data["ranges"]):
                rows.append([{"text": f"🗑 {item.get('label', 'Range')} ({item.get('rid', '?')})",
                              "callback_data": f"delete_range_{i}"}])
            rows.append([{"text": "🔙 BACK", "callback_data": "admin"}])
            edit(chat_id, msg_id, "Choose a range to delete:", ik(rows))
        elif action.startswith("admin_test_api"):
            edit(chat_id, msg_id, "⏳ Checking STEX /liveaccess endpoint...")
            try:
                r = api_request("/liveaccess")
                preview = r.text[:2500]
                edit(chat_id, msg_id, f"HTTP status: {r.status_code}\n\nResponse preview:\n{preview}",
                     admin_keyboard())
            except Exception as exc:
                edit(chat_id, msg_id, f"❌ API request failed:\n{str(exc)[:500]}", admin_keyboard())
        elif action == "admin_set_url":
            user_states[uid] = "set_url"
            edit(chat_id, msg_id, "Send the API base URL (HTTPS), or /cancel.")
        elif action == "admin_set_key":
            user_states[uid] = "set_key"
            edit(chat_id, msg_id, "Send the API key. It will be stored locally and not shown in settings.")
        elif action == "admin_show":
            masked = "configured" if data.get("api_key") else "not set"
            lines = [
                "📋 SETTINGS",
                f"API URL: {data.get('api_base', '')}",
                f"API key: {masked}",
                f"Ranges configured: {len(data.get('ranges', []))}",
                "",
                "Note: this safe build does not allocate phone numbers or forward OTPs."
            ]
            edit(chat_id, msg_id, "\n".join(lines), admin_keyboard())
    elif action.startswith("delete_range_"):
        if not is_owner(uid):
            answer_callback(callback_id, "Admin access denied.")
            return
        try:
            idx = int(action.rsplit("_", 1)[1])
            item = data["ranges"].pop(idx)
            save_data()
            edit(chat_id, msg_id, f"✅ Deleted range: {item.get('label', 'Range')}", admin_keyboard())
        except (ValueError, IndexError):
            edit(chat_id, msg_id, "Range not found.", admin_keyboard())
    else:
        answer_callback(callback_id)


def main():
    logging.info("Bot started. Data file: %s", DATA_FILE.resolve())
    offset = 0
    while True:
        try:
            updates = tg("getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"]
            }, timeout=35)
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
        except KeyboardInterrupt:
            logging.info("Stopped.")
            break
        except Exception as exc:
            logging.warning("Polling error: %s", exc)
            time.sleep(3)


if __name__ == "__main__":
    main()
