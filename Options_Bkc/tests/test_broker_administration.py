from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from app.broker.administration import create_broker_folder


class BrokerAdministrationTests(unittest.TestCase):
    def test_create_broker_folder_copies_reference_files(self) -> None:
        broker_root = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_admin"
        )
        broker_name = f"zerodha_{uuid4().hex}"
        target = broker_root / broker_name
        try:
            result = create_broker_folder(broker_name, broker_root=broker_root)
            self.assertEqual(result.broker_name, broker_name)
            self.assertTrue((target / "client.py").exists())
            self.assertTrue((target / "ONBOARDING_NOTES.md").exists())
        finally:
            if target.exists():
                shutil.rmtree(target)


if __name__ == "__main__":
    unittest.main()
