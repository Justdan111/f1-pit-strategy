"""Application settings.

Everything that might reasonably differ between a laptop, CI, and a deployed
host lives here rather than being hardcoded at the call site. Values can be
overridden with environment variables (or a .env file) using the F1_ prefix,
e.g. F1_REPLAY_TICK_INTERVAL_SECONDS=0.05 to make replays run fast in tests.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="F1_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- OpenF1 ---
    openf1_base_url: str = "https://api.openf1.org/v1"
    openf1_timeout_seconds: float = 10.0

    # --- Replay pacing ---
    # Seconds of wall-clock time between consecutive lap ticks in replay mode.
    # This is a deliberate simplification (see SPEC 7.3): we are NOT claiming
    # one tick per real lap time. It only exists to make the stream *feel*
    # live so the downstream plumbing is exercised the same way it will be
    # when LiveTickSource lands on Day 4.
    replay_tick_interval_seconds: float = 0.5

    # Guard rails for the per-connection ?tick_interval= override, so a client
    # cannot ask the server to hold a connection open for hours.
    min_tick_interval_seconds: float = 0.0
    max_tick_interval_seconds: float = 10.0

    # --- Decision engine (Day 2) ---

    # Time lost by driving through the pit lane and stopping, versus staying
    # out. SIMPLIFICATION: this is circuit-dependent in reality (Monaco and
    # Monza differ by several seconds) and we have not researched it per
    # circuit. One constant, stated openly, is the Day 2 scope.
    pit_lane_cost_seconds: float = 22.0

    # A line needs two points; two points always fit perfectly and tell you
    # nothing about whether the relationship is real. Three is the smallest
    # number at which the fit can disagree with the data, so it is the
    # smallest honest minimum.
    min_samples_for_fit: int = 3

    # Where on the fitted curve we read off "a fresh set of this compound".
    # Age 1 rather than 0, because lap one on a new set is an out-lap-ish
    # compromise, not the tyre's theoretical best.
    fresh_tyre_reference_age: float = 1.0

    # --- Fit hygiene ---
    # Confirmed necessary against real data (Baku 2025, session_key 9904):
    # of 51 laps, lap 1 was +25.7s (standing start), laps 2-4 were +55 to
    # +89s (safety car), lap 40 +5.4s (in-lap) and lap 41 +18.1s (out-lap).
    # Four of those sit at LOW tyre age, so a naive fit reads "slow when new,
    # fast when old" and reports NEGATIVE degradation. Measured: naive slope
    # -0.7669 s/lap versus -0.0459 s/lap after cleaning. A linear regression
    # over contaminated data is not "simple", it is wrong.
    #
    # Drop any lap more than this many seconds slower than the fastest lap
    # seen at a SIMILAR TYRE AGE (within +/- fit_outlier_window_laps).
    #
    # Comparing against the fastest lap of the whole stint instead was the
    # first attempt and it was wrong: on a heavily degrading tyre the late
    # laps are legitimately many seconds off the early ones, so a global
    # threshold discards exactly the data that proves degradation is
    # happening. A hand-checked scenario (m=1.5 s/lap, age 20) caught it —
    # the engine silently returned no decision at all.
    #
    # The local comparison works because degradation is monotonic: a lap
    # should never be much slower than a lap run on a similarly-aged tyre.
    # Anomalies (safety car, in-lap, standing start, traffic, mistakes) are
    # slow relative to their neighbours; a worn tyre is not.
    max_lap_time_excess_for_fit_s: float = 5.0

    # Half-width, in laps of tyre age, of the neighbourhood a lap is judged
    # against. Wide enough to always have neighbours, narrow enough that
    # degradation across the window is small compared to a real anomaly.
    #
    # KNOWN LIMIT: if every lap in a window is contaminated (a safety car
    # lasting longer than the window, with no clean lap at a nearby tyre
    # age), the local baseline is itself contaminated and those laps survive.
    # Accepted for Day 2; a residual-based refit would be the Day 4 answer.
    fit_outlier_window_laps: int = 3

    # Drop laps flagged is_pit_out_lap. Available from OpenF1 /v1/laps in
    # both replay and live mode, so this stays mode-agnostic.
    exclude_pit_out_laps_from_fit: bool = True


@lru_cache
def get_settings() -> Settings:
    """Settings are read from the environment once and reused.

    lru_cache makes this a lazy singleton: every caller gets the same object,
    but nothing is read at import time (which would make tests awkward).
    """
    return Settings()
