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
