"""SmartTar v0.4.0 isolated confirmation driver regressions."""

import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("confirm_repository_test", ROOT / "tools/confirm_repository_test.py")
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class ConfirmationTests(unittest.TestCase):
    def test_python_parent_and_child_each_receive_their_own_answer(self):
        child = 'import sys\nanswer = input("Child confirmation? ")\nsys.exit(0 if answer == "y" else 4)\n'
        parent = (
            'import subprocess, sys\n'
            'answer = input("Parent confirmation? ")\n'
            'if answer != "y":\n    sys.exit(3)\n'
            f'sys.exit(subprocess.call([sys.executable, "-c", {child!r}]))\n'
        )
        output = io.BytesIO()
        driver.run_confirmations([sys.executable, "-c", parent],
                                 (b"Parent confirmation? ", b"Child confirmation? "), timeout=5, output=output)
        self.assertIn(b"Child confirmation? ", output.getvalue())

    def test_missing_prompt_is_not_silently_approved(self):
        with self.assertRaises(RuntimeError):
            driver.run_confirmations([sys.executable, "-c", 'print("No confirmation requested")'],
                                     (b"Confirm? ",), timeout=5, output=io.BytesIO())

    def test_nonzero_exit_after_confirmation_is_failure(self):
        code = 'import sys\ninput("Confirm? ")\nsys.exit(6)\n'
        with self.assertRaisesRegex(RuntimeError, "exit status 6"):
            driver.run_confirmations([sys.executable, "-c", code], (b"Confirm? ",), timeout=5, output=io.BytesIO())

    def test_unknown_prompt_times_out_without_confirmation(self):
        code = 'input("Unexpected system change? ")\n'
        with self.assertRaisesRegex(RuntimeError, "expected confirmation"):
            driver.run_confirmations([sys.executable, "-c", code], (b"Confirm? ",), timeout=0.2, output=io.BytesIO())

    def test_entry_point_refuses_non_disposable_execution(self):
        with patch.object(os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(SystemExit, "disposable Arch"):
                driver.main()


if __name__ == "__main__":
    unittest.main()
