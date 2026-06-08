"""
File Manager MCP Server — organize + archive module
Handles file movement and periodic cleanup.
"""

import os
import shutil
import time
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

VAULT_ROOT = Path.home() / "FileVault"
META_DB = VAULT_ROOT / ".meta.db"

CATEGORY_DIRS = ["学习", "备忘", "提醒", "生活", "任务", "财务", "职务"]


def ensure_vault() -> dict:
    """Create vault directory structure if it doesn't exist."""
    created = []
    for d in [VAULT_ROOT, VAULT_ROOT / "_inbox", VAULT_ROOT / "_archive"] + \
             [VAULT_ROOT / c for c in CATEGORY_DIRS]:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))
    _init_meta_db()
    return {"vault_root": str(VAULT_ROOT), "created": created}


def _init_meta_db():
    """Initialize SQLite metadata database."""
    META_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(META_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            original_name TEXT,
            category TEXT DEFAULT '_inbox',
            file_type TEXT,
            extracted_text TEXT,
            keywords TEXT,
            file_size INTEGER,
            confidence REAL DEFAULT 0.0,
            importance REAL DEFAULT 0.0,
            starred INTEGER DEFAULT 0,
            view_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            last_accessed TEXT DEFAULT (datetime('now')),
            last_modified TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT,
            accessed_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (file_path) REFERENCES files(path)
        )
    """)
    # Descriptions table — human-readable summaries for files AND folders
    conn.execute("""
        CREATE TABLE IF NOT EXISTS descriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # FTS index for full-text search on descriptions
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS descriptions_fts USING fts5(
            path, description,
            content='descriptions',
            content_rowid='id'
        )
    """)
    # Triggers to keep FTS in sync
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS descriptions_ai AFTER INSERT ON descriptions BEGIN
            INSERT INTO descriptions_fts(rowid, path, description) VALUES (new.id, new.path, new.description);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS descriptions_ad AFTER DELETE ON descriptions BEGIN
            INSERT INTO descriptions_fts(descriptions_fts, rowid, path, description) VALUES ('delete', old.id, old.path, old.description);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS descriptions_au AFTER UPDATE ON descriptions BEGIN
            INSERT INTO descriptions_fts(descriptions_fts, rowid, path, description) VALUES ('delete', old.id, old.path, old.description);
            INSERT INTO descriptions_fts(rowid, path, description) VALUES (new.id, new.path, new.description);
        END
    """)
    conn.commit()
    conn.close()


def organize_file(filepath: str, category: str, learn: bool = True) -> dict:
    """Move a file to its category folder. If learn=True, records correction for the learning engine."""
    ensure_vault()

    src = Path(filepath)
    if not src.exists():
        return {"error": f"Source not found: {filepath}", "moved": False}

    # Get previous category for learning
    previous_category = _get_file_category(str(src)) if learn else None

    # Determine target directory
    target_dir = VAULT_ROOT / category
    if not target_dir.exists():
        target_dir = VAULT_ROOT / "_inbox"  # fallback

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / src.name

    # Handle name collisions
    if target.exists():
        stem = src.stem
        suffix = src.suffix
        counter = 1
        while target.exists():
            target = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    try:
        shutil.move(str(src), str(target))

        # Update metadata
        _upsert_meta(
            path=str(target),
            original_name=src.name,
            category=category
        )

        return {
            "moved": True,
            "src": str(src),
            "dst": str(target),
            "category": category,
            "learned": _maybe_learn_correction(str(target), previous_category, category) if previous_category and previous_category != category else None,
        }
    except Exception as e:
        return {"error": str(e), "moved": False, "src": str(src)}


def _upsert_meta(path: str, **kwargs):
    """Insert or update file metadata."""
    conn = sqlite3.connect(str(META_DB))

    # Check if exists
    cur = conn.execute("SELECT id FROM files WHERE path = ?", (path,))
    existing = cur.fetchone()

    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [path]
        conn.execute(f"UPDATE files SET {sets} WHERE path = ?", values)
    else:
        columns = ["path"] + list(kwargs.keys())
        placeholders = ", ".join("?" * len(columns))
        values = [path] + list(kwargs.values())
        conn.execute(f"INSERT INTO files ({', '.join(columns)}) VALUES ({placeholders})", values)

    conn.commit()
    conn.close()


def record_access(filepath: str):
    """Log file access and update view count."""
    conn = sqlite3.connect(str(META_DB))
    conn.execute("INSERT INTO access_log (file_path) VALUES (?)", (filepath,))
    conn.execute(
        "UPDATE files SET view_count = view_count + 1, last_accessed = datetime('now') WHERE path = ?",
        (filepath,)
    )
    conn.commit()
    conn.close()


def archive_cleanup(days: int = 180) -> dict:
    """Archive files not accessed in N days."""
    ensure_vault()

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    conn = sqlite3.connect(str(META_DB))
    cur = conn.execute(
        "SELECT path, category FROM files WHERE last_accessed < ? AND category != '_archive' AND starred = 0",
        (cutoff,)
    )
    candidates = cur.fetchall()
    conn.close()

    archive_dir = VAULT_ROOT / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archived = []
    total_size = 0

    for filepath, category in candidates:
        src = Path(filepath)
        if not src.exists():
            continue

        # Preserve nested structure: _archive/原类别/文件名
        nested = archive_dir / category
        nested.mkdir(parents=True, exist_ok=True)
        dst = nested / src.name

        # Handle collisions
        if dst.exists():
            stem = src.stem
            suffix = src.suffix
            counter = 1
            while dst.exists():
                dst = nested / f"{stem}_{counter}{suffix}"
                counter += 1

        try:
            file_size = src.stat().st_size
            shutil.move(str(src), str(dst))
            total_size += file_size
            archived.append({"src": str(src), "dst": str(dst), "size": file_size})

            # Update metadata
            _upsert_meta(path=str(dst), category="_archive")
        except Exception as e:
            archived.append({"src": str(src), "error": str(e)})

    return {
        "archived_count": len([a for a in archived if "error" not in a]),
        "total_freed_bytes": total_size,
        "details": archived
    }


def search_files(query: str) -> list[dict]:
    """Search files by keyword in metadata."""
    ensure_vault()
    conn = sqlite3.connect(str(META_DB))
    conn.create_function("CONTAINS", 2, lambda x, y: 1 if y.lower() in (x or "").lower() else 0)

    cur = conn.execute(
        "SELECT path, category, file_type, original_name, view_count, importance, confidence, "
        "last_accessed FROM files WHERE original_name LIKE ? OR keywords LIKE ? OR extracted_text LIKE ? "
        "ORDER BY importance DESC, view_count DESC LIMIT 50",
        (f"%{query}%", f"%{query}%", f"%{query}%")
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "path": r[0], "category": r[1], "file_type": r[2],
            "original_name": r[3], "view_count": r[4],
            "importance": r[5], "confidence": r[6], "last_accessed": r[7]
        }
        for r in rows
    ]


def get_stats() -> dict:
    """Get vault statistics."""
    ensure_vault()
    conn = sqlite3.connect(str(META_DB))

    cur = conn.execute(
        "SELECT category, COUNT(*) as count, SUM(file_size) as total_size "
        "FROM files WHERE category IS NOT NULL GROUP BY category"
    )
    by_category = {r[0]: {"count": r[1], "size": r[2] or 0} for r in cur.fetchall()}

    cur = conn.execute("SELECT COUNT(*), SUM(file_size) FROM files")
    total = cur.fetchone()

    cur = conn.execute(
        "SELECT path, view_count FROM files ORDER BY view_count DESC LIMIT 10"
    )
    top_viewed = [{"path": r[0], "view_count": r[1]} for r in cur.fetchall()]

    # Files needing attention (in inbox or overdue)
    cur = conn.execute("SELECT COUNT(*) FROM files WHERE category = '_inbox'")
    inbox_count = cur.fetchone()[0]

    conn.close()

    return {
        "total_files": total[0] or 0,
        "total_size_bytes": total[1] or 0,
        "by_category": by_category,
        "top_viewed": top_viewed,
        "inbox_count": inbox_count
    }


def get_recommendations(limit: int = 5) -> dict:
    """Get daily file recommendations."""
    ensure_vault()
    conn = sqlite3.connect(str(META_DB))

    # High-importance files not recently viewed
    cur = conn.execute(
        "SELECT path, category, original_name, importance, last_accessed "
        "FROM files WHERE importance > 0 AND category != '_archive' "
        "ORDER BY importance DESC, last_accessed ASC LIMIT ?",
        (limit,)
    )
    important = [
        {"path": r[0], "category": r[1], "name": r[2], "importance": r[3], "last_accessed": r[4]}
        for r in cur.fetchall()
    ]

    # Random discovery (files never accessed or very old)
    cur = conn.execute(
        "SELECT path, category, original_name, view_count "
        "FROM files WHERE view_count = 0 AND category != '_archive' "
        "ORDER BY RANDOM() LIMIT ?",
        (limit,)
    )
    discovery = [
        {"path": r[0], "category": r[1], "name": r[2], "view_count": r[3]}
        for r in cur.fetchall()
    ]

    # Overdue reminders (files in 提醒 category with old dates)
    cur = conn.execute(
        "SELECT path, original_name, last_accessed FROM files "
        "WHERE category = '提醒' ORDER BY last_accessed ASC LIMIT ?",
        (limit,)
    )
    overdue = [
        {"path": r[0], "name": r[1], "last_accessed": r[2]}
        for r in cur.fetchall()
    ]

    conn.close()

    return {
        "important": important,
        "discovery": discovery,
        "overdue": overdue
    }


def update_importance(filepath: str, importance: float) -> dict:
    """Set importance score for a file."""
    _upsert_meta(path=filepath, importance=importance)
    return {"path": filepath, "importance": importance, "updated": True}


def toggle_star(filepath: str) -> dict:
    """Toggle starred status for a file."""
    conn = sqlite3.connect(str(META_DB))
    cur = conn.execute("SELECT starred FROM files WHERE path = ?", (filepath,))
    row = cur.fetchone()
    if row:
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE files SET starred = ? WHERE path = ?", (new_val, filepath))
        conn.commit()
        conn.close()
        return {"path": filepath, "starred": bool(new_val)}
    conn.close()
    return {"error": "File not found in metadata"}


# ─── Learning engine integration ──────────────────────────────────────

def _get_file_category(filepath: str) -> str | None:
    """Get the stored category for a file from metadata."""
    conn = sqlite3.connect(str(META_DB))
    cur = conn.execute("SELECT category FROM files WHERE path = ?", (filepath,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _get_file_keywords(filepath: str) -> list[str]:
    """Get stored keywords for a file."""
    conn = sqlite3.connect(str(META_DB))
    cur = conn.execute("SELECT keywords FROM files WHERE path = ?", (filepath,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def _maybe_learn_correction(filepath: str, from_cat: str, to_cat: str) -> dict | None:
    """If a file is moved between categories, record it for the learning engine."""
    try:
        from classify import record_correction
        keywords = _get_file_keywords(filepath)
        return record_correction(filepath, from_cat, to_cat, keywords)
    except Exception:
        return None


def record_correction_wrapper(filepath: str, from_category: str, to_category: str) -> dict:
    """Explicitly record a correction (e.g., user manually reclassifies)."""
    from classify import record_correction
    keywords = _get_file_keywords(filepath)
    return record_correction(filepath, from_category, to_category, keywords)


def get_learning_stats_wrapper() -> dict:
    """Get learning engine statistics."""
    from classify import get_learning_stats
    return get_learning_stats()


def suggest_categories_wrapper(min_files: int = 5, locale: str = "zh") -> list[dict]:
    """Suggest new categories from inbox files."""
    from classify import suggest_categories
    return suggest_categories(min_files, locale)


# ─── File Grouping / Batch Collation ──────────────────────────────────

def detect_file_groups(directory: str, min_group_size: int = 2) -> list[dict]:
    """
    Scan a directory for files that appear to belong together (same title pattern).
    Returns list of groups, each with files, suggested folder name, and confidence.

    Detection strategies:
      1. Common prefix (before _/- separator): report_v1.pdf + report_v2.pdf → "report"
      2. Sequential numbering: photo_001.jpg + photo_002.jpg → "photo"
      3. Same stem, different extension: contract.pdf + contract.docx → "contract"
      4. Date-prefixed with common suffix: 2024-01-meeting.pdf + 2024-02-meeting.pdf → "meeting"
    """
    import re
    from pathlib import Path
    from collections import defaultdict

    dir_path = Path(directory)
    if not dir_path.exists() or not dir_path.is_dir():
        return [{"error": f"Directory not found: {directory}"}]

    files = [f for f in dir_path.iterdir() if f.is_file() and not f.name.startswith(".")]
    if len(files) < min_group_size:
        return [{"status": "no_groups", "message": f"Only {len(files)} files in directory (need {min_group_size}+)", "file_count": len(files)}]

    # For each file, generate stem candidates
    # stem_sets[stem] = list of file paths
    stem_sets = defaultdict(list)
    file_stems = {}  # filepath → list of stems

    for fp in files:
        name = fp.stem  # filename without extension
        stems = _generate_stems(name)
        file_stems[str(fp)] = stems
        for s in stems:
            stem_sets[s].append(str(fp))

    # Filter: only groups with >= min_group_size
    groups = []
    seen_files = set()

    # Sort by group size descending, then by stem length (prefer shorter, more generic stems)
    sorted_stems = sorted(stem_sets.items(), key=lambda x: (-len(x[1]), len(x[0])))

    for stem, group_files in sorted_stems:
        # Skip if all files already covered by a larger group
        if len(group_files) < min_group_size:
            continue
        new_files = [f for f in group_files if f not in seen_files]
        if len(new_files) < min_group_size:
            continue

        # Generate a human-friendly folder name
        folder_name = _folder_name_from_stem(stem, new_files)

        # Detect the grouping strategy for explanation
        strategy = _detect_strategy(stem, new_files)

        groups.append({
            "suggested_folder": folder_name,
            "stem": stem,
            "files": [{"path": f, "name": Path(f).name} for f in new_files],
            "file_count": len(new_files),
            "strategy": strategy,
            "confidence": min(0.5 + len(new_files) * 0.15, 1.0),
        })

        seen_files.update(new_files)

    # Find remaining files (no group)
    remaining = [str(f) for f in files if str(f) not in seen_files]

    return {
        "directory": str(dir_path),
        "total_files": len(files),
        "groups": groups,
        "grouped_count": len(seen_files),
        "ungrouped_count": len(remaining),
        "ungrouped_files": [{"path": f, "name": Path(f).name} for f in remaining[:20]],  # cap
    }


def _generate_stems(name: str) -> list[str]:
    """Generate stem candidates from a filename."""
    import re
    stems = set()
    name_lower = name.lower()

    # Candidate 1: Original stem
    stems.add(name)

    # Candidate 2: Strip version suffixes
    for suffix in [r'[ _\-]v\d+$', r'[ _\-]ver\w*$', r'[ _\-]final$',
                   r'[ _\-]draft$', r'[ _\-]副本$', r'[ _\-]copy$',
                   r'[ _\-]old$', r'[ _\-]new$', r'[ _\-]edited$',
                   r'[ _\-]\d+\.\d+$', r'\(\d+\)$', r'（\d+）$']:
        stripped = re.sub(suffix, '', name_lower, flags=re.IGNORECASE).strip()
        if stripped and stripped != name_lower:
            stems.add(stripped)

    # Candidate 3: Strip trailing numbers (sequential)
    stripped = re.sub(r'[ _\-]?\d{2,}$', '', name_lower).strip()
    if stripped and stripped != name_lower:
        stems.add(stripped)

    # Candidate 4: Strip date prefixes (YYYY-MM-DD / YYYYMMDD)
    date_stripped = re.sub(r'^\d{4}[ _\-]?\d{2}[ _\-]?\d{2}[ _\-]', '', name_lower).strip()
    if date_stripped and date_stripped != name_lower:
        stems.add(date_stripped)

    # Candidate 5: Common prefix before separator (when multiple files share it)
    for sep in ['_', '-', ' ']:
        parts = name_lower.split(sep)
        if len(parts) >= 2:
            # Try first N parts as prefix
            for n in range(1, len(parts)):
                prefix = sep.join(parts[:n])
                stems.add(prefix)

    # Candidate 6: Remove Chinese/English mixed numbering
    stripped = re.sub(r'[第]?\d+[章节部分]?', '', name_lower).strip()
    if stripped and stripped != name_lower:
        stems.add(stripped)

    # Keep only unique, non-empty stems, sorted by length (shortest first = most general)
    valid = [s for s in stems if len(s) >= 2]
    return sorted(set(valid), key=len)


def _folder_name_from_stem(stem: str, files: list[str]) -> str:
    """Generate a natural folder name from the stem."""
    # Capitalize first letter for readability
    name = stem.strip("_- ").replace("_", " ").replace("-", " ").title()
    # Truncate if too long
    if len(name) > 50:
        name = name[:47] + "..."
    return name or "Grouped Files"


def _detect_strategy(stem: str, files: list[str]) -> str:
    """Determine which grouping strategy matched."""
    from pathlib import Path
    names = [Path(f).stem.lower() for f in files]

    # Check for sequential numbering
    import re
    has_numbers = any(re.search(r'\d{2,}', n) for n in names)

    # Check for different extensions
    exts = {Path(f).suffix.lower() for f in files}
    if len(exts) > 1:
        if has_numbers:
            return "sequential_different_formats"
        return "same_stem_different_formats"

    if has_numbers:
        return "sequential_numbering"

    # Check for version suffixes
    version_words = {'v1', 'v2', 'v3', 'final', 'draft', 'old', 'new', '副本'}
    if any(w in ' '.join(names) for w in version_words):
        return "version_variants"

    return "common_prefix"


def group_files(file_list: list[str], folder_name: str, source_dir: str = None) -> dict:
    """
    Move a group of files into a subfolder.
    Creates the folder inside source_dir (or the parent of the first file if source_dir not given).

    Args:
        file_list: List of file paths to group together
        folder_name: Name for the new subfolder
        source_dir: Parent directory for the new folder (optional; auto-detected)

    Returns:
        {grouped: True, folder: path, moved_count: N, files: [...]}
    """
    from pathlib import Path

    if not file_list:
        return {"error": "No files provided", "grouped": False}

    # Determine the base directory
    if source_dir:
        base = Path(source_dir)
    else:
        base = Path(file_list[0]).parent

    # Sanitize folder name
    safe_name = folder_name.strip().replace("/", "_").replace("\\", "_").replace("\x00", "")
    if not safe_name:
        safe_name = "GroupedFiles"

    target_folder = base / safe_name

    # Handle name collision: append number
    counter = 1
    original = target_folder
    while target_folder.exists():
        target_folder = Path(str(original) + f"_{counter}")
        counter += 1

    target_folder.mkdir(parents=True, exist_ok=True)

    moved = []
    errors = []

    for filepath in file_list:
        src = Path(filepath)
        if not src.exists():
            errors.append({"file": filepath, "error": "File not found"})
            continue

        target = target_folder / src.name
        # Handle collision within target
        if target.exists():
            stem = src.stem
            suffix = src.suffix
            c = 1
            while target.exists():
                target = target_folder / f"{stem}_{c}{suffix}"
                c += 1

        try:
            shutil.move(str(src), str(target))
            moved.append({"src": filepath, "dst": str(target), "name": src.name})

            # Update metadata
            _upsert_meta(
                path=str(target),
                original_name=src.name,
            )
        except Exception as e:
            errors.append({"file": filepath, "error": str(e)})

    return {
        "grouped": True,
        "folder": str(target_folder),
        "moved_count": len(moved),
        "error_count": len(errors),
        "moved": moved,
        "errors": errors if errors else None,
    }


def ungroup_files(folder_path: str) -> dict:
    """
    Move all files out of a subfolder back to the parent directory.
    Deletes the now-empty folder.
    """
    from pathlib import Path

    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        return {"error": f"Not a valid folder: {folder_path}", "ungrouped": False}

    parent = folder.parent
    moved = []
    errors = []

    for fp in folder.iterdir():
        if fp.is_file():
            target = parent / fp.name
            if target.exists():
                stem = fp.stem
                suffix = fp.suffix
                c = 1
                while target.exists():
                    target = parent / f"{stem}_{c}{suffix}"
                    c += 1
            try:
                shutil.move(str(fp), str(target))
                moved.append({"src": str(fp), "dst": str(target)})
                _upsert_meta(path=str(target))
            except Exception as e:
                errors.append({"file": str(fp), "error": str(e)})

    # Remove folder if empty
    try:
        remaining = list(folder.iterdir())
        if not remaining:
            folder.rmdir()
            deleted_folder = True
        else:
            deleted_folder = False
    except Exception:
        deleted_folder = False

    return {
        "ungrouped": True,
        "moved_count": len(moved),
        "error_count": len(errors),
        "moved": moved,
        "errors": errors if errors else None,
        "folder_deleted": deleted_folder,
    }


# ─── Description Database (v2.3) ──────────────────────────────────────

def describe(path: str, description: str = None) -> dict:
    """
    Set or get a human-readable description for a file or folder.

    If description is provided (non-None, non-empty): upsert the description.
    If description is None or empty string: return the current description.

    Returns {path, description, created_at, updated_at, action: "set"|"get"}
    """
    conn = sqlite3.connect(str(META_DB))

    if description is not None and description != "":
        # SET mode — upsert
        conn.execute("""
            INSERT INTO descriptions (path, description, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
                description = excluded.description,
                updated_at = datetime('now')
        """, (path, description))
        conn.commit()
        conn.close()
        return {
            "action": "set",
            "path": path,
            "description": description,
        }

    # GET mode
    cur = conn.execute(
        "SELECT description, created_at, updated_at FROM descriptions WHERE path = ?",
        (path,)
    )
    row = cur.fetchone()
    conn.close()

    if row:
        return {
            "action": "get",
            "path": path,
            "description": row[0],
            "created_at": row[1],
            "updated_at": row[2],
        }
    return {
        "action": "get",
        "path": path,
        "description": None,
        "message": "No description set for this path",
    }


def search_descriptions(query: str, limit: int = 50) -> dict:
    """
    Search across all file/folder descriptions using LIKE matching.
    Supports multi-word queries (split by space, AND logic) and works for both Chinese and English.

    Returns {query, total_hits, results: [{path, description, updated_at}]}
    """
    conn = sqlite3.connect(str(META_DB))

    # Split query into individual keywords (AND logic)
    keywords = [kw.strip() for kw in query.split() if kw.strip()]
    if not keywords:
        conn.close()
        return {"query": query, "total_hits": 0, "results": []}

    # Build WHERE clause: description LIKE '%kw1%' AND description LIKE '%kw2%' ...
    conditions = " AND ".join(["d.description LIKE ?" for _ in keywords])
    params = [f"%{kw}%" for kw in keywords]

    # Count total
    cur = conn.execute(
        f"SELECT COUNT(*) FROM descriptions d WHERE {conditions}",
        params
    )
    total = cur.fetchone()[0]

    # Fetch results
    cur = conn.execute(
        f"SELECT d.path, d.description, d.updated_at FROM descriptions d WHERE {conditions} ORDER BY d.updated_at DESC LIMIT ?",
        params + [limit]
    )

    results = [
        {
            "path": r[0],
            "description": r[1],
            "updated_at": r[2],
        }
        for r in cur.fetchall()
    ]

    conn.close()
    return {
        "query": query,
        "total_hits": total,
        "results": results,
    }


def list_descriptions(directory: str = None, category: str = None) -> dict:
    """
    List all file/folder descriptions, optionally filtered by directory or category.

    Args:
        directory: Filter to paths under this directory (e.g., ~/FileVault/生活)
        category: Filter by category name (e.g., '生活') — shorthand for ~/FileVault/<category>/

    Returns {total, items: [{path, description, updated_at}]}
    """
    conn = sqlite3.connect(str(META_DB))

    if category:
        directory = str(VAULT_ROOT / category)

    if directory:
        cur = conn.execute("""
            SELECT path, description, updated_at
            FROM descriptions
            WHERE path LIKE ?
            ORDER BY updated_at DESC
        """, (directory + "%",))
    else:
        cur = conn.execute("""
            SELECT path, description, updated_at
            FROM descriptions
            ORDER BY updated_at DESC
        """)

    rows = cur.fetchall()
    conn.close()

    return {
        "total": len(rows),
        "filter": {"directory": directory, "category": category},
        "items": [
            {"path": r[0], "description": r[1], "updated_at": r[2]}
            for r in rows
        ],
    }


def delete_description(path: str) -> dict:
    """Remove a description entry."""
    conn = sqlite3.connect(str(META_DB))
    cur = conn.execute("DELETE FROM descriptions WHERE path = ?", (path,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return {"deleted": deleted > 0, "path": path, "rowcount": deleted}
