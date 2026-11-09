"""Clipboard transports can keep stdio alive after their initial process exits."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'copy_body.py'


class CopyBodyTests(unittest.TestCase):
    def test_daemon_inheriting_output_does_not_block_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft = root / 'draft.md'
            draft.write_text('---\ntitle: Draft\n---\n\nBody text.\n')
            transport = root / 'clipboard.py'
            output = root / 'clipboard'
            pidfile = root / 'pid'
            transport.write_text(
                "import subprocess, sys\n"
                "from pathlib import Path\n"
                f"Path({str(output)!r}).write_text(sys.stdin.read())\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
            )
            try:
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), str(draft), '--command',
                     shlex.join([sys.executable, str(transport)])],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(), 'Body text.\n')
                self.assertIn('copied the body', result.stderr)
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_failed_transport_retains_bounded_diagnostics_and_body(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / 'draft.md'
            draft.write_text('Body text.\n')
            command = shlex.join([
                sys.executable, '-c',
                "import sys; sys.stdin.read(); sys.stderr.write('failure ' + 'x' * 10000); sys.exit(7)",
            ])
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(draft), '--command', command],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, 'Body text.\n')
            self.assertIn('exited with 7: failure ', result.stderr)
            self.assertLess(len(result.stderr), 5000)


if __name__ == '__main__':
    unittest.main()
