#!/usr/bin/env python3
import cgi
import base64
import binascii
import hashlib
import hmac
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
from pathlib import Path
import tempfile
import threading
import uuid
from drive_storage import DriveStorage, DriveError, THRESHOLD, storage_for_size
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit, parse_qs


ROOT = Path(os.environ.get("SIMPLE_NETDISK_ROOT", "/srv/simple-netdisk/files")).resolve()
META_PATH = Path(os.environ.get("SIMPLE_NETDISK_META", "/srv/simple-netdisk/notes.json")).resolve()
MEMOS_PATH = Path(os.environ.get("SIMPLE_NETDISK_MEMOS", "/srv/simple-netdisk/memos.json")).resolve()
MEMO_ASSET_DIR = Path(os.environ.get("SIMPLE_NETDISK_MEMO_ASSETS", "/srv/simple-netdisk/memo-assets")).resolve()
SCHEDULE_PATH = Path(os.environ.get("SIMPLE_NETDISK_SCHEDULE", "/srv/simple-netdisk/schedule.json")).resolve()
UPLOAD_DIR = Path(os.environ.get("SIMPLE_NETDISK_UPLOADS", str(ROOT.parent / "uploads"))).resolve()
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
UPLOAD_LOCKS = [threading.Lock() for _ in range(64)]
ACTIVE_TRANSFERS = set()
ALLOW_SERVER_FALLBACK = True
DRIVE = DriveStorage(ROOT.parent)
PASSWORD = os.environ.get("SIMPLE_NETDISK_PASSWORD", "")
HOST = os.environ.get("SIMPLE_NETDISK_HOST", "127.0.0.1")
PORT = int(os.environ.get("SIMPLE_NETDISK_PORT", "8091"))
NOTE_LIMIT = int(os.environ.get("SIMPLE_NETDISK_NOTE_LIMIT", "5000"))
MEMO_TEXT_LIMIT = int(os.environ.get("SIMPLE_NETDISK_MEMO_TEXT_LIMIT", "5000"))
MEMO_IMAGE_LIMIT = int(os.environ.get("SIMPLE_NETDISK_MEMO_IMAGE_LIMIT", "12"))
META_LOCK = threading.Lock()
MEMO_LOCK = threading.Lock()
SCHEDULE_LOCK = threading.Lock()
SAFE_MEMO_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEDULE_PERIOD_LIMIT = 100000
SAFE_UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{64}$")


def upload_paths(upload_id: str) -> tuple:
    if not SAFE_UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("invalid upload id")
    return UPLOAD_DIR / (upload_id + ".json"), UPLOAD_DIR / (upload_id + ".part")


def save_upload_meta(upload_id: str, meta: dict) -> None:
    path, _ = upload_paths(upload_id)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=UPLOAD_DIR, delete=False, prefix="meta-", suffix=".tmp") as handle:
            temp_name = handle.name
            handle.write(json_bytes(meta))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def load_upload_meta(upload_id: str) -> dict:
    path, _ = upload_paths(upload_id)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def upload_target(meta: dict) -> Path:
    name = meta["name"]
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("invalid filename")
    target = (ROOT / name).resolve()
    if target.parent != ROOT:
        raise ValueError("invalid filename")
    return target


def upload_target_matches(meta: dict) -> bool:
    try:
        stat = upload_target(meta).stat()
        return (stat.st_size == meta["size"] and stat.st_ino == meta.get("inode")
                and stat.st_mtime_ns == meta.get("mtime_ns"))
    except FileNotFoundError:
        return False


def upload_snapshot(upload_id: str, meta: dict) -> dict:
    _, partial = upload_paths(upload_id)
    # The inode survives the atomic rename, including a crash before the receipt is saved.
    completed = meta.get("phase") in ("finalizing", "complete") and not partial.exists() and upload_target_matches(meta)
    entry = DRIVE.get("/" + meta["name"])
    if entry and entry.get("upload_id") == upload_id:
        completed = True
    offset = meta["size"] if completed else partial.stat().st_size if partial.exists() else 0
    return {"id": upload_id, "name": meta["name"], "size": meta["size"],
            "offset": offset, "completed": completed, "chunk_size": UPLOAD_CHUNK_SIZE,
            "storage": ("drive" if entry else "server") if completed else meta.get("storage", storage_for_size(meta["size"])),
            "processing": not completed and upload_id in ACTIVE_TRANSFERS,
            "error": meta.get("drive_error", ""),
            "can_fallback": meta.get("can_fallback", False),
            "fallback_reason": meta.get("fallback_reason", "")}


def cleanup_drive_file(file_id: str) -> None:
    try:
        DRIVE.trash(file_id)
    except DriveError:
        print("Drive replaced-file cleanup failed; file_id=" + file_id, flush=True)


def finish_server_upload(upload_id, meta):
    _, partial = upload_paths(upload_id)
    stat = partial.stat()
    meta.update({"phase": "finalizing", "storage": "server", "drive_error": "",
                 "inode": stat.st_ino, "mtime_ns": stat.st_mtime_ns})
    save_upload_meta(upload_id, meta)
    ROOT.mkdir(parents=True, exist_ok=True)
    os.replace(partial, upload_target(meta))
    previous = DRIVE.remove("/" + meta["name"])
    meta["phase"] = "complete"
    save_upload_meta(upload_id, meta)
    if previous:
        # A cloud outage must not delay publishing the server fallback.
        threading.Thread(target=cleanup_drive_file, args=(previous["id"],), daemon=True).start()


def finish_drive_upload(upload_id: str) -> None:
    lock = UPLOAD_LOCKS[int(upload_id[:2], 16) % len(UPLOAD_LOCKS)]
    try:
        meta = load_upload_meta(upload_id)
        _, partial = upload_paths(upload_id)
        file_id = DRIVE.upload(partial, meta["name"])
        with lock:
            previous = DRIVE.put("/" + meta["name"], file_id, partial.stat().st_size, upload_id)
            meta.update({"phase": "complete", "drive_error": "", "drive_id": file_id})
            save_upload_meta(upload_id, meta)
            upload_target(meta).unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
        if previous and previous["id"] != file_id:
            cleanup_drive_file(previous["id"])
    except Exception as exc:
        with lock:
            meta = load_upload_meta(upload_id)
            if isinstance(exc, DriveError) and ALLOW_SERVER_FALLBACK:
                try:
                    meta["fallback_reason"] = "Drive 暂不可用，已保存到服务器"
                    finish_server_upload(upload_id, meta)
                    return
                except OSError:
                    exc = RuntimeError("服务器兜底存储失败，上传进度已保留")
            meta.update({"phase": "failed", "drive_error": str(exc) if isinstance(exc, DriveError) else "上传完成失败，进度已保留",
                         "can_fallback": isinstance(exc, DriveError)})
            save_upload_meta(upload_id, meta)
    finally:
        with lock:
            ACTIVE_TRANSFERS.discard(upload_id)


def safe_path(raw: str) -> Path:
    rel = unquote(raw).lstrip("/")
    target = (ROOT / rel).resolve()
    if target != ROOT and ROOT not in target.parents:
        raise ValueError("invalid path")
    return target


def relative_key(path: Path) -> str:
    return "/" + path.relative_to(ROOT).as_posix()


def json_bytes(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def modified_iso(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_notes() -> dict:
    if not META_PATH.exists():
        return {}
    try:
        with META_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(key, str)}


def save_notes(notes: dict) -> None:
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=META_PATH.parent, prefix="notes-", suffix=".tmp") as temp:
            temp_name = temp.name
            json.dump(notes, temp, ensure_ascii=False, indent=2, sort_keys=True)
            temp.write("\n")
        os.replace(temp_name, META_PATH)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def memo_sort_key(item: dict) -> tuple:
    return (
        bool(item.get("pinned")),
        item.get("pinned_at", "") if item.get("pinned") else item.get("created", ""),
        item.get("id", ""),
    )


def paginate_memos(items: list, query: dict) -> dict:
    search = str(query.get("q", [""])[0]).strip().casefold()
    if search:
        items = [item for item in items if search in item.get("text", "").casefold()]
    if "limit" not in query:
        return {"items": items}
    try:
        limit = int(query["limit"][0])
        if limit < 1:
            raise ValueError()
    except (ValueError, TypeError, IndexError):
        raise ValueError("limit must be a positive integer")
    limit = min(limit, 20)
    cursor = str(query.get("cursor", [""])[0])
    if cursor:
        try:
            boundary = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(boundary, list) or len(boundary) != 3
                    or not isinstance(boundary[0], bool)
                    or not all(isinstance(value, str) for value in boundary[1:])):
                raise ValueError()
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise ValueError("invalid memo cursor")
        items = [item for item in items if memo_sort_key(item) < tuple(boundary)]
    page = items[:limit]
    has_more = len(items) > limit
    next_cursor = (
        base64.urlsafe_b64encode(json_bytes(memo_sort_key(page[-1]))).decode("ascii")
        if has_more else None
    )
    return {"items": page, "has_more": has_more, "next_cursor": next_cursor}


def load_memos() -> list:
    if not MEMOS_PATH.exists():
        return []
    try:
        with MEMOS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return []
    items = data.get("items", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue
        memo_id = str(item.get("id", ""))
        if not SAFE_MEMO_ID_RE.fullmatch(memo_id):
            continue
        images = item.get("images", [])
        if not isinstance(images, list):
            images = []
        normalized.append({
            "id": memo_id,
            "text": str(item.get("text", "")),
            "images": [img for img in images if isinstance(img, dict)],
            "created": str(item.get("created", "")),
            "updated": str(item.get("updated", item.get("created", ""))),
            "pinned": bool(item.get("pinned", False)),
            "pinned_at": str(item.get("pinned_at", "")),
        })
    normalized.sort(key=memo_sort_key, reverse=True)
    return normalized


def save_memos(items: list) -> None:
    MEMOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=MEMOS_PATH.parent, prefix="memos-", suffix=".tmp") as temp:
            temp_name = temp.name
            json.dump({"items": items}, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
        os.replace(temp_name, MEMOS_PATH)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def load_schedule() -> dict:
    if not SCHEDULE_PATH.exists():
        return {}
    try:
        with SCHEDULE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    normalized = {}
    for date_key, day in data.items():
        if not SAFE_DATE_RE.fullmatch(str(date_key)) or not isinstance(day, dict):
            continue
        slots = day.get("slots", {})
        if not isinstance(slots, dict):
            slots = {}
        normalized[str(date_key)] = {
            "slots": {
                str(hour): str(text)[:5000]
                for hour, text in slots.items()
                if str(hour).isdigit() and 0 <= int(hour) <= 23 and str(text).strip()
            },
            "updated": str(day.get("updated", "")),
        }
        periods = day.get("periods", {})
        if isinstance(periods, dict):
            periods = {key: str(value)[:SCHEDULE_PERIOD_LIMIT] for key, value in periods.items() if key in ("am", "pm")}
            if periods:
                normalized[str(date_key)]["periods"] = periods
    return normalized


def save_schedule(schedule: dict) -> None:
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=SCHEDULE_PATH.parent, prefix="schedule-", suffix=".tmp") as temp:
            temp_name = temp.name
            json.dump(schedule, temp, ensure_ascii=False, indent=2, sort_keys=True)
            temp.write("\n")
        os.replace(temp_name, SCHEDULE_PATH)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def clean_asset_name(name: str, fallback: str = "image.png") -> str:
    raw = Path(name or fallback).name.strip() or fallback
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")
    return raw[:120] or fallback


def safe_memo_asset_path(raw: str) -> Path:
    parts = unquote(raw).split("/", 1)
    if len(parts) != 2:
        raise ValueError("invalid asset path")
    memo_id, filename = parts
    if not SAFE_MEMO_ID_RE.fullmatch(memo_id):
        raise ValueError("invalid memo id")
    filename = clean_asset_name(filename)
    target = (MEMO_ASSET_DIR / memo_id / filename).resolve()
    base = (MEMO_ASSET_DIR / memo_id).resolve()
    if target != base and base not in target.parents:
        raise ValueError("invalid asset path")
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "SimpleNetdisk/1.1"

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), fmt % args), flush=True)

    def add_cors_headers(self) -> None:
        origin = self.headers.get('Origin') or '*'
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type,X-Upload-Password')
        self.send_header('Access-Control-Max-Age', '86400')

    def do_OPTIONS(self):
        self.send_response(204)
        self.add_cors_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_empty(self, code: int) -> None:
        self.send_response(code)
        self.add_cors_headers()
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_json(self, code: int, data) -> None:
        body = json_bytes(data)
        self.send_response(code)
        self.add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def require_password(self) -> bool:
        if self.headers.get("X-Upload-Password", "") == PASSWORD:
            return True
        self.send_json(401, {"message": "password required"})
        return False

    def read_json_body(self, max_bytes: int = 65536) -> dict:
        remaining = int(self.headers.get("Content-Length", "0") or "0")
        if remaining > max_bytes:
            raise ValueError("request body is too large")
        raw = self.rfile.read(remaining) if remaining else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object is required")
        return data

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ("/health", "/healthz"):
            self.send_json(200, {"ok": True, "drive_ready": DRIVE.ready(), "drive_threshold_bytes": THRESHOLD,
                                 "storage_priority": ["drive", "server"], "server_fallback": True})
            return
        if parsed.path.startswith("/api/uploads/"):
            self.handle_resumable_upload("status", parsed)
            return
        if parsed.path in ("/api/memos", "/api/memos/"):
            self.list_memos(parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.path in ("/api/schedule", "/api/schedule/"):
            self.get_schedule_day(parse_qs(parsed.query))
            return
        if parsed.path.startswith("/api/memo-assets/"):
            self.serve_memo_asset(parsed.path[len("/api/memo-assets/"):])
            return
        if parsed.path in ("/api/resources", "/api/resources/"):
            self.list_files("")
            return
        if parsed.path.startswith("/api/resources/"):
            self.list_files(parsed.path[len("/api/resources/"):])
            return
        if parsed.path.startswith("/api/preview/"):
            self.preview(parsed.path[len("/api/preview/"):])
            return
        if parsed.path.startswith("/api/raw/"):
            self.download(parsed.path[len("/api/raw/"):])
            return
        self.send_json(404, {"message": "not found"})

    def do_HEAD(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/memo-assets/"):
            try:
                target = safe_memo_asset_path(parsed.path[len("/api/memo-assets/"):])
            except ValueError:
                self.send_empty(400)
                return
            if not target.is_file():
                self.send_empty(404)
                return
            self.send_response(200)
            self.add_cors_headers()
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            return
        if parsed.path.startswith("/api/raw/") or parsed.path.startswith("/api/preview/"):
            try:
                prefix = "/api/preview/" if parsed.path.startswith("/api/preview/") else "/api/raw/"
                target = safe_path(parsed.path[len(prefix):])
            except ValueError:
                self.send_empty(400)
                return
            entry = DRIVE.get(relative_key(target))
            if not target.is_file() and not entry:
                self.send_empty(404)
                return
            self.send_response(200)
            self.add_cors_headers()
            self.send_header("Content-Length", str(entry["size"] if entry else target.stat().st_size))
            self.end_headers()
            return
        self.send_empty(200)

    def do_POST(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/accelerator/"):
            self.handle_accelerator(parsed.path.rsplit("/", 1)[-1])
            return
        if parsed.path in ("/api/uploads", "/api/uploads/"):
            self.handle_resumable_upload("init", parsed)
            return
        if parsed.path.startswith("/api/uploads/") and parsed.path.endswith("/complete"):
            self.handle_resumable_upload("complete", parsed)
            return
        if parsed.path in ("/api/memos", "/api/memos/"):
            if not self.require_password():
                return
            self.create_memo()
            return
        if not parsed.path.startswith("/api/resources/"):
            self.send_json(404, {"message": "not found"})
            return
        if not self.require_password():
            return
        self.upload(parsed.path[len("/api/resources/"):], parse_qs(parsed.query))

    def handle_accelerator(self, action):
        # Only the paired native component receives short-lived Google tokens.
        # No browser Origin or CORS access to this broker is accepted.
        key_file = ROOT.parent / "gdirect" / "helper-key"
        key = key_file.read_text().strip() if key_file.is_file() else ""
        if (self.headers.get("Origin") or not key or
                not hmac.compare_digest(self.headers.get("X-Netdisk-Helper-Key", ""), key)):
            self.send_json(403, {"message": "native accelerator pairing required"})
            return
        try:
            data = self.read_json_body()
            if action == "status":
                entry = DRIVE.get(relative_key(safe_path(data.get("path", ""))))
                self.send_json(200, {"entry": entry})
                return
            if action == "session":
                if data.get("mode") == "download":
                    entry = DRIVE.get(relative_key(safe_path(data.get("path", ""))))
                    if not entry:
                        self.send_json(404, {"message": "Drive file not found"})
                        return
                    self.send_json(200, {"access_token": DRIVE.token(), "file_id": entry["id"], "size": entry["size"]})
                else:
                    if not self.require_password():
                        return
                    self.send_json(200, {"access_token": DRIVE.token(), "folder": DRIVE.folder_file.read_text().strip()})
                return
            if action != "commit":
                self.send_json(404, {"message": "not found"})
                return
            if not self.require_password():
                return
            name, size, file_id, upload_id = (data.get(k) for k in ("name", "size", "file_id", "upload_id"))
            target = upload_target({"name": name})
            upload_paths(upload_id)
            if type(size) is not int or size < 0:
                raise ValueError("invalid file size")
            with DRIVE.request(file_id, query={"fields": "id,name,size,parents,trashed"}) as response:
                meta = json.load(response)
            folder = DRIVE.folder_file.read_text().strip()
            if (meta.get("name") != name or int(meta.get("size", -1)) != size or
                    folder not in meta.get("parents", []) or meta.get("trashed")):
                raise ValueError("uploaded file ownership or metadata mismatch")
            previous = DRIVE.put(relative_key(target), file_id, size, upload_id)
            target.unlink(missing_ok=True)
            if previous and previous["id"] != file_id:
                cleanup_drive_file(previous["id"])
            self.send_json(200, {"completed": True, "storage": "drive", "size": size})
        except DriveError as exc:
            self.send_json(503, {"message": str(exc), "can_fallback": True})
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {"message": "invalid accelerator request"})
        except OSError:
            self.send_json(503, {"message": "accelerator storage unavailable"})

    def do_PUT(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/uploads/"):
            self.handle_resumable_upload("chunk", parsed)
            return
        if parsed.path.startswith("/api/schedule/"):
            if not self.require_password():
                return
            self.save_schedule_slot(parsed.path[len("/api/schedule/"):])
            return
        if parsed.path.startswith("/api/memos/"):
            if not self.require_password():
                return
            self.update_memo(parsed.path[len("/api/memos/"):])
            return
        if not parsed.path.startswith("/api/notes/"):
            self.send_json(404, {"message": "not found"})
            return
        if not self.require_password():
            return
        self.save_note(parsed.path[len("/api/notes/"):])

    def do_PATCH(self):
        self.do_PUT()

    def do_DELETE(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/uploads/"):
            self.handle_resumable_upload("discard", parsed)
            return
        if parsed.path.startswith("/api/memos/"):
            if not self.require_password():
                return
            self.delete_memo(parsed.path[len("/api/memos/"):])
            return
        if not parsed.path.startswith("/api/resources/"):
            self.send_json(404, {"message": "not found"})
            return
        if not self.require_password():
            return
        self.delete(parsed.path[len("/api/resources/"):])


    def handle_resumable_upload(self, action: str, parsed) -> None:
        if not self.require_password():
            return
        try:
            if action == "init":
                self.init_resumable_upload()
                return
            suffix = parsed.path[len("/api/uploads/"):]
            upload_id = suffix[:-len("/complete")] if action == "complete" else suffix
            upload_paths(upload_id)
            lock = UPLOAD_LOCKS[int(upload_id[:2], 16) % len(UPLOAD_LOCKS)]
            if action == "chunk":
                self.receive_upload_chunk(upload_id, lock, parse_qs(parsed.query))
                return
            with lock:
                meta = load_upload_meta(upload_id)
                if action == "discard":
                    if upload_id in ACTIVE_TRANSFERS:
                        self.send_json(409, {"message": "Drive 转存中，请稍后再取消"})
                        return
                    metadata, partial = upload_paths(upload_id)
                    partial.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    self.send_empty(204)
                elif action == "complete":
                    self.complete_resumable_upload(upload_id, meta)
                else:
                    self.send_json(200, upload_snapshot(upload_id, meta))
        except DriveError as exc:
            self.send_json(503, {"message": str(exc), "can_fallback": True})
        except FileNotFoundError:
            self.send_json(404, {"message": "upload not found"})
        except (ValueError, TypeError, KeyError) as exc:
            self.send_json(400, {"message": str(exc)})
        except OSError as exc:
            self.send_json(507 if exc.errno == 28 else 500, {"message": "upload storage unavailable"})

    def init_resumable_upload(self) -> None:
        data = self.read_json_body()
        meta = {"name": data.get("name"), "size": data.get("size"),
                "last_modified": data.get("last_modified"), "fingerprint": data.get("fingerprint")}
        upload_target(meta)
        if type(meta["size"]) is not int or meta["size"] < 0:
            raise ValueError("invalid file size")
        if type(meta["last_modified"]) is not int or not isinstance(meta["fingerprint"], str) or not SAFE_UPLOAD_ID_RE.fullmatch(meta["fingerprint"]):
            raise ValueError("invalid file fingerprint")
        if data.get("fallback_to_server") is True:
            if not ALLOW_SERVER_FALLBACK:
                raise ValueError("server fallback requires the server endpoint")
            meta.update(storage="server", fallback_reason="直连通道不可用，已使用服务器兜底")
        upload_id = hashlib.sha256(json_bytes(meta)).hexdigest()
        if not DRIVE.ready():
            if not ALLOW_SERVER_FALLBACK:
                raise DriveError("Drive 暂不可用，可使用服务器通道")
            meta.update(storage="server", fallback_reason="Drive 尚未就绪，已使用服务器兜底")
        lock = UPLOAD_LOCKS[int(upload_id[:2], 16) % len(UPLOAD_LOCKS)]
        with lock:
            path, partial = upload_paths(upload_id)
            if path.exists():
                existing = load_upload_meta(upload_id)
                snapshot = upload_snapshot(upload_id, existing)
                if snapshot["completed"] or partial.exists():
                    self.send_json(200, snapshot)
                    return
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            meta["phase"] = "receiving"
            save_upload_meta(upload_id, meta)
            partial.touch()
            self.send_json(200, upload_snapshot(upload_id, meta))

    def receive_upload_chunk(self, upload_id: str, lock, query: dict) -> None:
        offset = int(query.get("offset", ["-1"])[0])
        expected_hash = query.get("sha256", [""])[0]
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > UPLOAD_CHUNK_SIZE or offset < 0 or not SAFE_UPLOAD_ID_RE.fullmatch(expected_hash):
            raise ValueError("invalid upload chunk")
        with lock:
            meta = load_upload_meta(upload_id)
            snapshot = upload_snapshot(upload_id, meta)
            if snapshot["completed"] or offset != snapshot["offset"] or offset + length > meta["size"]:
                self.send_json(409, {"message": "upload offset changed", **snapshot})
                return
        temp_name = None
        try:
            digest = hashlib.sha256()
            self.connection.settimeout(120)
            with tempfile.NamedTemporaryFile("wb", dir=UPLOAD_DIR, delete=False, prefix="chunk-", suffix=".tmp") as temp:
                temp_name = temp.name
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("incomplete upload chunk")
                    temp.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if digest.hexdigest() != expected_hash:
                raise ValueError("upload checksum mismatch")
            with lock:
                meta = load_upload_meta(upload_id)
                snapshot = upload_snapshot(upload_id, meta)
                if snapshot["completed"] or offset != snapshot["offset"]:
                    self.send_json(409, {"message": "upload offset changed", **snapshot})
                    return
                _, partial = upload_paths(upload_id)
                with partial.open("ab") as target, open(temp_name, "rb") as source:
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                self.send_json(200, upload_snapshot(upload_id, meta))
        finally:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)

    def complete_resumable_upload(self, upload_id: str, meta: dict) -> None:
        snapshot = upload_snapshot(upload_id, meta)
        if snapshot["completed"]:
            self.send_json(200, snapshot)
            return
        _, partial = upload_paths(upload_id)
        if not partial.exists() or partial.stat().st_size != meta["size"]:
            self.send_json(409, {"message": "upload is incomplete", **snapshot})
            return
        if meta.get("storage", storage_for_size(partial.stat().st_size)) == "drive":
            if upload_id not in ACTIVE_TRANSFERS:
                if not DRIVE.ready():
                    if not ALLOW_SERVER_FALLBACK:
                        raise DriveError("Drive 暂不可用，可使用服务器通道")
                    meta["fallback_reason"] = "Drive 尚未就绪，已使用服务器兜底"
                    finish_server_upload(upload_id, meta)
                    self.send_json(200, upload_snapshot(upload_id, meta))
                    return
                meta.update({"phase": "transferring", "drive_error": ""})
                save_upload_meta(upload_id, meta)
                ACTIVE_TRANSFERS.add(upload_id)
                threading.Thread(target=finish_drive_upload, args=(upload_id,), daemon=True).start()
            self.send_json(200, upload_snapshot(upload_id, meta))
            return
        finish_server_upload(upload_id, meta)
        self.send_json(200, upload_snapshot(upload_id, meta))

    def list_memos(self, query: dict) -> None:
        with MEMO_LOCK:
            items = load_memos()
        try:
            result = paginate_memos(items, query)
        except ValueError as exc:
            self.send_json(400, {"message": str(exc)})
            return
        self.send_json(200, result)

    def get_schedule_day(self, query: dict) -> None:
        date_key = str(query.get("date", [""])[0])
        if not SAFE_DATE_RE.fullmatch(date_key):
            self.send_json(400, {"message": "date must be YYYY-MM-DD"})
            return
        with SCHEDULE_LOCK:
            schedule = load_schedule()
            day = schedule.get(date_key, {"slots": {}, "updated": ""})
        self.send_json(200, {"date": date_key, "slots": day.get("slots", {}),
                             "periods": day.get("periods", {}), "updated": day.get("updated", "")})

    def save_schedule_slot(self, raw: str) -> None:
        parts = unquote(raw).strip("/").split("/")
        if len(parts) != 2 or not SAFE_DATE_RE.fullmatch(parts[0]) or (not parts[1].isdigit() and parts[1] not in ("am", "pm")):
            self.send_json(400, {"message": "schedule path must be date/hour or date/am|pm"})
            return
        date_key = parts[0]
        is_period = parts[1] in ("am", "pm")
        hour = None if is_period else int(parts[1])
        if not is_period and (hour < 0 or hour > 23):
            self.send_json(400, {"message": "hour must be 0-23"})
            return
        try:
            data = self.read_json_body(max_bytes=512 * 1024)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"message": str(exc)})
            return
        text = str(data.get("text", ""))[:SCHEDULE_PERIOD_LIMIT if is_period else 5000]
        updated = now_iso()
        with SCHEDULE_LOCK:
            schedule = load_schedule()
            day = schedule.setdefault(date_key, {"slots": {}, "updated": ""})
            slots = day.setdefault("periods" if is_period else "slots", {})
            if is_period:
                slots[parts[1]] = text
            elif text.strip():
                slots[str(hour)] = text
            else:
                slots.pop(str(hour), None)
            day["updated"] = updated
            if not day.get("slots") and not day.get("periods"):
                schedule.pop(date_key, None)
            save_schedule(schedule)
        self.send_json(200, {"ok": True, "date": date_key, "hour": hour,
                             "period": parts[1] if is_period else None, "text": text, "updated": updated})

    def serve_memo_asset(self, raw: str) -> None:
        try:
            target = safe_memo_asset_path(raw)
        except ValueError:
            self.send_json(400, {"message": "invalid asset path"})
            return
        if not target.is_file():
            self.send_json(404, {"message": "not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.add_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(target.name))
        self.end_headers()
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def create_memo(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        memo_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        memo_dir = MEMO_ASSET_DIR / memo_id
        images = []
        text = ""
        try:
            if "multipart/form-data" in content_type:
                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                        "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                    },
                )
                text = str(form.getfirst("text", ""))[:MEMO_TEXT_LIMIT]
                fields = form["images"] if "images" in form else []
                if not isinstance(fields, list):
                    fields = [fields]
                memo_dir.mkdir(parents=True, exist_ok=True)
                for field in fields[:MEMO_IMAGE_LIMIT]:
                    filename = getattr(field, "filename", "") or "paste.png"
                    file_type = (getattr(field, "type", "") or mimetypes.guess_type(filename)[0] or "application/octet-stream").lower()
                    if not file_type.startswith("image/"):
                        continue
                    clean = clean_asset_name(filename)
                    stored = f"{len(images)+1:02d}-{clean}"
                    target = (memo_dir / stored).resolve()
                    with target.open("wb") as output:
                        shutil.copyfileobj(field.file, output, length=1024 * 1024)
                    size = target.stat().st_size
                    if size <= 0:
                        target.unlink(missing_ok=True)
                        continue
                    images.append({
                        "name": clean,
                        "url": f"/api/memo-assets/{memo_id}/{quote(stored)}",
                        "type": file_type,
                        "size": size,
                    })
            else:
                data = self.read_json_body()
                text = str(data.get("text", ""))[:MEMO_TEXT_LIMIT]
        except Exception as exc:
            shutil.rmtree(memo_dir, ignore_errors=True)
            self.send_json(400, {"message": str(exc)})
            return
        if not text.strip() and not images:
            shutil.rmtree(memo_dir, ignore_errors=True)
            self.send_json(400, {"message": "text or image is required"})
            return
        created = now_iso()
        memo = {"id": memo_id, "text": text, "images": images, "created": created, "updated": created, "pinned": False}
        with MEMO_LOCK:
            items = load_memos()
            items.insert(0, memo)
            save_memos(items)
        self.send_json(200, {"ok": True, "memo": memo})

    def update_memo(self, raw: str) -> None:
        memo_id = unquote(raw).strip("/")
        if not SAFE_MEMO_ID_RE.fullmatch(memo_id):
            self.send_json(400, {"message": "invalid memo id"})
            return
        try:
            data = self.read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"message": str(exc)})
            return
        has_text = "text" in data
        has_pinned = "pinned" in data
        if not has_text and not has_pinned:
            self.send_json(400, {"message": "text or pinned is required"})
            return
        text = str(data.get("text", ""))[:MEMO_TEXT_LIMIT] if has_text else None
        requested_pinned = bool(data.get("pinned")) if has_pinned else None
        updated_memo = None
        pin_limit_reached = False
        with MEMO_LOCK:
            items = load_memos()
            for item in items:
                if item.get("id") != memo_id:
                    continue
                if has_text:
                    if not text.strip() and not item.get("images"):
                        self.send_json(400, {"message": "text or image is required"})
                        return
                    item["text"] = text
                if has_pinned:
                    if requested_pinned and not item.get("pinned"):
                        pinned_count = sum(1 for other in items if other.get("pinned") and other.get("id") != memo_id)
                        if pinned_count >= 2:
                            pin_limit_reached = True
                            break
                        item["pinned"] = True
                        item["pinned_at"] = now_iso()
                    elif not requested_pinned:
                        item["pinned"] = False
                        item.pop("pinned_at", None)
                item["updated"] = now_iso()
                updated_memo = item
                break
            if updated_memo is not None:
                save_memos(items)
        if pin_limit_reached:
            self.send_json(409, {"message": "pin limit reached", "limit": 2})
            return
        if updated_memo is None:
            self.send_json(404, {"message": "not found"})
            return
        self.send_json(200, {"ok": True, "memo": updated_memo})

    def delete_memo(self, raw: str) -> None:
        memo_id = unquote(raw).strip("/")
        if not SAFE_MEMO_ID_RE.fullmatch(memo_id):
            self.send_json(400, {"message": "invalid memo id"})
            return
        deleted = False
        with MEMO_LOCK:
            items = load_memos()
            kept = []
            for item in items:
                if item.get("id") == memo_id:
                    deleted = True
                else:
                    kept.append(item)
            if deleted:
                save_memos(kept)
        if deleted:
            shutil.rmtree(MEMO_ASSET_DIR / memo_id, ignore_errors=True)
            self.send_empty(204)
        else:
            self.send_json(404, {"message": "not found"})

    def list_files(self, raw: str) -> None:
        try:
            target = safe_path(raw)
        except ValueError:
            self.send_json(400, {"message": "invalid path"})
            return
        if not target.exists():
            self.send_json(404, {"message": "not found"})
            return
        if not target.is_dir():
            self.send_json(400, {"message": "not a directory"})
            return
        with META_LOCK:
            notes = load_notes()
        items = []
        for child in target.iterdir():
            if child.is_symlink():
                continue
            stat = child.stat()
            rel = relative_key(child)
            items.append({
                "name": child.name,
                "path": rel,
                "size": 0 if child.is_dir() else stat.st_size,
                "modified": modified_iso(child),
                "isDir": child.is_dir(),
                "note": notes.get(rel, ""),
            })
        merged = {item["path"]: {**item, "storage": "server"} for item in items}
        for entry in DRIVE.items():
            if Path(entry["path"]).parent.as_posix() != relative_key(target).rstrip("/") and target != ROOT:
                continue
            if target == ROOT and entry["path"].count("/") != 1:
                continue
            merged[entry["path"]] = {"name": Path(entry["path"]).name, "path": entry["path"],
                                     "size": entry["size"], "modified": entry["modified"],
                                     "isDir": False, "note": notes.get(entry["path"], ""), "storage": "drive"}
        items = sorted(merged.values(), key=lambda item: item["modified"], reverse=True)
        self.send_json(200, {"items": items, "drive_ready": DRIVE.ready(), "drive_threshold_bytes": THRESHOLD,
                             "storage_priority": ["drive", "server"], "server_fallback": True})

    def download(self, raw: str) -> None:
        try:
            target = safe_path(raw)
        except ValueError:
            self.send_json(400, {"message": "invalid path"})
            return
        entry = DRIVE.get(relative_key(target))
        if entry:
            self.serve_drive(entry, target.name, inline=False)
            return
        if not target.is_file():
            self.send_json(404, {"message": "not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.add_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name))
        self.end_headers()
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


    def serve_drive(self, entry: dict, name: str, inline: bool = False) -> None:
        try:
            headers = {}
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            response = DRIVE.request(entry["id"], query={"alt": "media"}, headers=headers)
        except DriveError as exc:
            self.send_json(503, {"message": str(exc)})
            return
        with response:
            self.send_response(response.status)
            self.add_cors_headers()
            self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
            self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(name))
            self.send_header("Accept-Ranges", "bytes")
            for key in ("Content-Length", "Content-Range"):
                if response.headers.get(key):
                    self.send_header(key, response.headers[key])
            self.end_headers()
            try:
                shutil.copyfileobj(response, self.wfile, 1024 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def preview(self, raw: str) -> None:
        try:
            target = safe_path(raw)
        except ValueError:
            self.send_json(400, {"message": "invalid path"})
            return
        entry = DRIVE.get(relative_key(target))
        if entry:
            self.serve_drive(entry, target.name, inline=True)
            return
        if not target.is_file():
            self.send_json(404, {"message": "not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.add_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(target.name))
        self.end_headers()
        with target.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def upload(self, raw: str, query) -> None:
        try:
            target = safe_path(raw)
        except ValueError:
            self.send_json(400, {"message": "invalid path"})
            return
        if target.parent != ROOT:
            self.send_json(400, {"message": "nested upload is disabled"})
            return
        if (target.exists() or DRIVE.get(relative_key(target))) and query.get("override", ["false"])[0].lower() != "true":
            self.send_json(409, {"message": "file exists"})
            return
        ROOT.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=ROOT, prefix="upload-", suffix=".tmp") as temp:
                temp_name = temp.name
                self.copy_request_body(temp)
            partial = Path(temp_name)
            storage = storage_for_size(partial.stat().st_size)
            if storage == "drive":
                try:
                    file_id = DRIVE.upload(partial, target.name)
                    previous = DRIVE.put(relative_key(target), file_id, partial.stat().st_size, None)
                    target.unlink(missing_ok=True)
                    partial.unlink()
                except DriveError:
                    storage = "server"
            if storage == "server":
                os.replace(temp_name, target)
                previous = DRIVE.remove(relative_key(target))
            if previous:
                threading.Thread(target=cleanup_drive_file, args=(previous["id"],), daemon=True).start()
            self.send_json(200, {"ok": True, "name": target.name, "storage": storage})
        except Exception as exc:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except Exception:
                    pass
            self.send_json(500, {"message": str(exc)})

    def copy_request_body(self, output) -> None:
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    raise ValueError("unexpected end of chunked body")
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    return
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("unexpected end of chunk")
                    output.write(chunk)
                    remaining -= len(chunk)
                self.rfile.read(2)
        else:
            remaining = int(self.headers.get("Content-Length", "0") or "0")
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("unexpected end of body")
                output.write(chunk)
                remaining -= len(chunk)

    def save_note(self, raw: str) -> None:
        try:
            target = safe_path(raw)
            data = self.read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"message": str(exc)})
            return
        if not target.is_file() and not DRIVE.get(relative_key(target)):
            self.send_json(404, {"message": "not found"})
            return
        note = str(data.get("note", ""))[:NOTE_LIMIT]
        rel = relative_key(target)
        with META_LOCK:
            notes = load_notes()
            if note:
                notes[rel] = note
            else:
                notes.pop(rel, None)
            save_notes(notes)
        self.send_json(200, {"ok": True, "path": rel, "note": note})

    def delete(self, raw: str) -> None:
        try:
            target = safe_path(raw)
        except ValueError:
            self.send_json(400, {"message": "invalid path"})
            return
        entry = DRIVE.get(relative_key(target))
        if entry:
            try:
                DRIVE.trash(entry["id"])
            except DriveError as exc:
                self.send_json(503, {"message": str(exc)})
                return
            DRIVE.remove(relative_key(target))
            with META_LOCK:
                notes = load_notes()
                notes.pop(relative_key(target), None)
                save_notes(notes)
            self.send_empty(204)
            return
        if target.is_file():
            rel = relative_key(target)
            target.unlink()
            with META_LOCK:
                notes = load_notes()
                if rel in notes:
                    notes.pop(rel, None)
                    save_notes(notes)
            self.send_empty(204)
            return
        if target.is_dir():
            try:
                target.rmdir()
            except OSError:
                self.send_json(400, {"message": "directory is not empty"})
                return
            self.send_empty(204)
            return
        self.send_json(404, {"message": "not found"})


def main() -> None:
    if not PASSWORD:
        raise RuntimeError("SIMPLE_NETDISK_PASSWORD must be configured")
    ROOT.mkdir(parents=True, exist_ok=True)
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMOS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMO_ASSET_DIR.mkdir(parents=True, exist_ok=True)
    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for pattern in ("chunk-*.tmp", "meta-*.tmp"):
        for orphan in UPLOAD_DIR.glob(pattern):
            orphan.unlink(missing_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on {HOST}:{PORT}, root={ROOT}, meta={META_PATH}, memos={MEMOS_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
