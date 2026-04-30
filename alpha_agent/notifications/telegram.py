"""
Notificaciones via Telegram Bot API.

Setup (una sola vez):
  1. Hablar con @BotFather en Telegram → /newbot → guardar el TOKEN
  2. Mandar cualquier mensaje al bot desde tu cuenta
  3. Abrir https://api.telegram.org/bot<TOKEN>/getUpdates → copiar "id" del chat
  4. Agregar al .env:
       TELEGRAM_BOT_TOKEN=<TOKEN>
       TELEGRAM_CHAT_ID=<ID numérico>

Sin las keys el módulo falla silenciosamente (no rompe el pipeline).
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10


def send_telegram(text: str, *, parse_mode: str = "Markdown") -> bool:
    """
    Envía un mensaje al chat configurado en TELEGRAM_CHAT_ID.
    Devuelve True si fue exitoso, False si falla (nunca levanta excepción).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        return False

    # Telegram tiene límite de 4096 chars por mensaje
    if len(text) > 4096:
        text = text[:4090] + "\n…"

    try:
        resp = requests.post(
            _API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            log.info("Telegram OK (%d chars)", len(text))
            return True
        log.warning("Telegram error %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        log.warning("Telegram no disponible: %s", exc)
        return False
