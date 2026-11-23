import asyncio, json, os, re, tempfile, uuid
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth import USERNAME, AuthManager, validate_new_password
from .config import Config, normalize_prefix
from .db import Database
from .jobs import JobManager, run_analyze
from .urlcheck import normalize_bilibili_url

__all__ = ['create_app']

STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')
COOKIE_NAME = 'bili_web_session'


class PrefixMiddleware:
    '''Support running under a sub-path (e.g. /hp/bilidown) behind a reverse proxy, no matter whether the proxy
    strips the prefix or not. The prefix comes from BILI_WEB_ROOT_PATH, or else the X-Forwarded-Prefix header.'''
    def __init__(self, app, configured_prefix=''):
        self.app, self.configured = app, configured_prefix

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            prefix = self.configured
            if not prefix:
                for k, v in scope['headers']:
                    if k == b'x-forwarded-prefix':
                        prefix = normalize_prefix(v.decode('latin-1'))
            path = scope['path']
            if prefix:
                if path == prefix and scope['type'] == 'http': #relative urls need the trailing slash
                    return await Response(status_code=308, headers={'Location': prefix + '/'})(scope, receive, send)
                if path.startswith(prefix + '/'):
                    scope = dict(scope, path=path[len(prefix):])
                    if scope.get('raw_path'):
                        scope['raw_path'] = scope['raw_path'][len(prefix.encode()):] \
                            if scope['raw_path'].startswith(prefix.encode()) else scope['raw_path']
                scope = dict(scope, root_path=prefix)
        await self.app(scope, receive, send)


class LoginReq(BaseModel):
    username: str
    password: str


class PasswordReq(BaseModel):
    old_password: str
    new_password: str


class AnalyzeReq(BaseModel):
    url: str


class JobReq(BaseModel):
    url: str
    quality: str = 'MAX' #MAX | MIN | <quality id>
    codec: Optional[str] = None #avc1 | hev1 | hvc1 | av01 (as reported by /api/analyze)
    mode: str = 'merged' #merged | video | audio
    playlist: bool = False
    danmu: bool = False
    cover: bool = False


class SettingsReq(BaseModel):
    cookie: Optional[str] = None #None/'' clears it


def create_app(config: Optional[Config] = None) -> FastAPI:
    cfg = config or Config()
    db = Database(cfg.db_path)
    auth = AuthManager(db, cfg.session_days, cfg.login_max_failures, cfg.login_window_sec)
    jobs = JobManager(db, cfg.jobs_dir, cfg.concurrency)
    app = FastAPI(title='bili-web', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.db, app.state.auth, app.state.jobs = cfg, db, auth, jobs

    def client_ip(request: Request):
        return request.client.host if request.client else 'unknown'

    def csrf(request: Request):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-requested-with') != 'bili-web':
            raise HTTPException(403, 'missing X-Requested-With header')

    def session(request: Request, _=Depends(csrf)):
        token = request.cookies.get(COOKIE_NAME)
        if not token or not auth.verify_token(token):
            raise HTTPException(401, 'not logged in')
        return USERNAME

    def full_session(_user=Depends(session)):
        if auth.must_change_password:
            raise HTTPException(403, 'password_change_required')
        return _user

    def set_session_cookie(request: Request, response: Response):
        secure = request.headers.get('x-forwarded-proto', request.url.scheme) == 'https'
        response.set_cookie(COOKIE_NAME, auth.create_token(), max_age=cfg.session_days * 86400, httponly=True,
                            samesite='lax', secure=secure, path=(request.scope.get('root_path') or '') + '/')

    def clear_session_cookie(request: Request, response: Response):
        response.delete_cookie(COOKIE_NAME, path=(request.scope.get('root_path') or '') + '/')

    # ---------------- public ----------------
    @app.get('/healthz')
    def healthz():
        return {'ok': True}

    @app.get('/')
    def index():
        return FileResponse(os.path.join(STATIC_DIR, 'index.html'), headers={'Cache-Control': 'no-cache'})

    @app.get('/static/{name}')
    def static_file(name: str):
        if name not in ('app.js', 'style.css'):
            raise HTTPException(404)
        return FileResponse(os.path.join(STATIC_DIR, name), headers={'Cache-Control': 'no-cache'})

    @app.post('/api/login')
    def login(req: LoginReq, request: Request, response: Response, _=Depends(csrf)):
        ip = client_ip(request)
        if auth.is_locked(ip):
            raise HTTPException(429, 'too many failed attempts, try again later')
        user_ok = req.username == USERNAME
        pw_ok = auth.check_password(req.password) #always verified, so timing doesn't reveal the username
        if not (user_ok and pw_ok):
            auth.record_failure(ip)
            raise HTTPException(401, 'wrong username or password')
        auth.clear_failures(ip)
        set_session_cookie(request, response)
        return {'username': USERNAME, 'must_change_password': auth.must_change_password}

    # ---------------- session only (usable before the forced password change) ----------------
    @app.post('/api/logout')
    def logout(request: Request, response: Response, _=Depends(csrf)):
        clear_session_cookie(request, response)
        return {'ok': True}

    @app.get('/api/me')
    def me(_user=Depends(session)):
        return {'username': USERNAME, 'must_change_password': auth.must_change_password}

    @app.post('/api/password')
    def change_password(req: PasswordReq, request: Request, response: Response, _user=Depends(session)):
        if not auth.check_password(req.old_password):
            raise HTTPException(400, 'old password is wrong')
        err = validate_new_password(req.new_password)
        if err:
            raise HTTPException(400, err)
        auth.set_password(req.new_password)
        clear_session_cookie(request, response) #all sessions are invalidated, log in again
        return {'ok': True}

    # ---------------- full access ----------------
    def _cookie_info():
        c = db.get_setting('bili_cookie') or ''
        return {'cookie_set': bool(c), 'cookie_tail': ('…' + c[-4:]) if len(c) > 8 else ''}

    @app.get('/api/settings')
    def get_settings(_user=Depends(full_session)):
        return _cookie_info()

    @app.put('/api/settings')
    def put_settings(req: SettingsReq, _user=Depends(full_session)):
        cookie = (req.cookie or '').strip()
        if '\n' in cookie or '\r' in cookie:
            raise HTTPException(400, 'invalid cookie')
        db.set_setting('bili_cookie', cookie or None)
        return _cookie_info()

    @app.post('/api/analyze')
    async def analyze(req: AnalyzeReq, _user=Depends(full_session)):
        url = normalize_bilibili_url(req.url)
        if url is None:
            raise HTTPException(400, 'only https://www.bilibili.com/video/BV... (or av...) urls are supported')
        work = os.path.join(tempfile.gettempdir(), 'bili-web-analyze-' + uuid.uuid4().hex)
        info, error = await run_in_threadpool(run_analyze, url, db.get_setting('bili_cookie'), work)
        if info is None:
            raise HTTPException(502, error or 'analyse failed')
        info['url'] = url
        return info

    def job_view(j):
        view = {k: j[k] for k in ('id', 'url', 'state', 'stage', 'percent', 'title', 'error', 'files', 'created', 'updated')}
        view['params'] = j['params']
        return view

    @app.post('/api/jobs')
    def create_job(req: JobReq, _user=Depends(full_session)):
        url = normalize_bilibili_url(req.url)
        if url is None:
            raise HTTPException(400, 'only https://www.bilibili.com/video/BV... (or av...) urls are supported')
        q = req.quality.strip().upper()
        if not (q in ('MAX', 'MIN') or (q.isdigit() and len(q) <= 4)):
            raise HTTPException(400, 'invalid quality')
        if req.codec not in (None, '', 'avc1', 'hev1', 'hvc1', 'av01'):
            raise HTTPException(400, 'invalid codec')
        if req.mode not in ('merged', 'video', 'audio'):
            raise HTTPException(400, 'invalid mode')
        params = {'quality': q, 'codec': req.codec or '', 'mode': req.mode, 'playlist': req.playlist,
                  'danmu': req.danmu, 'cover': req.cover}
        return {'id': jobs.create(url, params)}

    @app.get('/api/jobs')
    def list_jobs(_user=Depends(full_session)):
        return [job_view(j) for j in db.list_jobs()]

    def _get_job(job_id):
        j = db.get_job(job_id)
        if j is None:
            raise HTTPException(404, 'no such job')
        return j

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str, _user=Depends(full_session)):
        return job_view(_get_job(job_id))

    @app.get('/api/jobs/{job_id}/events')
    async def job_events(job_id: str, request: Request, _user=Depends(full_session)):
        _get_job(job_id)

        async def gen():
            last = None
            while True:
                j = db.get_job(job_id)
                if j is None:
                    yield 'event: gone\ndata: {}\n\n'
                    return
                view = json.dumps(job_view(j), ensure_ascii=False)
                if view != last:
                    last = view
                    yield f'data: {view}\n\n'
                if j['state'] in ('done', 'failed', 'canceled'):
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(1)
        return StreamingResponse(gen(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/api/jobs/{job_id}/cancel')
    def cancel_job(job_id: str, _user=Depends(full_session)):
        _get_job(job_id)
        jobs.cancel(job_id)
        return {'ok': True}

    @app.delete('/api/jobs/{job_id}')
    def delete_job(job_id: str, _user=Depends(full_session)):
        _get_job(job_id)
        jobs.delete(job_id)
        return {'ok': True}

    @app.get('/api/jobs/{job_id}/files/{name}')
    def get_file(job_id: str, name: str, _user=Depends(full_session)):
        j = _get_job(job_id)
        if j['state'] != 'done' or name not in j['files']: #only names recorded by the worker are served (no path traversal)
            raise HTTPException(404, 'no such file')
        path = os.path.join(jobs.job_dir(job_id), 'files', name)
        if not os.path.isfile(path):
            raise HTTPException(404, 'file is gone')
        return FileResponse(path, filename=name)

    @app.exception_handler(HTTPException)
    async def http_exc(_request, exc):
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)

    app.add_middleware(PrefixMiddleware, configured_prefix=cfg.root_path)
    return app
