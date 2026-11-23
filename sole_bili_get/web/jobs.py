import json, os, queue, shutil, signal, subprocess, sys, threading, uuid

from .worker import EVENT_PREFIX

__all__ = ['JobManager', 'run_analyze']

WORKER_CMD = [sys.executable, '-m', 'sole_bili_get.web.worker']


def _spawn(mode, req, cookie, log_path=None):
    env = dict(os.environ)
    env.pop('BILI_COOKIE', None)
    if cookie:
        env['BILI_COOKIE'] = cookie #env, not argv: argv is visible to every local user via ps
    log = open(log_path, 'ab') if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(WORKER_CMD + [mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                env=env, start_new_session=True)
    finally:
        if log_path:
            log.close()
    proc.stdin.write(json.dumps(req).encode('utf-8'))
    proc.stdin.close()
    return proc


def _events(proc):
    for raw in proc.stdout:
        line = raw.decode('utf-8', errors='replace')
        if line.startswith(EVENT_PREFIX):
            try:
                yield json.loads(line[len(EVENT_PREFIX):])
            except ValueError:
                pass


def run_analyze(url, cookie, work_dir, timeout=60):
    '''returns (info, error)'''
    proc = _spawn('analyze', {'url': url, 'work_dir': work_dir}, cookie)
    timer = threading.Timer(timeout, lambda: _kill(proc))
    timer.start()
    try:
        result, error = None, 'no response from worker'
        for evt in _events(proc):
            if evt.get('type') == 'result':
                result = evt['info']
            elif evt.get('type') == 'error':
                error = evt.get('message')
        proc.wait()
        return (result, None) if result else (None, error)
    finally:
        timer.cancel()
        shutil.rmtree(work_dir, ignore_errors=True)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class JobManager:
    def __init__(self, db, jobs_dir, concurrency=2):
        self.db, self.jobs_dir = db, jobs_dir
        self._procs = {}
        self._canceled = set()
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        db.fail_unfinished_jobs()
        for i in range(concurrency):
            threading.Thread(target=self._loop, name=f'bili-job-{i}', daemon=True).start()

    def job_dir(self, job_id):
        return os.path.join(self.jobs_dir, job_id)

    def create(self, url, params):
        job_id = uuid.uuid4().hex[:12]
        self.db.insert_job(job_id, url, params)
        self._queue.put(job_id)
        return job_id

    def cancel(self, job_id):
        job = self.db.get_job(job_id)
        if job is None:
            return
        if job['state'] == 'queued':
            self.db.update_job(job_id, state='canceled')
        elif job['state'] == 'running':
            with self._lock:
                self._canceled.add(job_id)
                proc = self._procs.get(job_id)
            if proc is not None:
                _kill(proc)

    def delete(self, job_id):
        '''cancels if still active, then removes record and files'''
        self.cancel(job_id)
        self.db.delete_job(job_id)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    def _loop(self):
        while True:
            job_id = self._queue.get()
            try:
                self._run(job_id)
            except Exception as e:
                self.db.update_job(job_id, state='failed', error=f'{type(e).__name__}: {e}')

    def _run(self, job_id):
        job = self.db.get_job(job_id)
        if job is None or job['state'] != 'queued':
            return #deleted or canceled while waiting
        work = self.job_dir(job_id)
        os.makedirs(work, exist_ok=True)
        req = dict(job['params'], url=job['url'], work_dir=work)
        cookie = self.db.get_setting('bili_cookie')
        proc = _spawn('download', req, cookie, os.path.join(work, 'worker.log'))
        with self._lock:
            self._procs[job_id] = proc
            if job_id in self._canceled:
                _kill(proc)
        self.db.update_job(job_id, state='running', stage='analyzing')
        error, files, title = None, None, ''
        for evt in _events(proc):
            t = evt.get('type')
            if t == 'progress':
                self.db.update_job(job_id, stage=evt.get('stage', ''), percent=evt.get('percent', 0))
            elif t == 'result':
                files, title = evt.get('files', []), evt.get('title', '')
            elif t == 'error':
                error = evt.get('message')
        proc.wait()
        with self._lock:
            self._procs.pop(job_id, None)
            canceled = job_id in self._canceled
            self._canceled.discard(job_id)
        if self.db.get_job(job_id) is None: #deleted meanwhile
            return
        if canceled:
            self.db.update_job(job_id, state='canceled', stage='')
        elif files is not None and error is None:
            self.db.update_job(job_id, state='done', stage='', percent=100, files=files, title=title)
        else:
            self.db.update_job(job_id, state='failed', stage='',
                               error=error or f'worker exited with code {proc.returncode}')
