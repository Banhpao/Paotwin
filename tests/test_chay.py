import io
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from paotwin import common
from paotwin.chay import _KillOnClose, pump


class KillOnCloseTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "chỉ áp dụng ngoài Windows")
    def test_does_nothing_outside_windows(self):
        job = _KillOnClose()
        self.assertIsNone(job.handle)
        with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
            self.assertFalse(job.add(child))
            child.wait(10)

    @unittest.skipUnless(sys.platform == "win32", "job object chỉ có trên Windows")
    def test_children_end_when_the_job_closes(self):
        # What happens when a supervisor ends the launcher: its handle to the
        # job closes and Windows ends every process in it.
        job = _KillOnClose()
        self.assertIsNotNone(job.handle)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(job.add(child))
            self.assertIsNone(child.poll())
            job.kernel32.CloseHandle(job.handle)
            deadline = time.monotonic() + 10
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertIsNotNone(child.poll(), "tiến trình con vẫn chạy sau khi đóng job")
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(10)


class OutputTest(unittest.TestCase):
    """Started by Paoihub, the launcher writes to a pipe in the ANSI code
    page (cp1252 on the laptop): it crashed on its first Vietnamese line."""

    def run_python(self, code):
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        return subprocess.run([sys.executable, "-c", code], cwd=common.ROOT, env=env,
                              capture_output=True, timeout=30)

    def test_vietnamese_needs_utf8_on_a_cp1252_pipe(self):
        plain = self.run_python("print('Bản sao đã dừng')")
        self.assertNotEqual(plain.returncode, 0)          # what happened under Paoihub
        fixed = self.run_python("from paotwin import chay; chay.utf8_output(); print('Bản sao đã dừng')")
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        self.assertEqual(fixed.stdout.decode("utf-8").strip(), "Bản sao đã dừng")

    def test_the_pump_keeps_reading_when_printing_fails(self):
        # The parts write UTF-8 (the launcher sets PYTHONIOENCODING for them);
        # here the child writes the bytes itself, whatever its own pipe's code page.
        child = subprocess.Popen([sys.executable, "-c",
                                  "import sys; sys.stdout.buffer.write('một\\nhai\\n'.encode('utf-8'))"],
                                 stdout=subprocess.PIPE)
        log = io.StringIO()
        with mock.patch("builtins.print", side_effect=UnicodeEncodeError("cp1252", "đ", 0, 1, "x")):
            pump("thu", child, log)
        self.assertEqual(log.getvalue().split(), ["một", "hai"])
        self.assertEqual(child.returncode, 0)


if __name__ == "__main__":
    unittest.main()
