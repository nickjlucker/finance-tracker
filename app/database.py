from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def run_startup_migrations() -> None:
    """Add columns to pre-existing tables that Base.metadata.create_all can't add."""
    inspector = inspect(engine)
    if "transactions" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("transactions")}
    with engine.begin() as conn:
        if "is_internal_transfer" not in existing:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN is_internal_transfer BOOLEAN DEFAULT 0"))
        if "matched_transaction_id" not in existing:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN matched_transaction_id INTEGER"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
