"""SoC during a home charge from the house charger meter, and battery model drift.

Rules and the numbers behind them: docs/home-charge.md.
"""

import statistics
from dataclasses import dataclass

MIN = 60_000
RUNNING_MS = 5 * MIN  # the last charging minute is this recent: the charge runs
ON_W = 150.0  # the meter shows charging above this power (as homy's ioniq-sessions)
GAP_MS = 2 * MIN  # a pause up to this long stays inside one block
PREHEAT_MAX_MS = 40 * MIN  # a later block this short is preheat, not charging
MIN_SOC_STEP = 20.0  # learn kWh per % only from charges of at least this many %
WARM_UP = 10  # the back-test scores a charge only after this many earlier ones
DEFAULT_RATIO = 0.90
DEFAULT_OVERHEAD = 1.0
DEFAULT_KWH_PER_PCT = 0.30
MEDIAN_MONTHS = 3


@dataclass
class Block:
    start: int  # first minute with power
    end: int  # last minute with power
    kwh: float  # meter kWh
    kw: float  # the last power seen
    total: float = 0.0  # the meter total at the last minute


def blocks(minutes: list[tuple[int, float | None, float | None]]) -> list[Block]:
    """Blocks of charging from 1-min meter rows (t, max power W, last total kWh).

    The energy of a block is the change of the meter total from the minute
    before it to its last minute, so a gap in the power data loses nothing.
    """
    out: list[Block] = []
    prev_e = None
    for t, w, e in minutes:
        if e is None:
            continue
        if w is not None and w > ON_W:
            if out and t - out[-1].end <= GAP_MS:
                b = out[-1]
                # A meter reset (the total goes down) adds nothing.
                b.kwh, b.end, b.kw = b.kwh + max(e - b.total, 0.0), t, w / 1000
            else:
                # No row in the minute before: the first minute is lost (about 0.02 kWh).
                base = prev_e if prev_e is not None else e
                b = Block(start=t, end=t, kwh=max(e - base, 0.0), kw=w / 1000)
                out.append(b)
            b.total = e
        prev_e = e
    return out


def split(bs: list[Block]) -> tuple[float, float]:
    """(charging kWh, preheat kWh). A block after the first, 40 min or shorter, is preheat."""
    pre = [i > 0 and b.end - b.start + MIN <= PREHEAT_MAX_MS for i, b in enumerate(bs)]
    return (
        sum(b.kwh for b, p in zip(bs, pre, strict=True) if not p),
        sum(b.kwh for b, p in zip(bs, pre, strict=True) if p),
    )


@dataclass
class HomeCharge:
    start: int
    meter_kwh: float  # E: meter kWh while charging, preheat left out
    battery_kwh: float
    soc_start: float
    soc_end: float
    month: str = ""

    @property
    def dsoc(self) -> float:
        return self.soc_end - self.soc_start


@dataclass
class Model:
    """battery kWh = ratio × (meter kWh − overhead); SoC % = battery kWh ÷ kwh_per_pct."""

    ratio: float = DEFAULT_RATIO
    overhead: float = DEFAULT_OVERHEAD
    kwh_per_pct: float = DEFAULT_KWH_PER_PCT

    def soc(self, soc_start: float, meter_kwh: float) -> float:
        gain = max(meter_kwh - self.overhead, 0.0) * self.ratio / self.kwh_per_pct
        return min(soc_start + gain, 100.0)

    def meter_kwh_for(self, pct: float) -> float:
        """Meter kWh to add pct % from now (the overhead is paid already)."""
        return max(pct, 0.0) * self.kwh_per_pct / self.ratio


def kwh_per_pct(hc: list[HomeCharge]) -> float | None:
    vals = [c.battery_kwh / c.dsoc for c in hc if c.dsoc >= MIN_SOC_STEP]
    return statistics.median(vals) if vals else None


def fit(hc: list[HomeCharge]) -> Model:
    """Least squares of battery kWh on meter kWh: slope = ratio, overhead = −intercept ÷ slope."""
    m = Model(kwh_per_pct=kwh_per_pct(hc) or DEFAULT_KWH_PER_PCT)
    if len(hc) < 2:
        return m
    xs, ys = [c.meter_kwh for c in hc], [c.battery_kwh for c in hc]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return m
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    if b <= 0:
        return m
    m.ratio, m.overhead = b, -(my - b * mx) / b
    return m


def backtest(hc: list[HomeCharge], warm_up: int = WARM_UP) -> list[float]:
    """|estimated − real| end SoC per charge. Each charge uses only the charges before it."""
    hc = sorted(hc, key=lambda c: c.start)
    return [
        abs(fit(hc[:i]).soc(c.soc_start, c.meter_kwh) - c.soc_end)
        for i, c in enumerate(hc)
        if i >= warm_up
    ]


def quantile(vals: list[float], q: float) -> float:
    """Linear interpolation between the closest ranks."""
    s = sorted(vals)
    k = (len(s) - 1) * q
    i = int(k)
    return s[i] if i + 1 >= len(s) else s[i] + (s[i + 1] - s[i]) * (k - i)


@dataclass
class MonthModel:
    kwh_per_pct: float | None = None
    meter_ratio: float | None = None
    overhead_kwh: float | None = None
    drift_flag: str = ""


def drift(hc: list[HomeCharge], limit_pct: float, limit_kwh: float) -> dict[str, MonthModel]:
    """Per month: kWh per %, meter-to-battery ratio and overhead per charge.

    A value is flagged when it moves more than limit_pct (overhead: limit_kwh)
    against the median of the up to 3 months before it.
    """
    all_ = fit(hc)
    by: dict[str, list[HomeCharge]] = {}
    for c in sorted(hc, key=lambda c: c.start):
        by.setdefault(c.month, []).append(c)
    out: dict[str, MonthModel] = {}
    for key in sorted(by):
        cs = by[key]
        # The ratio after the overhead of all charges, so a month with more
        # small charges does not look like a lower ratio.
        e = sum(max(c.meter_kwh - all_.overhead, 0.0) for c in cs)
        out[key] = MonthModel(
            kwh_per_pct=kwh_per_pct(cs),
            meter_ratio=sum(c.battery_kwh for c in cs) / e if e > 0 else None,
            overhead_kwh=statistics.median(c.meter_kwh - c.battery_kwh / all_.ratio for c in cs),
        )
    keys = sorted(out)
    for i, key in enumerate(keys):
        m, reasons = out[key], []
        for name in ("kwh_per_pct", "meter_ratio", "overhead_kwh"):
            v = getattr(m, name)
            prev = [getattr(out[k], name) for k in keys[:i]]
            prev = [p for p in prev if p is not None][-MEDIAN_MONTHS:]
            if v is None or not prev:
                continue
            med = statistics.median(prev)
            if name == "overhead_kwh":
                if abs(v - med) > limit_kwh:
                    reasons.append(f"{name} {v - med:+.2f} kWh")
            elif med and abs(v - med) / med * 100 > limit_pct:
                reasons.append(f"{name} {(v - med) / med * 100:+.1f} %")
        m.drift_flag = "; ".join(reasons)
    return out


def live(
    bs: list[Block], soc_plugin: float | None, model: Model, now: int, target: float
) -> dict | None:
    """The estimate for a home charge since the car came home, or None."""
    if not bs or soc_plugin is None:
        return None
    last = bs[-1]
    # last.end is the start of a minute bucket, and the meter data comes a bit late.
    running = now - last.end <= RUNNING_MS
    done = bs
    if running and len(bs) > 1 and last.end - last.start + MIN <= PREHEAT_MAX_MS:
        done = bs[:-1]  # may be preheat: left out until it is longer than 40 min
    meter, _pre = split(done)
    kw = done[-1].kw
    soc = model.soc(soc_plugin, meter)
    need = model.meter_kwh_for(target - soc)
    return {
        "running": running,
        "since": bs[0].start,
        "soc_plugin": soc_plugin,
        "soc_now": soc,
        "meter_kwh": meter,
        "target": target,
        "meter_kwh_to_target": need,
        "min_to_target": need / kw * 60 if running and kw > 0 else None,
    }
