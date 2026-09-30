"""Run with python -m src.personal_web --data-dir PATH [--host IP].

A separate small Web surface for personal evidence capture. Uses the same
DecisionJournal as native agent tools, without loading models or broker tools.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from src.decision_journal import DecisionJournal

STATIC = Path(__file__).parent / "static"
MAX_BODY = 12 * 1024 * 1024
JOURNAL = "personal:gtht"


def trade_draft(rows):
    """Conservative single-trade suggestion; ambiguous tables stay manual."""
    lines = [r["text"].strip() for r in rows]
    if not any("成交" in line for line in lines):
        return None
    codes = [line for line in lines if re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", line)]
    sides = [line for line in lines if line in {"买入", "卖出"}]
    numbers = [line for line in lines if re.fullmatch(r"\d+(?:\.\d+)?", line) and line not in codes]
    times = re.findall(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", " ".join(lines))
    if len(codes) != 1 or len(sides) != 1 or len(numbers) != 2 or not numbers[0].isdigit():
        return None
    quantity, price = int(numbers[0]), float(numbers[1])
    if quantity <= 0 or price <= 0:
        return None
    return dict(symbol=codes[0], side="buy" if sides[0] == "买入" else "sell", quantity=quantity, price=price,
                when=times[0].replace(" ", "T") if len(times) == 1 else None)


def report(journal):
    events, after = [], 0
    while True:
        page = journal.history(JOURNAL, after=after, limit=100)
        events.extend(page["events"])
        if page["next_after"] is None:
            break
        after = page["next_after"]
    by_id = {e["id"]: e for e in events}
    corrected = {e["parent_id"] for e in events if e["kind"] == "correction"}
    lines = ["个人决策记录核对（本地规则生成，非大模型分析）", "账户信息并非实时；未记录不等于未发生。"]
    for event in events:
        if event["id"] in corrected:
            continue
        original = event
        while original["kind"] == "correction":
            original = by_id[original["parent_id"]]
        kind = event.get("corrects_kind", event["kind"])
        data = event["data"].get("replacement", event["data"])
        if kind in {"snapshot", "intent", "execution", "plan", "review", "account_activity"}:
            lines.append(f"[{event['id']}] 类型={event['kind']}（内容类型={kind}） 发生/更正时间={event['occurred_at']} 记录时间={event['recorded_at']} 原始发生时间={original['occurred_at']} 原记录={original['id']} 原始来源={original['source_ref']} 本次来源={event['source_ref']}: " + json.dumps(data, ensure_ascii=False))
    lines.extend(["核查提示：截图可能遗漏行，重叠截图可能重复；请用成交编号核对。", "成交不能证明交易动机；事后补充的理由不能当作事前计划。", "没有最新完整快照时，不计算当前仓位比例或推断可用资金。"])
    return "\n\n".join(lines)


class PersonalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, ocr_binary=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.images = root / "screenshots"
        self.images.mkdir(exist_ok=True, mode=0o700)
        keyfile = root / "access-key.txt"
        if not keyfile.exists():
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        self.key = keyfile.read_text().strip()
        if len(self.key) < 32:
            raise ValueError("access key must have at least 32 characters")
        self.sessions = set()
        self.ocr_binary = ocr_binary
        self.ocr_lock = threading.Lock()
        self.journal = DecisionJournal(root / "decision_journal.sqlite3")
        self.allowed_hosts = {"127.0.0.1", "localhost", address[0]}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Do not log screenshots, user text, session keys or URL queries.

    def send(self, code, body, mime="application/json", cookie=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie.get("personal_session")
            return token is not None and token.value in self.server.sessions
        except Exception:
            return False

    def check_host(self):
        host = self.headers.get("Host", "")
        if host.split(":")[0] not in self.server.allowed_hosts:
            self.send(403, {"error": "访问地址不在允许列表"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self.send(403, {"error": "不允许跨站请求"})
            return False
        return True

    def do_GET(self):
        if not self.check_host():
            return
        path = urlparse(self.path).path
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in assets:
            filename, mime = assets[path]
            return self.send(200, (STATIC / filename).read_bytes(), mime)
        if not self.authorized():
            return self.send(401, {"error": "请先输入访问密钥"})
        try:
            if path == "/api/overview":
                events, after = [], 0
                while True:
                    page = self.server.journal.history(JOURNAL, after=after, limit=100)
                    events.extend(page["events"])
                    if page["next_after"] is None:
                        break
                    after = page["next_after"]
                snapshots = [e for e in events if e["kind"] == "snapshot"]
                latest = max(snapshots, key=lambda e: (e["occurred_at"], e["recorded_at"]), default=None)
                if latest:
                    amendments = self.server.journal.get(latest["id"])["corrections"]
                    if amendments:
                        latest = dict(latest, data=amendments[-1]["data"]["replacement"])
                coverage_path = self.server.root / "initialization.json"
                coverage = json.loads(coverage_path.read_text()) if coverage_path.exists() else None
                return self.send(200, {"count": len(events), "execution_count": sum(e["kind"] == "execution" for e in events), "activity_count": sum(e["kind"] == "account_activity" for e in events), "snapshot": latest, "initialization": coverage})
            if path.startswith("/api/statement/"):
                digest = path.rsplit("/", 1)[-1]
                if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
                    pdf = self.server.root / "imports" / digest / "statement.pdf"
                    if pdf.is_file():
                        return self.send(200, pdf.read_bytes(), "application/pdf")
                return self.send(404, {"error": "原始对账单不存在"})
            if path == "/api/history":
                after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
                return self.send(200, self.server.journal.history(JOURNAL, after=after, limit=100))
            if path == "/api/report":
                return self.send(200, {"text": report(self.server.journal)})
            if path.startswith("/api/image/"):
                image_id = path.rsplit("/", 1)[-1]
                if len(image_id) == 64 and all(c in "0123456789abcdef" for c in image_id):
                    image = self.server.images / image_id
                    if image.is_file():
                        data = image.read_bytes()
                        return self.send(200, data, "image/png" if data.startswith(b"\x89PNG") else "image/jpeg")
            return self.send(404, {"error": "记录不存在"})
        except ValueError:
            return self.send(400, {"error": "分页参数无效"})

    def do_POST(self):
        if not self.check_host():
            return
        path = urlparse(self.path).path
        if path != "/api/login" and not self.authorized():
            return self.send(401, {"error": "请先输入访问密钥"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                return self.send(413, {"error": "文件为空或超过 12 MB"})
            raw = self.rfile.read(size)
            if path == "/api/upload":
                return self.upload(raw)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("请求必须是对象")
            if path == "/api/login":
                if not hmac.compare_digest(str(body.get("key", "")), self.server.key):
                    return self.send(401, {"error": "访问密钥不正确"})
                token = secrets.token_urlsafe(32)
                self.server.sessions.add(token)
                return self.send(200, {"ok": True}, cookie=f"personal_session={token}; HttpOnly; SameSite=Strict; Path=/")
            if path == "/api/logout":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                self.server.sessions.discard(cookie["personal_session"].value)
                return self.send(200, {"ok": True}, cookie="personal_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            if path == "/api/events":
                if body.pop("confirmed", None) is not True:
                    raise ValueError("请核对后明确确认保存")
                image_id = body.pop("image_id", None)
                if image_id:
                    if not isinstance(image_id, str) or len(image_id) != 64 or any(c not in "0123456789abcdef" for c in image_id) or not (self.server.images / image_id).is_file():
                        raise ValueError("截图来源不存在")
                    body["source_ref"] = "screenshot:" + image_id
                else:
                    body["source_ref"] = "网页手工核对录入"
                if body.get("kind") not in {"snapshot", "execution", "intent", "plan", "review", "correction"}:
                    raise ValueError("不支持的记录类型")
                if set(body) - {"kind", "request_key", "source_ref", "data", "parent_id", "occurred_at"}:
                    raise ValueError("不支持的字段")
                if image_id and body["kind"] not in {"plan", "correction"}:
                    identity = json.dumps({k: v for k, v in body.items() if k != "request_key"}, sort_keys=True, ensure_ascii=False)
                    body["request_key"] = "web-image:" + hashlib.sha256(identity.encode()).hexdigest()
                result = self.server.journal.append(journal_id=JOURNAL, source="user", **body)
                return self.send(200, result)
            return self.send(404, {"error": "接口不存在"})
        except (ValueError, TypeError, KeyError) as exc:
            return self.send(400, {"error": str(exc)})
        except (OSError, sqlite3.Error):
            return self.send(500, {"error": "本地保存失败，请保留草稿后重试"})

    def upload(self, raw):
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")):
            raise ValueError("目前支持 PNG / JPEG 截图")
        image_id = hashlib.sha256(raw).hexdigest()
        target = self.server.images / image_id
        duplicate = target.exists()
        if not duplicate:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
        rows, error = [], None
        if self.server.ocr_binary and self.server.ocr_lock.acquire(blocking=False):
            try:
                result = subprocess.run([str(self.server.ocr_binary), str(target)], capture_output=True, timeout=45, check=True)
                rows = json.loads(result.stdout)
            except (subprocess.SubprocessError, ValueError, OSError):
                error = "本地识别未完成，请对照截图手工填写"
            finally:
                self.server.ocr_lock.release()
        else:
            error = "本地识别未启用或正在忙，请对照截图手工填写"
        return self.send(200, {"image_id": image_id, "duplicate": duplicate, "rows": rows, "draft": trade_draft(rows), "error": error})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8910)
    parser.add_argument("--ocr-binary", type=Path)
    args = parser.parse_args()
    if args.host == "0.0.0.0":
        parser.error("Use a specific local IP, not all interfaces")
    server = PersonalServer((args.host, args.port), args.data_dir, args.ocr_binary)
    print(f"Personal journal: http://{args.host}:{args.port} (access key stored in data directory)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
