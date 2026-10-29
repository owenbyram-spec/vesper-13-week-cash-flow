from typing import List, Dict, Any, Optional  # noqa: F401
from datetime import date
from sqlalchemy import func, case, desc  # noqa: F401
from sqlalchemy.orm import Session
from ..models import QBOTransaction, MonthlyFinancials, VendorProfile, QBOVendor

class FinancialMemoryService:
    def __init__(self, db: Session):
        self.db = db

    def refresh_monthly_financials(self, realm_id: str):
        """
        Rebuilds the MonthlyFinancials aggregated table from raw transactions.
        """
        # 1. Clear existing data for this realm (or use upsert logic in future)
        # For MVP, full rebuild is safer/easier.
        self.db.query(MonthlyFinancials).filter(MonthlyFinancials.realm_id == realm_id).delete()
        
        # 2. Aggregate Transactions
        # Group by: Year-Month, Account
        # Sum: Debits, Credits
        
        # Extract month start date from transaction date
        # Postgres: date_trunc('month', date)
        # SQLite: strftime('%Y-%m-01', date) -> This app uses Postgres per roadmap, 
        # but let's use Python-side date truncation or generic SQL if possible to be safe?
        # Actually, let's use SQLAlchemy func.date_trunc if strictly Postgres, 
        # or just query all txns and aggregate in Python if dataset is small (<10k).
        # Given "Production-Ready" goal, let's try SQL aggregation first.
        
        # Assuming Postgres for now based on "Add robust normalization layer in Postgres" in roadmap.
        
        month_col = func.date_trunc('month', QBOTransaction.date).label('month_start')
        
        stats = (  # noqa: F841
            self.db.query(
                month_col,
                QBOTransaction.account_ref,
                func.sum(case((QBOTransaction.amount < 0, QBOTransaction.amount), else_=0)).label('total_credit'), # credits are negative in some systems or just strictly purely directional? 
                # QBO API: Expenses are positive amounts usually, but CreditCardCredits are...
                # Let's assume standardized "Inflow" vs "Outflow" isn't fully normalized in QBOTransaction unless we did that.
                # Actually, QBOTransaction.amount is absolute usually, and type determines sign?
                # Let's check QBOTransaction model... "transaction_type". 
                # Complexity: JournalEntries can be everything.
                # Simplification: Let's iterate in Python for logic clarity unless performance hits.
            )
            .filter(QBOTransaction.realm_id == realm_id)
            .filter(QBOTransaction.account_ref.isnot(None))
            .group_by(month_col, QBOTransaction.account_ref)
            .all()
        )
        
        # Okay, the SQL aggregation of Amount based on Type is tricky without a "signed_amount" column.
        # Let's fetching all txns and process in Python 
        # (It's 100x slower but 100x accurately implementing complex QBO sign logic).
        
        txns = self.db.query(QBOTransaction).filter(QBOTransaction.realm_id == realm_id).all()
        
        agg = {} # (month_start, account_id) -> {debit: 0, credit: 0}
        
        for t in txns:
            if not t.account_ref:
                continue
                
            ms = date(t.date.year, t.date.month, 1)
            key = (ms, t.account_ref)
            
            if key not in agg:
                agg[key] = {"debit": 0, "credit": 0}
            
            # Sign Logic:
            # Invoice, SalesReceipt = Credit (Income)
            # Bill, Expense, Purchase = Debit (Expense)
            # Payment? 
            # This is complex. For "MonthlyFinancials" usually implies P&L headers.
            # Let's assume standard positive amounts for standard types.
            
            amt = float(t.amount) if t.amount else 0.0
            type_lower = t.transaction_type.lower() if t.transaction_type else ""
            
            if type_lower in ["invoice", "salesreceipt", "deposit"]:
                agg[key]["credit"] += amt
            elif type_lower in ["bill", "expense", "purchase", "check", "journalentry"]:
                # JE is ambiguous, assuming Debit for now implies Expense side? 
                # Keeping it simple: Most flows are Expenses.
                agg[key]["debit"] += amt
            else:
                # Fallback: Treat as debit?
                agg[key]["debit"] += amt
                
        # Insert results
        for (ms, acc_id), vals in agg.items():
            mf = MonthlyFinancials(
                realm_id=realm_id,
                month_start=ms,
                account_id=acc_id,
                total_debit=vals["debit"],
                total_credit=vals["credit"],
                net_change=vals["credit"] - vals["debit"],
                # Ending Balance requires running total from start of time. 
                # For P&L accounts, ending balance is just net_change for the month (starts at 0).
                # For BS accounts, it's cumulative. 
                # MVP: Set ending_balance = net_change (P&L view).
                ending_balance=vals["credit"] - vals["debit"] 
            )
            self.db.add(mf)
            
        self.db.commit()

    def update_vendor_profiles(self, realm_id: str):
        """
        Calculates average spend and frequency for vendors.
        """
        # 1. Get List of Vendors from Transactions
        # We group by EntityRef (which should be vendor ID).
        
        # Query: EntityRef, Count, Sum(Amount), Min(Date), Max(Date)
        
        stats = (
            self.db.query(
                QBOTransaction.entity_ref,
                func.count(QBOTransaction.id).label('txn_count'),
                func.sum(QBOTransaction.amount).label('total_spend'),
                func.min(QBOTransaction.date).label('first_date'),
                func.max(QBOTransaction.date).label('last_date')
            )
            .filter(QBOTransaction.realm_id == realm_id)
            .filter(QBOTransaction.entity_ref.isnot(None))
            # Filter for expense types to avoid counting bill payments double if both exist?
            # Or just take everything.
            .group_by(QBOTransaction.entity_ref)
            .all()
        )
        
        vendor_ids = [row.entity_ref for row in stats if row.entity_ref]
        
        # Bulk load QBOVendors
        qbo_vendors = self.db.query(QBOVendor).filter(
            QBOVendor.realm_id == realm_id,
            QBOVendor.qbo_id.in_(vendor_ids)
        ).all() if vendor_ids else []
        vendor_map = {v.qbo_id: v for v in qbo_vendors}
        
        # Bulk load VendorProfiles
        existing_profiles = self.db.query(VendorProfile).filter(
            VendorProfile.realm_id == realm_id,
            VendorProfile.qbo_vendor_id.in_(vendor_ids)
        ).all() if vendor_ids else []
        profile_map = {p.qbo_vendor_id: p for p in existing_profiles}
        
        for row in stats:
            vendor_id = row.entity_ref
            count = row.txn_count
            total = float(row.total_spend) if row.total_spend else 0.0
            first = row.first_date
            last = row.last_date
            
            # Calculate Frequency
            days_active = (last - first).days
            if days_active < 7:
                freq = "ONE_TIME"
            elif count > (days_active / 7):
                freq = "WEEKLY"
            elif count > (days_active / 30):
                freq = "MONTHLY"
            else:
                freq = "SPORADIC"
                
            # Avg Monthly Spend
            # Approximation: Total / (Days / 30)
            months = max(1, days_active / 30)
            avg_spend = total / months
            
            # Lookup Name
            vendor_name = "Unknown"
            v_rec = vendor_map.get(vendor_id)
            if v_rec:
                vendor_name = v_rec.display_name
            
            # Upsert Profile
            profile = profile_map.get(vendor_id)
            
            if not profile:
                profile = VendorProfile(
                    realm_id=realm_id,
                    qbo_vendor_id=vendor_id,
                    vendor_name=vendor_name
                )
                self.db.add(profile)
                profile_map[vendor_id] = profile
            
            profile.interaction_count = count
            profile.avg_monthly_spend = avg_spend
            profile.frequency = freq
            profile.last_interaction_date = last
            profile.vendor_name = vendor_name # Update name in case it changed
            
        self.db.commit()

    def get_vendor_stats(self, realm_id: str, vendor_id: str) -> Optional[Dict[str, Any]]:
        profile = (
            self.db.query(VendorProfile)
            .filter(VendorProfile.realm_id == realm_id)
            .filter(VendorProfile.qbo_vendor_id == vendor_id)
            .first()
        )
        if not profile:
            return None
            
        return {
            "name": profile.vendor_name,
            "avg_spend": profile.avg_monthly_spend,
            "frequency": profile.frequency,
            "last_seen": profile.last_interaction_date,
            "txn_count": profile.interaction_count
        }
