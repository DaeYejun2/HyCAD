"""Create a local Grafana admin secret without replacing existing configuration."""
from pathlib import Path
import os
import secrets

path = Path(__file__).resolve().parents[1] / '.env'
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print('Existing .env retained.')
else:
    with os.fdopen(fd, 'w') as stream:
        stream.write('GRAFANA_ADMIN_PASSWORD=' + secrets.token_urlsafe(32) + '\n')
    print('Created .env. Local Grafana opens with Viewer access; admin secret is stored here.')
