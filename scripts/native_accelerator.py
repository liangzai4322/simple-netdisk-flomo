#!/usr/bin/env python3
"""Loopback transport for the paired netdisk. Google credentials never enter JS."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CONFIG_DIR = Path.home() / 'Library/Application Support/simple-netdisk-accelerator'
CONFIG = json.loads((CONFIG_DIR / 'config.json').read_text())
APP = Path(__file__).resolve().parent
INBOX = Path('/Volumes/Samsung SSD/Inbox')
if not Path('/Volumes/Samsung SSD').is_mount() or not os.access(INBOX, os.W_OK):
    raise RuntimeError('Samsung SSD Inbox must be mounted and writable')
SPOOL = INBOX / '.simple-netdisk-accelerator'
SPOOL.mkdir(mode=0o700, exist_ok=True)
os.chmod(SPOOL, 0o700)
os.environ.update(SIMPLE_NETDISK_ROOT=str(SPOOL/'files'), SIMPLE_NETDISK_UPLOADS=str(SPOOL/'uploads'),
                  SIMPLE_NETDISK_PASSWORD=CONFIG['password'])
sys.path.insert(0, str(APP/'backend'))
import simple_netdisk_app as core
from drive_storage import DriveStorage, DriveError

ORIGIN = 'https://liangzai4322.github.io'
PORT = 18743
DOWNLOADS = {}
DOWNLOAD_LOCK = threading.Lock()
DOWNLOAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def broker(action, data):
    request = Request(CONFIG['api']+'/accelerator/'+action, method='POST',
                      data=json.dumps(data).encode(), headers={
                          'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 NetdiskAccelerator/1',
                          'X-Netdisk-Helper-Key': CONFIG['helper_key'], 'X-Upload-Password': CONFIG['password']})
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except Exception:
        raise DriveError('网盘连接失败，请重试；本机上传进度已保留') from None

def gdirect(arguments, token):
    environment = os.environ.copy()
    environment.update(GDIRECT_ACCESS_TOKEN=token, DYLD_INSERT_LIBRARIES=str(APP/'bind_physical.dylib'),
                       NETDISK_PHYSICAL_INTERFACE=CONFIG['interface'])
    command = [CONFIG['gdirect'], '--db', str(CONFIG_DIR/'routes.sqlite3'), '--google-only'] + arguments
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        raise DriveError('Google 直连传输不可用，请重试') from None
    if result.returncode:
        raise DriveError('Google 直连传输失败，请重试或使用普通通道')
    return result.stdout

class NativeDrive(DriveStorage):
    def ready(self):
        return bool(socket.if_nametoindex(CONFIG['interface']))

    def upload(self, partial, name):
        upload_id = partial.stem
        receipt = CONFIG_DIR / ('upload-'+upload_id+'.json')
        if receipt.is_file():
            return json.loads(receipt.read_text())['file_id']
        session = broker('session', {'mode': 'upload'})
        with self.transfer_slots:
            stdout = gdirect(['upload', str(partial), '--parent', session['folder'], '--name', name], session['access_token'])
        file_id = stdout.strip().splitlines()[-1].split('\t')[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
            raise DriveError('Google 上传未返回文件标识，请重试')
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps({'file_id': file_id}))
        os.chmod(temporary, 0o600)
        os.replace(temporary, receipt)
        return file_id

    def get(self, path):
        entry = super().get(path)
        if entry:
            current = broker('status', {'path': path})['entry']
            if not current or current['id'] != entry['id'] or current['upload_id'] != entry['upload_id']:
                super().remove(path)
                (CONFIG_DIR / ('upload-'+entry['upload_id']+'.json')).unlink(missing_ok=True)
                return None
        return entry

    def put(self, path, file_id, size, upload_id):
        broker('commit', {'name': path.lstrip('/'), 'file_id': file_id, 'size': size, 'upload_id': upload_id})
        return super().put(path, file_id, size, upload_id)

    def trash(self, file_id):
        # Replacement and deletion belong to the server's canonical file index.
        pass

core.DRIVE = NativeDrive(SPOOL)
# Uploads explicitly sent to this component always use Drive, including an
# optional user choice for small files. Ordinary devices retain server policy.
core.storage_for_size = lambda size: 'drive'

def download(job_id, path):
    try:
        session = broker('session', {'mode': 'download', 'path': path})
        directory = INBOX/'无痕同步下载'/(time.strftime('%Y%m%d-')+job_id[:8])
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        name = Path(path).name
        if not name or name in ('.', '..'):
            raise DriveError('无效文件名')
        target = directory/name
        gdirect(['download', session['file_id'], '-o', str(target), '-j', '8'], session['access_token'])
        if target.stat().st_size != session['size']:
            raise DriveError('下载文件大小不符，请重试')
        with DOWNLOAD_LOCK:
            DOWNLOADS[job_id].update(completed=True, local_path=str(target))
    except Exception as exc:
        with DOWNLOAD_LOCK:
            DOWNLOADS[job_id]['error'] = str(exc) if isinstance(exc, DriveError) else '直连下载失败，请重试'

class Handler(core.Handler):
    def allowed(self):
        if self.headers.get('Host') != '127.0.0.1:'+str(PORT) or self.headers.get('Origin') != ORIGIN:
            self.send_error(403)
            return False
        return True

    def add_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type,X-Upload-Password')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Access-Control-Max-Age', '600')

    def do_OPTIONS(self):
        if self.allowed():
            super().do_OPTIONS()

    def do_GET(self):
        if not self.allowed(): return
        path = urlsplit(self.path).path
        if path == '/health':
            self.send_json(200, {'ok': True, 'accelerator': 1, 'physical_interface': CONFIG['interface'],
                                 'google_only': True, 'connections': 8})
        elif path.startswith('/api/uploads/'):
            super().do_GET()
        elif path.startswith('/downloads/'):
            with DOWNLOAD_LOCK:
                state = dict(DOWNLOADS.get(path.rsplit('/', 1)[-1], {}))
            self.send_json(200 if state else 404, state)
        else:
            self.send_empty(404)

    def do_POST(self):
        if not self.allowed(): return
        path = urlsplit(self.path).path
        if path.startswith('/api/uploads'):
            super().do_POST()
        elif path == '/downloads':
            try:
                body = self.read_json_body()
                file_path = body.get('path', '')
                if not isinstance(file_path, str) or not file_path.startswith('/') or Path(file_path).name in ('', '.', '..'):
                    raise ValueError()
                job_id = os.urandom(16).hex()
                with DOWNLOAD_LOCK:
                    DOWNLOADS[job_id] = {'id': job_id, 'completed': False, 'error': ''}
                DOWNLOAD_POOL.submit(download, job_id, file_path)
                self.send_json(202, {'id': job_id})
            except (ValueError, TypeError):
                self.send_json(400, {'message': 'invalid download request'})
        else:
            self.send_empty(404)

    def do_PUT(self):
        if not self.allowed(): return
        if urlsplit(self.path).path.startswith('/api/uploads/'):
            super().do_PUT()
        else:
            self.send_empty(404)

    def do_DELETE(self):
        if not self.allowed(): return
        if urlsplit(self.path).path.startswith('/api/uploads/'):
            super().do_DELETE()
        else:
            self.send_empty(404)

    def do_HEAD(self):
        self.send_empty(405)

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    core.ROOT.mkdir(exist_ok=True)
    core.ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
