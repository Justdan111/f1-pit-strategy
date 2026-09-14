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


@lru_cache
def get_settings() -> Settings:
    """Settings are read from the environment once and reused.

    lru_cache makes this a lazy singleton: every caller gets the same object,
    but nothing is read at import time (which would make tests awkward).
    """
    return Settings()
