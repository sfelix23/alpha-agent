"""cloud_webhook.py — Webhook Telegram para Cloud Run Service (iter34).

Bot 24/7 sin la PC del usuario. Reusa alpha_agent.notifications.telegram para
responder. Comandos cloud-safe (read-only + Alpaca): estado, cartera, equity,
health, llm, universo, ayuda. Comandos PC-específicos (shutdown/sleep/wake) NO
están — siguen en dashboard/app.py local.

Secrets esperados (Secret Manager → env en Cloud Run):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALPACA_API_KEY, ALPACA_SECRET_KEY

Deploy:
  gcloud run deploy alpha-bot \
    --image us-central1-docker.pkg.dev/alpha-agent-2025/alpha/agent:latest \
    --region us-central1 --platform managed --allow-unauthenticated \
    --set-secrets=TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,\
TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest,\
ALPACA_API_KEY=ALPACA_API_KEY:latest,\
ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest \
    --command=python --args=cloud_webhook.py
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from flask import Flask, request

from alpha_agent.notifications.telegram import send_telegram

log = logging.getLogger("cloud_webhook")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s — %(message)s")

app = Flask(__name__)

ALLOWED_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
RAW_REPO = "https://raw.githubusercontent.com/sfelix23/alpha-agent/master"


def _fetch_repo_json(path: str):
    """Lee un JSON del repo master via raw URL. Devuelve dict o None."""
    try:
        url = f"{RAW_REPO}/{path}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning("fetch %s falló: %s", path, e)
        return None


def _alpaca_account():
    """Devuelve (equity, buying_power, positions_list) o None si Alpaca falla."""
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            os.getenv("ALPACA_API_KEY"),
            os.getenv("ALPACA_SECRET_KEY"),
            paper=True,
        )
        acc = client.get_account()
        positions = client.get_all_positions()
        return float(acc.equity), float(acc.buying_power), positions
    except Exception as e:
        log.error("Alpaca falló: %s", e)
        return None


def _cmd_estado() -> str:
    res = _alpaca_account()
    if not res:
        return "❌ Error consultando Alpaca."
    eq, bp, pos = res
    ws = _fetch_repo_json("signals/workflow_status.json") or {}
    daily = ws.get("alpha_daily", {})
    monitor = ws.get("alpha_monitor", {})
    return (
        f"*ESTADO*\n"
        f"Equity: ${eq:,.2f}\n"
        f"Buying power: ${bp:,.2f}\n"
        f"Posiciones abiertas: {len(pos)}\n"
        f"Último daily: {daily.get('ts','?')} (ok={daily.get('ok')})\n"
        f"Último monitor: {monitor.get('ts','?')}"
    )


def _cmd_cartera() -> str:
    res = _alpaca_account()
    if not res:
        return "❌ Error consultando Alpaca."
    _, _, pos = res
    if not pos:
        return "Sin posiciones abiertas."
    lines = ["*CARTERA*"]
    total_mv = 0.0
    total_pl = 0.0
    for p in pos:
        mv = float(p.market_value)
        pl = float(p.unrealized_pl)
        pl_pct = float(p.unrealized_plpc) * 100
        total_mv += mv
        total_pl += pl
        emoji = "🟢" if pl > 0 else ("🔴" if pl < 0 else "⚪")
        lines.append(f"{emoji} {p.symbol}: ${mv:,.0f} ({pl_pct:+.1f}%, ${pl:+.0f})")
    lines.append(f"\nTotal MV: ${total_mv:,.0f}  |  P&L abierto: ${total_pl:+,.0f}")
    return "\n".join(lines)


def _cmd_equity() -> str:
    res = _alpaca_account()
    if not res:
        return "❌ Error consultando Alpaca."
    eq, bp, pos = res
    baseline = 1600.0
    ret = (eq - baseline) / baseline * 100
    invested = sum(float(p.market_value) for p in pos)
    deploy_pct = (invested / eq * 100) if eq > 0 else 0
    return (
        f"*EQUITY*\n"
        f"${eq:,.2f}\n"
        f"Baseline: ${baseline:,.0f}\n"
        f"Retorno: {ret:+.2f}%\n"
        f"Desplegado: {deploy_pct:.0f}% (${invested:,.0f})\n"
        f"Cash: ${bp:,.0f}"
    )


def _cmd_llm() -> str:
    d = _fetch_repo_json("signals/llm_budget.json")
    if not d:
        return "❌ No pude leer llm_budget.json."
    total = d.get("total_calls", 0)
    cost = d.get("total_cost_usd", 0)
    lines = [f"*LLM* — calls hoy: {total} | costo: ${cost:.4f}"]
    for pid, info in d.get("providers", {}).items():
        c = info.get("calls", 0)
        co = info.get("cost_usd", 0)
        if c > 0 or co > 0:
            lines.append(f"  {pid}: {c} calls / ${co:.4f}")
    return "\n".join(lines)


def _cmd_health() -> str:
    """Snapshot completo tipo --health."""
    res = _alpaca_account()
    if not res:
        return "❌ Error consultando Alpaca."
    eq, _, pos = res
    ws = _fetch_repo_json("signals/workflow_status.json") or {}
    gate = _fetch_repo_json("signals/entry_gate.json")
    llm = _fetch_repo_json("signals/llm_budget.json") or {}
    lines = ["*HEALTH SNAPSHOT*"]
    lines.append(f"Equity: ${eq:,.2f}  |  Open: {len(pos)}")
    for job in ("alpha_daily", "alpha_monitor"):
        j = ws.get(job, {})
        ok = "🟢" if j.get("ok") else "🔴"
        lines.append(f"{ok} {job}: {j.get('ts','?')}")
    if gate and gate.get("last_entry_date"):
        from datetime import date
        try:
            d = date.fromisoformat(gate["last_entry_date"])
            days = (date.today() - d).days
            remaining = max(0, 21 - days)
            icon = "🔵" if remaining > 0 else "🟢"
            lines.append(f"{icon} entry_gate: hace {days}d, faltan {remaining}d")
        except Exception:
            lines.append(f"entry_gate: {gate['last_entry_date']}")
    else:
        lines.append("🟢 entry_gate: ABIERTA (bootstrap)")
    lines.append(f"LLM: {llm.get('total_calls',0)} calls / ${llm.get('total_cost_usd',0):.4f}")
    return "\n".join(lines)


def _cmd_universo() -> str:
    ov = _fetch_repo_json("signals/cp_universe_overrides.json")
    if not ov:
        return "Universe overrides: vacío (usa CP_UNIVERSE base)."
    added = ov.get("added", [])
    removed = ov.get("removed", [])
    vetoed = ov.get("vetoed", [])
    hist = ov.get("history", [])[-3:]
    lines = ["*UNIVERSO CP*"]
    if added:
        lines.append(f"➕ Agregados: {', '.join(added)}")
    if removed:
        lines.append(f"➖ Removidos: {', '.join(removed)}")
    if vetoed:
        lines.append(f"🚫 Vetados: {', '.join(vetoed)}")
    if hist:
        lines.append("\n_Últimas rotaciones:_")
        for h in hist:
            ts = (h.get("ts", "") or "")[:10]
            lines.append(f"  {ts}: +{h.get('in','?')} -{h.get('out','?')}")
    if not (added or removed or vetoed or hist):
        lines.append("(sin overrides ni historial)")
    return "\n".join(lines)


def _help() -> str:
    return (
        "*ALPHA BOT — cloud edition*\n"
        "estado — equity + posiciones + último daily\n"
        "cartera — posiciones con P&L abierto\n"
        "equity — capital, retorno, despliegue\n"
        "health — snapshot completo del sistema\n"
        "llm — costos LLM hoy\n"
        "universo — CP universe efectivo + rotaciones\n"
        "ayuda — este menú\n\n"
        "_Comandos PC-específicos (shutdown/wake/run) siguen en el bot local._"
    )


def _dispatch(text: str) -> str:
    t = (text or "").strip().lower().lstrip("/")
    if t in ("estado", "status", "hola", "ping"):
        return _cmd_estado()
    if t in ("cartera", "portfolio", "posiciones"):
        return _cmd_cartera()
    if t in ("equity", "capital", "plata", "dinero"):
        return _cmd_equity()
    if t in ("health", "salud", "diagnostico"):
        return _cmd_health()
    if t in ("llm", "budget", "costos"):
        return _cmd_llm()
    if t in ("universo", "universe"):
        return _cmd_universo()
    if t in ("ayuda", "help", "?", "start", "comandos"):
        return _help()
    return f"No entendí '{t}'. Envía *ayuda* para ver comandos."


@app.route("/", methods=["GET"])
def root():
    return ("alpha-bot Cloud Run service — POST a /webhook/telegram\n", 200)


@app.route("/health", methods=["GET"])
def health_probe():
    return ("ok\n", 200)


@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("edited_message") or {}
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text = msg.get("text", "")

    # Whitelist por chat_id (solo Santino)
    if ALLOWED_CHAT and chat_id != ALLOWED_CHAT:
        log.warning("Telegram inbound de chat no autorizado: %s text=%r", chat_id, text[:80])
        return ("", 200)  # silently ignore

    log.info("Telegram cmd from %s: %r", chat_id, text[:100])
    resp = _dispatch(text)
    try:
        send_telegram(resp)
    except Exception as e:
        log.error("send_telegram falló: %s", e)
    return ("", 200)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    log.info("Cloud webhook listening on :%d", port)
    app.run(host="0.0.0.0", port=port)
