from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


_COLUMN_MIGRATIONS = {
    "transactions": {
        "is_internal_transfer": "ALTER TABLE transactions ADD COLUMN is_internal_transfer BOOLEAN DEFAULT 0",
        "matched_transaction_id": "ALTER TABLE transactions ADD COLUMN matched_transaction_id INTEGER",
        "is_credit_card_payment": "ALTER TABLE transactions ADD COLUMN is_credit_card_payment BOOLEAN DEFAULT 0",
        "canonical_merchant": "ALTER TABLE transactions ADD COLUMN canonical_merchant VARCHAR DEFAULT ''",
        "duplicate_group_id": "ALTER TABLE transactions ADD COLUMN duplicate_group_id VARCHAR",
    },
    "plaid_items": {
        "investments_status": "ALTER TABLE plaid_items ADD COLUMN investments_status VARCHAR",
    },
}


def run_startup_migrations() -> None:
    """Add columns to pre-existing tables that Base.metadata.create_all can't add."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _COLUMN_MIGRATIONS.items():
            if table not in tables:
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(ddl))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
