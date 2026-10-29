from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import desc

from ..models import ClassificationRule, QBOTransaction

class RuleBasedClassifier:
    def __init__(self, db: Session):
        self.db = db
        # Cache rules? For now, fetch on demand or simple cache
        self.rules = self._fetch_active_rules()

    def _fetch_active_rules(self) -> List[ClassificationRule]:
        return (
            self.db.query(ClassificationRule)
            .filter(ClassificationRule.active == True)  # noqa: E712
            .order_by(desc(ClassificationRule.priority))
            .all()
        )

    def classify(self, transaction: QBOTransaction) -> Optional[Dict[str, Any]]:
        """
        Returns a classification suggestion if a rule matches.
        Output format:
        {
            "account_id": "123",
            "account_name": "Travel",
            "reason": "Rule match: 'uber'",
            "confidence": 1.0,
            "rule_id": 1
        }
        """
        description = (transaction.description or "").lower()
        
        # Simple keyword matching O(N*M) - acceptable for MVP
        for rule in self.rules:
            keyword = rule.keyword.lower()
            if keyword in description:
                return {
                    "account_id": rule.target_account_id,
                    "account_name": rule.target_account_name,
                    "reason": f"Rule match: '{rule.keyword}'",
                    "confidence": 1.0,
                    "rule_id": rule.id
                }
        
        return None
