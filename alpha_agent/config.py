"""
Configuración central del alpha_agent.

Toda constante, parámetro o universo de activos se define acá.
Si tenés que cambiar la lista de activos, los pesos LP/CP, o la tasa libre
de riesgo, lo hacés solo en este archivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ────────────────────────────────────────────────────────────────────────────
# UNIVERSO DE ACTIVOS (iter17: recortado — sin ADRs argentinos ilíquidos)
# ────────────────────────────────────────────────────────────────────────────
ACTIVOS: dict[str, str] = {
    # Energía y petróleo
    "Exxon": "XOM", "Chevron": "CVX", "Petrobras": "PBR", "Schlumberger": "SLB",
    "Shell": "SHEL", "TotalEnergies": "TTE", "Vista": "VIST", "YPF": "YPF",
    # Defensa y geopolítica
    "Lockheed": "LMT", "Raytheon": "RTX", "Northrop": "NOC", "General_Dynamics": "GD",
    "Boeing": "BA", "Palantir": "PLTR", "Anduril_Proxy": "AVAV",
    # Argentina (NYSE ADRs líquidos) — alto beta, asimétrico con recuperación macro AR.
    # iter17: sacados los ilíquidos (TGS, EDN, DESP, IRS, LOMA, TGNO4.BA, PAM) — spread
    # ancho/volumen bajo come el edge en cuenta chica. Quedan los líquidos con edge real.
    "Galicia": "GGAL", "Macro": "BMA", "MercadoLibre": "MELI",
    # Growth / momentum — alto potencial de retorno asimétrico
    "Broadcom": "AVGO", "Netflix": "NFLX", "CrowdStrike": "CRWD", "Coinbase": "COIN",
    # Minería / Litio / Cobre / Oro / Uranio
    "Lithium_Americas": "LAC", "SQM": "SQM",
    "Rio_Tinto": "RIO", "Vale": "VALE", "Barrick_Gold": "GOLD", "Newmont": "NEM",
    "Freeport_Cobre": "FCX", "Cameco_Uranium": "CCJ",
    # Tecnología e IA
    "Nvidia": "NVDA", "AMD": "AMD", "Microsoft": "MSFT", "Google": "GOOGL",
    "Apple": "AAPL", "Meta": "META", "Tesla": "TSLA", "TSM_Taiwan": "TSM", "ASML": "ASML",
    "Amazon": "AMZN",
    # Semis adicionales — demanda HBM/AI
    "Micron": "MU", "ARM_Holdings": "ARM",
    # Healthcare
    "Eli_Lilly": "LLY", "AbbVie": "ABBV", "Merck": "MRK",
    # Financials
    "JPMorgan": "JPM", "Goldman_Sachs": "GS", "Visa": "V",
    # Industrials / Infraestructura
    "Caterpillar": "CAT", "GE_Aerospace": "GE",
    # Consumer defensivo
    "Costco": "COST",
    # Energía adicional
    "Occidental": "OXY",
    # iter28: DIVERSIFICACIÓN cross-sector — el backtest mostró que el momentum
    # diverso (Sharpe 1.20) le gana al tech-concentrado (0.68, DD -41%). Sumamos
    # defensivos/value de baja correlación con tech para que el scorer elija amplio.
    "UnitedHealth": "UNH", "Pfizer": "PFE",            # Healthcare defensivo
    "BankOfAmerica": "BAC", "Mastercard": "MA",        # Financials
    "Procter_Gamble": "PG", "CocaCola": "KO",          # Consumer staples
    "HomeDepot": "HD", "Walmart": "WMT",               # Consumer
    "NextEra": "NEE", "Deere": "DE", "Honeywell": "HON",  # Utility / Industrials
    # AI infraestructura / Cloud
    "Datadog": "DDOG", "Cloudflare": "NET", "Uber": "UBER",
    # Crypto proxy
    "MicroStrategy": "MSTR",
    # Benchmarks y refugio
    "Bitcoin_ETF": "IBIT", "Ethereum_ETF": "ETHE", "Nasdaq_100": "QQQ",
    "S&P500": "SPY", "Gold_ETF": "GLD", "TLT_Bonds": "TLT",
}

# Benchmark de mercado para CAPM
BENCHMARK_TICKER: str = "SPY"

# Lista de tickers que NO entran en optimización Markowitz
# (son benchmarks o ETFs que solo usamos como referencia)
EXCLUIR_DE_OPTIMIZACION: set[str] = {"SPY", "QQQ", "GLD", "IBIT", "ETHE", "TLT"}

# Lista plana de tickers para scans CP/Midday — excluye ETFs benchmark y locales BA
UNIVERSE: list[str] = sorted(
    v for v in ACTIVOS.values()
    if v not in {"SPY", "QQQ", "GLD", "IBIT", "ETHE", "TLT", "TGNO4.BA"}
)

# Mega-caps de alta eficiencia de mercado — excluir del sleeve CP.
# En estos nombres el mercado es perfectamente eficiente (decenas de quant funds
# corriendo CAPM, ML, HFT). Comprar momentum CP en NVDA o AMD después de +20%
# semanal es lo opuesto de tener ventaja: es comprar lo que ya compraron todos.
QQQ_MEGA_CAPS: frozenset[str] = frozenset({
    "NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA",
    "AVGO", "COST", "AMD", "ASML", "NFLX", "V", "JPM", "GS",
})

# Tech stocks que reciben bonus CP en régimen BULL — momentum genuino líder del mercado
TECH_BULL_CP_BOOST: frozenset[str] = frozenset({
    "NVDA", "AMD", "MSFT", "META", "GOOGL", "AMZN", "AAPL", "ASML",
})

# Sub-sectores del CP_UNIVERSE para el guard de diversificación interna.
# Impide que ambos slots CP vayan al mismo clúster (ej: NVDA + AMD = doble-semis).
CP_SUB_SECTORS: dict[str, str] = {
    "NVDA": "Semis", "AMD": "Semis", "ARM": "Semis",
    "MU": "Semis", "ASML": "Semis", "TSM": "Semis",
    "META": "MegaTech", "TSLA": "MegaTech",
    "AMZN": "MegaTech", "GOOGL": "MegaTech",
    "CRWD": "AIInfra", "PLTR": "AIInfra",
    "NET": "AIInfra", "DDOG": "AIInfra",
    "COIN": "Crypto", "MSTR": "Crypto",
    "NFLX": "Growth", "AVGO": "Growth",
    "GGAL": "Argentina", "BMA": "Argentina",
    "MELI": "Argentina", "VIST": "Argentina",
    "LMT": "Defense", "GD": "Defense",
    "OXY": "Energy",
}

# Universo focalizado para sleeve CP — 25 tickers de alta beta/momentum.
# Solo estos entran al scoring CP; el resto del universo aplica solo a LP/options.
# Lógica: z-scores relativos entre 25 nombres concentrados > z-scores entre 49 heterogéneos.
# iter28: universo CP DIVERSO cross-sector (~36). El backtest mostró que el
# momentum diverso (Sharpe 1.20, DD -21%) le gana al tech-concentrado (0.68, DD
# -41%). Tech baja de 72% a ~36% del universo; el scorer de momentum elige entre
# todos los sectores (menos correlación, mejor Sharpe). Reversible vía git.
CP_UNIVERSE: frozenset[str] = frozenset({
    # Tech AI / Semis (~36% del universo, no 72%)
    "NVDA", "AMD", "ARM", "MU", "AVGO",
    "META", "AMZN", "GOOGL", "MSFT",
    "PLTR", "CRWD", "DDOG",
    # Healthcare defensivo
    "LLY", "ABBV", "MRK", "UNH", "PFE",
    # Financials
    "JPM", "GS", "V", "MA", "BAC",
    # Consumer staples / retail
    "COST", "PG", "KO", "HD", "WMT",
    # Industrials / Utility
    "CAT", "GE", "DE", "HON", "NEE",
    # Energía
    "XOM", "CVX", "OXY", "VIST",
    # Argentina (alpha genuino: baja cobertura institucional)
    "GGAL", "BMA", "MELI",
    # Defensa
    "LMT", "GD",
})

# Núcleo protegido: líderes líquidos que la rotación automática NUNCA saca solo
# (iter17). Se pueden sacar manualmente, pero el rotador no los toca.
PROTECTED_CP: frozenset[str] = frozenset({
    "NVDA", "AMD", "MSFT", "GOOGL", "META", "AMZN",
})


def get_effective_cp_universe() -> frozenset[str]:
    """CP_UNIVERSE efectivo = (estático ∪ added) − removed − vetoed (iter17).

    Lee signals/cp_universe_overrides.json (escrito por la rotación automática y
    los comandos veto del bot). Lazy: NO corre I/O en import. Si el JSON falta o
    está corrupto, devuelve el CP_UNIVERSE estático (fail-safe).
    """
    try:
        import json as _json
        from pathlib import Path as _Path
        p = _Path(__file__).resolve().parents[1] / "signals" / "cp_universe_overrides.json"
        if not p.exists():
            return CP_UNIVERSE
        data = _json.loads(p.read_text(encoding="utf-8"))
        added = set(data.get("added", []))
        removed = set(data.get("removed", []))
        vetoed = set(data.get("vetoed", []))
        eff = (set(CP_UNIVERSE) | added) - removed - vetoed
        return frozenset(eff) if eff else CP_UNIVERSE
    except Exception:
        return CP_UNIVERSE

# Tickers donde el sistema tiene ventaja informacional estructural sobre el mercado:
# Argentina (cobertura institucional muy baja), energía internacional, minerales,
# defensa. En estos sectores el CAPM + macro + noticias locales agrega alpha real.
ALPHA_PREMIUM_LP: frozenset[str] = frozenset({
    # Argentina ADRs líquidos — mercado ineficiente, pocos fondos los cubren
    # (iter17: removidos TGS/IRS/LOMA/EDN/TGNO4.BA/PAM por iliquidez)
    "GGAL", "BMA", "VIST", "YPF",
    # Energía internacional no-SPY
    "PBR", "SHEL", "TTE", "SLB", "XOM", "CVX", "OXY",
    # Minería / metales / uranio
    "RIO", "VALE", "NEM", "GOLD", "FCX", "SQM", "LAC", "CCJ",
    # Defensa — poco cubierta por quant retail, macro signals importan
    "LMT", "RTX", "NOC", "GD", "AVAV", "GE",
    # Healthcare pipeline — alpha en aprobaciones FDA / juicios patentes
    "ABBV", "MRK",
    # Financials ciclo earnings — menos cubiertos por quant retail que mega-caps
    "GS",
})

# Mapeo ticker → sector, para el guard de concentración sectorial.
# Si un ticker no está acá, se etiqueta como "Other".
SECTOR_MAP: dict[str, str] = {
    # Energía / Petróleo
    "XOM": "Energy", "CVX": "Energy", "PBR": "Energy", "SLB": "Energy",
    "SHEL": "Energy", "TTE": "Energy", "VIST": "Energy", "YPF": "Energy",
    "PAM": "Energy", "TGS": "Energy", "EDN": "Energy",
    # Defensa
    "LMT": "Defense", "RTX": "Defense", "NOC": "Defense", "GD": "Defense",
    "BA": "Defense", "PLTR": "Defense", "AVAV": "Defense",
    # Minería / materiales / uranio
    "ALTM": "Materials", "LAC": "Materials", "SQM": "Materials", "RIO": "Materials",
    "VALE": "Materials", "GOLD": "Materials", "NEM": "Materials", "FCX": "Materials",
    "ALUA.BA": "Materials", "CCJ": "Materials",
    # Tech / Semis / mega-cap
    "NVDA": "Tech", "AMD": "Tech", "MSFT": "Tech", "GOOGL": "Tech",
    "AAPL": "Tech", "META": "Tech", "TSLA": "Tech", "TSM": "Tech", "ASML": "Tech",
    "AMZN": "Tech", "MU": "Tech", "ARM": "Tech",
    # AI infra / Cloud / SaaS
    "DDOG": "Tech", "NET": "Tech", "UBER": "Tech",
    # Healthcare
    "LLY": "Healthcare", "ABBV": "Healthcare", "MRK": "Healthcare",
    # Financials US + Argentina
    "JPM": "Financials", "GS": "Financials", "V": "Financials",
    "GGAL": "Financials", "BMA": "Financials",
    # Consumer
    "MELI": "Consumer", "DESP": "Consumer", "COST": "Consumer",
    "NFLX": "Consumer",
    # Industrials
    "CAT": "Industrials", "GE": "Industrials",
    # Energy adicional
    "OXY": "Energy",
    # Real Estate / Other
    "IRS": "RealEstate", "LOMA": "Materials", "TGNO4.BA": "Energy",
    # Growth US
    "AVGO": "Tech", "CRWD": "Tech", "COIN": "Crypto",
    # Crypto proxy
    "MSTR": "Crypto",
    # ETFs / refugio
    "SPY": "ETF", "QQQ": "ETF", "GLD": "ETF", "IBIT": "Crypto", "ETHE": "Crypto",
    "TLT": "ETF",
}

# Máximo % del libro LP por sector (guard de diversificación)
MAX_SECTOR_WEIGHT_LP: float = 0.40      # ningún sector > 40% del sleeve LP
# Correlación máxima permitida entre dos activos LP (rolling 1y)
MAX_PAIR_CORRELATION: float = 0.80


# ────────────────────────────────────────────────────────────────────────────
# PARÁMETROS FINANCIEROS
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FinancialParams:
    """Parámetros macro y de modelado.

    Capital real objetivo del usuario: USD 1600. Usamos ese mismo número para
    paper trading de modo que los fills, slippage y comisiones reflejen la
    realidad futura cuando se pase a real money.
    """

    # Capital de referencia (paper trading == capital real planeado)
    paper_capital_usd: float = 1600.0

    risk_free_rate: float = 0.045         # tasa libre de riesgo anual (USD ~ 10y T-Note)
    trading_days: int = 252
    history_period: str = "2y"            # rango para descargar data histórica (2y: 252 warm-up + 252 backtest)
    history_interval: str = "1d"
    min_obs: int = 60                     # mínimo de observaciones para incluir un activo

    # ── PERFIL DE RIESGO (iter14): AGRESIVO — edge-driven ──────────────────────
    # Decisión Santino (2026-05-20): es paper, busca multiplicar rápido para luego
    # pasar a real. No conservador: tomar más riesgo SIEMPRE que el retorno esperado
    # lo justifique. Fundamento financiero — no "arriesgar a lo loco":
    #   · Kelly fraccional (0.65, no full) → ~91% del crecimiento log de full-Kelly
    #     con ~65% de la varianza. Full-Kelly arriesga ruina si μ está sobreestimado.
    #   · Mean-variance con menor aversión (kelly_alpha 0.50) → pondera más el edge
    #     real (Kelly) vs minimizar varianza (Markowitz).
    #   · Anti-martingala: piramidar en racha (equity_curve HOT >1.0), achicar en
    #     drawdown — pero con piso anti-ruina (kill -13%, no -8%): no se puede
    #     componer desde cero (media geométrica).
    # Para volver a conservador (real money): risk_appetite="MODERADO" y revertir.
    risk_appetite: str = "AGGRESSIVE"     # AGGRESSIVE | MODERADO | CONSERVADOR (doc/tuning)

    # ── GROWTH MODE para $1,600 ────────────────────────────────────────────────
    # Más CP (momentum, 1-5 días) que LP (hold semanas) para capitalizar tendencias.
    weight_long_term: float = 0.0    # LP siempre desactivado — CP momentum es superior para $1600
    weight_short_term: float = 0.90  # CP 90% (iter14: +cash deployment; allocation_agent lo modula)
    weight_options: float = 0.14          # 14% (iter14: +convexidad/leverage con riesgo definido)

    # Filtros de selección — iter14: dejar entrar nombres de mayor beta/vol con
    # retorno esperado alto (la volatilidad ES el combustible del retorno si hay edge).
    max_beta_lp: float = 2.8             # beta alto = más upside en bull (iter14: 2.0→2.8)
    min_sharpe_lp: float = 0.10          # iter14: 0.30→0.10 — admite alta-vol con alto μ

    # Parámetros técnicos para CP
    rsi_oversold: float = 35.0
    rsi_overbought: float = 80.0          # iter14: 75→80 — no castigar momentum fuerte tan temprano
    atr_stop_multiple: float = 2.5        # iter14: 2.0→2.5 — stops más anchos, dejar respirar la vol

    # Growth: 2 LP + n CP (el allocation_agent fija n_cp dinámico: 2 concentrado / 3 difuso)
    top_n_long_term: int = 2
    top_n_short_term: int = 2             # fallback; decide_allocation manda (2-3 según VIX)

    # Bucket options — iter14: más leverage cuando hay convicción (riesgo = prima, definido)
    top_n_bearish: int = 1                # máx 1 put direccional
    max_contracts_per_trade: int = 3      # iter14: 1→3 contratos (convexidad en alta convicción)

    # Concentración máxima (cap del blend Kelly/Markowitz, sobre todo LP).
    # iter14 agresivo: 0.40→0.55 — el edge se concentra en la mejor idea. El CP real
    # se concentra vía n_cp + conviction ×1.8 + floor 20% (signals.py), no por este cap.
    max_weight_per_asset: float = 0.55    # 55% max en un solo nombre

    # Kill switch — iter14: bandas más anchas. Con posiciones de mayor vol, -6% es
    # ruido; cortar ahí te saca por whipsaw. Piso anti-ruina a -12% intradía.
    max_daily_drawdown: float = 0.12      # 12% (iter14: 6%→12%) — vol productiva, no ruina

    # Límites derivados — calibrados para capital ~$1600 USD
    max_options_allocation: float = 0.25  # iter14: 0.20→0.25 techo del bucket options
    max_single_option_premium: float = 250.0  # iter14: 150→250 — bets de opciones más grandes
    max_hedge_allocation: float = 0.10    # hasta 10% del libro en puts SPY cuando bear
    enable_short_equity: bool = False     # OFF por default — preferimos puts (riesgo limitado)
    enable_options: bool = True           # ON — long calls/puts OTM, riesgo limitado a la prima
    # iter15: DT y scalping DESACTIVADOS por decisión de Santino (datos: DT nunca operó,
    # SCALP 6 abiertas / 0 cerradas / $0 realizado = sin edge medible). Foco y capital en
    # CP momentum (único motor con edge). Reversible: poner True para reactivar.
    enable_daytrading: bool = False       # OFF (iter15) — run_daytrader hace early-exit
    enable_scalping: bool = False         # OFF (iter15) — run_scalper hace early-exit

    # ── Discovery + rotación de universo (iter17) ──────────────────────────────
    # Gate de liquidez: discovery solo surface candidatos tradeables.
    discovery_min_adv_usd: float = 20_000_000   # avg dollar volume 20d mínimo
    discovery_min_price: float = 5.0            # precio mínimo (evita penny stocks)
    # Rotación automática del CP_UNIVERSE (corre semanal en run_rebalancer):
    rotation_enabled: bool = True               # auto-incorporar candidatos fuertes
    rotation_margin: float = 0.20               # candidato debe superar al más flojo por 20%
    rotation_max_per_week: int = 1              # máx 1 swap/semana (anti-churn)
    min_days_to_expiry: int = 30          # evita theta decay brutal y PDT en cuentas <$25k
    max_days_to_expiry: int = 45          # evita gamma muerta (bajé de 60 a 45)
    target_delta_directional: float = 0.35  # puts/calls direccionales delta 0.35 (más OTM = más barato)
    target_delta_hedge: float = 0.22      # puts de hedge SPY bien OTM (prima baja, es un seguro)
    otm_fallback_steps: int = 3           # cuántos pasos 0.60x de delta probar si la prima excede cap


PARAMS = FinancialParams()


# ────────────────────────────────────────────────────────────────────────────
# RUTAS DE PROYECTO
# ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Paths:
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])

    @property
    def cache_dir(self) -> Path:
        p = self.root / ".cache"
        p.mkdir(exist_ok=True)
        return p

    @property
    def signals_dir(self) -> Path:
        p = self.root / "signals"
        p.mkdir(exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self.root / "logs"
        p.mkdir(exist_ok=True)
        return p


PATHS = Paths()


# ────────────────────────────────────────────────────────────────────────────
# LOGGING CENTRALIZADO
# ────────────────────────────────────────────────────────────────────────────
def setup_agent_logging(agent_name: str, *, level: int = 20) -> None:
    """Configura logging consistente para todos los run_*.py.

    Reemplaza los `setup_logging()` duplicados que cada run_*.py tenía con
    código copy-paste. Beneficios sobre la implementación previa:

    - **RotatingFileHandler**: el archivo se rota al alcanzar 20MB (hasta 7
      backups). Antes era un FileHandler simple → crecía sin límite, con
      potencial agotamiento de FDs en jobs largos.
    - **atexit cleanup**: cierra handlers limpio al salir (evita locks de
      archivo en Windows si el proceso termina abrupto).
    - **Formato consistente** con timestamp ISO y nombre del agente, así los
      logs combinados en `logs/` son fáciles de parsear.
    - **UTF-8 explícito** para no romper en Windows cp1252.

    Args:
        agent_name: prefijo del archivo de log (ej. "analyst" → analyst_<date>.log).
        level: nivel mínimo a loguear (default INFO=20).
    """
    import atexit
    import logging
    import sys
    from datetime import datetime
    from logging.handlers import RotatingFileHandler

    logs_dir = PATHS.logs_dir
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = logs_dir / f"{agent_name}_{today}.log"

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = RotatingFileHandler(
        str(log_file),
        maxBytes=20 * 1024 * 1024,   # 20 MB
        backupCount=7,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # Limpiar handlers previos para que múltiples llamadas no dupliquen output.
    root.handlers = [fh, sh]

    atexit.register(logging.shutdown)


# ────────────────────────────────────────────────────────────────────────────
# NOTICIAS Y MACRO
# ────────────────────────────────────────────────────────────────────────────
# Tickers de Yahoo para indicadores macro — se consultan como si fueran activos
MACRO_TICKERS: dict[str, str] = {
    "oil_wti": "CL=F",         # Petróleo WTI
    "oil_brent": "BZ=F",       # Brent
    "gold": "GC=F",            # Oro
    "dxy": "DX-Y.NYB",         # Índice dólar
    "vix": "^VIX",             # Volatilidad esperada S&P500
    "us10y": "^TNX",           # Yield del 10y US Treasury
}

# Queries de Google News RSS para contexto global — se consultan una vez por run
MACRO_NEWS_QUERIES: list[str] = [
    "Trump economy tariffs",
    "Federal Reserve interest rates",
    "oil prices OPEC",
    "China economy",
    "Argentina Milei economy",
    "semiconductor export controls",
]

# Sentiment keyword-based (simple, sin ML). Se puede reemplazar por VADER o Gemini.
POSITIVE_KEYWORDS: set[str] = {
    "beat", "beats", "surge", "surges", "rally", "record", "upgrade", "upgraded",
    "buy", "bullish", "outperform", "growth", "strong", "jumps", "soars", "tops",
    "exceeds", "expansion", "approves", "approved", "wins", "profits", "profit",
}
NEGATIVE_KEYWORDS: set[str] = {
    "miss", "misses", "plunge", "plunges", "drop", "crash", "downgrade", "downgraded",
    "sell", "bearish", "underperform", "slump", "weak", "falls", "slides", "cuts",
    "warning", "layoffs", "bankruptcy", "probe", "lawsuit", "fraud", "delisted",
    "sanctions", "tariffs", "recession", "slowdown",
}


# ────────────────────────────────────────────────────────────────────────────
# IA / NOTIFICACIONES
# ────────────────────────────────────────────────────────────────────────────
# AI Ensemble multi-provider — gestionado por alpha_agent/llm/gateway.py
#
# Política Anthropic: default OFF para evitar abuse-flag por uso sin créditos.
# El sistema funciona con Groq/Gemini/DeepSeek/OpenRouter (todos free tier).
# Activar Anthropic sólo cuando haya créditos cargados.
GEMINI_MODEL: str = "models/gemini-2.0-flash"
CLAUDE_MODEL: str = "claude-haiku-4-5-20251001"        # fast + cheap — risk debate, monitor
CLAUDE_MODEL_DEEP: str = "claude-sonnet-4-6"           # deep analysis — Wall St thesis, SEC
WHATSAPP_FROM: str = "whatsapp:+14155238886"  # Twilio sandbox


# ────────────────────────────────────────────────────────────────────────────
# LLM GATEWAY — multi-provider con budget + cache + fallback
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class LLMConfig:
    """Configuración del gateway LLM multi-provider.

    Cascada por defecto: Groq → Gemini → DeepSeek → OpenRouter → heurística.
    Anthropic queda detrás de un flag manual (default OFF) y se enciende sólo
    cuando hay créditos cargados en la cuenta para evitar abuse-flag.

    NO frozen: el bot Telegram cambia `enable_anthropic`/`enable_sonnet` en
    runtime via comandos /anthropic_on /anthropic_off.
    """

    # ── Kill switches y budget ──────────────────────────────────────────────
    # Iter3: enable_anthropic se lee de env var ENABLE_ANTHROPIC en runtime via
    # property — permite encender/apagar SIN rebuild Docker. Default OFF.
    # Setear como env "true" / "1" / "yes" para activar.
    enable_sonnet: bool = False                  # Sonnet 4.6 detrás de flag separado (más caro)
    daily_anthropic_budget_usd: float = 0.10     # límite duro Anthropic/día
    daily_total_budget_usd: float = 0.50         # límite duro suma de todos los providers/día

    @property
    def enable_anthropic(self) -> bool:
        """Runtime flag desde env var ENABLE_ANTHROPIC (default False).

        Sin esto, cambiar el flag requería editar config.py + rebuild Docker
        + update jobs. Ahora con un `gcloud run jobs update --update-env-vars`
        se activa en segundos.
        """
        import os as _os
        v = _os.getenv("ENABLE_ANTHROPIC", "").strip().lower()
        return v in ("true", "1", "yes", "on")

    # ── Rate limits locales (requests/min por provider) ────────────────────
    rate_limit_anthropic_per_min: int = 5
    rate_limit_groq_per_min: int = 30
    rate_limit_gemini_per_min: int = 60
    rate_limit_deepseek_per_min: int = 20
    rate_limit_openrouter_per_min: int = 20

    # ── TTL del cache por purpose (horas) ──────────────────────────────────
    cache_ttl_sentiment_h: float = 24.0
    cache_ttl_event_score_h: float = 12.0
    cache_ttl_assess_position_h: float = 0.5
    cache_ttl_narrative_h: float = 4.0
    cache_ttl_wall_street_h: float = 12.0
    cache_ttl_risk_debate_h: float = 6.0

    # ── Modelos por provider ───────────────────────────────────────────────
    anthropic_fast_model: str = "claude-haiku-4-5-20251001"
    anthropic_deep_model: str = "claude-sonnet-4-6"
    groq_fast_model: str = "llama-3.3-70b-versatile"
    groq_reasoning_model: str = "deepseek-r1-distill-llama-70b"
    gemini_model: str = "gemini-2.0-flash"
    deepseek_chat_model: str = "deepseek-chat"
    deepseek_reasoning_model: str = "deepseek-reasoner"
    openrouter_fast_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    openrouter_reasoning_model: str = "qwen/qwen-2.5-72b-instruct:free"

    # ── Backoff ────────────────────────────────────────────────────────────
    retry_max_attempts: int = 2                  # tras la 1ª falla, hasta 2 retries
    retry_backoff_seconds: tuple[float, ...] = (5.0, 20.0)
    disable_provider_on_4xx_hours: float = 24.0  # 400/401/403 desactiva 24h sin retry

    # ── Costos estimados (USD por 1M tokens, ballpark) ─────────────────────
    # Sólo se usa para budget tracking. Inputs y outputs se promedian para simplificar.
    cost_per_mtok_anthropic_haiku: float = 1.0   # avg input+output haiku-4.5
    cost_per_mtok_anthropic_sonnet: float = 6.0  # avg input+output sonnet-4.6
    cost_per_mtok_groq: float = 0.0              # free tier
    cost_per_mtok_gemini: float = 0.0            # free tier
    cost_per_mtok_deepseek: float = 0.0          # free tier hasta cuota
    cost_per_mtok_openrouter: float = 0.0        # modelos :free


LLM = LLMConfig()


# Cascada preferida por purpose: el gateway intenta en este orden.
# Cada elemento es ("provider_id", "model_alias"). El gateway resuelve el alias
# contra LLMConfig (ej: "fast" → groq_fast_model).
LLM_CASCADE_BY_PURPOSE: dict[str, list[tuple[str, str]]] = {
    "sentiment": [
        ("groq", "fast"),
        ("gemini", "default"),
        # keywords es el fallback final, gestionado en sentiment.py
    ],
    "event_score": [
        ("groq", "fast"),
        ("gemini", "default"),
    ],
    "assess_position": [
        ("groq", "fast"),
        ("gemini", "default"),
        ("anthropic", "fast"),  # sólo si enable_anthropic=True
    ],
    "narrative": [
        ("groq", "fast"),
        ("gemini", "default"),
    ],
    "wall_street": [
        ("groq", "reasoning"),
        ("deepseek", "reasoning"),
        ("openrouter", "reasoning"),
        ("anthropic", "deep"),  # sólo si enable_anthropic=True y enable_sonnet=True
    ],
    "risk_debate": [
        ("groq", "reasoning"),
        ("gemini", "default"),
        ("anthropic", "fast"),  # sólo si enable_anthropic=True
    ],
}
