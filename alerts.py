#!/usr/bin/env python3
"""
Simple alert utilities (currently Telegram only).
"""
from __future__ import annotations

import requests


def send_telegram_alert(token: str | None, chat_id: str | None, text: str):
    """Send a Telegram message if token/chat_id are provided; otherwise no-op."""
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=5)
        if resp.status_code >= 400:
            print(f"[telegram][warn] sendMessage failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[telegram][warn] sendMessage error: {e}")
