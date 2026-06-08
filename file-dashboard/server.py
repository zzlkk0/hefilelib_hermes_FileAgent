#!/usr/bin/env python3
"""
File Dashboard — Secure HTTP server with authentication and injection protection.
Run: python server.py [--port 8765] [--password yourpassword]
"""

import sys
import os
import json
import hmac
import hashlib
import secrets
import time
import re
import http.server
import socketserver
import argparse
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from collections import defaultdict

# ─── Security Configuration ──────────────────────────────────────────

# Content-Security-Policy header
CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
)

# Security headers applied to every response
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

# Allowed file extensions for direct serving
ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".webm", ".mov", ".avi",
    ".mp3", ".wav", ".ogg", ".flac",
    ".pdf",
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".html", ".css",
    ".docx", ".pptx", ".xlsx",
    ".ipynb",
}

# Allowed MIME types
MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".ipynb": "application/json",
}

# Rate limiting for login attempts
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60
SESSION_EXPIRY_SECONDS = 86400  # 24 hours

# ─── Imports from file-manager ───────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent.parent / "file-manager"))
from organize import ensure_vault, get_stats, get_recommendations, search_files, record_access
from classify import get_categories, add_custom_category
from ingest import extract_text

WEB_DIR = Path(__file__).parent / "web"
VAULT_ROOT = Path.home() / "FileVault"
CONFIG_FILE = Path.home() / ".hermes" / "dashboard_config.json"

# ─── Authentication ──────────────────────────────────────────────────

class AuthManager:
    """Manages password hashing, session tokens, and rate limiting."""

    def __init__(self):
        self.password_hash = None
        self.sessions: dict[str, float] = {}  # token → expiry timestamp
        self.csrf_tokens: dict[str, str] = {}  # session_token → csrf_token
        self.login_attempts: defaultdict[str, list[float]] = defaultdict(list)
        self._load_or_create_password()

    def _load_or_create_password(self):
        """Load password from config or generate a random one."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    config = json.load(f)
                self.password_hash = config.get("password_hash")
                if not self.password_hash:
                    raise ValueError("No password_hash in config")
                return
            except Exception:
                pass

        # Generate random password on first run
        password = secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        self.password_hash = salt + ":" + self._hash(password, salt)
        self._save_config()
        print(f"\n🔐 FIRST RUN — Generated password: {password}")
        print(f"   Save this! Config stored at: {CONFIG_FILE}\n")

    def _save_config(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump({"password_hash": self.password_hash}, f)
        os.chmod(CONFIG_FILE, 0o600)

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()

    def verify_password(self, password: str) -> bool:
        """Check password against stored hash."""
        if not self.password_hash or ":" not in self.password_hash:
            return False
        salt, stored_hash = self.password_hash.split(":", 1)
        return hmac.compare_digest(self._hash(password, salt), stored_hash)

    def check_rate_limit(self, ip: str) -> tuple[bool, int]:
        """Returns (allowed, remaining_attempts)."""
        now = time.time()
        self.login_attempts[ip] = [t for t in self.login_attempts[ip] if now - t < LOGIN_WINDOW_SECONDS]
        attempts = len(self.login_attempts[ip])
        remaining = MAX_LOGIN_ATTEMPTS - attempts
        return attempts < MAX_LOGIN_ATTEMPTS, max(0, remaining)

    def record_attempt(self, ip: str):
        self.login_attempts[ip].append(time.time())

    def create_session(self) -> tuple[str, str]:
        """Create a new session. Returns (session_token, csrf_token)."""
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        self.sessions[session_token] = time.time() + SESSION_EXPIRY_SECONDS
        self.csrf_tokens[session_token] = csrf_token
        # Cleanup expired sessions
        now = time.time()
        expired = [t for t, exp in self.sessions.items() if exp < now]
        for t in expired:
            self.sessions.pop(t, None)
            self.csrf_tokens.pop(t, None)
        return session_token, csrf_token

    def validate_session(self, token: str | None) -> bool:
        """Check if a session token is valid."""
        if not token:
            return False
        expiry = self.sessions.get(token)
        if not expiry or time.time() > expiry:
            self.sessions.pop(token, None)
            self.csrf_tokens.pop(token, None)
            return False
        return True

    def get_csrf(self, session_token: str) -> str | None:
        return self.csrf_tokens.get(session_token)

    def logout(self, token: str):
        self.sessions.pop(token, None)
        self.csrf_tokens.pop(token, None)


auth = AuthManager()

# ─── Path Safety ──────────────────────────────────────────────────────

def safe_resolve(base: Path, user_path: str) -> Path | None:
    """Resolve a path and ensure it stays within base. Returns None on escape attempt."""
    # Decode URL encoding and strip null bytes
    user_path = unquote(user_path).replace("\0", "")

    # Clean the path
    cleaned = user_path.lstrip("/")
    parts = []
    for part in cleaned.split("/"):
        if part in ("", ".", ".."):
            if part == "..":
                # Any .. means path traversal attempt
                return None
            continue
        # Reject suspicious patterns
        if part.startswith("~") or "\x00" in part:
            return None
        parts.append(part)

    if not parts:
        return None

    resolved = (base / "/".join(parts)).resolve()

    # Must be inside base
    try:
        resolved.relative_to(base)
    except ValueError:
        return None

    return resolved


def is_safe_filename(name: str) -> bool:
    """Reject filenames with path separators or control characters."""
    if not name or "\x00" in name or "\n" in name or "\r" in name:
        return False
    if any(c in name for c in ("/", "\\", "..")):
        return False
    return True


def validate_category(category: str) -> str:
    """Validate and sanitize category name. Returns safe string or empty."""
    if not category:
        return ""
    # Only allow specific safe characters
    cleaned = re.sub(r'[^\w\u4e00-\u9fff\-_]', '', category)[:64]
    return cleaned


# ─── Input Validation ────────────────────────────────────────────────

def safe_int(value: str, default: int = 0, min_val: int = 0, max_val: int = 10000) -> int:
    """Safely parse an integer with bounds."""
    try:
        v = int(value)
        return max(min_val, min(v, max_val))
    except (ValueError, TypeError):
        return default


def safe_str(value: str, max_len: int = 500) -> str:
    """Sanitize a string parameter."""
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "")[:max_len]


# ─── HTTP Handler ────────────────────────────────────────────────────

class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Secure HTTP handler with authentication and injection protection."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    # ── Auth helpers ──────────────────────────────────────────────

    def _get_session_token(self) -> str | None:
        """Extract session token from cookie."""
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"session_token=([^;]+)", cookie)
        return match.group(1) if match else None

    def _get_csrf_header(self) -> str | None:
        return self.headers.get("X-CSRF-Token")

    def _require_auth(self) -> bool:
        """Check if request is authenticated. Sends 401 if not."""
        token = self._get_session_token()
        if auth.validate_session(token):
            return True
        self._json({"error": "Unauthorized", "require_login": True}, 401)
        return False

    def _require_csrf(self) -> bool:
        """Verify CSRF token for state-changing requests."""
        session_token = self._get_session_token()
        expected = auth.get_csrf(session_token) if session_token else None
        provided = self._get_csrf_header()
        if expected and provided and hmac.compare_digest(expected, provided):
            return True
        self._json({"error": "CSRF validation failed"}, 403)
        return False

    # ── Security headers ──────────────────────────────────────────

    def _add_security_headers(self):
        self.send_header("Content-Security-Policy", CSP_HEADER)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)

    # ── Response helpers ───────────────────────────────────────────

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._add_security_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _html_response(self, html: str, status=200, extra_cookies=None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._add_security_headers()
        if extra_cookies:
            for cookie in extra_cookies:
                self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    # ── Routing ────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # ── Public routes (no auth required) ────────────────────
        if path == "/api/auth/status":
            token = self._get_session_token()
            self._json({"authenticated": auth.validate_session(token)})

        elif path == "/api/auth/login_page" or path == "/login":
            self._serve_login_page()

        # ── Root / static files: serve dashboard or login ──────
        elif path in ("/", "") or not path.startswith("/api/"):
            token = self._get_session_token()
            if auth.validate_session(token):
                super().do_GET()  # Serve dashboard or static asset
            else:
                self._serve_login_page()

        # ── Protected API routes ────────────────────────────────
        elif not self._require_auth():
            return  # 401 already sent

        elif path == "/api/stats":
            self._json(get_stats())

        elif path == "/api/recommendations":
            limit = safe_int(params.get("limit", ["5"])[0], default=5, min_val=1, max_val=50)
            self._json(get_recommendations(limit))

        elif path == "/api/search":
            query = safe_str(params.get("q", [""])[0], max_len=200)
            self._json(search_files(query) if query else [])

        elif path == "/api/categories":
            self._json(get_categories())

        elif path == "/api/file/info":
            self._handle_file_info(params)

        elif path == "/api/file/view":
            self._handle_file_view(params)

        elif path == "/api/vault_files":
            self._handle_vault_files(params)

        elif path == "/api/logout":
            token = self._get_session_token()
            if token:
                auth.logout(token)
            self._json({"status": "logged_out"})

        elif path == "/api/csrf_token":
            session_token = self._get_session_token()
            csrf = auth.get_csrf(session_token) if session_token else None
            self._json({"csrf_token": csrf} if csrf else {"error": "No session"}, 401 if not csrf else 200)

        elif path.startswith("/static/"):
            self._handle_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = max(0, min(int(self.headers.get("Content-Length", 0)), 1024 * 1024))
        body = self.rfile.read(content_length) if content_length else b"{}"

        # Parse body safely
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._json({"error": "Invalid JSON"}, 400)
            return

        # ── Public routes ──────────────────────────────────────
        if path == "/api/auth/login":
            self._handle_login(data)

        # ── Protected API routes ────────────────────────────────
        elif not self._require_auth():
            return

        elif not self._require_csrf():
            return

        elif path == "/api/ingest":
            filepath = safe_str(data.get("filepath", ""), max_len=1000)
            if filepath:
                result = extract_text(filepath)
            else:
                result = {"error": "filepath required"}
            self._json(result)

        elif path == "/api/add_category":
            name = validate_category(data.get("name", ""))
            keywords = data.get("keywords", [])
            if not isinstance(keywords, list):
                keywords = []
            keywords = [safe_str(k, max_len=100) for k in keywords if isinstance(k, str)][:50]
            extensions = data.get("extensions")
            if extensions is not None and not isinstance(extensions, list):
                extensions = None

            if not name:
                self._json({"error": "Category name required"}, 400)
            else:
                result = add_custom_category(name=name, keywords=keywords, extensions=extensions)
                self._json(result)

        else:
            self._json({"error": "Unknown endpoint"}, 404)

    # ── Route Handlers ────────────────────────────────────────────

    def _handle_login(self, data: dict):
        """Handle login attempt with rate limiting."""
        ip = self.client_address[0]
        allowed, remaining = auth.check_rate_limit(ip)

        if not allowed:
            self._json({
                "error": "Too many login attempts. Please wait.",
                "retry_after_seconds": LOGIN_WINDOW_SECONDS
            }, 429)
            return

        password = data.get("password", "")
        auth.record_attempt(ip)

        if auth.verify_password(password):
            session_token, csrf_token = auth.create_session()
            cookie = (
                f"session_token={session_token}; "
                "Path=/; HttpOnly; SameSite=Strict; Max-Age=86400"
            )
            # Set cookie BEFORE _json() which ends headers
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", cookie)
            self._add_security_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "authenticated": True,
                "csrf_token": csrf_token
            }, ensure_ascii=False).encode())
        else:
            remaining_after = max(0, MAX_LOGIN_ATTEMPTS - len(auth.login_attempts[ip]))
            self._json({
                "error": "Invalid password",
                "remaining_attempts": remaining_after
            }, 401)

    def _handle_file_info(self, params: dict):
        """Get file metadata with path safety check."""
        raw_path = params.get("path", [""])[0]
        filepath = safe_resolve(VAULT_ROOT, raw_path)
        if not filepath or not filepath.exists():
            self._json({"error": "File not found"})
            return
        self._json(self._file_info(str(filepath)))

    def _handle_file_view(self, params: dict):
        """Serve a file with path traversal protection."""
        raw_path = params.get("path", [""])[0]
        filepath = safe_resolve(VAULT_ROOT, raw_path)
        if not filepath or not filepath.exists():
            self._json({"error": "File not found"}, 404)
            return

        # Extension allowlist
        ext = filepath.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            self._json({"error": "File type not allowed"}, 403)
            return

        record_access(str(filepath))
        self._serve_file(str(filepath))

    def _handle_vault_files(self, params: dict):
        """List vault files with category validation."""
        cat_raw = params.get("category", [""])[0]
        category = validate_category(cat_raw) if cat_raw else ""
        self._json(self._list_vault_files(category))

    def _handle_static(self, path: str):
        """Serve static vault files with path safety."""
        rel = path[len("/static/"):]
        filepath = safe_resolve(VAULT_ROOT, rel)
        if not filepath or not filepath.exists() or not filepath.is_file():
            self._json({"error": "File not found"}, 404)
            return
        self._serve_file(str(filepath))

    # ── File operations ───────────────────────────────────────────

    def _file_info(self, filepath: str) -> dict:
        p = Path(filepath)
        if not p.exists():
            return {"error": "File not found"}
        return {
            "path": str(p),
            "name": p.name,
            "size": p.stat().st_size,
            "modified": p.stat().st_mtime,
            "extension": p.suffix.lower(),
            "is_image": p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
            "is_video": p.suffix.lower() in {".mp4", ".webm", ".mov", ".avi"},
            "is_audio": p.suffix.lower() in {".mp3", ".wav", ".ogg", ".flac"},
            "is_text": p.suffix.lower() in {".txt", ".md", ".csv", ".json", ".log", ".py"},
        }

    def _serve_file(self, filepath: str):
        p = Path(filepath)
        if not p.exists() or not p.is_file():
            self._json({"error": "File not found"}, 404)
            return

        ext = p.suffix.lower()
        content_type = MIME_MAP.get(ext, "application/octet-stream")
        file_size = p.stat().st_size

        # Reject oversized files (>500MB)
        if file_size > 500 * 1024 * 1024:
            self._json({"error": "File too large"}, 413)
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self._add_security_headers()

        # Support range requests for video seeking
        range_header = self.headers.get("Range")
        if range_header and ext in {".mp4", ".webm", ".mov"}:
            self._handle_range(p, file_size, content_type, range_header)
            return

        self.end_headers()
        try:
            with open(p, "rb") as f:
                self.wfile.write(f.read())
        except Exception:
            pass  # Client disconnected

    def _handle_range(self, p: Path, file_size: int, content_type: str, range_header: str):
        match = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not match:
            self.send_response(416)
            self._add_security_headers()
            self.end_headers()
            return

        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else file_size - 1

        # Validate range bounds
        if start >= file_size or end >= file_size or start > end:
            self.send_response(416)
            self._add_security_headers()
            self.end_headers()
            return

        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self._add_security_headers()
        self.end_headers()

        try:
            with open(p, "rb") as f:
                f.seek(start)
                self.wfile.write(f.read(end - start + 1))
        except Exception:
            pass

    def _list_vault_files(self, category: str = "") -> list[dict]:
        """List files in vault, optionally filtered by validated category."""
        results = []
        search_root = VAULT_ROOT / category if category else VAULT_ROOT

        # Safety: resolve and ensure inside vault
        try:
            search_root = search_root.resolve()
            search_root.relative_to(VAULT_ROOT.resolve())
        except (ValueError, OSError):
            return results

        if not search_root.exists() or not search_root.is_dir():
            return results

        for p in search_root.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                # Safety: ensure resolved path is still in vault
                try:
                    p.resolve().relative_to(VAULT_ROOT.resolve())
                except ValueError:
                    continue

                ext = p.suffix.lower()
                results.append({
                    "path": str(p),
                    "name": p.name,
                    "size": p.stat().st_size,
                    "modified": p.stat().st_mtime,
                    "extension": ext,
                    "is_image": ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
                    "is_video": ext in {".mp4", ".webm", ".mov", ".avi"},
                    "is_audio": ext in {".mp3", ".wav", ".ogg", ".flac"},
                    "is_text": ext in {".txt", ".md", ".csv", ".json", ".log", ".py"},
                    "is_pdf": ext == ".pdf",
                    "relative_path": str(p.relative_to(VAULT_ROOT)),
                })

        return results

    def _serve_login_page(self):
        """Serve the login page HTML."""
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔐 FileVault Login</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #0f1117; --surface: #1a1d27; --surface2: #242734;
  --border: #2a2d3a; --text: #e1e4ed; --text2: #8b8fa3;
  --accent: #6c8cff; --danger: #ef4444; --radius: 10px;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg); color: var(--text);
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh;
}
.login-box {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 40px; width: 380px; max-width: 90vw;
}
.login-box h1 { font-size: 24px; margin-bottom: 8px; color: var(--accent); }
.login-box .sub { color: var(--text2); font-size: 13px; margin-bottom: 24px; }
.input-group { margin-bottom: 16px; }
.input-group label { display: block; font-size: 12px; color: var(--text2); margin-bottom: 6px; }
.input-group input {
  width: 100%; padding: 10px 14px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface2);
  color: var(--text); font-size: 14px; outline: none;
}
.input-group input:focus { border-color: var(--accent); }
.btn {
  width: 100%; padding: 10px; border-radius: 8px;
  border: none; background: var(--accent); color: white;
  font-size: 14px; font-weight: 600; cursor: pointer;
  transition: opacity 0.2s;
}
.btn:hover { opacity: 0.9; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.error { color: var(--danger); font-size: 12px; margin-top: 12px; text-align: center; min-height: 18px; }
</style>
</head>
<body>
<div class="login-box">
  <h1>📊 FileVault</h1>
  <div class="sub">请输入密码以访问文件仪表盘</div>
  <div class="input-group">
    <label>密码</label>
    <input type="password" id="password" placeholder="输入密码..." autofocus onkeydown="if(event.key==='Enter')login()">
  </div>
  <button class="btn" id="loginBtn" onclick="login()">登录</button>
  <div class="error" id="errorMsg"></div>
</div>
<script>
async function login() {
  const pw = document.getElementById('password').value;
  const btn = document.getElementById('loginBtn');
  const err = document.getElementById('errorMsg');

  if (!pw) { err.textContent = '请输入密码'; return; }

  btn.disabled = true;
  btn.textContent = '验证中...';

  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw})
    });
    const data = await r.json();

    if (data.authenticated) {
      // Store CSRF token for subsequent requests
      sessionStorage.setItem('csrf_token', data.csrf_token);
      window.location.href = '/';
    } else {
      err.textContent = data.error || '密码错误';
      if (data.remaining_attempts !== undefined) {
        err.textContent += ' (剩余尝试: ' + data.remaining_attempts + ')';
      }
    }
  } catch(e) {
    err.textContent = '网络错误，请重试';
  }

  btn.disabled = false;
  btn.textContent = '登录';
}
</script>
</body>
</html>"""
        self._html_response(html)

    def log_message(self, format, *args):
        sys.stderr.write(f"[dashboard] {args[0]}\n")


# ─── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="File Dashboard — Secure HTTP Server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--password", help="Set dashboard password (overwrites config)")
    args = parser.parse_args()

    # Handle password change
    if args.password:
        salt = secrets.token_hex(16)
        auth.password_hash = salt + ":" + auth._hash(args.password, salt)
        auth._save_config()
        print(f"🔐 Password updated. Config: {CONFIG_FILE}")

    ensure_vault()
    os.chdir(WEB_DIR)

    with ReusableTCPServer((args.host, args.port), DashboardHandler) as httpd:
        print(f"📊 File Dashboard running at http://{args.host}:{args.port}")
        print(f"   Vault: {VAULT_ROOT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")


if __name__ == "__main__":
    main()
