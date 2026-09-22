"""Launch the real entry point from an isolated source copy, then stop the check."""
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile

root = Path(__file__).resolve().parent.parent
env = dict(os.environ)
env["QT_QPA_PLATFORM"] = os.environ.get("YANLIN_STARTUP_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
with tempfile.TemporaryDirectory(prefix='studio-startup-') as folder:
    checkout = Path(folder)
    # Override both normal AppData lookup and any inherited portable data root.
    env['YANLIN_CONFIG_DIR'] = str(checkout / 'data')
    # Copy source only so legacy migration cannot consume production credentials.
    shutil.copy2(root / 'main.py', checkout / 'main.py')
    for name in ('core', 'ui'):
        shutil.copytree(root / name, checkout / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    process = subprocess.Popen([sys.executable, '-u', 'main.py'], cwd=checkout, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        if stderr:
            print(stderr.decode('utf-8', errors='replace'))
            raise SystemExit('Startup produced stderr; inspect before accepting')
        print('PASS: python main.py ran for 3 seconds on the native platform with no stderr (isolated config).')
    else:
        print(stdout.decode('utf-8', errors='replace'))
        print(stderr.decode('utf-8', errors='replace'))
        raise SystemExit(f'App exited before startup check completed: {process.returncode}')
