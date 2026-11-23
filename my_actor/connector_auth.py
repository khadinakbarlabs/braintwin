"""First-party Apify device login for installed connectors. Tokens stay in the OS keyring."""
from __future__ import annotations

import json
import re
import sys
import time
import webbrowser
from urllib.parse import urlsplit

import httpx

from .connections import SetupError, locked, put_file, state_dir
from .signin_health import clear_signin_problem, mark_signin_problem, refresh_lock, refused

CLIENT_ID = 'https://braintwin.dev/connector-client.json'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
ISSUER = 'https://console-backend.apify.com/oauth/apps'
SERVICE = 'dev.braintwin.connector'


def _owner(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise SetupError('Apify did not confirm the account identity.')
    return value


def _check_owner(http, token):
    res = http.get('https://api.apify.com/v2/users/me', headers={'Authorization': f'Bearer {token}'})
    if not res.is_success:
        raise SetupError('Apify could not confirm this account. Sign in again.')
    return _owner(res.json().get('data', {}).get('id'))


def _credentials(data, old_refresh=''):
    if not isinstance(data.get('access_token'), str) or not data['access_token']:
        raise SetupError('Apify did not issue an access token.')
    return {'access_token': data['access_token'], 'refresh_token': data.get('refresh_token') or old_refresh,
            'expires_at': time.time() + int(data.get('expires_in') or 3600)}


def _open(url, code):
    print(f'Authorize Braintwin in Apify. Check code {code}.\n{url}', file=sys.stderr, flush=True)
    webbrowser.open(url)


def device_login(http, *, show=_open, wait=time.sleep):
    response = http.post(ISSUER + '/devices', data={'client_id': CLIENT_ID, 'scope': 'full_api_access'})
    if not response.is_success:
        raise SetupError('Apify could not start sign-in. Your agent configuration has been preserved.')
    data = response.json()
    url = data.get('verification_uri_complete') or data.get('verification_uri') or ''
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in {'apify.com', 'console.apify.com'} or not data.get('device_code') or not data.get('user_code'):
        raise SetupError('Apify returned an unrecognized device approval page.')
    interval = max(1, int(data.get('interval', 5)))
    until = time.monotonic() + min(1800, int(data.get('expires_in', 600)))
    show(url, data['user_code'])
    while time.monotonic() + interval < until:
        wait(interval)
        res = http.post(ISSUER + '/token', data={'grant_type': DEVICE_GRANT, 'client_id': CLIENT_ID, 'device_code': data['device_code']})
        body = res.json()
        if res.is_success:
            creds = _credentials(body)
            return _check_owner(http, creds['access_token']), creds
        if body.get('error') == 'slow_down':
            interval += 5
        elif body.get('error') != 'authorization_pending':
            raise SetupError('Apify sign-in was declined or expired. Run the setup again to connect.')
    raise SetupError('Apify sign-in expired. Run the setup again to connect.')


def _vault():
    # Use an actual OS credential store even if a plaintext third-party backend is installed.
    try:
        if sys.platform == 'darwin':
            from keyring.backends.macOS import Keyring
        elif sys.platform.startswith('linux'):
            from keyring.backends.SecretService import Keyring
        else:
            raise SetupError('Use macOS Keychain or a Linux desktop with Secret Service for this connection.')
        vault = Keyring()
        if vault.priority <= 0:
            raise RuntimeError
        return vault
    except Exception as exc:
        raise SetupError('Unlock your system keyring to use Apify, or choose local storage.') from exc


def _client():
    return httpx.Client(timeout=12, follow_redirects=False)


def _stored(vault, account):
    raw = vault.get_password(SERVICE, account)
    return json.loads(raw) if raw else None


def _usable(creds):
    return bool(creds) and creds.get('expires_at', 0) > time.time() + 90


def token_for(account):
    account = _owner(account)
    # The shared lock covers every Braintwin folder of this computer user, because they all use the
    # same keyring entry. A second renewal of an already used key can make Apify cancel the sign-in.
    with locked('account'), refresh_lock(account):
        vault = _vault()
        creds = _stored(vault, account)
        if not creds:
            mark_signin_problem(account, 'missing')
            raise SetupError('Sign in to Apify again with the connector setup command.')
        if _usable(creds):
            clear_signin_problem(account)
            return creds['access_token']
        if not creds.get('refresh_token'):
            mark_signin_problem(account, 'expired')
            raise SetupError('The Apify connection expired. Run the connector setup again.')
        with _client() as http:
            res = http.post(ISSUER + '/token', data={'client_id': CLIENT_ID, 'grant_type':'refresh_token', 'refresh_token':creds['refresh_token']})
            if not res.is_success:
                latest = _stored(vault, account)
                if _usable(latest) and latest.get('refresh_token') != creds['refresh_token']:
                    clear_signin_problem(account)  # an older copy without the shared lock renewed it
                    return latest['access_token']
                if refused(res):
                    mark_signin_problem(account, 'expired')
                raise SetupError('Apify sign-in expired. Reconnect this agent.')
            fresh = _credentials(res.json(), creds['refresh_token'])
            if _check_owner(http, fresh['access_token']) != account:
                raise SetupError('The refreshed authorization belongs to a different account.')
        vault.set_password(SERVICE, account, json.dumps(fresh))
        clear_signin_problem(account)
        return fresh['access_token']


def authorize(expected=None):
    current = state_dir() / 'account'
    if not expected and current.exists():
        expected = _owner(current.read_text().strip())
    if expected:
        try:
            cached = token_for(expected)
            with _client() as http:
                if _check_owner(http, cached) == expected:
                    return expected
        except SetupError:
            pass
    vault = _vault()
    with _client() as http:
        owner, creds = device_login(http)
    if expected and owner != expected:
        raise SetupError('This connection belongs to a different Apify account. Disconnect and sign out before switching accounts.')
    with locked('account'):
        vault.set_password(SERVICE, owner, json.dumps(creds))
        put_file(current, owner.encode())
    clear_signin_problem(owner)
    return owner


def logout():
    current = state_dir() / 'account'
    with locked('account'):
        if current.exists():
            owner = _owner(current.read_text().strip())
            vault = _vault()
            if vault.get_password(SERVICE, owner):
                vault.delete_password(SERVICE, owner)
            current.unlink()
            clear_signin_problem(owner)
