from typing import Dict, Any
from ..qbo_client import QBOClient

def update_vendor(client: QBOClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Updates a Vendor in QBO.
    """
    vendor_id = payload.get("Id")
    if not vendor_id:
         raise ValueError("Vendor payload must contain 'Id'.")
         
    return client.update_transaction("Vendor", vendor_id, payload)
