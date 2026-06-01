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

from html import escape as _xml_escape

from flask import Flask, Response, request

from alpha_agent.notifications.telegram import send_telegram

log = logging.getLogger("cloud_webhook")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s — %(message)s")

app = Flask(__name__)

ALLOWED_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
# Whitelist WhatsApp por número (formato Twilio: "whatsapp:+5491134567890"). Si
# está vacío, no se filtra (recomendado para no bloquear).
ALLOWED_WA = os.getenv("MY_PHONE_NUMBER", "")
RAW_REPO = "https://raw.githubusercontent.com/sfelix23/alpha-agent/master"
GCP_PROJECT = "alpha-agent-2025"
GCP_REGION = "us-central1"


def _fetch_repo_json(path: str):
    """Lee un JSON del repo master via raw URL. Devuelve dict o None."""
    try:
        url = f"{RAW_REPO}/{path}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning("fetch %s falló: %s", path, e)
        return None


def _github_put_file(path: str, content: str, message: str) -> tuple[bool, str]:
    """iter38: crea/actualiza un archivo en el repo via GitHub API. Usa GH_TOKEN.
    Devuelve (ok, mensaje). Path es relativo a la raíz del repo (ej signals/paused.flag).
    """
    import base64
    token = os.getenv("GH_TOKEN", "")
    if not token:
        return False, "GH_TOKEN no configurado en el Service"
    api = f"https://api.github.com/repos/sfelix23/alpha-agent/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    sha = None
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            sha = json.loads(r.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False, f"GET error {e.code}: {e.reason}"
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": "master",
    }
    if sha:
        body["sha"] = sha
    try:
        req2 = urllib.request.Request(
            api, method="PUT",
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(body).encode("utf-8"),
        )
        with urllib.request.urlopen(req2, timeout=15):
            pass
        return True, "ok"
    except urllib.error.HTTPError as e:
        return False, f"PUT error {e.code}: {e.read().decode('utf-8',errors='ignore')[:100]}"
    except Exception as e:
        return False, f"PUT exception: {e}"


def _github_delete_file(path: str, message: str) -> tuple[bool, str]:
    """iter38: borra un archivo del repo via GitHub API. Idempotente: si no existe, OK."""
    token = os.getenv("GH_TOKEN", "")
    if not token:
        return False, "GH_TOKEN no configurado"
    api = f"https://api.github.com/repos/sfelix23/alpha-agent/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            sha = json.loads(r.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True, "ya no existía (idempotente)"
        return False, f"GET error {e.code}"
    body = {"message": message, "sha": sha, "branch": "master"}
    try:
        req2 = urllib.request.Request(
            api, method="DELETE",
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(body).encode("utf-8"),
        )
        with urllib.request.urlopen(req2, timeout=15):
            pass
        return True, "borrado"
    except Exception as e:
        return False, f"DELETE error: {e}"


def _trigger_cloud_run_job(job_name: str) -> tuple[bool, str]:
    """iter37: dispara un Cloud Run Job via REST API usando el OAuth token del
    metadata server (Cloud Run Service expone el SA default).

    Necesita IAM `roles/run.invoker` para el SA sobre el job target. No requiere
    ninguna librería extra (urllib + el token del metadata server).
    Retorna (ok, mensaje).
    """
    try:
        # 1. Obtener access token del metadata server
        md_url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
        req = urllib.request.Request(md_url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=10) as r:
            token = json.loads(r.read())["access_token"]
        # 2. POST al endpoint :run del job
        exec_url = (
            f"https://run.googleapis.com/v2/projects/{GCP_PROJECT}/locations/"
            f"{GCP_REGION}/jobs/{job_name}:run"
        )
        req2 = urllib.request.Request(
            exec_url, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=b"{}",
        )
        with urllib.request.urlopen(req2, timeout=15) as r:
            resp = json.loads(r.read())
        exec_id = resp.get("metadata", {}).get("name", "?").split("/")[-1]
        return True, f"✅ {job_name} disparado — execution: {exec_id}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        return False, f"❌ {job_name} HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"❌ {job_name} error: {e}"


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


def _alpaca_full():
    """iter44: devuelve dict con equity + last_equity + cash + bp + positions.
    Usado por _cmd_resumen para mostrar daily P&L correctamente.
    """
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(
            os.getenv("ALPACA_API_KEY"),
            os.getenv("ALPACA_SECRET_KEY"),
            paper=True,
        )
        acc = client.get_account()
        return {
            "equity": float(acc.equity),
            "last_equity": float(acc.last_equity),
            "buying_power": float(acc.buying_power),
            "cash": float(acc.cash),
            "positions": client.get_all_positions(),
        }
    except Exception as e:
        log.error("Alpaca full falló: %s", e)
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


def _cmd_oportunidades() -> str:
    """iter50: radar de oportunidades del mercado amplio (read-only).
    Lee opportunities.json (refrescado por el job semanal). No auto-opera."""
    d = _fetch_repo_json("signals/opportunities.json")
    if not d or not d.get("opportunities"):
        return "📡 Radar sin oportunidades cacheadas (se refresca el viernes)."
    opps = d["opportunities"]
    sectors = d.get("sectors", {})
    gen = (d.get("generated_at", "") or "")[:10]
    lines = [f"📡 *RADAR DE OPORTUNIDADES* ({gen})", "_Research, NO auto-opera — vos decidís._"]
    top_sec = list(sectors.items())[:3]
    if top_sec:
        lines.append("Sectores fuertes: " + " · ".join(
            f"{s} {dd['avg_mom_1m']:+.0f}%" for s, dd in top_sec))
    lines.append("")
    for o in opps[:10]:
        tag = "" if o.get("in_universe") else " 🆕"
        lines.append(f"{o.get('etapa','')} {o['ticker']}{tag} — 1m {o.get('ret_1m',0):+.0f}% · "
                     f"vs SPY {o.get('rel_strength',0):+.0f}% · {o.get('setup','')}")
    lines.append("\n🟢 temprana = trend joven · 🔴 tardía = ya corrió (riesgo)")
    fresh = d.get("fresh", [])
    if fresh:
        lines.append(f"\n🆕 Fuera del universo: {', '.join(o['ticker'] for o in fresh[:6])}")
    return "\n".join(lines)


def _cmd_resumen() -> str:
    """iter44: resumen completo del día en una respuesta.
    P&L total, posiciones, deployment, gate iter31, capas defensivas — todo.
    """
    d = _alpaca_full()
    if not d:
        return "❌ Error consultando Alpaca."
    eq = d["equity"]; last_eq = d["last_equity"]; cash = d["cash"]; pos = d["positions"]
    daily_chg = eq - last_eq
    daily_pct = (daily_chg / last_eq * 100) if last_eq > 0 else 0
    real = sorted([p for p in pos if float(p.market_value) >= 5], key=lambda p: -float(p.market_value))
    total_mv = sum(float(p.market_value) for p in real)
    open_pl = sum(float(p.unrealized_pl) for p in real)
    baseline = 1600.0
    total_ret = (eq - baseline) / baseline * 100

    daily_emoji = "🟢" if daily_chg >= 0 else "🔴"
    open_emoji = "🟢" if open_pl >= 0 else "🔴"

    lines = [
        "*📊 RESUMEN DEL DÍA*",
        "",
        f"*Equity*: ${eq:,.2f}  (total {total_ret:+.2f}% desde inicio)",
        f"{daily_emoji} *Daily*: ${daily_chg:+,.2f}  ({daily_pct:+.2f}%)",
        f"{open_emoji} *P&L abierto*: ${open_pl:+,.2f}",
        f"*Deployment*: {total_mv/eq*100:.0f}%  (cash ${cash:,.0f})",
        "",
        "*Posiciones:*",
    ]
    worst_pct = 0.0
    for p in real:
        mv = float(p.market_value); plpc = float(p.unrealized_plpc) * 100
        emoji = "🟢" if plpc > 0.3 else ("🔴" if plpc < -0.3 else "⚪")
        lines.append(f"{emoji} {p.symbol}: ${mv:,.0f} ({mv/eq*100:.0f}%) — {plpc:+.1f}%")
        if plpc < worst_pct:
            worst_pct = plpc
    if not real:
        lines.append("_(sin posiciones reales)_")

    # Capas defensivas — margen al backstop
    margin_backstop = abs(-8.0 - worst_pct)
    lines.append("")
    lines.append(f"*Riesgo:* peor pos {worst_pct:+.1f}%  ·  margen al backstop iter33: {margin_backstop:.1f}pp")

    # Gate iter31
    gate = _fetch_repo_json("signals/entry_gate.json")
    if gate and gate.get("last_entry_date"):
        from datetime import date as _date
        try:
            ge = _date.fromisoformat(gate["last_entry_date"])
            days = (_date.today() - ge).days
            remaining = max(0, 21 - days)
            lines.append(f"*Gate iter31:* cerrada · faltan {remaining}d para próxima rotación")
        except Exception:
            pass

    # Último daily
    ws = _fetch_repo_json("signals/workflow_status.json") or {}
    daily_ws = ws.get("alpha_daily", {})
    if daily_ws.get("ts"):
        lines.append(f"*Último daily:* {daily_ws['ts']}  ok={daily_ws.get('ok')}")
    return "\n".join(lines)


def _help() -> str:
    return (
        "*ALPHA BOT — cloud edition*\n"
        "*Lectura:*\n"
        "resumen / hoy — *RESUMEN COMPLETO del día* (iter44)\n"
        "estado — equity + posiciones + último daily\n"
        "cartera — posiciones con P&L abierto\n"
        "equity — capital, retorno, despliegue\n"
        "health — snapshot completo del sistema\n"
        "llm — costos LLM hoy\n"
        "universo — CP universe efectivo + rotaciones\n"
        "oportunidades — radar del mercado amplio (research)\n"
        "\n*Disparar jobs (iter37):*\n"
        "run / correr — fuerza el analyst+trader (~2-5 min)\n"
        "monitor — corre el monitor (stops/TPs)\n"
        "weekly — corre la discovery semanal\n"
        "\n*Control (iter38):*\n"
        "pause — frena trading (escribe signals/paused.flag)\n"
        "resume — reanuda trading (borra el flag)\n"
        "\nayuda — este menú\n\n"
        "_Comandos PC-específicos (shutdown/wake) siguen en el bot local._"
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
    # iter37: triggers de Cloud Run Jobs
    if t in ("run", "correr", "force daily", "daily", "analyst", "analizar"):
        ok, msg = _trigger_cloud_run_job("alpha-daily")
        return msg + ("\n_Recibirás el brief cuando termine (~2-5 min)._" if ok else "")
    if t in ("monitor", "force monitor", "vigilar"):
        ok, msg = _trigger_cloud_run_job("alpha-monitor")
        return msg
    if t in ("weekly", "discovery", "force weekly"):
        ok, msg = _trigger_cloud_run_job("alpha-weekly")
        return msg
    # iter38: pause / resume (escribe/borra signals/paused.flag via GitHub API)
    if t in ("pause", "pausar", "stop", "frenar"):
        from datetime import datetime as _dt
        ts = _dt.utcnow().isoformat(timespec="seconds")
        ok, msg = _github_put_file(
            "signals/paused.flag",
            f"paused via bot at {ts}Z",
            f"chore: pause trading via bot ({ts})",
        )
        if ok:
            return ("⏸ *PAUSED* — el próximo daily/monitor abortará trading.\n"
                    "El kill_switch_check lee signals/paused.flag.\n"
                    "Para reanudar: envía *resume*.")
        return f"❌ pause falló: {msg}"
    if t in ("resume", "reanudar", "play", "continuar"):
        ok, msg = _github_delete_file(
            "signals/paused.flag",
            "chore: resume trading via bot",
        )
        if ok:
            return "▶️ *RESUMED* — el próximo daily/monitor opera normalmente."
        return f"❌ resume falló: {msg}"
    # iter44: resumen completo del día
    if t in ("resumen", "hoy", "dia", "día", "summary"):
        return _cmd_resumen()
    # iter50: radar de oportunidades (read-only)
    if t in ("oportunidades", "radar", "opps", "oportunidad"):
        return _cmd_oportunidades()
    if t in ("ayuda", "help", "?", "start", "comandos"):
        return _help()
    return f"No entendí '{t}'. Envía *ayuda* para ver comandos."


@app.route("/", methods=["GET"])
def root():
    return ("alpha-bot Cloud Run service — POST a /webhook/telegram\n", 200)


@app.route("/health", methods=["GET"])
def health_probe():
    return ("ok\n", 200)


@app.route("/webhook/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Twilio WhatsApp webhook. Recibe form-encoded, responde con TwiML."""
    body = request.form.get("Body", "")
    from_num = request.form.get("From", "")  # ej: "whatsapp:+5491134567890"

    # Whitelist por número (si ALLOWED_WA está configurado)
    if ALLOWED_WA and ALLOWED_WA not in from_num:
        log.warning("WhatsApp inbound de número no autorizado: %s text=%r", from_num, body[:80])
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            mimetype="application/xml",
        )

    log.info("WhatsApp cmd from %s: %r", from_num, body[:100])
    resp_text = _dispatch(body)
    # TwiML reply (Twilio espera XML; escapamos < > & en el texto)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Message>{_xml_escape(resp_text)}</Message></Response>'
    )
    return Response(twiml, mimetype="application/xml")


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
