#!/usr/bin/env python3
"""Create one least-privilege viewing account over loopback; never print secrets.

Run on the monitor host before enabling HTTPS sharing. Existing credentials are
verified and reused. An existing account without this credential file is not
overwritten or demoted. The private output is excluded from code backups.
"""
import argparse
import base64
import datetime
import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Refusing to send local credentials through a redirect')


def env_values(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip().strip('\"\'')
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('/etc/hycad/monitor.env'))
    parser.add_argument('--credentials-file', type=Path, default=Path('/etc/hycad/grafana-viewer.json'))
    args = parser.parse_args()
    os.umask(0o077)
    config = env_values(args.env_file)
    admin = config.get('GRAFANA_ADMIN_USER', 'admin')
    password = config['GRAFANA_ADMIN_PASSWORD']
    opener = urllib.request.build_opener(NoRedirect)

    def api(path, method='GET', data=None, account=None):
        user, secret = account or (admin, password)
        token = base64.b64encode((user + ':' + secret).encode()).decode()
        request = urllib.request.Request('http://127.0.0.1:3000' + path,
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={'Authorization':'Basic ' + token, 'Content-Type':'application/json'})
        with opener.open(request, timeout=15) as response:
            return json.load(response)

    path = args.credentials_file
    if path.is_symlink(): raise RuntimeError('Credential output cannot be a symlink')
    if path.exists():
        if path.stat().st_mode & 0o077: raise RuntimeError('Credential file must have mode 0600')
        credentials = json.loads(path.read_text())
    else:
        login = 'hycad-viewer'
        try:
            api('/api/users/lookup?' + urllib.parse.urlencode({'loginOrEmail':login}))
        except urllib.error.HTTPError as exc:
            if exc.code != 404: raise RuntimeError('Account lookup failed') from None
        else:
            raise RuntimeError('Account already exists; use its existing private credentials')
        credentials = {'username':login, 'password':secrets.token_urlsafe(24),
                       'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        user = api('/api/admin/users', 'POST', {'login':login, 'name':'HyCAD shared viewer',
                   'password':credentials['password'], 'OrgId':1})
        credentials['id'] = user['id']
        # Explicitly scope the new account to Viewer in the monitoring org.
        api('/api/org/users/' + str(user['id']), 'PATCH', {'role':'Viewer'})
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            json.dump(credentials, output, ensure_ascii=False, indent=2)
            output.flush(); os.fsync(output.fileno())
    account = (credentials['username'], credentials['password'])
    own = api('/api/user', account=account)
    orgs = api('/api/user/orgs', account=account)
    if own.get('isGrafanaAdmin') or not orgs or any(org['role'] != 'Viewer' for org in orgs):
        raise RuntimeError('Sharing account is not restricted to Viewer')
    api('/api/dashboards/uid/hycad-overview', account=account)
    try:
        api('/api/admin/settings', account=account)
    except urllib.error.HTTPError as exc:
        if exc.code != 403: raise RuntimeError('Unexpected admin-access result') from None
    else:
        raise RuntimeError('Viewer unexpectedly has administrator access')
    # Use the provisioned dashboard UID for the login/Home destination. A custom
    # home-dashboard JSON file can produce Grafana's synthetic default-home UID,
    # which does not resolve as a saved dashboard after browser navigation.
    api('/api/org/preferences', 'PATCH', {'homeDashboardUID': 'hycad-overview'})
    home = api('/api/dashboards/home', account=account)
    if not home.get('redirectUri', '').startswith('/d/hycad-overview/'):
        raise RuntimeError('Viewer home does not resolve to the provisioned dashboard')
    print(json.dumps({'username':credentials['username'], 'role':'Viewer',
                      'dashboard_read':True,'admin_access':False,
                      'home_dashboard':'hycad-overview','private_file':str(path)}))


if __name__ == '__main__':
    main()
