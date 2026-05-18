from pydantic import BaseModel


class Settings(BaseModel):
    environment: str = "dev"
    sqlite_path: str = "sqlite:///./alphaforge.db"
    postgres_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/alphaforge"

    ai_brain_enabled: bool = True
    ai_brain_fail_closed_live: bool = True
    database_url_override: str = "sqlite:///data/alphaforge.db"
    ai_min_score: int = 60
    ai_market_min_score: int = 85
    ai_aggressive_score: int = 90
    ai_normal_score: int = 75
    ai_reduced_score: int = 60
    adaptive_learning_enabled: bool = False
    adaptive_shadow_mode: bool = True
    adaptive_min_sample_size: int = 50
    adaptive_max_score_adjustment: float = 0.05
    adaptive_max_effective_rr_adjustment: float = 0.15
    adaptive_allow_loosening_gates: bool = False
    adaptive_record_rejected_signals: bool = True
    adaptive_record_closed_trades: bool = True

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return self.sqlite_path if self.environment == "dev" else self.postgres_url
