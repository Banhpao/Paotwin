import subprocess
import sys
import time
import unittest

from paotwin.chay import _KillOnClose


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


if __name__ == "__main__":
    unittest.main()
