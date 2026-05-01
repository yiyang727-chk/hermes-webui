"""
Hermes Web UI -- Optional password authentication.
Off by default. Enable by setting HERMES_WEBUI_PASSWORD env var
or configuring a password in the Settings panel.
"""
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from api.config import STATE_DIR, load_settings

logger = logging.getLogger(__name__)

# ── Public paths (no auth required) ─────────────────────────────────────────
PUBLIC_PATHS = frozenset({
    '/login', '/health', '/favicon.ico',
    '/sw.js', '/manifest.json', '/manifest.webmanifest',
    '/api/auth/login', '/api/auth/status',
})

COOKIE_NAME = 'hermes_session'
SESSION_TTL = 86400  # 24 hours

_SESSIONS_FILE = STATE_DIR / '.sessions.json'
_tls = threading.local()


def _normalize_profile_list(values) -> list[str]:
    """Normalize a profile allowlist, preserving order and dropping invalid names."""
    try:
        from api.profiles import _PROFILE_ID_RE
    except Exception:
        _PROFILE_ID_RE = None
    out = []
    seen = set()
    for raw in values or []:
        name = str(raw or '').strip()
        if not name:
            continue
        if name != 'default' and _PROFILE_ID_RE is not None and not _PROFILE_ID_RE.fullmatch(name):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _normalize_workspace_list(values) -> list[str]:
    """Normalize a workspace allowlist to existing absolute paths."""
    out = []
    seen = set()
    for raw in values or []:
        text = str(raw or '').strip()
        if not text:
            continue
        try:
            normalized = str(Path(text).expanduser().resolve())
        except Exception:
            continue
        if not Path(normalized).is_dir():
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _normalize_user_record(raw: dict) -> dict | None:
    """Normalize one auth user record from settings.json."""
    if not isinstance(raw, dict):
        return None
    username = str(raw.get('username') or '').strip().lower()
    password_hash = str(raw.get('password_hash') or '').strip()
    if not username or not password_hash:
        return None
    allowed_profiles = _normalize_profile_list(raw.get('allowed_profiles') or [])
    if not allowed_profiles:
        allowed_profiles = ['default']
    default_profile = str(raw.get('default_profile') or '').strip()
    if default_profile not in allowed_profiles:
        default_profile = allowed_profiles[0]
    allowed_workspaces = _normalize_workspace_list(raw.get('allowed_workspaces') or [])
    default_workspace = str(raw.get('default_workspace') or '').strip()
    if default_workspace:
        normalized_default = _normalize_workspace_list([default_workspace])
        default_workspace = normalized_default[0] if normalized_default else ''
    if default_workspace and default_workspace not in allowed_workspaces:
        allowed_workspaces.append(default_workspace)
    if not default_workspace and allowed_workspaces:
        default_workspace = allowed_workspaces[0]
    return {
        'username': username,
        'password_hash': password_hash,
        'allowed_profiles': allowed_profiles,
        'default_profile': default_profile,
        'allowed_workspaces': allowed_workspaces,
        'default_workspace': default_workspace or None,
        'is_admin': bool(raw.get('is_admin', False)),
    }


def get_auth_users() -> list[dict]:
    """Return normalized account records from settings.json."""
    settings = load_settings()
    users = settings.get('auth_users')
    if not isinstance(users, list):
        return []
    normalized = []
    seen = set()
    for user in users:
        item = _normalize_user_record(user)
        if not item or item['username'] in seen:
            continue
        seen.add(item['username'])
        normalized.append(item)
    return normalized


def _find_auth_user(username: str | None) -> dict | None:
    target = str(username or '').strip().lower()
    if not target:
        return None
    for user in get_auth_users():
        if user['username'] == target:
            return user
    return None


def _load_sessions() -> dict[str, dict]:
    """Load persisted sessions from STATE_DIR, pruning expired entries.

    Returns an empty dict on any read or parse error so startup is never
    blocked by a corrupt or missing sessions file.
    """
    try:
        if _SESSIONS_FILE.exists():
            data = json.loads(_SESSIONS_FILE.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('malformed sessions file — expected dict')
            now = time.time()
            normalized = {}
            for token, value in data.items():
                if not isinstance(token, str):
                    continue
                if isinstance(value, (int, float)):
                    if value > now:
                        normalized[token] = {'exp': float(value)}
                    continue
                if not isinstance(value, dict):
                    continue
                exp = value.get('exp')
                if not isinstance(exp, (int, float)) or exp <= now:
                    continue
                normalized[token] = {
                    'exp': float(exp),
                    'username': str(value.get('username') or '').strip().lower() or None,
                    'allowed_profiles': _normalize_profile_list(value.get('allowed_profiles') or []),
                    'default_profile': str(value.get('default_profile') or '').strip() or None,
                    'allowed_workspaces': _normalize_workspace_list(value.get('allowed_workspaces') or []),
                    'default_workspace': str(value.get('default_workspace') or '').strip() or None,
                    'is_admin': bool(value.get('is_admin', False)),
                }
            return normalized
    except Exception as e:
        logger.debug("Failed to load sessions file, starting fresh: %s", e)
    return {}


def _save_sessions(sessions: dict[str, dict]) -> None:
    """Atomically persist sessions to STATE_DIR/.sessions.json (0600).

    Uses a temp file + os.replace() so a crash mid-write never leaves a
    truncated file.  Mirrors the same pattern as .signing_key persistence.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix='.sessions.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(sessions, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, _SESSIONS_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to persist sessions: %s", e)


# Active sessions: token -> expiry timestamp (persisted across restarts via STATE_DIR)
_sessions = _load_sessions()

# ── Login rate limiter ──────────────────────────────────────────────────────
_login_attempts = {}  # ip -> [timestamp, ...]
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW = 60  # seconds

def _check_login_rate(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Prune old attempts
    attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < _LOGIN_MAX_ATTEMPTS

def _record_login_attempt(ip: str) -> None:
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts.append(now)
    _login_attempts[ip] = attempts


def _signing_key():
    """Return a random signing key, generating and persisting one on first call."""
    key_file = STATE_DIR / '.signing_key'
    try:
        if key_file.exists():
            raw = key_file.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
    except Exception:
        logger.debug("Failed to read or access signing key file, using in-memory key")
    # Generate a new random key
    key = secrets.token_bytes(32)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        key_file.chmod(0o600)
    except Exception:
        logger.debug("Failed to persist signing key, using in-memory key only")
    return key


def _hash_password(password):
    """PBKDF2-SHA256 with 600k iterations (OWASP recommendation).
    Salt is the persisted random signing key, which is secret and unique per
    installation. This keeps the stored hash format a plain hex string
    (no format change to settings.json) while replacing the predictable
    STATE_DIR-derived salt from the original implementation."""
    salt = _signing_key()
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return dk.hex()


def get_password_hash() -> str | None:
    """Return the active password hash, or None if auth is disabled.
    Priority: env var > settings.json."""
    env_pw = os.getenv('HERMES_WEBUI_PASSWORD', '').strip()
    if env_pw:
        return _hash_password(env_pw)
    settings = load_settings()
    return settings.get('password_hash') or None


def is_auth_enabled() -> bool:
    """True if either legacy single-password auth or user auth is configured."""
    return bool(get_auth_users()) or get_password_hash() is not None


def verify_password(plain) -> bool:
    """Verify a plaintext password against the stored hash."""
    expected = get_password_hash()
    if not expected:
        return False
    return hmac.compare_digest(_hash_password(plain), expected)


def verify_login(username: str | None, password: str) -> dict | None:
    """Verify credentials and return normalized identity metadata on success."""
    users = get_auth_users()
    if users:
        user = _find_auth_user(username)
        if not user:
            return None
        if not hmac.compare_digest(_hash_password(password or ''), user['password_hash']):
            return None
        return {
            'username': user['username'],
            'allowed_profiles': list(user['allowed_profiles']),
            'default_profile': user['default_profile'],
            'allowed_workspaces': list(user.get('allowed_workspaces') or []),
            'default_workspace': user.get('default_workspace'),
            'is_admin': bool(user['is_admin']),
        }
    if verify_password(password):
        return {
            'username': None,
            'allowed_profiles': [],
            'default_profile': 'default',
            'allowed_workspaces': [],
            'default_workspace': None,
            'is_admin': True,
        }
    return None


def create_session(identity: dict | None = None) -> str:
    """Create a new auth session. Returns signed cookie value."""
    token = secrets.token_hex(32)
    identity = identity or {}
    if any(identity.get(k) for k in ('username', 'allowed_profiles', 'default_profile', 'allowed_workspaces', 'default_workspace')) or identity.get('is_admin'):
        _sessions[token] = {
            'exp': time.time() + SESSION_TTL,
            'username': str(identity.get('username') or '').strip().lower() or None,
            'allowed_profiles': _normalize_profile_list(identity.get('allowed_profiles') or []),
            'default_profile': str(identity.get('default_profile') or '').strip() or None,
            'allowed_workspaces': _normalize_workspace_list(identity.get('allowed_workspaces') or []),
            'default_workspace': str(identity.get('default_workspace') or '').strip() or None,
            'is_admin': bool(identity.get('is_admin', False)),
        }
    else:
        _sessions[token] = time.time() + SESSION_TTL
    _save_sessions(_sessions)
    sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{token}.{sig}"


def _prune_expired_sessions():
    """Remove all expired session entries to prevent unbounded memory growth."""
    now = time.time()
    expired = []
    for token, meta in _sessions.items():
        if isinstance(meta, (int, float)):
            if now > float(meta):
                expired.append(token)
            continue
        if now > float((meta or {}).get('exp', 0)):
            expired.append(token)
    if expired:
        for token in expired:
            _sessions.pop(token, None)
        _save_sessions(_sessions)


def get_session_record(cookie_value) -> dict | None:
    """Return the normalized session metadata for a signed cookie, or None."""
    if not cookie_value or '.' not in cookie_value:
        return None
    _prune_expired_sessions()
    token, sig = cookie_value.rsplit('.', 1)
    expected_sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        return None
    entry = _sessions.get(token)
    if isinstance(entry, (int, float)):
        if time.time() > float(entry):
            _sessions.pop(token, None)
            return None
        return {
            'token': token,
            'exp': float(entry),
            'username': None,
            'allowed_profiles': [],
            'default_profile': 'default',
            'allowed_workspaces': [],
            'default_workspace': None,
            'is_admin': True,
        }
    if not isinstance(entry, dict):
        return None
    exp = entry.get('exp')
    if not isinstance(exp, (int, float)) or time.time() > exp:
        _sessions.pop(token, None)
        return None
    allowed_profiles = _normalize_profile_list(entry.get('allowed_profiles') or [])
    default_profile = str(entry.get('default_profile') or '').strip() or (allowed_profiles[0] if allowed_profiles else 'default')
    allowed_workspaces = _normalize_workspace_list(entry.get('allowed_workspaces') or [])
    default_workspace = str(entry.get('default_workspace') or '').strip() or (allowed_workspaces[0] if allowed_workspaces else None)
    username = str(entry.get('username') or '').strip().lower() or None
    return {
        'token': token,
        'exp': float(exp),
        'username': username,
        'allowed_profiles': allowed_profiles,
        'default_profile': default_profile,
        'allowed_workspaces': allowed_workspaces,
        'default_workspace': default_workspace,
        'is_admin': bool(entry.get('is_admin', False) or not username),
    }


def verify_session(cookie_value) -> bool:
    """Verify a signed session cookie. Returns True if valid and not expired."""
    return get_session_record(cookie_value) is not None


def invalidate_session(cookie_value) -> None:
    """Remove a session token."""
    if cookie_value and '.' in cookie_value:
        token = cookie_value.rsplit('.', 1)[0]
        if token in _sessions:
            _sessions.pop(token, None)
            _save_sessions(_sessions)


def parse_cookie(handler) -> str | None:
    """Extract the auth cookie from the request headers."""
    cookie_header = handler.headers.get('Cookie', '')
    if not cookie_header:
        return None
    cookie = http.cookies.SimpleCookie()
    try:
        cookie.load(cookie_header)
    except http.cookies.CookieError:
        return None
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else None


def check_auth(handler, parsed) -> bool:
    """Check if request is authorized. Returns True if OK.
    If not authorized, sends 401 (API) or 302 redirect (page) and returns False."""
    if not is_auth_enabled():
        return True
    # Public paths don't require auth
    if parsed.path in PUBLIC_PATHS or parsed.path.startswith('/static/'):
        return True
    # Check session cookie
    cookie_val = parse_cookie(handler)
    record = get_session_record(cookie_val)
    if record:
        set_request_auth(record)
        return True
    # Not authorized
    if parsed.path.startswith('/api/'):
        handler.send_response(401)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"error":"Authentication required"}')
    else:
        handler.send_response(302)
        handler.send_header('Location', '/login')
        handler.end_headers()
    return False


def set_request_auth(record: dict | None) -> None:
    _tls.auth = record or None


def clear_request_auth() -> None:
    _tls.auth = None


def get_request_auth() -> dict | None:
    return getattr(_tls, 'auth', None)


def get_request_username() -> str | None:
    auth = get_request_auth()
    return (auth or {}).get('username')


def is_request_admin() -> bool:
    auth = get_request_auth()
    if auth is None:
        return True if not is_auth_enabled() else False
    return bool(auth.get('is_admin', False))


def can_access_profile(name: str | None, auth: dict | None = None) -> bool:
    profile = str(name or 'default').strip() or 'default'
    auth = get_request_auth() if auth is None else auth
    if auth is None:
        return True if not is_auth_enabled() else False
    if auth.get('is_admin'):
        return True
    return profile in set(auth.get('allowed_profiles') or [])


def can_access_workspace(path: str | None, auth: dict | None = None) -> bool:
    """Return True when the current user may use the workspace path."""
    auth = get_request_auth() if auth is None else auth
    if auth is None:
        return True if not is_auth_enabled() else False
    if auth.get('is_admin'):
        return True
    if not path:
        return False
    try:
        target = str(Path(path).expanduser().resolve())
    except Exception:
        target = str(path or '').strip()
    return target in set(_normalize_workspace_list(auth.get('allowed_workspaces') or []))


def get_request_default_workspace(auth: dict | None = None) -> str | None:
    """Return the request-scoped default workspace, if configured."""
    auth = get_request_auth() if auth is None else auth
    if auth is None:
        return None
    default_workspace = str(auth.get('default_workspace') or '').strip()
    if default_workspace:
        return default_workspace
    allowed = _normalize_workspace_list(auth.get('allowed_workspaces') or [])
    return allowed[0] if allowed else None


def set_auth_cookie(handler, cookie_value) -> None:
    """Set the auth cookie on the response."""
    cookie = http.cookies.SimpleCookie()
    cookie[COOKIE_NAME] = cookie_value
    cookie[COOKIE_NAME]['httponly'] = True
    cookie[COOKIE_NAME]['samesite'] = 'Lax'
    cookie[COOKIE_NAME]['path'] = '/'
    cookie[COOKIE_NAME]['max-age'] = str(SESSION_TTL)
    # Set Secure flag when connection is HTTPS
    if getattr(handler.request, 'getpeercert', None) is not None or handler.headers.get('X-Forwarded-Proto', '') == 'https':
        cookie[COOKIE_NAME]['secure'] = True
    handler.send_header('Set-Cookie', cookie[COOKIE_NAME].OutputString())


def clear_auth_cookie(handler) -> None:
    """Clear the auth cookie on the response."""
    cookie = http.cookies.SimpleCookie()
    cookie[COOKIE_NAME] = ''
    cookie[COOKIE_NAME]['httponly'] = True
    cookie[COOKIE_NAME]['path'] = '/'
    cookie[COOKIE_NAME]['max-age'] = '0'
    handler.send_header('Set-Cookie', cookie[COOKIE_NAME].OutputString())
