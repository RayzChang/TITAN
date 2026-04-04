"""
TITAN v1 — 幣種過濾器
篩選幣安 USDT-M 合約中可交易的有效幣種，排除穩定幣
"""

from utils.logger import get_logger

logger = get_logger()

# 預設排除清單（穩定幣、不適合合約交易的幣種）
DEFAULT_EXCLUDE = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP"}


def filter_symbols(symbols: list[str], exclude: list[str] = None) -> list[str]:
    """
    過濾有效的 USDT-M 合約交易對
    - 只保留 /USDT:USDT 格式（永續合約）
    - 排除穩定幣
    - 回傳過濾後的 symbol 清單
    """
    exclude_set = DEFAULT_EXCLUDE.copy()
    if exclude:
        exclude_set.update(s.upper() for s in exclude)

    result = []
    for sym in symbols:
        # ccxt USDT-M 合約格式：BTC/USDT:USDT
        if not sym.endswith(":USDT"):
            continue
        base = sym.split("/")[0].upper()
        if base in exclude_set:
            continue
        result.append(sym)

    return result
