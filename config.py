"""Environment loading, shared by everything that can run standalone.

This used to live in fetch.py alone, which meant anything invoked directly -
`alerts.py fetch-failed` from run_and_publish.sh, `digest.py --force` - ran
without .env loaded and so silently lost NTFY_TOPIC. The fetch-failure alert in
particular is the one you most need to reach your phone, since by definition
nothing else in the pipeline is running to tell you.
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path = None):
    path = path or HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
