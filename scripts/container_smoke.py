"""Smoke-test built deployment images using only disposable Docker resources.

Run from the repository root after building the two :readiness image tags.
No source mounts, real credentials, hosted database, or model requests are used.
"""
import subprocess
import time
import uuid


BACKEND = "vibeledger-backend:readiness"
DASHBOARD = "vibeledger-dashboard:readiness"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, **kwargs)


def wait_for(*args):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", *args], text=True, capture_output=True,
        )
        if result.returncode == 0:
            print(result.stdout.strip(), flush=True)
            return
        time.sleep(2)
    raise RuntimeError(f"Timed out: {args}\n{result.stdout}\n{result.stderr}")


def main():
    prefix = "vl-readiness-" + uuid.uuid4().hex[:10]
    postgres, backend, dashboard = [prefix + suffix for suffix in ("-db", "-api", "-ui")]
    env = [
        "-e", "ENVIRONMENT=test",
        "-e", f"DATABASE_URL=postgresql://postgres:smoke@{postgres}:5432/vibeledger_ci",
        "-e", "DB_SCHEMA=vibeledger_test_container",
        "-e", "ENV_FILE_PATH=/nonexistent",
    ]
    try:
        docker("network", "create", prefix)
        docker("run", "-d", "--name", postgres, "--network", prefix,
               "-e", "POSTGRES_PASSWORD=smoke", "-e", "POSTGRES_DB=vibeledger_ci", "postgres:17")
        wait_for("exec", postgres, "pg_isready", "-h", "127.0.0.1",
                 "-U", "postgres", "-d", "vibeledger_ci")
        docker("run", "--rm", "--network", prefix, *env, BACKEND,
               "python", "-m", "migrations.runner")
        docker("run", "-d", "--name", backend, "--network", prefix, *env, BACKEND)
        for route, expected in (
            ("health", {"status": "ok", "service": "vibeledger-api", "version": "1.0.0"}),
            ("ready", {"status": "degraded", "database": "ok", "gemini": "unavailable"}),
        ):
            # Missing Gemini credentials are intentionally reported as degraded;
            # successful HTTP readiness must still verify the migrated database.
            code = (
                "import json, urllib.request; "
                f"data=json.load(urllib.request.urlopen('http://127.0.0.1:7860/{route}', timeout=5)); "
                f"assert data == {expected!r}, data; print(data)"
            )
            wait_for("exec", backend, "python", "-c", code)
        docker("run", "-d", "--name", dashboard, "--network", prefix, DASHBOARD)
        wait_for("exec", dashboard, "python", "-c",
                 "import urllib.request; "
                 "body=urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=5).read(); "
                 "assert body == b'ok', body; print('Streamlit health: ok')")
        # Streamlit's health endpoint does not execute app.py. Import all shipped
        # modules (including deferred page imports), then execute the real entry
        # point through a Streamlit session and verify that login renders.
        docker("exec", "-i", dashboard, "python", "-", input="""
import importlib
import os
from pathlib import Path
from streamlit.testing.v1 import AppTest
for module in sorted(Path('/app').glob('*.py')):
    if module.stem != 'app':
        importlib.import_module(module.stem)
os.environ['SUPABASE_URL'] = 'https://readiness.supabase.co'
os.environ['SUPABASE_PUBLISHABLE_KEY'] = 'sb_publishable_readiness'
app = AppTest.from_file('/app/app.py').run(timeout=30)
assert not app.exception, list(app.exception)
assert [field.key for field in app.text_input] == ['login_email', 'login_password']
assert not app.error, list(app.error)
print('Dashboard runtime imports and app execution: ok')
""")
        for container in (backend, dashboard):
            docker("exec", container, "python", "-c",
                   "import sys; assert sys.version_info[:2] == (3, 13); print(sys.version)")
            state = docker("inspect", "--format", "{{.State.Running}}", container, capture_output=True)
            assert state.stdout.strip() == "true", state.stdout
        print("Backend and Dashboard image build/start verification passed.", flush=True)
    except Exception:
        for container in (postgres, backend, dashboard):
            subprocess.run(["docker", "logs", "--tail", "80", container], check=False)
        raise
    finally:
        for container in (dashboard, backend, postgres):
            subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        subprocess.run(["docker", "network", "rm", prefix], check=False, capture_output=True)


if __name__ == "__main__":
    main()
