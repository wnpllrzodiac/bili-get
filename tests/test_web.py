import os, sys, time

import pytest
from fastapi.testclient import TestClient

from sole_bili_get.web import jobs as jobs_mod
from sole_bili_get.web.app import create_app
from sole_bili_get.web.config import Config
from sole_bili_get.web.urlcheck import normalize_bilibili_url

H = {'X-Requested-With': 'bili-web'}
GOOD = 'https://www.bilibili.com/video/BV1xx411c7mD'


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_mod, 'WORKER_CMD', [sys.executable, os.path.join(os.path.dirname(__file__), 'fake_worker.py')])
    def make(**cfg):
        app = create_app(Config(data_dir=str(tmp_path / 'data'), **cfg))
        return TestClient(app)
    return make


@pytest.fixture
def client(make_client):
    return make_client()


def login(c, pw='admin'):
    return c.post('api/login', json={'username': 'admin', 'password': pw}, headers=H)


@pytest.fixture
def authed(client):
    '''logged in, forced password change already done'''
    assert login(client).json()['must_change_password'] is True
    assert client.post('api/password', json={'old_password': 'admin', 'new_password': 'secret-pass'}, headers=H).status_code == 200
    assert login(client, 'secret-pass').json()['must_change_password'] is False
    return client


# ---------------- auth ----------------
def test_requires_login(client):
    assert client.get('api/me').status_code == 401
    assert client.get('api/jobs').status_code == 401
    assert client.get('healthz').status_code == 200
    assert client.get('/').status_code == 200 #the page itself is public, the data is not


def test_wrong_credentials(client):
    assert login(client, 'nope').status_code == 401
    assert client.post('api/login', json={'username': 'root', 'password': 'admin'}, headers=H).status_code == 401


def test_forced_password_change(client):
    r = login(client)
    assert r.status_code == 200 and r.json()['must_change_password'] is True
    assert 'HttpOnly' in r.headers['set-cookie']
    assert client.get('api/me').json()['must_change_password'] is True
    for method, url in (('get', 'api/jobs'), ('get', 'api/settings')):
        r = getattr(client, method)(url)
        assert r.status_code == 403 and r.json()['detail'] == 'password_change_required'
    assert client.post('api/analyze', json={'url': GOOD}, headers=H).status_code == 403
    bad = lambda pw: client.post('api/password', json={'old_password': 'admin', 'new_password': pw}, headers=H)
    assert bad('short').status_code == 400
    assert bad('admin').status_code == 400
    assert client.post('api/password', json={'old_password': 'wrong', 'new_password': 'secret-pass'}, headers=H).status_code == 400
    assert bad('secret-pass').status_code == 200
    assert client.get('api/me').status_code == 401 #sessions are invalidated, log in again
    assert login(client).status_code == 401 #default password no longer works
    assert login(client, 'secret-pass').json()['must_change_password'] is False
    assert client.get('api/jobs').status_code == 200


def test_old_session_dies_after_password_change(authed):
    old = authed.cookies.get('bili_web_session')
    assert authed.post('api/password', json={'old_password': 'secret-pass', 'new_password': 'another-pass'}, headers=H).status_code == 200
    authed.cookies.clear()
    authed.cookies.set('bili_web_session', old)
    assert authed.get('api/me').status_code == 401


def test_login_rate_limit(make_client):
    c = make_client(login_max_failures=3)
    for _ in range(3):
        assert login(c, 'bad').status_code == 401
    assert login(c, 'bad').status_code == 429
    assert login(c).status_code == 429 #even the right password is refused while locked


def test_csrf_header_required(authed):
    assert authed.post('api/analyze', json={'url': GOOD}).status_code == 403
    assert authed.put('api/settings', json={'cookie': 'x'}).status_code == 403
    assert authed.post('api/login', json={'username': 'admin', 'password': 'x'}).status_code == 403


def test_reset_password(tmp_path, monkeypatch):
    from sole_bili_get.web.__main__ import main
    monkeypatch.setenv('BILI_WEB_DATA_DIR', str(tmp_path / 'data'))
    c = TestClient(create_app(Config()))
    login(c)
    c.post('api/password', json={'old_password': 'admin', 'new_password': 'secret-pass'}, headers=H)
    assert login(c).status_code == 401
    assert main(['--reset-password']) == 0
    assert login(TestClient(create_app(Config()))).json()['must_change_password'] is True


# ---------------- url validation ----------------
@pytest.mark.parametrize('url', [
    'http://www.bilibili.com/video/BV1xx411c7mD', 'https://bilibili.com.evil.com/video/BV1xx411c7mD',
    'https://evil.com/?https://www.bilibili.com/video/BV1xx411c7mD', 'http://169.254.169.254/', 'file:///etc/passwd',
    'https://www.bilibili.com/video/BV1xx411c7mD/../../x', 'https://b23.tv/abc', '', 'BV1xx411c7mD'])
def test_url_rejected(url):
    assert normalize_bilibili_url(url) is None


def test_url_normalized():
    assert normalize_bilibili_url(GOOD) == GOOD and normalize_bilibili_url(GOOD + '/') == GOOD
    assert normalize_bilibili_url(GOOD + '/?p=3&spm=x') == GOOD + '?p=3'
    assert normalize_bilibili_url('https://m.bilibili.com/video/BV1xx411c7mD') == GOOD
    assert normalize_bilibili_url('https://www.bilibili.com/video/av170001').startswith('https://www.bilibili.com/video/BV')


# ---------------- analyze / settings / jobs ----------------
def test_analyze(authed):
    r = authed.post('api/analyze', json={'url': GOOD}, headers=H)
    assert r.status_code == 200
    assert r.json()['qualities'][0]['id'] == 32 and r.json()['url'].startswith('https://www.bilibili.com/video/')
    assert authed.post('api/analyze', json={'url': 'https://evil.com/'}, headers=H).status_code == 400
    r = authed.post('api/analyze', json={'url': 'https://www.bilibili.com/video/BV1fail00000'}, headers=H)
    assert r.status_code == 502 and 'failed to analyse' in r.json()['detail']


def test_cookie_is_server_side_only(authed):
    secret = 'SESSDATA=abcdefghijklmnop; bili_jct=1234'
    r = authed.put('api/settings', json={'cookie': secret}, headers=H)
    assert r.json()['cookie_set'] is True
    assert secret not in r.text and secret not in authed.get('api/settings').text
    info = authed.post('api/analyze', json={'url': GOOD}, headers=H).json()
    assert info['cookie_len'] == len(secret) #the worker received it through its environment
    assert authed.put('api/settings', json={'cookie': ''}, headers=H).json()['cookie_set'] is False


def wait_state(c, job_id, states, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f'api/jobs/{job_id}').json()
        if j['state'] in states:
            return j
        time.sleep(0.1)
    raise AssertionError(f'job stuck: {j}')


def test_job_lifecycle(authed):
    r = authed.post('api/jobs', json={'url': GOOD, 'quality': '32', 'mode': 'audio', 'danmu': True}, headers=H)
    assert r.status_code == 200
    j = wait_state(authed, r.json()['id'], ('done', 'failed'))
    assert j['state'] == 'done' and j['files'] == ['demo.mp4'] and j['params']['mode'] == 'audio' and j['params']['quality'] == '32'
    f = authed.get(f"api/jobs/{j['id']}/files/demo.mp4")
    assert f.status_code == 200 and f.content == b'fake-video:audio'
    assert j['media']['files']['demo.mp4']['height'] == 360 and j['media']['download']['speed'] > 0
    assert 'attachment' in f.headers['content-disposition']
    v = authed.get(f"api/jobs/{j['id']}/files/demo.mp4?inline=1", headers={'Range': 'bytes=0-3'}) #what the in-page player requests
    assert v.status_code == 206 and v.content == b'fake' and v.headers['content-disposition'].startswith('inline')
    assert authed.get(f"api/jobs/{j['id']}/files/..%2F..%2Fworker.log").status_code == 404
    assert authed.get(f"api/jobs/{j['id']}/files/worker.log").status_code == 404 #only recorded names are served
    assert [x['id'] for x in authed.get('api/jobs').json()] == [j['id']]
    assert authed.delete(f"api/jobs/{j['id']}", headers=H).status_code == 200
    assert authed.get(f"api/jobs/{j['id']}").status_code == 404


def test_job_failure_is_reported(authed):
    r = authed.post('api/jobs', json={'url': 'https://www.bilibili.com/video/BV1bad000000'}, headers=H)
    j = wait_state(authed, r.json()['id'], ('done', 'failed'))
    assert j['state'] == 'failed' and j['error'] == 'download failed'


def test_job_cancel(authed):
    r = authed.post('api/jobs', json={'url': 'https://www.bilibili.com/video/BV1slow00000'}, headers=H)
    job_id = r.json()['id']
    wait_state(authed, job_id, ('running',))
    assert authed.post(f'api/jobs/{job_id}/cancel', headers=H).status_code == 200
    assert wait_state(authed, job_id, ('canceled', 'failed'))['state'] == 'canceled'


def test_job_validation(authed):
    post = lambda **kw: authed.post('api/jobs', json=dict({'url': GOOD}, **kw), headers=H).status_code
    assert post() == 200
    assert post(quality='1080') == 200
    assert post(quality='abc') == 400
    assert post(mode='zip') == 400
    assert post(codec='x264') == 400
    assert post(codec='hvc1') == 200
    assert authed.post('api/jobs', json={'url': 'http://localhost:22/'}, headers=H).status_code == 400


def test_events_stream(authed):
    job_id = authed.post('api/jobs', json={'url': GOOD}, headers=H).json()['id']
    with authed.stream('GET', f'api/jobs/{job_id}/events') as r:
        assert r.headers['content-type'].startswith('text/event-stream')
        assert r.headers['x-accel-buffering'] == 'no'
        body = ''.join(r.iter_text())
    assert '"state": "done"' in body


# ---------------- sub-path / reverse proxy ----------------
def session_cookie(resp):
    return {'Cookie': resp.headers['set-cookie'].split(';')[0]}


@pytest.mark.parametrize('prefix_kept', [True, False]) #nginx proxy_pass without / keeps the prefix, with / strips it
def test_prefix_configured(make_client, prefix_kept):
    c = make_client(root_path='/hp/bilidown')
    pre = '/hp/bilidown' if prefix_kept else ''
    r = c.post(pre + '/api/login', json={'username': 'admin', 'password': 'admin'}, headers=H)
    assert r.status_code == 200
    assert 'Path=/hp/bilidown/' in r.headers['set-cookie'] #the cookie must not leak to other apps on the host
    c.cookies.clear() #the browser applies the cookie path to the external url, emulate it by sending it explicitly
    assert c.get(pre + '/api/me', headers=session_cookie(r)).status_code == 200
    assert c.get(pre + '/', headers=session_cookie(r)).status_code == 200
    assert c.get(pre + '/static/app.js').status_code == 200


def test_prefix_without_trailing_slash_redirects(make_client):
    c = make_client(root_path='/hp/bilidown')
    r = c.get('/hp/bilidown', follow_redirects=False)
    assert r.status_code == 308 and r.headers['location'] == '/hp/bilidown/'


def test_prefix_from_forwarded_header(client):
    h = dict(H, **{'X-Forwarded-Prefix': '/hp/bilidown', 'X-Forwarded-Proto': 'https'})
    r = client.post('api/login', json={'username': 'admin', 'password': 'admin'}, headers=h)
    client.cookies.clear()
    assert 'Path=/hp/bilidown/' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']
    assert client.get('/hp/bilidown/api/me', headers=dict(session_cookie(r), **{'X-Forwarded-Prefix': '/hp/bilidown'})).status_code == 200
    assert client.get('/api/me', headers=dict(session_cookie(r), **{'X-Forwarded-Prefix': '/hp/bilidown'})).status_code == 200


def test_invalid_forwarded_prefix_ignored(client):
    r = client.post('api/login', json={'username': 'admin', 'password': 'admin'},
                    headers=dict(H, **{'X-Forwarded-Prefix': '/a b;"<script>'}))
    assert 'Path=/;' in r.headers['set-cookie'] + ';' #the bogus prefix is ignored
    assert '<script>' not in r.headers['set-cookie']


def test_frontend_uses_relative_urls(client):
    html = client.get('/').text
    assert 'href="static/style.css"' in html and 'src="static/app.js"' in html
    js = client.get('/static/app.js').text
    assert "'api/" in js and "'/api/" not in js and '"/api/' not in js
    assert 'innerHTML' not in js #titles come from bilibili, they are only ever inserted as text
    assert client.get('/static/../app.py').status_code == 404


# ---------------- logs ----------------
def test_job_log_endpoint_and_server_log(authed):
    job_id = authed.post('api/jobs', json={'url': 'https://www.bilibili.com/video/BV1bad000000'}, headers=H).json()['id']
    j = wait_state(authed, job_id, ('failed',))
    assert j['log_file'].endswith(os.path.join(job_id, 'worker.log'))
    r = authed.get(f'api/jobs/{job_id}/log')
    assert r.status_code == 200 and 'hello-from-worker mode=download' in r.text
    assert authed.get('api/jobs/nope/log').status_code == 404
    srv = open(os.path.join(authed.app.state.cfg.data_dir, 'bili-web.log'), encoding='utf-8').read()
    assert f'job {job_id} created' in srv and f'job {job_id} failed: download failed' in srv
    assert 'hello-from-worker' in srv #the tail of the job log is copied into the server log on failure


def test_job_log_requires_login(client):
    assert client.get('api/jobs/abc/log').status_code == 401


def test_failed_login_is_logged(client):
    login(client, 'wrong')
    srv = open(os.path.join(client.app.state.cfg.data_dir, 'bili-web.log'), encoding='utf-8').read()
    assert 'failed login from' in srv


# ---------------- worker diagnosis (the real worker with a stubbed downloader, no network) ----------------
class FakeBili:
    result = {}

    def __init__(self, url, **kw):
        import logging
        self.logger = logging.getLogger('FakeBili')
        self.playlists_info = {}

    def start(self):
        self.playlists_info = dict(FakeBili.result)


def run_worker(monkeypatch, tmp_path, playlists, mode='merged', playlist=False):
    import io, json
    from sole_bili_get import bili_get
    from sole_bili_get.web import worker
    FakeBili.result = playlists
    monkeypatch.setattr(bili_get, 'Bilibili', FakeBili)
    events = io.StringIO()
    monkeypatch.setattr(worker, '_events', events)
    monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/ffmpeg')
    rc = worker.do_download({'url': GOOD, 'work_dir': str(tmp_path / 'w'), 'mode': mode, 'quality': 'MAX',
                            'playlist': playlist})
    evts = [json.loads(l[len(worker.EVENT_PREFIX):]) for l in events.getvalue().splitlines() if l.startswith(worker.EVENT_PREFIX)]
    return rc, [e for e in evts if e['type'] in ('error', 'result')][-1]


def test_worker_reports_why_it_failed(monkeypatch, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    rc, e = run_worker(monkeypatch, tmp_path, {1: {'download_flag': 0, 'merge_flag': False, 'title': 't'}})
    assert rc == 1 and 'p1: video download failed' in e['message'] and 'job log' in e['message']
    rc, e = run_worker(monkeypatch, tmp_path, {1: {'download_flag': 1, 'merge_flag': False, 'title': 't'}})
    assert 'p1: audio download failed' in e['message']
    rc, e = run_worker(monkeypatch, tmp_path, {1: {'download_flag': 3, 'merge_flag': False, 'title': 't'}})
    assert 'p1: downloaded, but ffmpeg merge failed' in e['message']
    rc, e = run_worker(monkeypatch, tmp_path, {})
    assert 'no episode information' in e['message']
    rc, e = run_worker(monkeypatch, tmp_path, {1: {'download_flag': 3, 'merge_flag': True, 'title': 't'}, 2: {'download_flag': 0, 'title': 't2'}}, playlist=True)
    assert 'p2: video download failed' in e['message'] and 'p1' not in e['message']
    assert 'episode p1: download_flag=3' in caplog.text and 'job failed:' in caplog.text


def test_worker_success(monkeypatch, tmp_path):
    rc, e = run_worker(monkeypatch, tmp_path, {1: {'download_flag': 3, 'merge_flag': True, 'title': 'My title'}})
    assert rc == 0 and e['type'] == 'result' and e['title'] == 'My title'


def test_worker_unhandled_exception_is_reported(monkeypatch, tmp_path):
    import io, json
    from sole_bili_get.web import worker
    events = io.StringIO()
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'url': GOOD, 'work_dir': str(tmp_path)})))
    monkeypatch.setattr('sys.stdout', events)
    assert worker.main(['bogus-mode']) == 1
    assert 'see the job log for the traceback' in events.getvalue()


def test_merge_failure_keeps_ffmpeg_stderr(tmp_path):
    """the downloader used to log only 'returned non-zero exit status', the real ffmpeg error was lost"""
    import logging, shutil
    if shutil.which('ffmpeg') is None:
        pytest.skip('ffmpeg not installed')
    from sole_bili_get.bili_get import Bilibili
    b = Bilibili(GOOD, base_dir=str(tmp_path), auto_merge=True, disable_console_log=True)
    b._Bilibili__ffmpeg_exist = True
    v, a = tmp_path / 'v.mp4', tmp_path / 'a.mp3'
    v.write_bytes(b'not a video')
    a.write_bytes(b'not audio')
    records = []

    class H(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())
    b.logger.addHandler(H())
    assert b.merge_video_and_audio(str(v), str(a)) is False
    msg = '\n'.join(records)
    assert 'merge failed, ffmpeg exit code' in msg and 'Invalid data found' in msg


# ---------------- job titles ----------------
def test_job_title_is_kept_when_the_job_fails(authed):
    r = authed.post('api/jobs', json={'url': 'https://www.bilibili.com/video/BV1bad000000', 'title': '  My   video\n title '}, headers=H)
    j = wait_state(authed, r.json()['id'], ('failed',))
    assert j['title'] == 'My video title' #normalised whitespace, known before the download started


def test_job_title_is_limited_and_optional(authed):
    j = authed.post('api/jobs', json={'url': GOOD, 'title': 'x' * 1000}, headers=H).json()
    assert len(authed.get(f"api/jobs/{j['id']}").json()['title']) == 200
    j2 = wait_state(authed, authed.post('api/jobs', json={'url': GOOD}, headers=H).json()['id'], ('done',))
    assert j2['title'] == 'demo' #without a title from the page, the worker's result fills it in


def test_worker_error_carries_the_title(monkeypatch, tmp_path):
    rc, e = run_worker(monkeypatch, tmp_path, {1: {'download_flag': 0, 'merge_flag': False, 'title': 'Some title'}})
    assert e['type'] == 'error' and e['title'] == 'Some title'


# ---------------- multi-episode video, only one episode requested (seen in a real job log) ----------------
def placeholders(first, n=15):
    eps = {p: {'download_flag': 0, 'merge_flag': False, 'title': f'p{p}'} for p in range(2, n + 1)}
    eps[1] = dict(first, title='p1')
    return eps


def test_other_episodes_placeholders_are_not_failures(monkeypatch, tmp_path):
    eps = placeholders({'download_flag': 3, 'merge_flag': True})
    rc, e = run_worker(monkeypatch, tmp_path, eps)
    assert rc == 0 and e['type'] == 'result'


def test_requested_episode_from_url_is_judged(monkeypatch, tmp_path):
    from sole_bili_get.web import worker
    eps = placeholders({'download_flag': 0, 'merge_flag': False})
    eps[3] = {'download_flag': 3, 'merge_flag': True, 'title': 'p3'}
    monkeypatch.setattr(worker, '_events', None, raising=False)
    import io, json
    from sole_bili_get import bili_get
    FakeBili.result = eps
    monkeypatch.setattr(bili_get, 'Bilibili', FakeBili)
    monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/ffmpeg')
    ev = io.StringIO()
    monkeypatch.setattr(worker, '_events', ev)
    rc = worker.do_download({'url': GOOD + '?p=3', 'work_dir': str(tmp_path / 'w'), 'mode': 'merged', 'quality': 'MAX'})
    assert rc == 0 #p3 is fine, the failed p1 placeholder was not requested


def test_download_all_still_reports_missing_episodes(monkeypatch, tmp_path):
    from sole_bili_get.web import worker
    import io
    from sole_bili_get import bili_get
    FakeBili.result = placeholders({'download_flag': 3, 'merge_flag': True}, 3)
    monkeypatch.setattr(bili_get, 'Bilibili', FakeBili)
    monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/ffmpeg')
    monkeypatch.setattr(worker, '_events', io.StringIO())
    rc = worker.do_download({'url': GOOD, 'work_dir': str(tmp_path / 'w'), 'mode': 'merged', 'quality': 'MAX', 'playlist': True})
    assert rc == 1
    assert 'p2: video download failed' in worker._events.getvalue() and 'p3: video download failed' in worker._events.getvalue()


# ---------------- data directory ----------------
def test_default_data_dir_is_inside_the_app_folder(monkeypatch, tmp_path):
    from sole_bili_get.web import config
    monkeypatch.delenv('BILI_WEB_DATA_DIR', raising=False)
    assert config.default_data_dir() == os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))), 'bili-web-data')
    assert not config.default_data_dir().startswith(os.path.expanduser('~/.bili-web'))


def test_default_data_dir_falls_back_to_cwd_when_installed(monkeypatch, tmp_path):
    from sole_bili_get.web import config
    monkeypatch.setattr(config.os.path, 'isfile', lambda p: False) #no setup.py next to the package: a pip install
    monkeypatch.chdir(tmp_path)
    assert config.default_data_dir() == os.path.join(str(tmp_path), 'bili-web-data')


def test_env_var_overrides_and_dir_is_private(monkeypatch, tmp_path):
    monkeypatch.setenv('BILI_WEB_DATA_DIR', str(tmp_path / 'elsewhere'))
    cfg = Config()
    assert cfg.data_dir == str(tmp_path / 'elsewhere')
    assert oct(os.stat(cfg.data_dir).st_mode & 0o777) == '0o700'
    assert os.path.isdir(cfg.jobs_dir)


# ---------------- save folder setting ----------------
def test_save_folder_setting_validation(authed, tmp_path):
    put = lambda **kw: authed.put('api/settings', json=kw, headers=H)
    assert authed.get('api/settings').json()['save_dir'] == ''
    r = put(save_dir='relative/path')
    assert r.status_code == 400 and 'absolute' in r.json()['detail']
    blocker = tmp_path / 'a-file'
    blocker.write_text('x')
    assert put(save_dir=str(blocker / 'sub')).status_code == 400 #a file is in the way, can't create the folder
    target = tmp_path / 'my' / 'videos'
    r = put(save_dir=str(target))
    assert r.status_code == 200 and r.json()['save_dir'] == str(target) and target.is_dir()
    assert list(target.iterdir()) == [] #the write probe is removed again
    assert r.json()['free_bytes'] > 0
    assert authed.get('api/settings').json()['save_dir'] == str(target)
    assert put(save_dir='').json()['save_dir'] == '' #back to the default


def test_settings_fields_are_independent(authed, tmp_path):
    authed.put('api/settings', json={'cookie': 'SESSDATA=abcdefghijkl'}, headers=H)
    r = authed.put('api/settings', json={'save_dir': str(tmp_path / 'v')}, headers=H).json()
    assert r['cookie_set'] is True and r['save_dir'] == str(tmp_path / 'v') #saving the folder doesn't wipe the cookie
    r = authed.put('api/settings', json={'cookie': ''}, headers=H).json()
    assert r['cookie_set'] is False and r['save_dir'] == str(tmp_path / 'v') #and the other way round


def test_job_saves_into_the_save_folder(authed, tmp_path):
    target = tmp_path / 'videos'
    authed.put('api/settings', json={'save_dir': str(target)}, headers=H)
    j = wait_state(authed, authed.post('api/jobs', json={'url': GOOD}, headers=H).json()['id'], ('done', 'failed'))
    assert j['state'] == 'done' and j['out_dir'] == str(target)
    assert (target / 'demo.mp4').read_bytes() == b'fake-video:merged'
    assert authed.get(f"api/jobs/{j['id']}/files/demo.mp4").content == b'fake-video:merged' #served from where it was saved
    authed.delete(f"api/jobs/{j['id']}", headers=H)
    assert (target / 'demo.mp4').exists() #deleting a job never deletes the user's files
    assert not os.path.exists(os.path.join(authed.app.state.cfg.jobs_dir, j['id']))


def test_jobs_before_the_setting_changed_keep_working(authed, tmp_path):
    j = wait_state(authed, authed.post('api/jobs', json={'url': GOOD}, headers=H).json()['id'], ('done',))
    authed.put('api/settings', json={'save_dir': str(tmp_path / 'later')}, headers=H)
    assert authed.get(f"api/jobs/{j['id']}/files/demo.mp4").status_code == 200 #still served from its recorded folder
    assert j['out_dir'].endswith(os.path.join(j['id'], 'files'))


def test_old_database_gets_the_out_dir_column(tmp_path):
    import sqlite3
    from sole_bili_get.web.db import Database
    path = str(tmp_path / 'old.sqlite3')
    c = sqlite3.connect(path)
    c.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL,"
                    " stage TEXT DEFAULT '', percent REAL DEFAULT 0, title TEXT DEFAULT '', error TEXT DEFAULT '',"
                    " files TEXT DEFAULT '[]', created REAL NOT NULL, updated REAL NOT NULL);"
                    "INSERT INTO jobs(id,url,params,state,created,updated) VALUES('x','u','{}','done',1,1);")
    c.commit(); c.close()
    db = Database(path)
    assert db.get_job('x')['out_dir'] == ''


class FilesBili(FakeBili):
    '''pretends to have downloaded a file'''
    def __init__(self, url, base_dir=None, **kw):
        super().__init__(url, **kw)
        self.base = base_dir

    def start(self):
        d = os.path.join(self.base, 'bili_output', 'BV1xx411c7mD')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'video.mp4'), 'wb') as f:
            f.write(b'new')
        self.playlists_info = dict(FakeBili.result)


def run_files_worker(monkeypatch, tmp_path, playlists, save_dir):
    import io, json
    from sole_bili_get import bili_get
    from sole_bili_get.web import worker
    FakeBili.result = playlists
    monkeypatch.setattr(bili_get, 'Bilibili', FilesBili)
    monkeypatch.setattr('shutil.which', lambda n: '/usr/bin/ffmpeg')
    ev = io.StringIO()
    monkeypatch.setattr(worker, '_events', ev)
    work = tmp_path / 'work'
    rc = worker.do_download({'url': GOOD, 'work_dir': str(work), 'mode': 'merged', 'quality': 'MAX', 'save_dir': save_dir})
    evts = [json.loads(l[len(worker.EVENT_PREFIX):]) for l in ev.getvalue().splitlines() if l.startswith(worker.EVENT_PREFIX)]
    return rc, evts[-1], work


def test_existing_files_are_never_overwritten(monkeypatch, tmp_path):
    save = tmp_path / 'save'
    save.mkdir()
    (save / 'video.mp4').write_bytes(b'precious')
    ok = {1: {'download_flag': 3, 'merge_flag': True, 'title': 't'}}
    rc, e, _ = run_files_worker(monkeypatch, tmp_path, ok, str(save))
    assert rc == 0 and e['files'] == ['video (1).mp4'] and e['out_dir'] == str(save)
    assert (save / 'video.mp4').read_bytes() == b'precious' and (save / 'video (1).mp4').read_bytes() == b'new'
    rc, e, _ = run_files_worker(monkeypatch, tmp_path / 'again', ok, str(save))
    assert e['files'] == ['video (2).mp4']


def test_failed_job_does_not_touch_the_save_folder(monkeypatch, tmp_path):
    save = tmp_path / 'save'
    save.mkdir()
    bad = {1: {'download_flag': 1, 'merge_flag': False, 'title': 't'}}
    rc, e, work = run_files_worker(monkeypatch, tmp_path, bad, str(save))
    assert rc == 1 and list(save.iterdir()) == []
    assert (work / 'files' / 'video.mp4').exists() #partial result stays in the job folder for debugging


SAMPLE = """Input #0, mov,mp4,m4a,fragmented, from 'a.mp4':
  Duration: 02:20:41.23, start: 0.000000, bitrate: 430 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 640x360 [SAR 1:1 DAR 16:9], 300 kb/s, 25 fps, 25 tbr, 12800 tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 128 kb/s (default)
At least one output file must be specified
"""


def test_parse_media_info():
    from sole_bili_get.web.worker import parse_media_info
    m = parse_media_info(SAMPLE)
    assert (m['width'], m['height'], m['vcodec'], m['acodec']) == (640, 360, 'h264', 'aac')
    assert m['bitrate'] == 430000 and m['vbitrate'] == 300000 and m['abitrate'] == 128000
    assert abs(m['duration'] - 8441.23) < 0.01
    assert parse_media_info('garbage') == {}
    mp3 = SAMPLE.replace('Video: h264', 'Video: mjpeg').replace('640x360 [SAR 1:1 DAR 16:9]', '300x300') .replace('(default)', '(attached pic)', 1)
    assert 'vcodec' not in parse_media_info(mp3) #cover art in an audio file is not a video stream
    assert parse_media_info('  Duration: N/A, bitrate: N/A') == {}
