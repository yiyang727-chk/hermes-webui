"""
Hermes Web UI -- Profile state management.
Wraps hermes_cli.profiles to provide profile switching for the web UI.

The web UI maintains a process-level "active profile" that determines which
HERMES_HOME directory is used for config, skills, memory, cron, and API keys.
Profile switches update os.environ['HERMES_HOME'] and monkey-patch module-level
cached paths in hermes-agent modules (skills_tool, cron/jobs) that snapshot
HERMES_HOME at import time.
"""
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ── Constants (match hermes_cli.profiles upstream) ─────────────────────────
_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_PROFILE_DIRS = [
    'memories', 'sessions', 'skills', 'skins',
    'logs', 'plans', 'workspace', 'cron',
]
_CLONE_CONFIG_FILES = ['config.yaml', '.env', 'SOUL.md']

# ── Module state ────────────────────────────────────────────────────────────
_active_profile = 'default'
_profile_lock = threading.Lock()
_gateway_status_lock = threading.Lock()
_loaded_profile_env_keys: set[str] = set()
_channel_qr_sessions: dict[str, dict] = {}
_channel_qr_sessions_lock = threading.Lock()

# Thread-local profile context: set per-request by server.py, cleared after.
# Enables per-client profile isolation (issue #798) — each HTTP request thread
# reads its own profile from the hermes_profile cookie instead of the
# process-global _active_profile.
_tls = threading.local()

def _resolve_base_hermes_home() -> Path:
    """Return the BASE ~/.hermes directory — the root that contains profiles/.

    This is intentionally distinct from HERMES_HOME, which tracks the *active
    profile's* home and changes on every profile switch.  The base dir must
    always point to the top-level .hermes regardless of which profile is active.

    Resolution order:
      1. HERMES_BASE_HOME env var (set explicitly, highest priority)
      2. HERMES_HOME env var — but only if it does NOT look like a profile subdir
         (i.e. its parent is not named 'profiles').  This handles test isolation
         where HERMES_HOME is set to an isolated test state dir.
      3. ~/.hermes (always-correct default)

    The bug this prevents: if HERMES_HOME has already been mutated to
    /home/user/.hermes/profiles/webui (by init_profile_state at startup),
    reading it here would make _DEFAULT_HERMES_HOME point to that subdir,
    causing switch_profile('webui') to look for
    /home/user/.hermes/profiles/webui/profiles/webui — which doesn't exist.
    """
    # Explicit override for tests or unusual setups
    base_override = os.getenv('HERMES_BASE_HOME', '').strip()
    if base_override:
        return Path(base_override).expanduser()

    hermes_home = os.getenv('HERMES_HOME', '').strip()
    if hermes_home:
        p = Path(hermes_home).expanduser()
        # If HERMES_HOME points to a profiles/ subdir, walk up two levels to the base
        if p.parent.name == 'profiles':
            return p.parent.parent
        # Otherwise trust it (e.g. test isolation sets HERMES_HOME to TEST_STATE_DIR)
        return p

    return Path.home() / '.hermes'

_DEFAULT_HERMES_HOME = _resolve_base_hermes_home()


def _read_active_profile_file() -> str:
    """Read the sticky active profile from ~/.hermes/active_profile."""
    ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
    if ap_file.exists():
        try:
            name = ap_file.read_text(encoding="utf-8").strip()
            if name:
                return name
        except Exception:
            logger.debug("Failed to read active profile file")
    return 'default'


# ── Public API ──────────────────────────────────────────────────────────────

def get_active_profile_name() -> str:
    """Return the currently active profile name.

    Priority:
      1. Thread-local (set per-request from hermes_profile cookie) — issue #798
      2. Process-level default (_active_profile)
    """
    tls_name = getattr(_tls, 'profile', None)
    if tls_name is not None:
        return tls_name
    return _active_profile


def set_request_profile(name: str) -> None:
    """Set the per-request profile context for this thread.

    Called by server.py at the start of each request when a hermes_profile
    cookie is present.  Always paired with clear_request_profile() in a
    finally block so the thread-local is released after the request.
    """
    _tls.profile = name


def clear_request_profile() -> None:
    """Clear the per-request profile context for this thread.

    Called by server.py in the finally block of do_GET / do_POST.
    Safe to call even if set_request_profile() was never called.
    """
    _tls.profile = None


def get_active_hermes_home() -> Path:
    """Return the HERMES_HOME path for the currently active profile.

    Uses get_active_profile_name() so per-request TLS context (issue #798)
    is respected, not just the process-level global.
    """
    name = get_active_profile_name()
    if name == 'default':
        return _DEFAULT_HERMES_HOME
    profile_dir = _DEFAULT_HERMES_HOME / 'profiles' / name
    if profile_dir.is_dir():
        return profile_dir
    return _DEFAULT_HERMES_HOME



def get_hermes_home_for_profile(name: str) -> Path:
    """Return the HERMES_HOME Path for *name* without mutating any process state.

    Safe to call from per-request context (streaming, session creation) because
    it reads only the filesystem — it never touches os.environ, module-level
    cached paths, or the process-level _active_profile global.

    Falls back to _DEFAULT_HERMES_HOME (same as 'default') when *name* is None,
    empty, 'default', or does not match the profile-name format (rejects path
    traversal such as '../../etc').
    """
    if not name or name == 'default' or not _PROFILE_ID_RE.fullmatch(name):
        return _DEFAULT_HERMES_HOME
    profile_dir = _DEFAULT_HERMES_HOME / 'profiles' / name
    return profile_dir


_TERMINAL_ENV_MAPPINGS = {
    'backend': 'TERMINAL_ENV',
    'env_type': 'TERMINAL_ENV',
    'cwd': 'TERMINAL_CWD',
    'timeout': 'TERMINAL_TIMEOUT',
    'lifetime_seconds': 'TERMINAL_LIFETIME_SECONDS',
    'modal_mode': 'TERMINAL_MODAL_MODE',
    'docker_image': 'TERMINAL_DOCKER_IMAGE',
    'docker_forward_env': 'TERMINAL_DOCKER_FORWARD_ENV',
    'docker_env': 'TERMINAL_DOCKER_ENV',
    'docker_mount_cwd_to_workspace': 'TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE',
    'singularity_image': 'TERMINAL_SINGULARITY_IMAGE',
    'modal_image': 'TERMINAL_MODAL_IMAGE',
    'daytona_image': 'TERMINAL_DAYTONA_IMAGE',
    'container_cpu': 'TERMINAL_CONTAINER_CPU',
    'container_memory': 'TERMINAL_CONTAINER_MEMORY',
    'container_disk': 'TERMINAL_CONTAINER_DISK',
    'container_persistent': 'TERMINAL_CONTAINER_PERSISTENT',
    'docker_volumes': 'TERMINAL_DOCKER_VOLUMES',
    'persistent_shell': 'TERMINAL_PERSISTENT_SHELL',
    'ssh_host': 'TERMINAL_SSH_HOST',
    'ssh_user': 'TERMINAL_SSH_USER',
    'ssh_port': 'TERMINAL_SSH_PORT',
    'ssh_key': 'TERMINAL_SSH_KEY',
    'ssh_persistent': 'TERMINAL_SSH_PERSISTENT',
    'local_persistent': 'TERMINAL_LOCAL_PERSISTENT',
}


def _stringify_env_value(value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def get_profile_runtime_env(home: Path) -> dict[str, str]:
    """Return env vars needed to run an agent turn for a profile home.

    WebUI profile switching is per-client/cookie scoped, so it intentionally
    does not call ``switch_profile(..., process_wide=True)`` for every browser.
    Agent/tool code still consumes terminal backend settings through
    environment variables (matching ``hermes -p <profile>``), so streaming must
    apply the selected profile's terminal config and ``.env`` for the duration
    of that run.
    """
    home = Path(home).expanduser()
    env: dict[str, str] = {}

    try:
        import yaml as _yaml

        cfg_path = home / 'config.yaml'
        cfg = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = {}

    terminal_cfg = cfg.get('terminal', {}) if isinstance(cfg, dict) else {}
    if isinstance(terminal_cfg, dict):
        for key, env_key in _TERMINAL_ENV_MAPPINGS.items():
            if key in terminal_cfg and terminal_cfg[key] is not None:
                env[env_key] = _stringify_env_value(terminal_cfg[key])

    env_path = home / '.env'
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and v:
                        env[k] = v
        except Exception:
            logger.debug("Failed to read runtime env from %s", env_path)

    return env


def _set_hermes_home(home: Path):
    """Set HERMES_HOME env var and monkey-patch cached module-level paths."""
    os.environ['HERMES_HOME'] = str(home)

    # Patch skills_tool module-level cache (snapshots HERMES_HOME at import)
    try:
        import tools.skills_tool as _sk
        _sk.HERMES_HOME = home
        _sk.SKILLS_DIR = home / 'skills'
    except (ImportError, AttributeError):
        logger.debug("Failed to patch skills_tool module")

    # Patch cron/jobs module-level cache
    try:
        import cron.jobs as _cj
        _cj.HERMES_DIR = home
        _cj.CRON_DIR = home / 'cron'
        _cj.JOBS_FILE = _cj.CRON_DIR / 'jobs.json'
        _cj.OUTPUT_DIR = _cj.CRON_DIR / 'output'
    except (ImportError, AttributeError):
        logger.debug("Failed to patch cron.jobs module")


def _reload_dotenv(home: Path):
    """Load .env from the profile dir into os.environ with profile isolation.

    Clears env vars that were loaded from the previously active profile before
    applying the current profile's .env. This prevents API keys and other
    profile-scoped secrets from leaking across profile switches.
    """
    global _loaded_profile_env_keys

    # Remove keys loaded from the previous profile first.
    for key in list(_loaded_profile_env_keys):
        os.environ.pop(key, None)
    _loaded_profile_env_keys = set()

    env_path = home / '.env'
    if not env_path.exists():
        return
    try:
        loaded_keys: set[str] = set()
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and v:
                    os.environ[k] = v
                    loaded_keys.add(k)
        _loaded_profile_env_keys = loaded_keys
    except Exception:
        _loaded_profile_env_keys = set()
        logger.debug("Failed to reload dotenv from %s", env_path)


def init_profile_state() -> None:
    """Initialize profile state at server startup.

    Reads ~/.hermes/active_profile, sets HERMES_HOME env var, patches
    module-level cached paths.  Called once from config.py after imports.
    """
    global _active_profile
    _active_profile = _read_active_profile_file()
    home = get_active_hermes_home()
    _set_hermes_home(home)
    _reload_dotenv(home)


def switch_profile(name: str, *, process_wide: bool = True) -> dict:
    """Switch the active profile.

    Validates the profile exists, updates process state, patches module caches,
    reloads .env, and reloads config.yaml.

    Args:
        name: Profile name to switch to.
        process_wide: If True (default), updates the process-global
            _active_profile.  Set to False for per-client switches from the
            WebUI where the profile is managed via cookie + thread-local (#798).

    Returns: {'profiles': [...], 'active': name}
    Raises ValueError if profile doesn't exist or agent is busy.
    """
    global _active_profile

    # Import here to avoid circular import at module load
    from api.config import STREAMS, STREAMS_LOCK, reload_config

    # Block if agent is running
    with STREAMS_LOCK:
        if len(STREAMS) > 0:
            raise RuntimeError(
                'Cannot switch profiles while an agent is running. '
                'Cancel or wait for it to finish.'
            )

    # Resolve profile directory
    if name == 'default':
        home = _DEFAULT_HERMES_HOME
    else:
        home = _resolve_named_profile_home(name)
        if not home.is_dir():
            raise ValueError(f"Profile '{name}' does not exist.")

    with _profile_lock:
        if process_wide:
            global _active_profile
            _active_profile = name
            _set_hermes_home(home)
            _reload_dotenv(home)

    if process_wide:
        # Write sticky default for CLI consistency
        try:
            ap_file = _DEFAULT_HERMES_HOME / 'active_profile'
            ap_file.write_text(name if name != 'default' else '', encoding='utf-8')
        except Exception:
            logger.debug("Failed to write active profile file")

        # Reload config.yaml from the new profile
        reload_config()

    # Return profile-specific defaults so frontend can apply them.
    # For process_wide=False (per-client switch), read the target profile's
    # config.yaml directly from disk rather than from _cfg_cache (process-global),
    # since reload_config() was intentionally skipped.
    if process_wide:
        from api.config import get_config
        cfg = get_config()
    else:
        # Direct disk read — does not touch _cfg_cache
        try:
            import yaml as _yaml
            cfg_path = home / 'config.yaml'
            cfg = _yaml.safe_load(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
    model_cfg = cfg.get('model', {})
    default_model = None
    if isinstance(model_cfg, str):
        default_model = model_cfg
    elif isinstance(model_cfg, dict):
        default_model = model_cfg.get('default')

    # Read the target profile's workspace directly from *home* rather than via
    # get_last_workspace() which routes through the thread-local/process-global active
    # profile — both of which still point to the OLD profile during process_wide=False
    # switches (the Set-Cookie has been sent but hasn't been processed by a new request
    # yet).  We derive workspace in priority order:
    #   1. {home}/webui_state/last_workspace.txt  (previously chosen workspace for this profile)
    #   2. cfg terminal.cwd / workspace / default_workspace keys
    #   3. Boot-time DEFAULT_WORKSPACE constant
    # Use the module-level ``Path`` (imported at line 17) rather than re-importing
    # it locally — keeps the exception fallback simple and avoids a latent NameError
    # if a future refactor moves the inner imports.
    default_workspace = None
    try:
        from api.config import DEFAULT_WORKSPACE as _DW
        lw_file = home / 'webui_state' / 'last_workspace.txt'
        if lw_file.exists():
            _p = lw_file.read_text(encoding='utf-8').strip()
            if _p:
                _pp = Path(_p).expanduser()
                if _pp.is_dir():
                    default_workspace = str(_pp.resolve())
        if default_workspace is None:
            for _key in ('workspace', 'default_workspace'):
                _v = cfg.get(_key)
                if _v:
                    _pp = Path(str(_v)).expanduser().resolve()
                    if _pp.is_dir():
                        default_workspace = str(_pp)
                        break
        if default_workspace is None:
            _tc = cfg.get('terminal', {})
            if isinstance(_tc, dict):
                _cwd = _tc.get('cwd', '')
                if _cwd and str(_cwd) not in ('.', ''):
                    _pp = Path(str(_cwd)).expanduser().resolve()
                    if _pp.is_dir():
                        default_workspace = str(_pp)
        if default_workspace is None:
            default_workspace = str(_DW)
    except Exception:
        try:
            from api.config import DEFAULT_WORKSPACE as _DW2
            default_workspace = str(_DW2)
        except Exception:
            default_workspace = str(Path.home())

    return {
        'profiles': list_profiles_api(active_name=name),
        'active': name,
        'default_model': default_model,
        'default_workspace': default_workspace,
    }


def list_profiles_api(active_name: str | None = None) -> list:
    """List all profiles with metadata, serialized for JSON response."""
    try:
        from api.auth import can_access_profile, is_request_admin
        _is_admin = is_request_admin()
    except Exception:
        _is_admin = True
        can_access_profile = lambda name: True
    try:
        from hermes_cli.profiles import list_profiles
        infos = list_profiles()
    except ImportError:
        # hermes_cli not available -- return just the default
        fallback = [_default_profile_dict()]
        return fallback if _is_admin else [p for p in fallback if can_access_profile(p.get('name'))]

    active = active_name or get_active_profile_name()
    result = []
    for p in infos:
        if not _is_admin and not can_access_profile(p.name):
            continue
        gateway_running = _detect_gateway_running(Path(p.path), fallback=bool(getattr(p, "gateway_running", False)))
        result.append({
            'name': p.name,
            'path': str(p.path),
            'is_default': p.is_default,
            'is_active': p.name == active,
            'gateway_running': gateway_running,
            'model': p.model,
            'provider': p.provider,
            'has_env': p.has_env,
            'skill_count': p.skill_count,
        })
    return result


def _detect_gateway_running(profile_dir: Path, fallback: bool = False) -> bool:
    """Detect gateway runtime status using the same signals Hermes CLI status uses.

    Older WebUI code only trusted ``<profile>/gateway.pid`` via
    ``hermes_cli.profiles.list_profiles()``, which misses service-managed
    gateways on macOS/Linux.  Hermes CLI status uses launchctl/systemd and
    manual-process detection instead, so mirror that here.
    """
    profile_dir = Path(profile_dir).expanduser().resolve()
    old_home = os.environ.get("HERMES_HOME")

    with _gateway_status_lock:
        try:
            os.environ["HERMES_HOME"] = str(profile_dir)
            from hermes_cli.gateway import (
                find_gateway_pids,
                get_launchd_label,
                get_service_name,
                supports_systemd_services,
            )

            if sys.platform == "darwin":
                try:
                    result = subprocess.run(
                        ["launchctl", "list", get_launchd_label()],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if result.returncode == 0:
                        return True
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                    logger.debug("launchctl gateway status probe failed", exc_info=True)
                return bool(find_gateway_pids())

            if sys.platform.startswith("linux"):
                try:
                    if supports_systemd_services():
                        result = subprocess.run(
                            ["systemctl", "--user", "is-active", get_service_name()],
                            capture_output=True,
                            text=True,
                            timeout=5,
                            check=False,
                        )
                        if result.stdout.strip() == "active":
                            return True
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                    logger.debug("systemd gateway status probe failed", exc_info=True)
                return bool(find_gateway_pids())

            return bool(find_gateway_pids())
        except Exception:
            logger.debug("Gateway status detection failed for %s", profile_dir, exc_info=True)
            return fallback
        finally:
            if old_home is not None:
                os.environ["HERMES_HOME"] = old_home
            else:
                os.environ.pop("HERMES_HOME", None)


def _default_profile_dict() -> dict:
    """Fallback profile dict when hermes_cli is not importable."""
    return {
        'name': 'default',
        'path': str(_DEFAULT_HERMES_HOME),
        'is_default': True,
        'is_active': True,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'has_env': (_DEFAULT_HERMES_HOME / '.env').exists(),
        'skill_count': 0,
    }


def _validate_profile_name(name: str):
    """Validate profile name format (matches hermes_cli.profiles upstream)."""
    if name == 'default':
        raise ValueError("Cannot create a profile named 'default' -- it is the built-in profile.")
    # Use fullmatch (not match) so a trailing newline can't sneak past the $ anchor
    if not _PROFILE_ID_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. "
            "Must match [a-z0-9][a-z0-9_-]{0,63}"
        )


def _profiles_root() -> Path:
    """Return the canonical root that contains named profiles."""
    return (_DEFAULT_HERMES_HOME / 'profiles').resolve()


def _resolve_named_profile_home(name: str) -> Path:
    """Resolve a named profile to a directory under the profiles root.

    Validates *name* as a logical profile identifier first, then resolves the
    final filesystem path and enforces containment under ~/.hermes/profiles.
    """
    _validate_profile_name(name)
    profiles_root = _profiles_root()
    candidate = (profiles_root / name).resolve()
    candidate.relative_to(profiles_root)
    return candidate


def _create_profile_fallback(name: str, clone_from: str = None,
                              clone_config: bool = False) -> Path:
    """Create a profile directory without hermes_cli (Docker/standalone fallback)."""
    profile_dir = _DEFAULT_HERMES_HOME / 'profiles' / name
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{name}' already exists.")

    # Bootstrap directory structure (exist_ok=False so a concurrent create raises)
    profile_dir.mkdir(parents=True, exist_ok=False)
    for subdir in _PROFILE_DIRS:
        (profile_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Clone config files from source profile if requested
    if clone_config and clone_from:
        if clone_from == 'default':
            source_dir = _DEFAULT_HERMES_HOME
        else:
            source_dir = _DEFAULT_HERMES_HOME / 'profiles' / clone_from
        if source_dir.is_dir():
            for filename in _CLONE_CONFIG_FILES:
                src = source_dir / filename
                if src.exists():
                    shutil.copy2(src, profile_dir / filename)

    return profile_dir


def _copy_profile_skills(source_dir: Path, target_dir: Path) -> None:
    """Copy the source profile's skills/ contents into the target profile."""
    src_skills = source_dir / 'skills'
    dst_skills = target_dir / 'skills'
    if not src_skills.is_dir():
        return
    dst_skills.mkdir(parents=True, exist_ok=True)
    for entry in src_skills.iterdir():
        dst = dst_skills / entry.name
        if entry.is_dir():
            shutil.copytree(entry, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, dst)


def _write_endpoint_to_config(profile_dir: Path, base_url: str = None, api_key: str = None) -> None:
    """Write custom endpoint fields into config.yaml for a profile."""
    if not base_url and not api_key:
        return
    config_path = profile_dir / 'config.yaml'
    try:
        import yaml as _yaml
    except ImportError:
        return
    cfg = {}
    if config_path.exists():
        try:
            loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            logger.debug("Failed to load config from %s", config_path)
    model_section = cfg.get('model', {})
    if not isinstance(model_section, dict):
        model_section = {}
    if base_url:
        model_section['base_url'] = base_url
    if api_key:
        model_section['api_key'] = api_key
    cfg['model'] = model_section
    config_path.write_text(_yaml.dump(cfg, default_flow_style=False, allow_unicode=True), encoding='utf-8')


_ENV_ASSIGN_RE = re.compile(r'^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')

_PROFILE_CHANNEL_SPECS = {
    "weixin": {
        "label": "Weixin / WeChat",
        "supports_qr": True,
        "secret_fields": {"token"},
        "env_keys": {
            "account_id": "WEIXIN_ACCOUNT_ID",
            "token": "WEIXIN_TOKEN",
            "base_url": "WEIXIN_BASE_URL",
            "cdn_base_url": "WEIXIN_CDN_BASE_URL",
            "dm_policy": "WEIXIN_DM_POLICY",
            "allowed_users": "WEIXIN_ALLOWED_USERS",
            "group_policy": "WEIXIN_GROUP_POLICY",
            "group_allowed_users": "WEIXIN_GROUP_ALLOWED_USERS",
            "home_channel": "WEIXIN_HOME_CHANNEL",
        },
        "all_env_keys": [
            "WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN", "WEIXIN_BASE_URL", "WEIXIN_CDN_BASE_URL",
            "WEIXIN_DM_POLICY", "WEIXIN_ALLOWED_USERS", "WEIXIN_GROUP_POLICY",
            "WEIXIN_GROUP_ALLOWED_USERS", "WEIXIN_HOME_CHANNEL", "WEIXIN_HOME_CHANNEL_NAME",
            "WEIXIN_ALLOW_ALL_USERS",
        ],
    },
    "qqbot": {
        "label": "QQ Bot",
        "supports_qr": False,
        "secret_fields": {"client_secret"},
        "env_keys": {
            "app_id": "QQ_APP_ID",
            "client_secret": "QQ_CLIENT_SECRET",
            "allowed_users": "QQ_ALLOWED_USERS",
            "group_policy": "QQ_GROUP_POLICY",
            "group_allowed_users": "QQ_GROUP_ALLOWED_USERS",
            "home_channel": "QQ_HOME_CHANNEL",
        },
        "all_env_keys": [
            "QQ_APP_ID", "QQ_CLIENT_SECRET", "QQ_ALLOWED_USERS",
            "QQ_GROUP_POLICY", "QQ_GROUP_ALLOWED_USERS", "QQ_HOME_CHANNEL", "QQ_HOME_CHANNEL_NAME",
        ],
        "note": "Hermes 当前通过 QQ 官方 Bot App ID / Secret 接入，不支持扫码登录。",
    },
    "wecom": {
        "label": "WeCom",
        "supports_qr": False,
        "secret_fields": {"secret"},
        "env_keys": {
            "bot_id": "WECOM_BOT_ID",
            "secret": "WECOM_SECRET",
            "websocket_url": "WECOM_WEBSOCKET_URL",
            "dm_policy": "WECOM_DM_POLICY",
            "allowed_users": "WECOM_ALLOWED_USERS",
            "group_policy": "WECOM_GROUP_POLICY",
            "group_allowed_users": "WECOM_GROUP_ALLOWED_USERS",
            "home_channel": "WECOM_HOME_CHANNEL",
        },
        "all_env_keys": [
            "WECOM_BOT_ID", "WECOM_SECRET", "WECOM_WEBSOCKET_URL", "WECOM_DM_POLICY",
            "WECOM_ALLOWED_USERS", "WECOM_GROUP_POLICY", "WECOM_GROUP_ALLOWED_USERS",
            "WECOM_HOME_CHANNEL", "WECOM_HOME_CHANNEL_NAME",
        ],
    },
    "feishu": {
        "label": "Feishu / Lark",
        "supports_qr": True,
        "secret_fields": {"app_secret"},
        "env_keys": {
            "app_id": "FEISHU_APP_ID",
            "app_secret": "FEISHU_APP_SECRET",
            "domain": "FEISHU_DOMAIN",
            "connection_mode": "FEISHU_CONNECTION_MODE",
            "dm_policy": None,
            "allowed_users": "FEISHU_ALLOWED_USERS",
            "group_policy": "FEISHU_GROUP_POLICY",
            "home_channel": "FEISHU_HOME_CHANNEL",
        },
        "all_env_keys": [
            "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOMAIN", "FEISHU_CONNECTION_MODE",
            "FEISHU_ALLOWED_USERS", "FEISHU_ALLOW_ALL_USERS", "FEISHU_GROUP_POLICY",
            "FEISHU_HOME_CHANNEL", "FEISHU_HOME_CHANNEL_NAME",
        ],
    },
}


def _read_env_map(env_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            result[key] = value
    except Exception:
        logger.debug("Failed to read env file %s", env_path, exc_info=True)
    return result


def _quote_env_value(value: str) -> str:
    value = str(value)
    if not value:
        return ""
    if any(ch in value for ch in (' ', '#', '"', "'", '\n', '\t')):
        return json.dumps(value, ensure_ascii=False)
    return value


def _write_env_updates(env_path: Path, updates: dict[str, str | None]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(updates)
    output: list[str] = []

    for line in lines:
        match = _ENV_ASSIGN_RE.match(line)
        if not match:
            output.append(line)
            continue
        indent, key = match.groups()
        if key not in pending:
            output.append(line)
            continue
        value = pending.pop(key)
        if value is None:
            continue
        output.append(f"{indent}{key}={_quote_env_value(value)}")

    for key, value in pending.items():
        if value is None:
            continue
        output.append(f"{key}={_quote_env_value(value)}")

    content = "\n".join(output).rstrip()
    env_path.write_text((content + "\n") if content else "", encoding="utf-8")


def _clean_csv(value: str | None) -> str:
    items = [part.strip() for part in str(value or "").split(",")]
    return ",".join(part for part in items if part)


def _profile_env_path(name: str) -> Path:
    return get_hermes_home_for_profile(name) / ".env"


def _profile_channel_enabled(env: dict[str, str], spec: dict) -> bool:
    return any(str(env.get(key) or "").strip() for key in spec["all_env_keys"])


def _profile_channel_configured(platform: str, env: dict[str, str]) -> bool:
    if platform == "weixin":
        return bool((env.get("WEIXIN_ACCOUNT_ID") or "").strip() and (env.get("WEIXIN_TOKEN") or "").strip())
    if platform == "qqbot":
        return bool((env.get("QQ_APP_ID") or "").strip() and (env.get("QQ_CLIENT_SECRET") or "").strip())
    if platform == "wecom":
        return bool((env.get("WECOM_BOT_ID") or "").strip() and (env.get("WECOM_SECRET") or "").strip())
    if platform == "feishu":
        return bool((env.get("FEISHU_APP_ID") or "").strip() and (env.get("FEISHU_APP_SECRET") or "").strip())
    return False


def _serialize_profile_channel(platform: str, env: dict[str, str]) -> dict:
    spec = _PROFILE_CHANNEL_SPECS[platform]
    enabled = _profile_channel_enabled(env, spec)
    configured = _profile_channel_configured(platform, env)
    data = {
        "platform": platform,
        "label": spec["label"],
        "supports_qr": bool(spec.get("supports_qr")),
        "enabled": enabled,
        "configured": configured,
        "note": spec.get("note") or "",
        "secrets": {},
        "fields": {},
    }

    if platform == "weixin":
        data["fields"] = {
            "account_id": env.get("WEIXIN_ACCOUNT_ID", ""),
            "base_url": env.get("WEIXIN_BASE_URL", ""),
            "cdn_base_url": env.get("WEIXIN_CDN_BASE_URL", "") or "https://novac2c.cdn.weixin.qq.com/c2c",
            "dm_policy": env.get("WEIXIN_DM_POLICY", "open") or "open",
            "allowed_users": env.get("WEIXIN_ALLOWED_USERS", ""),
            "group_policy": env.get("WEIXIN_GROUP_POLICY", "disabled") or "disabled",
            "group_allowed_users": env.get("WEIXIN_GROUP_ALLOWED_USERS", ""),
            "home_channel": env.get("WEIXIN_HOME_CHANNEL", ""),
        }
        data["secrets"] = {"token_configured": bool((env.get("WEIXIN_TOKEN") or "").strip())}
    elif platform == "qqbot":
        data["fields"] = {
            "app_id": env.get("QQ_APP_ID", ""),
            "allowed_users": env.get("QQ_ALLOWED_USERS", ""),
            "group_policy": env.get("QQ_GROUP_POLICY", "open") or "open",
            "group_allowed_users": env.get("QQ_GROUP_ALLOWED_USERS", ""),
            "home_channel": env.get("QQ_HOME_CHANNEL", ""),
        }
        data["secrets"] = {"client_secret_configured": bool((env.get("QQ_CLIENT_SECRET") or "").strip())}
    elif platform == "wecom":
        data["fields"] = {
            "bot_id": env.get("WECOM_BOT_ID", ""),
            "websocket_url": env.get("WECOM_WEBSOCKET_URL", "") or "wss://openws.work.weixin.qq.com",
            "dm_policy": env.get("WECOM_DM_POLICY", "open") or "open",
            "allowed_users": env.get("WECOM_ALLOWED_USERS", ""),
            "group_policy": env.get("WECOM_GROUP_POLICY", "open") or "open",
            "group_allowed_users": env.get("WECOM_GROUP_ALLOWED_USERS", ""),
            "home_channel": env.get("WECOM_HOME_CHANNEL", ""),
        }
        data["secrets"] = {"secret_configured": bool((env.get("WECOM_SECRET") or "").strip())}
    elif platform == "feishu":
        allow_all = str(env.get("FEISHU_ALLOW_ALL_USERS", "")).strip().lower() == "true"
        allowed_users = env.get("FEISHU_ALLOWED_USERS", "")
        dm_policy = "open" if allow_all else ("allowlist" if allowed_users else "pairing")
        data["fields"] = {
            "app_id": env.get("FEISHU_APP_ID", ""),
            "domain": env.get("FEISHU_DOMAIN", "") or "feishu",
            "connection_mode": env.get("FEISHU_CONNECTION_MODE", "") or "websocket",
            "dm_policy": dm_policy,
            "allowed_users": allowed_users,
            "group_policy": env.get("FEISHU_GROUP_POLICY", "") or "open",
            "home_channel": env.get("FEISHU_HOME_CHANNEL", ""),
        }
        data["secrets"] = {"app_secret_configured": bool((env.get("FEISHU_APP_SECRET") or "").strip())}
    return data


def get_profile_channels_api(name: str) -> dict:
    profile_name = str(name or "default").strip() or "default"
    env = _read_env_map(_profile_env_path(profile_name))
    return {
        "name": profile_name,
        "channels": [_serialize_profile_channel(platform, env) for platform in ("weixin", "qqbot", "wecom", "feishu")],
    }


def _refresh_live_profile_env(profile_name: str) -> None:
    if profile_name != get_active_profile_name():
        return
    home = get_hermes_home_for_profile(profile_name)
    _reload_dotenv(home)
    try:
        from api.config import reload_config
        reload_config()
    except Exception:
        logger.debug("Failed to reload config after channel update", exc_info=True)


def _validate_choice(value: str, allowed: set[str], field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _build_channel_updates(platform: str, fields: dict, existing_env: dict[str, str]) -> dict[str, str | None]:
    if platform not in _PROFILE_CHANNEL_SPECS:
        raise ValueError("Unsupported channel platform")
    spec = _PROFILE_CHANNEL_SPECS[platform]
    enabled = bool(fields.get("enabled"))
    if not enabled:
        return {key: None for key in spec["all_env_keys"]}

    if platform == "weixin":
        account_id = str(fields.get("account_id") or "").strip() or existing_env.get("WEIXIN_ACCOUNT_ID", "")
        token = str(fields.get("token") or "").strip() or existing_env.get("WEIXIN_TOKEN", "")
        if not account_id:
            raise ValueError("Weixin account_id is required")
        if not token:
            raise ValueError("Weixin token is required")
        dm_policy = _validate_choice(fields.get("dm_policy") or existing_env.get("WEIXIN_DM_POLICY") or "open", {"open", "allowlist", "disabled", "pairing"}, "weixin dm_policy")
        group_policy = _validate_choice(fields.get("group_policy") or existing_env.get("WEIXIN_GROUP_POLICY") or "disabled", {"open", "allowlist", "disabled"}, "weixin group_policy")
        updates = {
            "WEIXIN_ACCOUNT_ID": account_id,
            "WEIXIN_TOKEN": token,
            "WEIXIN_BASE_URL": str(fields.get("base_url") or "").strip() or None,
            "WEIXIN_CDN_BASE_URL": str(fields.get("cdn_base_url") or "").strip() or "https://novac2c.cdn.weixin.qq.com/c2c",
            "WEIXIN_DM_POLICY": dm_policy,
            "WEIXIN_ALLOWED_USERS": _clean_csv(fields.get("allowed_users")),
            "WEIXIN_GROUP_POLICY": group_policy,
            "WEIXIN_GROUP_ALLOWED_USERS": _clean_csv(fields.get("group_allowed_users")),
            "WEIXIN_HOME_CHANNEL": str(fields.get("home_channel") or "").strip() or None,
            "WEIXIN_ALLOW_ALL_USERS": None,
        }
        return updates

    if platform == "qqbot":
        app_id = str(fields.get("app_id") or "").strip() or existing_env.get("QQ_APP_ID", "")
        client_secret = str(fields.get("client_secret") or "").strip() or existing_env.get("QQ_CLIENT_SECRET", "")
        if not app_id:
            raise ValueError("QQ Bot app_id is required")
        if not client_secret:
            raise ValueError("QQ Bot client_secret is required")
        return {
            "QQ_APP_ID": app_id,
            "QQ_CLIENT_SECRET": client_secret,
            "QQ_ALLOWED_USERS": _clean_csv(fields.get("allowed_users")),
            "QQ_GROUP_POLICY": _validate_choice(fields.get("group_policy") or existing_env.get("QQ_GROUP_POLICY") or "open", {"open", "allowlist", "disabled"}, "qq group_policy"),
            "QQ_GROUP_ALLOWED_USERS": _clean_csv(fields.get("group_allowed_users")),
            "QQ_HOME_CHANNEL": str(fields.get("home_channel") or "").strip() or None,
        }

    if platform == "wecom":
        bot_id = str(fields.get("bot_id") or "").strip() or existing_env.get("WECOM_BOT_ID", "")
        secret = str(fields.get("secret") or "").strip() or existing_env.get("WECOM_SECRET", "")
        if not bot_id:
            raise ValueError("WeCom bot_id is required")
        if not secret:
            raise ValueError("WeCom secret is required")
        websocket_url = str(fields.get("websocket_url") or "").strip()
        if websocket_url and not websocket_url.startswith(("ws://", "wss://")):
            raise ValueError("WeCom websocket_url must start with ws:// or wss://")
        return {
            "WECOM_BOT_ID": bot_id,
            "WECOM_SECRET": secret,
            "WECOM_WEBSOCKET_URL": websocket_url or None,
            "WECOM_DM_POLICY": _validate_choice(fields.get("dm_policy") or existing_env.get("WECOM_DM_POLICY") or "open", {"open", "allowlist", "disabled", "pairing"}, "wecom dm_policy"),
            "WECOM_ALLOWED_USERS": _clean_csv(fields.get("allowed_users")),
            "WECOM_GROUP_POLICY": _validate_choice(fields.get("group_policy") or existing_env.get("WECOM_GROUP_POLICY") or "open", {"open", "allowlist", "disabled"}, "wecom group_policy"),
            "WECOM_GROUP_ALLOWED_USERS": _clean_csv(fields.get("group_allowed_users")),
            "WECOM_HOME_CHANNEL": str(fields.get("home_channel") or "").strip() or None,
        }

    app_id = str(fields.get("app_id") or "").strip() or existing_env.get("FEISHU_APP_ID", "")
    app_secret = str(fields.get("app_secret") or "").strip() or existing_env.get("FEISHU_APP_SECRET", "")
    if not app_id:
        raise ValueError("Feishu app_id is required")
    if not app_secret:
        raise ValueError("Feishu app_secret is required")
    dm_policy = _validate_choice(fields.get("dm_policy") or "pairing", {"pairing", "open", "allowlist"}, "feishu dm_policy")
    allow_all = "true" if dm_policy == "open" else "false"
    allowed_users = _clean_csv(fields.get("allowed_users"))
    if dm_policy != "allowlist":
        allowed_users = ""
    return {
        "FEISHU_APP_ID": app_id,
        "FEISHU_APP_SECRET": app_secret,
        "FEISHU_DOMAIN": _validate_choice(fields.get("domain") or existing_env.get("FEISHU_DOMAIN") or "feishu", {"feishu", "lark"}, "feishu domain"),
        "FEISHU_CONNECTION_MODE": _validate_choice(fields.get("connection_mode") or existing_env.get("FEISHU_CONNECTION_MODE") or "websocket", {"websocket", "webhook"}, "feishu connection_mode"),
        "FEISHU_ALLOW_ALL_USERS": allow_all,
        "FEISHU_ALLOWED_USERS": allowed_users,
        "FEISHU_GROUP_POLICY": _validate_choice(fields.get("group_policy") or existing_env.get("FEISHU_GROUP_POLICY") or "open", {"open", "disabled"}, "feishu group_policy"),
        "FEISHU_HOME_CHANNEL": str(fields.get("home_channel") or "").strip() or None,
    }


def update_profile_channel_api(name: str, platform: str, fields: dict) -> dict:
    profile_name = str(name or "default").strip() or "default"
    platform_key = str(platform or "").strip().lower()
    env_path = _profile_env_path(profile_name)
    env = _read_env_map(env_path)
    updates = _build_channel_updates(platform_key, fields or {}, env)
    _write_env_updates(env_path, updates)
    _refresh_live_profile_env(profile_name)
    return get_profile_channels_api(profile_name)


def _store_channel_qr_session(profile_name: str, platform: str, payload: dict) -> dict:
    session_id = secrets.token_urlsafe(12)
    entry = {
        "id": session_id,
        "profile": profile_name,
        "platform": platform,
        "created_at": time.time(),
        **payload,
    }
    with _channel_qr_sessions_lock:
        _channel_qr_sessions[session_id] = entry
    return entry


def _get_channel_qr_session(session_id: str, profile_name: str, platform: str) -> dict:
    with _channel_qr_sessions_lock:
        entry = _channel_qr_sessions.get(session_id)
    if not entry or entry.get("profile") != profile_name or entry.get("platform") != platform:
        raise ValueError("QR session not found")
    return entry


def _delete_channel_qr_session(session_id: str) -> None:
    with _channel_qr_sessions_lock:
        _channel_qr_sessions.pop(session_id, None)


def _weixin_api_get(base_url: str, endpoint: str) -> dict:
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    req = Request(url, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read()
        if body:
            return json.loads(body.decode("utf-8"))
        raise


def start_profile_channel_qr_api(name: str, platform: str) -> dict:
    profile_name = str(name or "default").strip() or "default"
    platform_key = str(platform or "").strip().lower()

    if platform_key == "feishu":
        from gateway.platforms.feishu import _begin_registration, _init_registration

        _init_registration("feishu")
        begin = _begin_registration("feishu")
        session = _store_channel_qr_session(
            profile_name,
            "feishu",
            {
                "device_code": begin["device_code"],
                "domain": "feishu",
                "interval": int(begin.get("interval") or 5),
                "expires_at": time.time() + int(begin.get("expire_in") or 600),
                "qr_url": begin["qr_url"],
            },
        )
        return {
            "session_id": session["id"],
            "platform": "feishu",
            "status": "pending",
            "qr_url": begin["qr_url"],
            "interval": session["interval"],
            "expires_at": session["expires_at"],
        }

    if platform_key == "weixin":
        from gateway.platforms.weixin import EP_GET_BOT_QR, ILINK_BASE_URL

        begin = _weixin_api_get(ILINK_BASE_URL, f"{EP_GET_BOT_QR}?bot_type=3")
        qr_value = str(begin.get("qrcode") or "")
        qr_image_url = str(begin.get("qrcode_img_content") or "")
        if not qr_value:
            raise RuntimeError("Weixin QR setup did not return a qrcode value")
        session = _store_channel_qr_session(
            profile_name,
            "weixin",
            {
                "qrcode": qr_value,
                "qr_image_url": qr_image_url,
                "current_base_url": ILINK_BASE_URL,
                "refresh_count": 0,
                "expires_at": time.time() + 480,
            },
        )
        return {
            "session_id": session["id"],
            "platform": "weixin",
            "status": "pending",
            "qr_url": qr_image_url,
            "qr_image_url": qr_image_url,
            "expires_at": session["expires_at"],
        }

    raise ValueError("QR setup is not supported for this platform")


def poll_profile_channel_qr_api(name: str, platform: str, session_id: str) -> dict:
    profile_name = str(name or "default").strip() or "default"
    platform_key = str(platform or "").strip().lower()
    session = _get_channel_qr_session(session_id, profile_name, platform_key)
    if time.time() > float(session.get("expires_at") or 0):
        _delete_channel_qr_session(session_id)
        return {"status": "expired"}

    if platform_key == "feishu":
        from gateway.platforms.feishu import _accounts_base_url, _post_registration, probe_bot

        current_domain = session.get("domain", "feishu")
        try:
            result = _post_registration(
                _accounts_base_url(current_domain),
                {"action": "poll", "device_code": session["device_code"], "tp": "ob_app"},
            )
        except (URLError, OSError, json.JSONDecodeError):
            return {"status": "pending"}

        user_info = result.get("user_info") or {}
        if user_info.get("tenant_brand") == "lark":
            current_domain = "lark"
            with _channel_qr_sessions_lock:
                if session_id in _channel_qr_sessions:
                    _channel_qr_sessions[session_id]["domain"] = current_domain

        if result.get("client_id") and result.get("client_secret"):
            probe = probe_bot(result["client_id"], result["client_secret"], current_domain) or {}
            _delete_channel_qr_session(session_id)
            update_profile_channel_api(
                profile_name,
                "feishu",
                {
                    "enabled": True,
                    "app_id": result["client_id"],
                    "app_secret": result["client_secret"],
                    "domain": current_domain,
                    "connection_mode": "websocket",
                    "dm_policy": "pairing",
                    "group_policy": "open",
                },
            )
            return {
                "status": "confirmed",
                "bot_name": probe.get("bot_name"),
                "domain": current_domain,
            }

        error = str(result.get("error") or "").strip().lower()
        if error in {"access_denied", "expired_token"}:
            _delete_channel_qr_session(session_id)
            return {"status": "denied" if error == "access_denied" else "expired"}
        return {"status": "pending"}

    if platform_key == "weixin":
        from gateway.platforms.weixin import EP_GET_BOT_QR, EP_GET_QR_STATUS, ILINK_BASE_URL, WEIXIN_CDN_BASE_URL, save_weixin_account

        try:
            status = _weixin_api_get(session["current_base_url"], f"{EP_GET_QR_STATUS}?qrcode={quote(session['qrcode'])}")
        except (URLError, OSError, json.JSONDecodeError):
            return {"status": "pending"}

        qr_status = str(status.get("status") or "wait")
        if qr_status == "scaned_but_redirect":
            redirect_host = str(status.get("redirect_host") or "").strip()
            if redirect_host:
                with _channel_qr_sessions_lock:
                    if session_id in _channel_qr_sessions:
                        _channel_qr_sessions[session_id]["current_base_url"] = f"https://{redirect_host}"
            return {"status": "scanned"}

        if qr_status == "expired":
            refresh_count = int(session.get("refresh_count") or 0) + 1
            if refresh_count > 3:
                _delete_channel_qr_session(session_id)
                return {"status": "expired"}
            begin = _weixin_api_get(ILINK_BASE_URL, f"{EP_GET_BOT_QR}?bot_type=3")
            qr_value = str(begin.get("qrcode") or "")
            qr_image_url = str(begin.get("qrcode_img_content") or "")
            with _channel_qr_sessions_lock:
                if session_id in _channel_qr_sessions:
                    _channel_qr_sessions[session_id].update({
                        "qrcode": qr_value,
                        "qr_image_url": qr_image_url,
                        "current_base_url": ILINK_BASE_URL,
                        "refresh_count": refresh_count,
                    })
            return {"status": "refreshed", "qr_url": qr_image_url, "qr_image_url": qr_image_url}

        if qr_status == "confirmed":
            account_id = str(status.get("ilink_bot_id") or "").strip()
            token = str(status.get("bot_token") or "").strip()
            base_url = str(status.get("baseurl") or ILINK_BASE_URL).strip()
            user_id = str(status.get("ilink_user_id") or "").strip()
            if not account_id or not token:
                _delete_channel_qr_session(session_id)
                return {"status": "error", "error": "Weixin QR login returned incomplete credentials"}
            save_weixin_account(str(get_hermes_home_for_profile(profile_name)), account_id=account_id, token=token, base_url=base_url, user_id=user_id)
            _delete_channel_qr_session(session_id)
            update_profile_channel_api(
                profile_name,
                "weixin",
                {
                    "enabled": True,
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "cdn_base_url": WEIXIN_CDN_BASE_URL,
                    "dm_policy": "open",
                    "group_policy": "disabled",
                    "home_channel": user_id or "",
                },
            )
            return {"status": "confirmed", "account_id": account_id, "user_id": user_id}

        if qr_status == "scaned":
            return {"status": "scanned"}
        return {"status": "pending"}

    raise ValueError("QR polling is not supported for this platform")


def create_profile_api(name: str, clone_from: str = None,
                       clone_config: bool = False,
                       clone_skills: bool = False,
                       base_url: str = None,
                       api_key: str = None) -> dict:
    """Create a new profile. Returns the new profile info dict."""
    _validate_profile_name(name)
    # Defense-in-depth: validate clone_from here too, even though routes.py
    # also validates it. Any caller that bypasses the HTTP layer gets protection.
    if clone_from is not None and clone_from != 'default':
        _validate_profile_name(clone_from)

    try:
        from hermes_cli.profiles import create_profile
        create_profile(
            name,
            clone_from=clone_from,
            clone_config=clone_config,
            clone_all=False,
            no_alias=True,
        )
    except ImportError:
        _create_profile_fallback(name, clone_from, clone_config)

    # Resolve the profile directory from the profile list when possible.
    # hermes_cli and the webui runtime do not always agree on the exact root,
    # so we prefer the path returned by list_profiles_api() and fall back to the
    # standard profile location only if the profile cannot be found there yet.
    profile_path = _DEFAULT_HERMES_HOME / 'profiles' / name
    for p in list_profiles_api():
        if p['name'] == name:
            try:
                profile_path = Path(p.get('path') or profile_path)
            except Exception:
                logger.debug("Failed to parse profile path")
            break

    profile_path.mkdir(parents=True, exist_ok=True)
    _write_endpoint_to_config(profile_path, base_url=base_url, api_key=api_key)
    if clone_skills and clone_from:
        if clone_from == 'default':
            source_dir = _DEFAULT_HERMES_HOME
        else:
            source_dir = _DEFAULT_HERMES_HOME / 'profiles' / clone_from
        if source_dir.is_dir():
            _copy_profile_skills(source_dir, profile_path)

    # Find and return the newly created profile info.
    # When hermes_cli is not importable, list_profiles_api() also falls back
    # to the stub default-only list and won't find the new profile by name.
    # In that case, return a complete profile dict directly.
    for p in list_profiles_api():
        if p['name'] == name:
            return p
    return {
        'name': name,
        'path': str(profile_path),
        'is_default': False,
        'is_active': _active_profile == name,
        'gateway_running': False,
        'model': None,
        'provider': None,
        'has_env': (profile_path / '.env').exists(),
        'skill_count': 0,
    }


def delete_profile_api(name: str) -> dict:
    """Delete a profile. Switches to default first if it's the active one."""
    if name == 'default':
        raise ValueError("Cannot delete the default profile.")
    _validate_profile_name(name)

    # If deleting the active profile, switch to default first
    if _active_profile == name:
        try:
            switch_profile('default')
        except RuntimeError:
            raise RuntimeError(
                f"Cannot delete active profile '{name}' while an agent is running. "
                "Cancel or wait for it to finish."
            )

    try:
        from hermes_cli.profiles import delete_profile
        delete_profile(name, yes=True)
    except ImportError:
        # Manual fallback: just remove the directory
        import shutil
        profile_dir = _resolve_named_profile_home(name)
        if profile_dir.is_dir():
            shutil.rmtree(str(profile_dir))
        else:
            raise ValueError(f"Profile '{name}' does not exist.")

    return {'ok': True, 'name': name}
