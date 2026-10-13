"""Export only reviewed project files to a NEW directory, without Git history."""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = [".gitignore", ".dockerignore", ".env.example", "Dockerfile", "compose.yaml",
         "dashboard_auth.py", "scripts/dashboard-password.py", "requirements.txt", "README.md", "start.sh", "live_quant_engine.py",
         "Generating_Keys/get_token.py", "web/index.html", "deploy/Caddyfile", "deploy/HETZNER.md",
         "scripts/setup-local.sh", "scripts/rebuild_sdk.py", "scripts/export-clean.py",
         "tests/test_auth.py", "tests/test_engine.py", "tests/test_oauth.py", "vendor/README.md",
         "vendor/ctrader_open_api-0.9.2+gimm.1-py3-none-any.whl"]


def main():
    destination = Path(sys.argv[1]).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in FILES:
        source = ROOT / name
        if source.is_symlink():
            raise SystemExit("Refusing symlink: " + name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print("Clean export:", destination)
    print("Review these files before initializing and pushing a new private repository.")


if __name__ == "__main__":
    main()
