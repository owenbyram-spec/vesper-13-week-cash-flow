from sqlalchemy.orm import Session

from ..dependencies import check_tenant_access
from ..models import User


def _require_budget_access(
    minimum_role: str,
    *,
    company_id: str,
    db: Session,
    current_user: User,
) -> None:
    check_tenant_access(minimum_role, company_id=company_id, db=db, user=current_user)
