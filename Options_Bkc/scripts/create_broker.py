"""Create a new broker folder from the Angle One reference implementation."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.broker.administration import create_broker_folder


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a broker folder from app/broker/angleone.")
    parser.add_argument("broker_name", help="New broker folder name, for example zerodha")
    args = parser.parse_args()

    result = create_broker_folder(args.broker_name, broker_root=Path(ROOT_DIR) / "app" / "broker")
    print(f"Created broker folder: {result.target_dir}")
    for copied_file in result.copied_files:
        print(f" - {copied_file}")


if __name__ == "__main__":
    main()
