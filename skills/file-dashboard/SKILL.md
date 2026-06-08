---
name: file-dashboard
description: Use when the user wants to view, browse, or get stats about files in the FileVault (~/FileVault/). Start the secure web dashboard (password-protected, CSRF, CSP, path-traversal hardened), view file statistics, browse by category, search files, get daily recommendations, or manage importance/starring. Also use when the user asks "show me my files" or "what's in my vault".
version: 1.2.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [dashboard, file-browser, visualization, statistics, recommendations, security]
    related_skills: [file-manager, hierarchical-memory]
---

# File Dashboard — 文件仪表盘 (Security-Hardened)

## Overview

Web-based dashboard for the FileVault. Single-page dark-theme app with password authentication, CSRF protection, CSP headers, path-traversal hardening, and XSS prevention. Served by `~/.hermes/mcp-servers/file-dashboard/server.py`.

## When to Use

- **User asks "show me my files"** → start dashboard, provide URL + password
- **User asks for file stats** → use dashboard or call API directly
- **User wants recommendations** → dashboard recommendations panel
- **User needs to browse by category** → dashboard with sidebar navigation
- **Login/password issues** → use `--password` flag or check `~/.hermes/dashboard_config.json`

Don't use for: file ingestion/organization → use `file-manager` skill.

---

## Starting the Dashboard

```bash
cd ~/.hermes/mcp-servers/file-dashboard
python3 server.py --port 8765
# First run: generates random password → save it!
# Custom password: python3 server.py --port 8765 --password mysecret
```

Access: `http://localhost:8765` → login page → dashboard.

Stop: `fuser -k 8765/tcp`

---

## Security Architecture

### Authentication
- **PBKDF2-SHA256** password hashing (200,000 iterations)
- Password stored in `~/.hermes/dashboard_config.json` (chmod 600)
- **HttpOnly + SameSite=Strict** session cookies, 24-hour expiry
- Rate limiting: **5 attempts per 60 seconds** per IP
- Logout clears session server-side + client cookie

### CSRF Protection
- **Double-submit cookie pattern**: CSRF token returned on login, stored in `sessionStorage`
- All POST/PUT/DELETE requests require `X-CSRF-Token` header
- Server validates CSRF token against session before processing

### Injection Prevention
- **Path traversal**: `safe_resolve()` ensures all file paths stay within `VAULT_ROOT`. Rejects `..`, URL-encoded traversal, null bytes.
- **XSS**: All server-provided data HTML-escaped before `innerHTML`. No inline event handlers — all clicks use `addEventListener`. Category names, file names, and paths all go through `esc()`.
- **CSP header**: `default-src 'self'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'`
- **File extension allowlist**: Only known safe extensions served
- **File size limit**: 500MB max per file
- **Input validation**: `safe_int()`, `safe_str()`, `validate_category()` on all query params

### Response Headers
```
Content-Security-Policy: default-src 'self'; ...
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
```

---

## Login Flow

```
GET /  →  No session cookie?
          ├─ Yes → serve dashboard (index.html)
          └─ No  → serve login page

POST /api/auth/login {password}
  → Verify PBKDF2 hash
  → Create session token + CSRF token
  → Set-Cookie: session_token=...; HttpOnly; SameSite=Strict
  → Return {authenticated: true, csrf_token}

All subsequent API calls:
  GET  → Cookie: session_token=...    (validated by server)
  POST → Cookie + X-CSRF-Token header (both validated)
```

---

## API Endpoints

### Public (no auth)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/status` | GET | Check if authenticated |
| `/api/auth/login` | POST | Login {password} → cookie + csrf |
| `/login` | GET | Login page HTML |

### Protected (auth required)
| `/api/stats` | GET | Vault statistics |
| `/api/recommendations?limit=N` | GET | Daily recommendations |
| `/api/search?q=keyword` | GET | Search files |
| `/api/categories` | GET | Category definitions |
| `/api/vault_files?category=X` | GET | List files by category |
| `/api/file/info?path=X` | GET | File metadata |
| `/api/file/view?path=X` | GET | Serve file (records view) |
| `/api/logout` | GET | Clear session |
| `/api/csrf_token` | GET | Get CSRF token for session |

### Protected + CSRF
| `/api/ingest` | POST | Extract text from file |
| `/api/add_category` | POST | Add custom category |

---

## Common Pitfalls

1. **Port already in use.** `fuser -k 8765/tcp`, retry.

2. **Forgot password.** Delete `~/.hermes/dashboard_config.json` and restart — new random password generated on first run. Or use `--password newpass` to set.

3. **Login returns JSON 401 instead of page.** The server distinguishes API routes (`/api/*`) from page routes. `/` serves the login page for unauthenticated users; `/api/*` returns JSON.

4. **CSRF errors on POST.** Ensure the client stores the CSRF token from login response (`sessionStorage.setItem('csrf_token', ...)`) and sends it as `X-CSRF-Token` header.

5. **Cookie not set after login.** The `Set-Cookie` header must arrive BEFORE `end_headers()`. If modifying the server, never call `_json()` before setting cookies.

6. **Path traversal returning 401 instead of 404.** Path traversal checks run AFTER auth. Unauthenticated requests to `/api/file/view?path=../../etc/passwd` return 401 (not 404) to avoid leaking path existence.

7. **CSP blocking inline scripts.** The dashboard uses `'unsafe-inline'` for `script-src` (needed for the SPA). If tightening CSP, ensure the dashboard's inline JS still executes.

---

## Verification Checklist

- [ ] Dashboard starts without errors
- [ ] Login page renders at `/` for unauthenticated users
- [ ] Login with correct password → dashboard loads
- [ ] Login with wrong password → 401 with remaining attempts
- [ ] Path traversal blocked (`/api/file/view?path=../../etc/passwd` → 404/403)
- [ ] POST without CSRF token → 403
- [ ] POST with CSRF token → succeeds
- [ ] Security headers present (CSP, X-Frame-Options, X-Content-Type-Options)
- [ ] Logout → redirect to login page
- [ ] Rate limiting triggers after 5 rapid failed attempts
