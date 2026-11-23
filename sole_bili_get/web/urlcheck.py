import re

__all__ = ['normalize_bilibili_url']

_RE = re.compile(r'^https://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)/?(?:\?(.*))?$')


def normalize_bilibili_url(url):
    '''Only plain bilibili video urls are accepted (SSRF protection). Returns the canonical url or None.'''
    m = _RE.match((url or '').strip())
    if not m:
        return None
    vid, query = m.group(1), m.group(2) or ''
    if vid.startswith('av'):
        from ..av2bv import av2bv
        try:
            vid = av2bv(int(vid[2:]))
        except Exception:
            return None
    p = re.search(r'(?:^|&)p=(\d+)(?:&|$)', query)
    #no trailing slash: with it bilibili returns a different (smaller) page that analyse_playinfo can't parse
    return f'https://www.bilibili.com/video/{vid}' + (f'?p={p.group(1)}' if p else '')
