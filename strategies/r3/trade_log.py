"""R3 Sprint 6 backtest records.

These dataclasses describe simulated fills, exits, positions, and portfolio
state. They are for historical simulation only and do not touch exchange APIs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class FillResult:
    order_id: str
    signal_id: str | None
    symbol: str
    direction: str
    requested_quantity: float
    filled_quantity: float
    fill_price: float | None
    fill_timestamp: datetime | None
    status: str
    fee: float
    slippage: float
    reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExitEvent:
    position_id: str
    symbol: str
    strategy_name: str
    direction: str
    exit_type: str
    exit_price: float
    exit_timestamp: datetime
    quantity: float
    realized_pnl: float
    fee: float
    slippage: float
    funding_cost: float
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class Position:
    position_id: str
    signal_id: str | None
    symbol: str
    strategy_name: str
    direction: str
    entry_timestamp: datetime
    entry_price: float
    quantity: float
    remaining_quantity: float
    stop_price: float
    tp1_price: float | None
    tp2_price: float | None
    time_stop_at: datetime | None
    trailing_state: dict[str, Any] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    status: str = "OPEN"
    tp1_done: bool = False
    entry_fee: float = 0.0
    entry_slippage: float = 0.0


@dataclass
class PortfolioState:
    initial_capital: float
    current_equity: float
    cash_balance: float
    open_positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[ExitEvent] = field(default_factory=list)
    daily_pnl: dict[str, float] = field(default_factory=dict)
    max_drawdown: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    total_funding: float = 0.0


def fill_results_to_frame(fills: list[FillResult]) -> pd.DataFrame:
    return pd.DataFrame([asdict(item) for item in fills])


def exit_events_to_frame(events: list[ExitEvent]) -> pd.DataFrame:
    return pd.DataFrame([asdict(item) for item in events])


def write_report_files(
    *,
    output_dir: Path,
    trade_log: pd.DataFrame,
    daily_pnl: pd.DataFrame,
    equity_curve: pd.DataFrame,
    drawdown_curve: pd.DataFrame,
    metrics: dict[str, Any],
) -> dict[str, Path]:
    """Write Sprint 6 backtest artifacts to a report directory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "trade_log": output_dir / "trade_log.csv",
        "daily_pnl": output_dir / "daily_pnl.csv",
        "equity_curve": output_dir / "equity_curve.csv",
        "drawdown_curve": output_dir / "drawdown_curve.csv",
        "metrics": output_dir / "backtest_metrics.json",
        "report": output_dir / "backtest_report.md",
    }

    trade_log.to_csv(paths["trade_log"], index=False)
    daily_pnl.to_csv(paths["daily_pnl"], index=False)
    equity_curve.to_csv(paths["equity_curve"], index=False)
    drawdown_curve.to_csv(paths["drawdown_curve"], index=False)

    import json

    paths["metrics"].write_text(
        json.dumps(metrics, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    paths["report"].write_text(_markdown_report(metrics), encoding="utf-8")
    return paths


def _markdown_report(metrics: dict[str, Any]) -> str:
    lines = [
        "# R3 Backtest Smoke Report",
        "",
        "This is a Sprint 6 historical simulation report, not an L0-L6 validation report.",
        "",
        "## Metrics",
        "",
    ]
    for key in sorted(metrics):
        lines.append(f"- `{key}`: {metrics[key]}")
    warnings = metrics.get("data_warnings") or metrics.get("warnings") or []
    if warnings:
        lines.extend(["", "## Data Warnings", ""])
        for warning in warnings:
            lines.append(f"- `{warning}`")
    lines.append("")
    return "\n".join(lines)
