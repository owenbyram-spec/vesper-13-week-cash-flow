"""Backend process entry point; launcher supplies only explicit local settings."""
from __future__ import annotations

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Run the local PE cash-flow backend")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--frontend-origin", default="http://127.0.0.1:8770")
    parser.add_argument("--testing", action="store_true")
    args = parser.parse_args()
    from .backend.main import create_app
    import uvicorn
    application = create_app(data_dir=args.data_dir, port=args.port,
                             frontend_origin=args.frontend_origin, testing=args.testing)
    uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
