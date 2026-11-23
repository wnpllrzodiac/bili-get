import argparse, sys


def main(argv=None):
    parser = argparse.ArgumentParser(prog='bili-web', description='Web front end of bili-get')
    parser.add_argument('--host', help='listen address, default 127.0.0.1 (env BILI_WEB_HOST); use 0.0.0.0 for LAN access')
    parser.add_argument('--port', type=int, help='listen port, default 8000 (env BILI_WEB_PORT)')
    parser.add_argument('--root-path', help='sub-path prefix behind a reverse proxy, e.g. /hp/bilidown (env BILI_WEB_ROOT_PATH)')
    parser.add_argument('--data-dir', help='where the database and downloads are stored, default ~/.bili-web (env BILI_WEB_DATA_DIR)')
    parser.add_argument('--reset-password', action='store_true', help='reset the admin password to the default (admin) and exit')
    args = parser.parse_args(argv)

    from .config import Config, normalize_prefix
    cfg = Config()
    if args.host: cfg.host = args.host
    if args.port: cfg.port = args.port
    if args.root_path is not None: cfg.root_path = normalize_prefix(args.root_path)
    if args.data_dir:
        import os
        cfg = Config(data_dir=os.path.abspath(os.path.expanduser(args.data_dir)), host=cfg.host, port=cfg.port, root_path=cfg.root_path)

    if args.reset_password:
        from .auth import AuthManager
        from .db import Database
        AuthManager(Database(cfg.db_path)).set_default_password()
        print('admin password has been reset to "admin", you must change it at the next login')
        return 0

    try:
        import uvicorn
    except ImportError:
        print('missing web dependencies, install them with: pip install "sole-bili-get[web]"  (or pip install -e ".[web]")', file=sys.stderr)
        return 1
    from .app import create_app
    # trust X-Forwarded-* only from the listed proxies, default is the local machine (a proxy on the same host)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, proxy_headers=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
