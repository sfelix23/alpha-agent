"""
diagnostico.py — Chequeo completo del Agente Financiero.

Corre esto en tu terminal de VS Code (D:\\Agente) para verificar
que todo está conectado y funcionando:

    python diagnostico.py

No hace trades ni manda WhatsApp — solo verifica.
"""

import sys
import io
import os

# Fix encoding Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

VERDE = "\033[92m"
ROJO  = "\033[91m"
RESET = "\033[0m"
BOLD  = "\033[1m"

def ok(msg):  print(f"  {VERDE}✅ {msg}{RESET}")
def err(msg): print(f"  {ROJO}❌ {msg}{RESET}")
def info(msg):print(f"     {msg}")
def header(msg): print(f"\n{BOLD}[{msg}]{RESET}")

print("=" * 60)
print(f"{BOLD}DIAGNÓSTICO AGENTE FINANCIERO — {__import__('datetime').date.today()}{RESET}")
print("=" * 60)

# ── 1. Variables de entorno ──────────────────────────────────────────
header("1. CREDENCIALES .env")
keys = {
    "ALPACA_API_KEY":     "Alpaca API Key",
    "ALPACA_SECRET_KEY":  "Alpaca Secret",
    "TWILIO_SID":         "Twilio Account SID",
    "TWILIO_TOKEN":       "Twilio Auth Token",
    "MY_PHONE_NUMBER":    "Tu número WhatsApp (+549...)",
    "GOOGLE_API_KEY":     "Google/Gemini API Key (opcional)",
}
missing = []
for k, desc in keys.items():
    v = os.getenv(k, "")
    if v:
        masked = v[:8] + "..." if len(v) > 8 else v
        ok(f"{desc}: {masked}")
    else:
        err(f"{desc}: FALTA ({k})")
        if k != "GOOGLE_API_KEY":
            missing.append(k)

# ── 2. Paquetes Python ───────────────────────────────────────────────
header("2. PAQUETES PYTHON")
paquetes = [
    ("alpaca",        "alpaca-py"),
    ("twilio",        "twilio"),
    ("yfinance",      "yfinance"),
    ("pandas",        "pandas"),
    ("numpy",         "numpy"),
    ("scipy",         "scipy"),
    ("feedparser",    "feedparser"),
    ("dotenv",        "python-dotenv"),
]
paquetes_faltantes = []
for mod, pkg in paquetes:
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "?")
        ok(f"{pkg} ({ver})")
    except ImportError:
        err(f"{pkg} — NO INSTALADO  →  pip install {pkg}")
        paquetes_faltantes.append(pkg)

# ── 3. Conexión Alpaca ───────────────────────────────────────────────
header("3. CONEXIÓN ALPACA (paper trading)")
try:
    from alpaca.trading.client import TradingClient
    api_key = os.getenv("ALPACA_API_KEY")
    secret  = os.getenv("ALPACA_SECRET_KEY")
    client  = TradingClient(api_key, secret, paper=True)
    acc = client.get_account()
    ok(f"Conectado — Account ID: {acc.id}")
    info(f"Equity:        ${float(acc.equity):>10,.2f} USD")
    info(f"Buying Power:  ${float(acc.buying_power):>10,.2f} USD")
    info(f"Status:        {acc.status}")
    clock = client.get_clock()
    mercado = "ABIERTO 🟢" if clock.is_open else "CERRADO 🔴"
    info(f"Mercado:       {mercado}")
    positions = client.get_all_positions()
    info(f"Posiciones:    {len(positions)}")
    for p in positions:
        pl = float(p.unrealized_pl)
        signo = "+" if pl >= 0 else ""
        info(f"  → {p.symbol:<10} qty={p.qty}  P&L={signo}${pl:.2f}")
    # Órdenes pendientes
    orders = client.get_orders()
    info(f"Órdenes pend.: {len(orders)}")
except Exception as e:
    err(f"ERROR Alpaca: {e}")

# ── 4. Conexión Twilio ───────────────────────────────────────────────
header("4. CONEXIÓN TWILIO (WhatsApp sandbox)")
try:
    from twilio.rest import Client
    sid   = os.getenv("TWILIO_SID")
    token = os.getenv("TWILIO_TOKEN")
    to    = os.getenv("MY_PHONE_NUMBER")
    c     = Client(sid, token)
    acc_twilio = c.api.accounts(sid).fetch()
    ok(f"Cuenta: {acc_twilio.friendly_name}")
    info(f"Status: {acc_twilio.status}")
    info(f"From:   whatsapp:+14155238886  (Twilio sandbox)")
    info(f"To:     whatsapp:{to}")
    # Último mensaje enviado
    try:
        msgs = c.messages.list(limit=1)
        if msgs:
            m = msgs[0]
            info(f"Último msg:  {m.date_sent.strftime('%Y-%m-%d %H:%M')} — status={m.status}")
            if m.status in ("failed", "undelivered"):
                err("SANDBOX EXPIRADO — mandá 'join <palabra>' al +14155238886 desde tu WhatsApp")
        else:
            info("Sin mensajes previos en la cuenta")
    except Exception:
        pass
except Exception as e:
    err(f"ERROR Twilio: {e}")

# ── 5. Señales recientes ─────────────────────────────────────────────
header("5. ÚLTIMAS SEÑALES (signals/latest.json)")
try:
    import json
    p = Path("signals/latest.json")
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        ts = d.get("generated_at", "?")
        regime = d.get("macro", {}).get("regime", "?")
        lt  = d.get("long_term", [])
        st  = d.get("short_term", [])
        opt = d.get("options_book", [])
        ok(f"Generado: {ts}")
        info(f"Régimen:  {regime.upper()}")
        info(f"LP ({len(lt)}): {[s['ticker'] for s in lt]}")
        info(f"CP ({len(st)}): {[s['ticker'] for s in st]}")
        info(f"Options ({len(opt)}): {[s.get('ticker','?') for s in opt]}")
    else:
        err("signals/latest.json no encontrado")
except Exception as e:
    err(f"Error leyendo signals: {e}")

# ── 6. Archivos clave ────────────────────────────────────────────────
header("6. ARCHIVOS CLAVE DEL PROYECTO")
archivos = [
    "run_analyst.py",
    "run_trader.py",
    "run_monitor.py",
    "run_autonomous.ps1",
    "alpha_agent/config.py",
    "alpha_agent/notifications/whatsapp.py",
    "trader_agent/brokers/alpaca_broker.py",
    ".env",
]
for f in archivos:
    if Path(f).exists():
        ok(f)
    else:
        err(f"{f} — NO ENCONTRADO")

# ── Resumen final ────────────────────────────────────────────────────
print()
print("=" * 60)
if missing or paquetes_faltantes:
    print(f"{ROJO}{BOLD}⚠️  HAY PROBLEMAS A RESOLVER:{RESET}")
    for k in missing:
        print(f"  - Falta variable en .env: {k}")
    for p in paquetes_faltantes:
        print(f"  - Instalar: pip install {p}")
else:
    print(f"{VERDE}{BOLD}✅  TODO OK — el sistema está listo para operar.{RESET}")
print("=" * 60)
print()
