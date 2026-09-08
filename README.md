# alpha-agent

A multi-agent system that turns financial news and market data into quantified trading signals, runs on a schedule, and reports what it did.

Personal research project. Built and evaluated in **paper trading only — no real money was ever traded.** Not investment advice.

---

## What it does

The system runs as a pipeline of specialised agents rather than a single script:

| Agent | Job |
|---|---|
| **Analyst** | Collects financial news and market data, and translates it into quantitative indicators |
| **Trader** | Turns indicators into position decisions under explicit entry and exit rules |
| **Monitor** | Watches open positions and portfolio health, and raises alerts on regime changes |
| **Reporting** | End-of-day report, performance review and email digest |

Each stage writes its output to disk, so any decision can be traced back to the data and the reasoning that produced it.

## Architecture

```
alpha_agent/     Analysis engine — indicators, scoring, valuation
trader_agent/    Execution logic and position management
signals/         Generated signals, persisted per run
docs/            Design notes and iteration log
tests/           Test suite
gcloud/          Google Cloud Run deployment
vps/             VPS deployment scripts (Hetzner / Oracle Cloud)
.github/         Scheduled workflows
```

Entry points are the `run_*.py` scripts: `run_analyst`, `run_trader`, `run_monitor`, `run_backtest`, `run_dashboard`, `run_premarket`, `run_eod_report`, and others.

## Stack

**Python** · Anthropic and other LLM APIs · **Alpaca** broker API for market data and paper execution · **Docker** · **Google Cloud Run** with Oracle Cloud as backup · **GitHub Actions** for scheduling · Flask dashboard · Telegram bot for alerts

## How it was evaluated

Run continuously for one month in paper trading, with a backtesting harness that corrects for survivorship and selection bias in the universe.

**The main conclusion was negative, and that was the point of the exercise:** having an agent carry the trading strategy itself is not where the value is — every trader has their own strategy and their own risk tolerance. Where this kind of system does add value is in the quantitative valuation layer: estimating what an asset is actually worth versus what it is trading at, and sizing accordingly. The iteration log in `docs/` records the findings that did not work, not only the ones that did.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env      # add your own API keys
python run_analyst.py
```

All credentials are read from environment variables. No keys are committed to this repository.

---

**Santino Félix** — [LinkedIn](https://www.linkedin.com/in/santino-felix/)
