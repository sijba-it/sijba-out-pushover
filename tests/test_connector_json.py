import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_connector_json_fields_are_read_by_main():
    spec = json.loads((ROOT / "connector.json").read_text())
    used = set(re.findall(r'environ\.get\("([A-Z0-9_]+)"', (ROOT / "main.py").read_text()))
    for f in spec["fields"]:
        assert f["name"] in used, f["name"]
    assert spec["port"] == 9101
