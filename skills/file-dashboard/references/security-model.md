# Dashboard Security Model

> Reference for file-dashboard skill v1.2.0+

## Threat Model

The dashboard is a **local single-user web application**. The primary threats are:

1. **Local network attackers** — someone on the same LAN accessing the dashboard
2. **XSS via file content** — malicious filenames or text content rendered in the browser
3. **Path traversal** — reading arbitrary files from the server filesystem
4. **CSRF** — tricking an authenticated user's browser into making unwanted requests
5. **Brute force** — guessing the dashboard password

## Defense Layers

```
Layer 1: Authentication
  PBKDF2-SHA256 (200k rounds) + HttpOnly cookies + rate limiting

Layer 2: Transport Security
  CSP + X-Frame-Options: DENY + X-Content-Type-Options: nosniff

Layer 3: Input Sanitization
  safe_resolve() for paths, esc() for HTML, allowlists for extensions

Layer 4: CSRF
  Double-submit cookie pattern with X-CSRF-Token header

Layer 5: Operational
  Config file chmod 600, 500MB file size cap, extension allowlist
```

## Known Test Vectors (all blocked)

| Attack | Vector | Result |
|--------|--------|--------|
| Path traversal | `?path=../../../etc/passwd` | 404 |
| URL-encoded traversal | `?path=%2e%2e%2fetc%2fpasswd` | 404 |
| Null byte injection | `?path=/etc/passwd%00.jpg` | 404 |
| Unauthenticated API | `GET /api/stats` (no cookie) | 401 + require_login |
| CSRF | POST without X-CSRF-Token | 403 |
| Brute force | 6 rapid login attempts | 429 + retry_after |

## Cookie Fix (Lessons Learned)

**Bug:** Login returned `Set-Cookie` header AFTER `_json()` called `end_headers()`, so the cookie was silently dropped.

**Fix:** Inline the JSON response in `_handle_login`:
```python
# WRONG (v1.0):
self._json({"authenticated": True, "csrf_token": csrf}, 200)
self.send_header("Set-Cookie", cookie)  # ← silently dropped!

# RIGHT (v1.1+):
self.send_response(200)
self.send_header("Content-Type", "application/json; charset=utf-8")
self.send_header("Set-Cookie", cookie)  # ← before end_headers()
self._add_security_headers()
self.end_headers()
self.wfile.write(json.dumps({...}).encode())
```

**Routing Fix:** Root path `/` was checked AFTER `_require_auth()`, so unauthenticated users saw JSON 401 instead of the login page. Moved root + static file routing ABOVE the auth gate.

## Password Management

```bash
# First run: auto-generates random 12-char password
python3 server.py --port 8765

# Set specific password
python3 server.py --port 8765 --password mysecret

# Reset (lost password)
rm ~/.hermes/dashboard_config.json
python3 server.py --port 8765  # generates new one
```
