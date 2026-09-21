"""Generate the runtime contract; --check fails on checked-in drift."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import create_app

target = Path(__file__).resolve().parents[2] / "docs/api/openapi.json"
content = json.dumps(create_app().openapi(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
if "--check" in sys.argv:
    if target.read_text(encoding="utf-8") != content:
        raise SystemExit("OpenAPI drift: run scripts/export_openapi.py")
else:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
