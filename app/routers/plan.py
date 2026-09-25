from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session

from app import plan
from app.database import get_db
from app.routers.dashboard import templates

router = APIRouter(tags=["plan"])


@router.get("/plan")
def plan_page(request: Request):
    return templates.TemplateResponse("plan.html", {"request": request, "active": "plan"})


@router.get("/api/plan")
def get_plan(db: Session = Depends(get_db)):
    return plan.build_plan(db)


@router.put("/api/plan/settings")
def put_plan_settings(patch: dict = Body(...), db: Session = Depends(get_db)):
    settings = plan.update_settings(db, patch)
    db.commit()
    return settings
