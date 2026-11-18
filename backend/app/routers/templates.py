from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.template import Template
from app.models.user import User
from app.schemas.template import TemplateOut

router = APIRouter(prefix="/templates", tags=["templates"])

# "List/read templates —
# authenticated". Both endpoints are reads of platform configuration, so they
# take the same requirement as every other read.


@router.get("", response_model=list[TemplateOut])
def list_templates(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Template]:
    return list(db.scalars(select(Template).order_by(Template.name)))


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(
    template_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> Template:
    template = db.get(Template, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return template
