"""Launch the real entry point, detect early failure, then stop the check process."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
env = dict(os.environ)
env["QT_QPA_PLATFORM"] = "windows" if sys.platform == "win32" else "offscreen"
process = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=root, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    stdout, stderr = process.communicate(timeout=3)
except subprocess.TimeoutExpired:
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    if stderr:
        print(stderr.decode("utf-8", errors="replace"))
        raise SystemExit("Startup produced stderr; inspect before accepting")
    print("PASS: python main.py remained running for 3 seconds on the native platform, with no stderr.")
else:
    print(stdout.decode("utf-8", errors="replace"))
    print(stderr.decode("utf-8", errors="replace"))
    raise SystemExit(f"App exited before startup check completed: {process.returncode}")
