from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PlaidItem(Base):
    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    access_token: Mapped[str] = mapped_column(String)
    institution_name: Mapped[str] = mapped_column(String, default="")
    transactions_cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("plaid_items.id"))
    name: Mapped[str] = mapped_column(String)
    official_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mask: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, default="")
    subtype: Mapped[str] = mapped_column(String, default="")
    current_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    available_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)

    item: Mapped["PlaidItem"] = relationship(back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    name: Mapped[str] = mapped_column(String)
    merchant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    pending: Mapped[bool] = mapped_column(default=False)
    category_primary: Mapped[str] = mapped_column(String, default="OTHER", index=True)
    category_detailed: Mapped[str] = mapped_column(String, default="")
    is_internal_transfer: Mapped[bool] = mapped_column(default=False, index=True)
    matched_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )

    account: Mapped["Account"] = relationship(back_populates="transactions")


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category_primary", name="uq_budget_category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_primary: Mapped[str] = mapped_column(String, index=True)
    monthly_limit: Mapped[float] = mapped_column(Float)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (UniqueConstraint("account_id", "recorded_at", name="uq_snapshot_account_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    balance: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[date] = mapped_column(Date, index=True)


class CardLiability(Base):
    __tablename__ = "card_liabilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    apr_purchase: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_statement_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    minimum_payment_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_payment_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_overdue: Mapped[bool | None] = mapped_column(nullable=True)
    last_payment_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
