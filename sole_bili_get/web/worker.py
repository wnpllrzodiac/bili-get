'''Subprocess entry: `python -m sole_bili_get.web.worker analyze|download`

One subprocess per task because the downloader keeps process-wide state (sole_bili_get.utils._base_dir,
pid based locks). Request json comes from stdin, the bilibili cookie from env BILI_COOKIE, events go to
stdout as lines prefixed by EVENT_PREFIX (everything else printed by the downloader is discarded).
'''
import json, os, shutil, sys, time

EVENT_PREFIX = '@@EVT@@ '
_events = None


def emit(**evt):
    _events.write(EVENT_PREFIX + json.dumps(evt, ensure_ascii=False) + '\n')
    _events.flush()


def _cookies():
    from ..utils import parse_cookies
    raw = os.environ.get('BILI_COOKIE', '').strip()
    return parse_cookies(raw) if raw else None


def do_analyze(req):
    from ..bili_get import Bilibili
    base = req['work_dir']
    os.makedirs(base, exist_ok=True)
    b = Bilibili(req['url'], base_dir=base, cookies=_cookies(), disable_console_log=False, headers={})
    info = b.analyze()
    if info is None:
        emit(type='error', message='failed to analyse the page (invalid video, or blocked by bilibili)')
        return 1
    emit(type='result', info=info)
    return 0


def do_download(req):
    from .. import utils
    from ..bili_get import Bilibili
    mode = req.get('mode', 'merged') #merged | video | audio
    if mode == 'merged' and shutil.which('ffmpeg') is None:
        emit(type='error', message='ffmpeg is not installed on the server, cannot merge video and audio '
                                   '(choose "video only"/"audio only" or install ffmpeg)')
        return 1
    work = req['work_dir']
    os.makedirs(work, exist_ok=True)
    stages = {'merged': ['video', 'audio'], 'video': ['video'], 'audio': ['audio']}[mode]
    st = {'idx': 0, 'last': 0.0}

    def on_progress(title, count, total):
        done = total > 0 and count >= total
        stage = 'cover' if 'image' in str(title) else (stages[min(st['idx'], len(stages) - 1)])
        now = time.time()
        if done or now - st['last'] >= 0.5:
            st['last'] = now
            emit(type='progress', stage=stage, percent=round(100.0 * count / total, 1) if total else 0,
                 done=count, total=total)
        if done and stage != 'cover':
            st['idx'] += 1
            if st['idx'] >= len(stages) and mode == 'merged':
                emit(type='progress', stage='merging', percent=100)
    utils.progress_callback = on_progress

    quality = str(req.get('quality') or 'MAX').upper()
    b = Bilibili(req['url'], base_dir=work, cookies=_cookies(), headers={}, quality=quality,
                 codec=req.get('codec') or None, fetch_playlists=bool(req.get('playlist')),
                 auto_merge=(mode == 'merged'), download_video=(mode != 'audio'), download_audio=(mode != 'video'),
                 download_danmu=bool(req.get('danmu')), download_cover=bool(req.get('cover')),
                 disable_console_log=True, multi_thread_num=int(req.get('threads', 0)))
    emit(type='progress', stage='analyzing', percent=0)
    b.start()
    eps = [v for v in b.playlists_info.values() if isinstance(v, dict) and 'download_flag' in v]
    ok = bool(eps) and all(v['download_flag'] == 3 and (mode != 'merged' or v.get('merge_flag')) for v in eps)
    # collect results into <work>/files
    out_root = os.path.join(work, 'bili_output')
    files_dir = os.path.join(work, 'files')
    os.makedirs(files_dir, exist_ok=True)
    names = []
    if os.path.isdir(out_root):
        for root, _dirs, files in os.walk(out_root):
            for f in sorted(files):
                if mode == 'video' and f.endswith('_audio.mp3'):
                    continue
                shutil.move(os.path.join(root, f), os.path.join(files_dir, f))
                names.append(f)
    shutil.rmtree(out_root, ignore_errors=True)
    shutil.rmtree(os.path.join(work, 'bili_tmp'), ignore_errors=True)
    if not ok:
        emit(type='error', message='download failed (see the worker log); partially downloaded files were discarded' if not names
             else 'some episodes failed to download or merge')
        return 1
    emit(type='result', files=names, title=(eps[0].get('title') or ''))
    return 0


def main(argv=None):
    global _events
    argv = argv if argv is not None else sys.argv[1:]
    _events = sys.stdout
    sys.stdout = open(os.devnull, 'w', encoding='utf-8') #the downloader prints a lot, keep stdout clean for events
    req = json.loads(sys.stdin.read())
    try:
        return {'analyze': do_analyze, 'download': do_download}[argv[0]](req)
    except Exception as e:
        emit(type='error', message=f'{type(e).__name__}: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
