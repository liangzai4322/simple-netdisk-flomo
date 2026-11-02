"""Primary private Drive storage. The application keeps server storage as fallback."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


# Compatibility field for clients using size > threshold: every valid size,
# including an empty file, now prefers Drive.
THRESHOLD = -1
ACCOUNT = os.environ.get("SIMPLE_NETDISK_GOOGLE_ACCOUNT", "")


class DriveError(Exception):
    pass


def storage_for_size(size):
    return "drive" if size > THRESHOLD else "server"


class DriveStorage:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.index = self.directory / "drive-files.sqlite3"
        self.credentials = self.directory / "gdirect" / "state.sqlite3"
        self.binary = os.environ.get("SIMPLE_NETDISK_GDIRECT", "/opt/simple-netdisk/bin/gdirect")
        self.folder_file = self.directory / "gdirect" / "folder-id"
        self.transfer_slots = threading.BoundedSemaphore(2)
        self.auth_lock = threading.Lock()

    def connection(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, id TEXT NOT NULL, size INTEGER NOT NULL, modified TEXT NOT NULL, upload_id TEXT)")
        return db

    def get(self, path):
        with self.connection() as db:
            row = db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def items(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM files")]

    def put(self, path, file_id, size, upload_id):
        modified = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        with self.connection() as db:
            previous = db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)", (path, file_id, size, modified, upload_id))
        return dict(previous) if previous else None

    def remove(self, path):
        with self.connection() as db:
            previous = db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
            db.execute("DELETE FROM files WHERE path=?", (path,))
        return dict(previous) if previous else None

    def ready(self):
        return bool(ACCOUNT) and Path(self.binary).is_file() and self.credentials.is_file() and self.folder_file.is_file()

    def token(self, force=False):
        with self.auth_lock:
            try:
                with sqlite3.connect(self.credentials, timeout=15) as db:
                    row = db.execute("SELECT credentials, token, expires_at FROM accounts WHERE email=?", (ACCOUNT,)).fetchone()
                    if not row:
                        raise DriveError("Drive 尚未授权指定账号")
                    if not force and row[1] and row[2] > time.time() + 60:
                        return row[1]
                    credentials = json.loads(row[0])
                    body = urlencode({"client_id": credentials["client_id"], "client_secret": credentials.get("client_secret", ""),
                                      "refresh_token": credentials["refresh_token"], "grant_type": "refresh_token"}).encode()
                    request = Request("https://oauth2.googleapis.com/token", data=body)
                    with urlopen(request, timeout=45) as response:
                        data = json.load(response)
                    db.execute("UPDATE accounts SET token=?,expires_at=? WHERE email=?", (data["access_token"], int(time.time()) + int(data.get("expires_in", 3600)), ACCOUNT))
                    return data["access_token"]
            except (HTTPError, URLError, OSError, sqlite3.Error, KeyError, ValueError) as exc:
                raise DriveError("Drive 授权不可用，请重新授权指定账号") from exc

    def request(self, file_id="", *, method="GET", data=None, query=None, headers=None):
        url = "https://www.googleapis.com/drive/v3/files"
        if file_id:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
                raise DriveError("Drive 文件标识无效")
            url += "/" + quote(file_id)
        if query:
            url += "?" + urlencode(query)
        for attempt in range(2):
            request_headers = {"Authorization": "Bearer " + self.token(force=bool(attempt)), **(headers or {})}
            if data is not None:
                request_headers["Content-Type"] = "application/json"
            request = Request(url, method=method, headers=request_headers,
                              data=json.dumps(data).encode() if data is not None else None)
            try:
                return urlopen(request, timeout=120)
            except HTTPError as exc:
                if exc.code == 401 and not attempt:
                    exc.close()
                    continue
                status = exc.code
                exc.close()
                raise DriveError("Drive 请求失败（HTTP %s）" % status) from None
            except (URLError, OSError) as exc:
                raise DriveError("Drive 网络连接失败，文件进度已保留") from exc

    def upload(self, partial, name):
        if not self.ready():
            raise DriveError("大文件存储尚未授权，上传进度已保留")
        folder = self.folder_file.read_text().strip()
        with self.transfer_slots:
            try:
                # The server has ordinary Google access. Pin the Google address resolved
                # by its DNS; --google-only excludes gdirect's default third-party relay.
                ip = socket.gethostbyname("www.googleapis.com")
                result = subprocess.run([self.binary, "--db", str(self.credentials), "--account", ACCOUNT,
                                         "--google-only", "upload", str(partial), "--parent", folder,
                                         "--name", name, "--ip", ip], capture_output=True, timeout=3600)
                output = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
                file_id = output[-1].split("\t", 1)[0] if output else ""
                if result.returncode or not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
                    raise DriveError("Drive 转存失败，上传进度已保留，可继续上传")
                with self.request(file_id, query={"fields": "id,size,md5Checksum"}) as response:
                    metadata = json.load(response)
                if int(metadata.get("size", -1)) != partial.stat().st_size:
                    raise DriveError("Drive 文件大小校验失败，上传进度已保留")
                return file_id
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise DriveError("Drive 转存超时或不可用，上传进度已保留") from exc

    def trash(self, file_id):
        with self.request(file_id, method="PATCH", data={"trashed": True}) as response:
            response.read()
