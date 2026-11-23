import base64, hashlib, hmac, os, secrets, threading, time
from collections import defaultdict, deque

__all__ = ['AuthManager', 'USERNAME', 'DEFAULT_PASSWORD', 'validate_new_password']

USERNAME = 'admin' #hardcoded, only the password is configurable
DEFAULT_PASSWORD = 'admin'
MIN_PASSWORD_LEN = 8
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def hash_password(password):
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode('utf-8'), salt=salt, **_SCRYPT)
    return f'scrypt${salt.hex()}${h.hex()}'


def verify_password(password, stored):
    try:
        scheme, salt, h = stored.split('$')
        if scheme != 'scrypt':
            return False
        calc = hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt), **_SCRYPT)
        return hmac.compare_digest(calc, bytes.fromhex(h))
    except Exception:
        return False


def validate_new_password(password):
    '''returns an error message, or None when ok'''
    if len(password) < MIN_PASSWORD_LEN:
        return f'password must be at least {MIN_PASSWORD_LEN} characters'
    if password == DEFAULT_PASSWORD:
        return 'password must not be the default password'
    return None


class AuthManager:
    def __init__(self, db, session_days=7, max_failures=5, window_sec=300):
        self.db = db
        self.session_sec = session_days * 86400
        self.max_failures = max_failures
        self.window_sec = window_sec
        self._failures = defaultdict(deque)
        self._lock = threading.Lock()
        if self.db.get_setting('password_hash') is None:
            self.set_default_password()
        if self.db.get_setting('session_secret') is None:
            self.db.set_setting('session_secret', secrets.token_hex(32))
        if self.db.get_setting('session_epoch') is None:
            self.db.set_setting('session_epoch', '1')

    def set_default_password(self):
        self.db.set_setting('password_hash', hash_password(DEFAULT_PASSWORD))
        self.db.set_setting('must_change_password', '1')
        self._bump_epoch()

    @property
    def must_change_password(self):
        return self.db.get_setting('must_change_password', '0') == '1'

    def _bump_epoch(self):
        self.db.set_setting('session_epoch', str(int(self.db.get_setting('session_epoch', '0')) + 1))

    def set_password(self, new_password):
        self.db.set_setting('password_hash', hash_password(new_password))
        self.db.set_setting('must_change_password', '0')
        self._bump_epoch() #invalidates all existing sessions

    def check_password(self, password):
        return verify_password(password, self.db.get_setting('password_hash', ''))

    # ---- login rate limit (per client ip) ----
    def is_locked(self, ip):
        now = time.time()
        with self._lock:
            q = self._failures[ip]
            while q and now - q[0] > self.window_sec:
                q.popleft()
            return len(q) >= self.max_failures

    def record_failure(self, ip):
        with self._lock:
            self._failures[ip].append(time.time())

    def clear_failures(self, ip):
        with self._lock:
            self._failures.pop(ip, None)

    # ---- stateless signed session token ----
    def _sign(self, payload):
        key = bytes.fromhex(self.db.get_setting('session_secret'))
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def create_token(self):
        payload = f"{USERNAME}|{int(time.time()) + self.session_sec}|{self.db.get_setting('session_epoch')}"
        raw = base64.urlsafe_b64encode(payload.encode()).decode()
        return f'{raw}.{self._sign(payload)}'

    def verify_token(self, token):
        try:
            raw, sig = token.split('.')
            payload = base64.urlsafe_b64decode(raw.encode()).decode()
            if not hmac.compare_digest(sig, self._sign(payload)):
                return False
            user, exp, epoch = payload.split('|')
            return user == USERNAME and int(exp) > time.time() and epoch == self.db.get_setting('session_epoch')
        except Exception:
            return False
