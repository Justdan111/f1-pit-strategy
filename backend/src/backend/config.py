"""Application settings. Overridable by environment variable with the F1_ prefix."""

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
    # Wall-clock seconds between lap ticks. A deliberate simplification: not a
    # claim about real lap timing, only enough to make the stream feel live.
    replay_tick_interval_seconds: float = 0.5

    # Bounds for the per-connection ?tick_interval= override.
    min_tick_interval_seconds: float = 0.0
    max_tick_interval_seconds: float = 10.0

    # --- Deployment ---
    # Comma-separated browser origins. Empty means allow any, which is right
    # for local development and wrong for production.
    allowed_origins: str = ""

    @property
    def allowed_origin_list(self) -> list[str]:
        """Parsed origins. Empty list means unrestricted."""
        return [
            o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()
        ]

    # --- Authentication ---
    # Comma-separated API keys accepted on the WebSocket. Empty disables the
    # check entirely, which is right for local development and wrong for a
    # deployed service.
    api_keys: str = ""

    @property
    def api_key_list(self) -> list[str]:
        """Accepted keys. Empty list means the WebSocket is open."""
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    # --- Decision log ---
    # Every decision is recorded so a race can be reviewed after the fact.
    # Best-effort: a storage failure must never take a live stream down.
    decision_log_enabled: bool = True
    decision_log_path: str = "decisions.db"
    # Oldest runs are pruned beyond this, so an unattended service does not
    # grow without bound.
    decision_log_max_runs: int = 200

    # --- Live mode ---
    # OpenF1 serves live data from 30 minutes before a session to 30 after.
    # Do not widen this to make local testing easier.
    live_window_margin_minutes: int = 30

    # Each poll costs 2 requests, so 10s is 12 req/min against a ~30/min free
    # tier. A lap takes 80-120s, so this cannot miss one.
    live_poll_interval_seconds: float = 10.0

    # Per-second guard, enforced across the whole client. 0.4s = 2.5 req/s.
    openf1_min_request_interval_seconds: float = 0.4

    live_backoff_initial_seconds: float = 5.0
    live_backoff_max_seconds: float = 60.0
    live_max_consecutive_failures: int = 6

    # Generous: a red flag stops a race for longer than a normal lap.
    live_idle_timeout_seconds: float = 900.0

    # --- Decision engine ---
    # Circuit-dependent in reality; one constant is a stated simplification.
    pit_lane_cost_seconds: float = 22.0

    # Three, not two: two points always fit a line perfectly.
    min_samples_for_fit: int = 3

    # Read the curve at age 1, not 0: lap one on a new set is not its best.
    fresh_tyre_reference_age: float = 1.0

    # --- Fuel-burn correction ---
    # A car sheds fuel through a race and gets faster for reasons that have
    # nothing to do with tyres. Within a stint, fuel load and tyre age are
    # perfectly collinear, so regression alone can never separate them; the
    # fuel term has to come from outside the data.
    #
    # ASSUMED CONSTANTS, not measured per circuit. Roughly 0.03 s of lap time
    # per kg carried is the standard figure, and 110 kg is the regulation
    # maximum race fuel load. Both are simplifications: real consumption
    # varies with circuit, and teams rarely start on a full load.
    fuel_correction_enabled: bool = True
    fuel_effect_s_per_kg: float = 0.03
    race_start_fuel_kg: float = 110.0

    # Burn per lap is start fuel divided by race distance, so the correction
    # is larger at short races. Used when the race length is not known --
    # notably live mode, where OpenF1 reports no lap count for a session in
    # progress. Roughly the middle of the calendar.
    assumed_race_laps: int = 57

    # --- Fit hygiene ---
    # Drop laps this much slower than the best lap at a similar tyre age.
    # Needed against real data: on Baku 2025, lap 1 was +25.7s (standing
    # start), laps 2-4 +55 to +89s (safety car), lap 41 +18.1s (out-lap).
    max_lap_time_excess_for_fit_s: float = 5.0

    # Half-width in laps of the neighbourhood a lap is judged against.
    fit_outlier_window_laps: int = 3

    exclude_pit_out_laps_from_fit: bool = True


@lru_cache
def get_settings() -> Settings:
    """Read once, reuse. Lazy so tests are not bound by import order."""
    return Settings()
