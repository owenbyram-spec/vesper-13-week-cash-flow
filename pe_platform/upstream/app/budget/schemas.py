from typing import Any, Dict, Optional

from pydantic import BaseModel


class BudgetLineCreate(BaseModel):
    company_id: str
    budget_year: int
    account_name: str
    account_type: str
    qbo_account_id: Optional[str] = None
    monthly_amounts: Dict[str, float] = {}
    sort_order: int = 0
    notes: Optional[str] = None
    department: Optional[str] = None
    scenario: str = "base"
    driver_formula: Optional[str] = None


class BudgetLineUpdate(BaseModel):
    account_name: Optional[str] = None
    account_type: Optional[str] = None
    qbo_account_id: Optional[str] = None
    monthly_amounts: Optional[Dict[str, float]] = None
    sort_order: Optional[int] = None
    notes: Optional[str] = None
    department: Optional[str] = None
    scenario: Optional[str] = None
    driver_formula: Optional[str] = None


class DriverCreate(BaseModel):
    company_id: str
    budget_year: int
    account_name: str
    qbo_account_id: Optional[str] = None
    driver_method: str
    driver_params: Dict[str, Any] = {}
    driver_category: str = "expense"
    vendor_name: Optional[str] = None
    customer_name: Optional[str] = None
    scenario: str = "base"


class DriverUpdate(BaseModel):
    driver_method: Optional[str] = None
    driver_params: Optional[Dict[str, Any]] = None


class VendorLineCreate(BaseModel):
    parent_line_id: int
    vendor_name: str
    monthly_amounts: Dict[str, float] = {}


class CustomerLineCreate(BaseModel):
    parent_line_id: int
    customer_name: str
    monthly_amounts: Dict[str, float] = {}
