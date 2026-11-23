'''Stand-in for sole_bili_get.web.worker: same stdin/stdout protocol, no network.'''
import json, os, sys, time

mode = sys.argv[1]
req = json.loads(sys.stdin.read())
out = lambda **e: (print('@@EVT@@ ' + json.dumps(e), flush=True))
print('noise that must be ignored')
print('hello-from-worker mode=' + mode, file=sys.stderr, flush=True)
if mode == 'analyze':
    if 'BV1fail' in req['url']:
        out(type='error', message='failed to analyse')
        sys.exit(1)
    out(type='result', info={'bvid': 'BV1xx411c7mD', 'title': 'demo', 'desc': '', 'author': 'me', 'cover': '', 'episodes': 1,
                             'current_episode': 1, 'pages': [], 'has_audio': True, 'cookie_len': len(os.environ.get('BILI_COOKIE', '')),
                             'qualities': [{'id': 32, 'name': '480P', 'width': 852, 'height': 480, 'codecs': ['avc1']}]})
else:
    out(type='progress', stage='video', percent=50)
    if 'slow' in req['url']:
        time.sleep(60)
    if 'BV1bad' in req['url']:
        out(type='error', message='download failed')
        sys.exit(1)
    d = os.path.join(req['work_dir'], 'files')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'demo.mp4'), 'wb') as f:
        f.write(b'fake-video:' + req['mode'].encode())
    out(type='result', files=['demo.mp4'], title='demo')
