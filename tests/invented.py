"""Invented test data: tickers, prices, limits.

No real trades, figures, or rules. This repo is public.
"""

# Invented tickers for testing
TICKERS = ["ACME", "WXYZ", "TEST"]

# Invented prices
PRICES = {
    "ACME": 100.0,
    "WXYZ": 50.0,
    "TEST": 25.0,
}

# Invented limits for trading rules
LIMITS = {
    "max_position_size": 1000,
    "max_daily_trades": 10,
    "risk_per_trade": 0.02,
}

# Invented futures contract: 100 per point, so a 1.5-point stop risks 150 on one contract
FUTURE_POINT_VALUE = 100.0

# An invented exchange rate, for the check that converts a risk into the limit's currency
FX_USD_CAD = 1.4

# Invented trade ID for tests
TEST_TRADE_ID = "test-trade-001"
