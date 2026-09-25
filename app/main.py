from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import Base, engine, run_startup_migrations
from app.routers import accounts, budgets, dashboard, insights, link, plan, sync, transactions

Base.metadata.create_all(bind=engine)
run_startup_migrations()

app = FastAPI(title="Personal Finance Tracker")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)
app.include_router(link.router)
app.include_router(plan.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(budgets.router)
app.include_router(sync.router)
app.include_router(insights.router)
