import os, re

__all__ = ['Config', 'normalize_prefix', 'default_data_dir']

DATA_DIR_NAME = 'bili-web-data'

_PREFIX_RE = re.compile(r'^/[A-Za-z0-9._~\-]+(/[A-Za-z0-9._~\-]+)*$')


def default_data_dir():
    '''<app folder>/bili-web-data. The app folder is the checkout the code runs from (the folder holding setup.py);
    for a normal pip install (code in site-packages) it is the current working directory instead.'''
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.isfile(os.path.join(project_root, 'setup.py')):
        return os.path.join(project_root, DATA_DIR_NAME)
    return os.path.join(os.getcwd(), DATA_DIR_NAME)


def normalize_prefix(prefix):
    '''"/hp/bilidown/" -> "/hp/bilidown"; invalid or empty -> ""'''
    prefix = (prefix or '').strip().rstrip('/')
    if prefix and not prefix.startswith('/'):
        prefix = '/' + prefix
    return prefix if _PREFIX_RE.match(prefix) else ''


class Config:
    '''All settings come from environment variables (read at construction time)'''
    def __init__(self, **overrides):
        env = os.environ
        self.host = env.get('BILI_WEB_HOST', '127.0.0.1')
        self.port = int(env.get('BILI_WEB_PORT', '8000'))
        self.root_path = normalize_prefix(env.get('BILI_WEB_ROOT_PATH', ''))
        self.data_dir = os.path.abspath(os.path.expanduser(env.get('BILI_WEB_DATA_DIR') or default_data_dir()))
        self.concurrency = max(1, int(env.get('BILI_WEB_CONCURRENCY', '2')))
        self.session_days = int(env.get('BILI_WEB_SESSION_DAYS', '7'))
        self.login_max_failures = int(env.get('BILI_WEB_LOGIN_MAX_FAILURES', '5'))
        self.login_window_sec = int(env.get('BILI_WEB_LOGIN_WINDOW_SEC', '300'))
        for k, v in overrides.items():
            setattr(self, k, v)
        os.makedirs(self.data_dir, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700) #holds the password hash, the session secret and the bilibili cookie
        except OSError:
            pass
        os.makedirs(self.jobs_dir, exist_ok=True)

    @property
    def db_path(self):
        return os.path.join(self.data_dir, 'bili-web.sqlite3')

    @property
    def jobs_dir(self):
        return os.path.join(self.data_dir, 'downloads')
