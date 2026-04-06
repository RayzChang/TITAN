"""
TITAN v1 — 回測績效報告模組
功能：格式化輸出繁體中文報告，並可匯出交易明細 CSV
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd


class BacktestReport:
    """格式化回測結果，列印繁體中文報告並可匯出 CSV"""

    # 預設 CSV 輸出目錄
    DEFAULT_REPORT_DIR = Path('D:/02_trading/data')

    def __init__(self, results: dict):
        """
        Parameters
        ----------
        results : BacktestEngine.run() 回傳的績效 dict
        """
        self.results = results

    # ------------------------------------------------------------------
    # 公開介面
    # ------------------------------------------------------------------

    def print_report(self):
        """列印繁體中文回測績效報告至 stdout"""
        r = self.results

        # 時間格式化
        start = self._fmt_time(r.get('start_time'))
        end   = self._fmt_time(r.get('end_time'))

        total_return = r.get('total_return_pct', 0.0)
        max_dd       = r.get('max_drawdown_pct', 0.0)
        sharpe       = r.get('sharpe_ratio', 0.0)
        win_rate     = r.get('win_rate_pct', 0.0)
        avg_win      = r.get('avg_win_pct', 0.0)
        avg_loss     = r.get('avg_loss_pct', 0.0)
        total_trades = r.get('total_trades', 0)
        win_trades   = r.get('winning_trades', 0)
        lose_trades  = r.get('losing_trades', 0)

        # 正負號
        return_sign = '+' if total_return >= 0 else ''
        win_sign    = '+' if avg_win    >= 0 else ''
        loss_sign   = '+' if avg_loss   >= 0 else ''

        border = '=' * 36

        print(border)
        print('TITAN v1 — 回測績效報告')
        print(border)
        print(f'回測期間：{start} ~ {end}')
        print(f'總交易次數：{total_trades} 筆')
        print(f'獲利交易：{win_trades} 筆 | 虧損交易：{lose_trades} 筆')
        print(f'勝率：{win_rate:.1f}%')
        print(f'平均獲利：{win_sign}{avg_win:.2f}% | 平均虧損：{loss_sign}{avg_loss:.2f}%')
        print(f'總報酬率：{return_sign}{total_return:.2f}%')
        print(f'最大回撤：-{abs(max_dd):.2f}%')
        print(f'夏普比率：{sharpe:.2f}')
        print(border)

    def save_csv(self, path: str = None):
        """
        儲存交易明細到 CSV。

        Parameters
        ----------
        path : 自訂完整檔案路徑（含 .csv），不指定則自動產生時間戳記檔名
        """
        trade_list = self.results.get('trade_list', [])

        if not trade_list:
            print('[報告] 無交易紀錄，略過 CSV 匯出')
            return

        df = pd.DataFrame(trade_list)

        # 整理欄位順序
        cols = ['entry_time', 'exit_time', 'side', 'entry_price',
                'exit_price', 'pnl_pct', 'pnl_usdt', 'exit_reason']
        df = df[[c for c in cols if c in df.columns]]

        # 數值格式化
        for col in ['pnl_pct', 'pnl_usdt', 'entry_price', 'exit_price']:
            if col in df.columns:
                df[col] = df[col].round(4)

        # 決定輸出路徑
        if path is None:
            self.DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
            ts   = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = str(self.DEFAULT_REPORT_DIR / f'backtest_trades_{ts}.csv')

        df.to_csv(path, index=False, encoding='utf-8-sig')
        print(f'[報告] 交易明細已儲存至：{path}')

    # ------------------------------------------------------------------
    # 內部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_time(t) -> str:
        """將各種時間格式統一轉成 YYYY-MM-DD 字串"""
        if t is None:
            return 'N/A'
        if isinstance(t, str):
            return t[:10]
        if isinstance(t, (int, float)):
            return datetime.utcfromtimestamp(t / 1000).strftime('%Y-%m-%d')
        try:
            # pandas Timestamp 或 datetime
            return pd.Timestamp(t).strftime('%Y-%m-%d')
        except Exception:
            return str(t)[:10]
