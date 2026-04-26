#!/bin/bash
# Setup one-time para Oracle Cloud Ampere A1 — Ubuntu 22.04
# Ejecutar UNA VEZ después del primer SSH a la VM:
#   bash setup_oracle.sh
set -e

echo "=== Alpha Agent — Oracle Cloud Setup ==="

# 1. Sistema
sudo apt-get update -qq
sudo apt-get install -y python3.11 python3-pip python3.11-venv git cron

# 2. Clonar repo
cd ~
if [ ! -d "alpha-agent" ]; then
    git clone https://github.com/sfelix23/alpha-agent.git
fi
cd alpha-agent

# 3. Virtualenv y dependencias
python3.11 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# 4. Crear directorio de logs
mkdir -p logs

# 5. Verificar importaciones clave
python -c "from alpha_agent.config import PARAMS; print('Config OK')"
python -c "from trader_agent.brokers.alpaca_broker import AlpacaBroker; print('Broker OK')"

echo ""
echo "=== Setup completo. Próximos pasos: ==="
echo ""
echo "1. Cargar credenciales:"
echo "   nano ~/alpha-agent/.env"
echo "   (copiar el contenido del .env local)"
echo ""
echo "2. Configurar git push con tu GitHub PAT:"
echo "   cd ~/alpha-agent"
echo "   git remote set-url origin https://<TU_GITHUB_PAT>@github.com/sfelix23/alpha-agent.git"
echo "   git config user.name 'Alpha Bot Oracle'"
echo "   git config user.email 'alpha-bot@noreply.github.com'"
echo ""
echo "3. Instalar cron jobs:"
echo "   crontab ~/alpha-agent/oracle_crontab.txt"
echo "   crontab -l  # verificar"
echo ""
echo "4. Test manual:"
echo "   cd ~/alpha-agent && source venv/bin/activate"
echo "   python run_analyst.py --no-send --force"
