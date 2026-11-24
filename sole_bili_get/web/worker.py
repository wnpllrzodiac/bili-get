'''Subprocess entry: `python -m sole_bili_get.web.worker analyze|download`

One subprocess per task because the downloader keeps process-wide state (sole_bili_get.utils._base_dir,
pid based locks). Request json comes from stdin, the bilibili cookie from env BILI_COOKIE, events go to
stdout as lines prefixed by EVENT_PREFIX (everything else printed by the downloader is discarded).
'''
import json, logging, os, re, shutil, subprocess, sys, time, traceback

EVENT_PREFIX = '@@EVT@@ '
_events = None
log = logging.getLogger('bili-worker')


def setup_logging():
    '''Everything goes to stderr, which the server stores as <job dir>/worker.log. The downloader's own logger is
    attached too (its file log lives in bili_tmp/, which is deleted when the job ends).'''
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format='%(asctime)s %(levelname)s [%(name)s] %(message)s')


def attach_core_logger(bili):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [core:%(funcName)s:%(lineno)s] %(message)s'))
    bili.logger.addHandler(handler)


def unique_path(directory, name):
    '''never overwrite a file the user already has: "a.mp4" -> "a (1).mp4" -> "a (2).mp4"'''
    base, ext = os.path.splitext(name)
    path, i = os.path.join(directory, name), 0
    while os.path.exists(path):
        i += 1
        path = os.path.join(directory, f'{base} ({i}){ext}')
    return path


def describe_files(root):
    out = []
    for r, _d, files in os.walk(root):
        for f in sorted(files):
            fp = os.path.join(r, f)
            out.append(f'{os.path.relpath(fp, root)} ({os.path.getsize(fp)} bytes)')
    return out


_DUR = re.compile(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)(?:, start: [\d.]+)?(?:, bitrate: (\d+) kb/s)?')
_VID = re.compile(r'Stream #\S+: Video: (\w+).*?, (\d{2,5})x(\d{2,5})')
_AUD = re.compile(r'Stream #\S+: Audio: (\w+)')
_KBS = re.compile(r'(\d+) kb/s')


def parse_media_info(text):
    '''parses the stderr of `ffmpeg -i <file>` (ffprobe is not always installed next to ffmpeg)'''
    info = {}
    m = _DUR.search(text)
    if m:
        info['duration'] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        if m[4]:
            info['bitrate'] = int(m[4]) * 1000
    for line in text.splitlines():
        v, a = _VID.search(line), _AUD.search(line)
        if v and 'vcodec' not in info and 'attached pic' not in line: #a cover image inside an mp3 is not a video
            info['vcodec'], info['width'], info['height'] = v[1], int(v[2]), int(v[3])
            k = _KBS.search(line)
            if k:
                info['vbitrate'] = int(k[1]) * 1000
        elif a and 'acodec' not in info:
            info['acodec'] = a[1]
            k = _KBS.search(line)
            if k:
                info['abitrate'] = int(k[1]) * 1000
    return info


def probe_file(ffmpeg, path):
    if not ffmpeg:
        return {}
    try:
        r = subprocess.run([ffmpeg, '-hide_banner', '-i', path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=30) #exits 1 because there is no output file, that is expected
        info = parse_media_info(r.stderr.decode('utf-8', 'replace'))
    except Exception as e:
        log.warning('cannot probe %s: %s', path, e)
        return {}
    info['size'] = os.path.getsize(path)
    return info


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
    log.info('analyze %s (cookie %s)', req['url'], 'set' if os.environ.get('BILI_COOKIE') else 'not set')
    b = Bilibili(req['url'], base_dir=base, cookies=_cookies(), disable_console_log=False, headers={})
    attach_core_logger(b)
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
    ffmpeg = shutil.which('ffmpeg')
    log.info('download %s mode=%s quality=%s codec=%s playlist=%s danmu=%s cover=%s cookie=%s ffmpeg=%s python=%s',
             req['url'], mode, req.get('quality'), req.get('codec') or '-', req.get('playlist'), req.get('danmu'),
             req.get('cover'), 'set' if os.environ.get('BILI_COOKIE') else 'not set', ffmpeg, sys.version.split()[0])
    if mode == 'merged' and ffmpeg is None:
        log.error('ffmpeg not found in PATH=%s', os.environ.get('PATH'))
        emit(type='error', message='ffmpeg is not installed on the server, cannot merge video and audio '
                                   '(choose "video only"/"audio only" or install ffmpeg)')
        return 1
    work = req['work_dir']
    os.makedirs(work, exist_ok=True)
    stages = {'merged': ['video', 'audio'], 'video': ['video'], 'audio': ['audio']}[mode]
    st = {'idx': 0, 'last': 0.0, 't0': None, 't1': None, 'bytes': 0} #t0..t1 = time spent downloading video/audio

    def on_progress(title, count, total):
        done = total > 0 and count >= total
        stage = 'cover' if 'image' in str(title) else (stages[min(st['idx'], len(stages) - 1)])
        now = time.time()
        if stage != 'cover' and st['t0'] is None:
            st['t0'] = now
        if done and stage != 'cover':
            st['bytes'] += total
            st['t1'] = now
        if done or now - st['last'] >= 0.5:
            st['last'] = now
            emit(type='progress', stage=stage, percent=round(100.0 * count / total, 1) if total else 0,
                 done=count, total=total)
        if done:
            log.info('%s finished: %s bytes (%s)', stage, total, title)
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
    attach_core_logger(b)
    emit(type='progress', stage='analyzing', percent=0)
    b.start()
    # why did (not) it work: one line per episode with the internal state
    problems = []
    known = {p: v for p, v in b.playlists_info.items() if isinstance(v, dict) and 'download_flag' in v}
    for p, v in sorted(known.items()):
        log.info('episode p%s: download_flag=%s (3=video+audio ok) merge_flag=%s video=%s audio=%s', p, v.get('download_flag'),
                 v.get('merge_flag'), v.get('video_save_path'), v.get('audio_save_path'))
    # The downloader keeps a placeholder for every episode of a multi-episode video. Without "download all" only
    # the requested one (?p=N in the url, default p1) is downloaded, the placeholders of the others are not failures.
    requested = int((re.search(r'[?&]p=(\d+)', req['url']) or [0, 1])[1])
    eps = known if req.get('playlist') else {p: v for p, v in known.items() if p == requested}
    if not eps:
        problems.append('no episode information was obtained (page could not be analysed, or blocked by bilibili)')
    for p, v in sorted(eps.items()):
        flag, merged = v.get('download_flag'), v.get('merge_flag')
        if flag != 3:
            what = {0: 'video download failed', 1: 'audio download failed'}.get(flag, f'incomplete download (flag {flag})')
            if mode == 'audio':
                what = 'audio download failed'
            elif mode == 'video':
                what = 'video download failed'
            problems.append(f'p{p}: {what}')
        elif mode == 'merged' and not merged:
            problems.append(f'p{p}: downloaded, but ffmpeg merge failed')
    ok = not problems
    # collect results into <work>/files
    out_root = os.path.join(work, 'bili_output')
    tmp_root = os.path.join(work, 'bili_tmp')
    log.info('files in output dir: %s', describe_files(out_root) if os.path.isdir(out_root) else 'none')
    # finished files go to the configured save folder; the files of a failed job stay in the job dir (for debugging)
    save_dir = (req.get('save_dir') or '') if ok else ''
    files_dir = save_dir or os.path.join(work, 'files')
    os.makedirs(files_dir, exist_ok=True)
    log.info('moving results into %s%s', files_dir, ' (save folder)' if save_dir else ' (job dir)')
    names = []
    if os.path.isdir(out_root):
        for root, _dirs, files in os.walk(out_root):
            for f in sorted(files):
                if mode == 'video' and f.endswith('_audio.mp3'):
                    continue
                dest = unique_path(files_dir, f)
                shutil.move(os.path.join(root, f), dest)
                names.append(os.path.basename(dest))
    shutil.rmtree(out_root, ignore_errors=True)
    shutil.rmtree(tmp_root, ignore_errors=True)
    if not ok:
        log.error('job failed: %s', '; '.join(problems))
        emit(type='error', message='; '.join(problems) + ' (see the job log for details)',
             title=(next(iter(eps.values())).get('title') or '') if eps else '')
        return 1
    log.info('job ok: %s', names)
    media = {'files': {}}
    for n in names:
        if re.search(r'\.(mp4|m4v|webm|mp3|m4a|aac)$', n, re.I):
            media['files'][n] = probe_file(ffmpeg, os.path.join(files_dir, n))
    if st['t0'] is not None and st['t1'] is not None and st['bytes']:
        secs = max(st['t1'] - st['t0'], 0.001)
        media['download'] = {'bytes': st['bytes'], 'seconds': round(secs, 2), 'speed': int(st['bytes'] / secs)}
    log.info('media info: %s', media)
    emit(type='result', media=media, files=names, out_dir=files_dir, title=(next(iter(eps.values())).get('title') or ''))
    return 0


def main(argv=None):
    global _events
    argv = argv if argv is not None else sys.argv[1:]
    _events = sys.stdout
    sys.stdout = open(os.devnull, 'w', encoding='utf-8') #the downloader prints a lot, keep stdout clean for events
    setup_logging()
    req = json.loads(sys.stdin.read())
    try:
        return {'analyze': do_analyze, 'download': do_download}[argv[0]](req)
    except Exception as e:
        log.error('unhandled exception:\n%s', traceback.format_exc())
        emit(type='error', message=f'{type(e).__name__}: {e} (see the job log for the traceback)')
        return 1


if __name__ == '__main__':
    sys.exit(main())
