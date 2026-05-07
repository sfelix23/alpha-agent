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
# UNIVERSO DE ACTIVOS (49)
# ────────────────────────────────────────────────────────────────────────────
ACTIVOS: dict[str, str] = {
    # Energía y petróleo
    "Exxon": "XOM", "Chevron": "CVX", "Petrobras": "PBR", "Schlumberger": "SLB",
    "Shell": "SHEL", "TotalEnergies": "TTE", "Vista": "VIST", "YPF": "YPF", "Pampa": "PAM",
    # Defensa y geopolítica
    "Lockheed": "LMT", "Raytheon": "RTX", "Northrop": "NOC", "General_Dynamics": "GD",
    "Boeing": "BA", "Palantir": "PLTR", "Anduril_Proxy": "AVAV",
    # Argentina (NYSE ADRs) — alto beta, asimétrico con recuperación macro AR
    "Galicia": "GGAL", "Macro": "BMA", "TGS": "TGS", "Edenor": "EDN",
    "MercadoLibre": "MELI", "Despegar": "DESP", "IRSA": "IRS",
    "Loma_Negra": "LOMA", "Transportadora_Gas_Norte": "TGNO4.BA",
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
CP_UNIVERSE: frozenset[str] = frozenset({
    # Tech AI / Semis — lideran cada rally (alta beta, FOMO institucional)
    "NVDA", "AMD", "ARM", "MU", "ASML", "TSM",
    # Mega-cap tech con momentum extendido
    "META", "TSLA", "AMZN", "GOOGL",
    # AI infra / Cloud / SaaS growth
    "CRWD", "PLTR", "NET", "DDOG",
    # Crypto proxy — máxima beta en risk-on
    "COIN", "MSTR",
    # Growth de alta convicción
    "NFLX", "AVGO",
    # Argentina (alpha genuino: baja cobertura institucional)
    "GGAL", "BMA", "MELI", "VIST",
    # Defensa (momentum ciclo geopolítico)
    "LMT", "GD",
    # Energía selectiva alta beta
    "OXY",
})

# Tickers donde el sistema tiene ventaja informacional estructural sobre el mercado:
# Argentina (cobertura institucional muy baja), energía internacional, minerales,
# defensa. En estos sectores el CAPM + macro + noticias locales agrega alpha real.
ALPHA_PREMIUM_LP: frozenset[str] = frozenset({
    # Argentina ADRs — mercado ineficiente, pocos fondos los cubren
    "GGAL", "BMA", "TGS", "VIST", "IRS", "LOMA", "EDN", "TGNO4.BA", "YPF", "PAM",
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

    # ── GROWTH MODE para $1,600 ────────────────────────────────────────────────
    # Objetivo: multiplicar capital en plazo medio-largo con riesgo productivo.
    # Más CP (momentum, 1-5 días) que LP (hold semanas) para capitalizar tendencias.
    # Sleeves: 55% LP (2 pos × $440) + 35% CP (2 pos × $280) + 10% cash reserva
    weight_long_term: float = 0.0    # LP siempre desactivado — CP momentum es superior para $1600
    weight_short_term: float = 0.85  # CP: 85% (fallback; build_target_portfolio lee signals.params)
    weight_options: float = 0.10          # 10% = ~$160 → 1 contrato long call/put por ciclo

    # Filtros LP — menos estrictos para que haya más candidatos
    max_beta_lp: float = 2.0             # beta alto = más upside en bull (era 1.8)
    min_sharpe_lp: float = 0.30          # menos estricto (era 0.40) — más candidatos LP

    # Parámetros técnicos para CP
    rsi_oversold: float = 35.0
    rsi_overbought: float = 75.0          # era 70 — permite entrar en momentum fuerte
    atr_stop_multiple: float = 2.0

    # Growth: 2 LP (alta convicción, ~$440/pos) + 2 CP (momentum, ~$280/pos)
    top_n_long_term: int = 2
    top_n_short_term: int = 2             # era 1 — 2 CP para más exposición al momentum

    # Bucket options — call sobre el pick #1 del LP
    top_n_bearish: int = 1                # máx 1 put direccional
    max_contracts_per_trade: int = 1      # 1 contrato por trade (capital limitado)

    # Concentración máxima — hasta 50% en la mejor idea
    max_weight_per_asset: float = 0.50    # 50% max en un solo nombre

    # Kill switch — growth mode acepta volatilidad intradía para capturar tendencias
    # -6% en $1600 = $96 pérdida diaria máxima antes del kill switch
    max_daily_drawdown: float = 0.06      # 6% — era 5%, más margen para que las posiciones respiren

    # Límites derivados — calibrados para capital ~$1600 USD
    max_options_allocation: float = 0.20  # techo duro sobre el bucket options (= weight_options)
    max_single_option_premium: float = 150.0  # máx USD por contrato — cabe en sleeve de $160
    max_hedge_allocation: float = 0.10    # hasta 10% del libro en puts SPY cuando bear
    enable_short_equity: bool = False     # OFF por default — preferimos puts (riesgo limitado)
    enable_options: bool = True           # ON — long calls/puts OTM, riesgo limitado a la prima
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
# AI Ensemble — cada modelo para lo que hace mejor:
#   Haiku:  decisiones en tiempo real, clasificaciones rápidas ($0.25/M)
#   Sonnet: análisis profundo, SEC filings, tesis de inversión ($3/M)
#   Gemini: sentiment masivo, scan de 51 tickers, macro context (gratis/barato)
GEMINI_MODEL: str = "models/gemini-2.0-flash"
CLAUDE_MODEL: str = "claude-haiku-4-5-20251001"        # fast + cheap — risk debate, monitor
CLAUDE_MODEL_DEEP: str = "claude-sonnet-4-6"           # deep analysis — Wall St thesis, SEC
WHATSAPP_FROM: str = "whatsapp:+14155238886"  # Twilio sandbox
