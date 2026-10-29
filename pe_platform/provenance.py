"""Check the pinned business-kernel bytes independently of the source checkout."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def verify_source() -> dict:
    manifest = json.loads((ROOT / "source-manifest.json").read_text())
    upstream = (ROOT / "upstream").resolve()
    for entry in manifest["files"]:
        path = (upstream / entry["path"]).resolve()
        if upstream not in path.parents or not path.is_file():
            raise RuntimeError("The pinned PE source tree is incomplete or outside its package.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError("Pinned PE source differs from its manifest: " + entry["path"])
    return {"source_repository": manifest["source_repository"],
            "source_revision": manifest["source_revision"],
            "source_files_verified": len(manifest["files"]),
            "business_kernel": "pinned upstream source",
            "local_adapters": "local identity, workspace credentials, scoped routes and review controls",
            "live_integrations_verified": False}
