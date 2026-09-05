import os, subprocess, sys


def test_oauth_flow_matches_alexa_plus_requirements():
    """Runs the full two-tier OAuth flow in a separate process (auth is configured at import time)."""
    script = os.path.join(os.path.dirname(__file__), "oauth_flow_check.py")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, timeout=180, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert r.returncode == 0 and "OAUTH_FLOW_OK" in r.stdout, r.stdout[-2000:] + "\n" + r.stderr[-4000:]
