"""Generate a dashboard login hash without exposing the password in arguments."""
import getpass
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard_auth import password_hash

password = getpass.getpass('New dashboard password: ')
if len(password) < 14:
    raise SystemExit('Use at least 14 characters.')
if password != getpass.getpass('Confirm password: '):
    raise SystemExit('Passwords do not match.')
print("DASHBOARD_LOGIN_HASH='" + password_hash(password) + "'")
