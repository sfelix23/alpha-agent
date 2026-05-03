#!/bin/bash
# =============================================================================
# setup_vps.sh — Instala el agente financiero en Ubuntu 22.04/24.04 (ARM o x86)
# Testeado en: Hetzner CAX11 (ARM €3.29/mes), Oracle Cloud Free (ARM)
#
# Uso:
#   curl -O https://raw.githubusercontent.com/sfelix23/alpha-agent/master/vps/setup_vps.sh
#   chmod +x setup_vps.sh && sudo ./setup_vps.sh
# =============================================================================
set -e

REPO_URL="https://github.com/sfelix23/alpha-agent.git"
INSTALL_DIR="/opt/agente"
SERVICE_USER="agente"
PYTHON_VERSION="3.11"

echo "======================================================"
echo " Alpha Agent VPS Setup"
echo " $(date)"
echo "======================================================"

# ── 1. Sistema base ───────────────────────────────────────
echo "[1/7] Actualizando sistema..."
apt-get update -qq && apt-get upgrade -y -qq

echo "[2/7] Instalando dependencias del sistema..."
apt-get install -y -qq \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python3-pip \
    git curl wget tzdata build-essential \
    libssl-dev libffi-dev python${PYTHON_VERSION}-dev \
    sqlite3

# Timezone Buenos Aires
timedatectl set-timezone America/Argentina/Buenos_Aires
echo "Timezone: $(timedatectl show --property=Timezone --value)"

# ── 2. Usuario sin privilegios ────────────────────────────
echo "[3/7] Creando usuario '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$SERVICE_USER"
fi

# ── 3. Clonar repositorio ─────────────────────────────────
echo "[4/7] Clonando repositorio..."
if [ -d "$INSTALL_DIR" ]; then
    echo "  Directorio existente — haciendo git pull..."
    cd "$INSTALL_DIR" && sudo -u "$SERVICE_USER" git pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi

# ── 4. Entorno Python ────────────────────────────────────
echo "[5/7] Creando virtualenv e instalando paquetes..."
sudo -u "$SERVICE_USER" bash -c "
    cd $INSTALL_DIR
    python${PYTHON_VERSION} -m venv .venv
    source .venv/bin/activate
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    echo 'Paquetes instalados OK'
"

# ── 5. Estructura de directorios ─────────────────────────
echo "[6/7] Creando directorios de trabajo..."
sudo -u "$SERVICE_USER" bash -c "
    mkdir -p $INSTALL_DIR/{logs,signals,alpha_agent/data/cache}
"

# ── 6. Cron ──────────────────────────────────────────────
echo "[7/7] Instalando crontab..."
# ART = UTC-3. Horarios en UTC:
#   10:00 ART = 13:00 UTC
#   10:35 ART = 13:35 UTC
#   11:05 ART = 14:05 UTC
#   14:00 ART = 17:00 UTC
#   15:30 ART = 18:30 UTC (rebalancer viernes)

CRON_FILE="/tmp/agente_cron"
cat > "$CRON_FILE" << 'CRON'
SHELL=/bin/bash
PATH=/opt/agente/.venv/bin:/usr/local/bin:/usr/bin:/bin
PYTHONPATH=/opt/agente
PYTHONUNBUFFERED=1

# ── Lunes a viernes ───────────────────────────────────────
# 10:00 ART (13:00 UTC) — Wake / health pre-check
0 13 * * 1-5  cd /opt/agente && python run_health_check.py >> logs/health_$(date +\%Y-\%m-\%d).log 2>&1

# 10:35 ART (13:35 UTC) — Analyst principal
35 13 * * 1-5  cd /opt/agente && python run_analyst.py --send >> logs/analyst_$(date +\%Y-\%m-\%d).log 2>&1

# 10:50 ART (13:50 UTC) — Trader (ejecuta señales del analyst)
50 13 * * 1-5  cd /opt/agente && python run_trader.py --live >> logs/trader_$(date +\%Y-\%m-\%d).log 2>&1

# Monitor cada 30min (11:05 → 17:35 ART = 14:05 → 20:35 UTC)
5,35 14-20 * * 1-5  cd /opt/agente && python run_monitor.py --live >> logs/monitor_$(date +\%Y-\%m-\%d).log 2>&1

# 14:00 ART lun-jue (17:00 UTC) — Midday scan
0 17 * * 1-4  cd /opt/agente && python run_midday.py --live >> logs/midday_$(date +\%Y-\%m-\%d).log 2>&1

# Dashboard (cada hora en horario de mercado)
0 14-21 * * 1-5  cd /opt/agente && python run_dashboard.py --no-open >> logs/dashboard_$(date +\%Y-\%m-\%d).log 2>&1

# 15:30 ART viernes (18:30 UTC) — Rebalancer semanal
30 18 * * 5  cd /opt/agente && python run_rebalancer.py --live >> logs/rebalancer_$(date +\%Y-\%m-\%d).log 2>&1

# Health check 12:30 ART (15:30 UTC)
30 15 * * 1-5  cd /opt/agente && python run_health_check.py >> logs/health_$(date +\%Y-\%m-\%d).log 2>&1

# Limpiar logs viejos (>30 días) — todos los domingos
0 3 * * 0  find /opt/agente/logs -name "*.log" -mtime +30 -delete
CRON

crontab -u "$SERVICE_USER" "$CRON_FILE"
rm "$CRON_FILE"

echo ""
echo "======================================================"
echo " Setup completado!"
echo "======================================================"
echo ""
echo " PRÓXIMO PASO OBLIGATORIO:"
echo " Copiar tu archivo .env al servidor:"
echo ""
echo "   scp .env ubuntu@TU_IP_VPS:/opt/agente/.env"
echo "   sudo chown agente:agente /opt/agente/.env"
echo "   sudo chmod 600 /opt/agente/.env"
echo ""
echo " Verificar que el cron funciona:"
echo "   sudo -u agente crontab -l"
echo ""
echo " Test manual del analyst:"
echo "   sudo -u agente bash -c 'cd /opt/agente && source .venv/bin/activate && python run_analyst.py --no-ai'"
echo ""
echo " Ver logs en tiempo real:"
echo "   tail -f /opt/agente/logs/analyst_\$(date +%Y-%m-%d).log"
echo "======================================================"
