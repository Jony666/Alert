from dataclasses import dataclass, asdict
import hashlib
import json

STRATEGIES = ('BIAS25_REVERSAL', 'MACD_TREND', 'RANGE_VOLUME')
STEPS = {'1H': 3600000, '4H': 14400000, '1D': 86400000}
DAY = 86400000

@dataclass(frozen=True)
class Config:
    fee: float = .0005  # taker per fill; no assumed maker rebate
    slippage: float = .0005
    funding_per_8h: float = .0003  # adverse charge to both directions
    max_days: int = 14
    time_exit_days: int = 7
    ideal_days: tuple = (1, 3)
    min_quote_volume: float = 10_000_000  # trailing completed 20-bar daily equivalent
    participation: float = .0001
    min_oos: int = 100
    min_win_lower: float = .60
    initial: float = 10000
    reserve: float = 1000
    total_risk: float = 2000
    margin_fraction: float = .45
    leverages: tuple = (5, 10, 20)
    tp_weights: tuple = (.5, .5)

    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

CONFIG = Config()
