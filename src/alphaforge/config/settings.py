from pydantic import BaseModel


class Settings(BaseModel):
    environment: str = "dev"
    sqlite_path: str = "sqlite:///./alphaforge.db"
    postgres_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/alphaforge"

    @property
    def database_url(self) -> str:
        return self.sqlite_path if self.environment == "dev" else self.postgres_url
