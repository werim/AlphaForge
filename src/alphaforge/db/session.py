from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from alphaforge.config.settings import Settings


def create_db_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, echo=False, future=True)


def create_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
