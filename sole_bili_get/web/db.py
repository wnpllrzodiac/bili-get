import json, sqlite3, threading, time

__all__ = ['Database']

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    params TEXT NOT NULL,
    state TEXT NOT NULL,
    stage TEXT DEFAULT '',
    percent REAL DEFAULT 0,
    title TEXT DEFAULT '',
    error TEXT DEFAULT '',
    files TEXT DEFAULT '[]',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
'''


class Database:
    def __init__(self, path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def get_setting(self, key, default=None):
        with self._lock:
            row = self._conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key, value):
        with self._lock:
            if value is None:
                self._conn.execute('DELETE FROM settings WHERE key=?', (key,))
            else:
                self._conn.execute('INSERT INTO settings(key,value) VALUES(?,?) '
                                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
            self._conn.commit()

    def insert_job(self, job_id, url, params):
        now = time.time()
        with self._lock:
            self._conn.execute('INSERT INTO jobs(id,url,params,state,created,updated) VALUES(?,?,?,?,?,?)',
                               (job_id, url, json.dumps(params), 'queued', now, now))
            self._conn.commit()

    def update_job(self, job_id, **fields):
        if 'files' in fields:
            fields['files'] = json.dumps(fields['files'])
        fields['updated'] = time.time()
        cols = ','.join(f'{k}=?' for k in fields)
        with self._lock:
            self._conn.execute(f'UPDATE jobs SET {cols} WHERE id=?', (*fields.values(), job_id))
            self._conn.commit()

    def _row_to_job(self, row):
        d = dict(row)
        d['params'] = json.loads(d['params'])
        d['files'] = json.loads(d['files'])
        return d

    def get_job(self, job_id):
        with self._lock:
            row = self._conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, limit=200):
        with self._lock:
            rows = self._conn.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        return [self._row_to_job(r) for r in rows]

    def delete_job(self, job_id):
        with self._lock:
            self._conn.execute('DELETE FROM jobs WHERE id=?', (job_id,))
            self._conn.commit()

    def fail_unfinished_jobs(self):
        '''jobs left running/queued by a previous server process can never finish'''
        with self._lock:
            self._conn.execute("UPDATE jobs SET state='failed', error='interrupted by server restart', updated=? "
                               "WHERE state IN ('queued','running')", (time.time(),))
            self._conn.commit()
