# File Manager System — Full Architecture

> Generated: 2026-06-08 | Session: notes/sessions/20260608-file-manager/

## System Diagram

```
User Upload (Telegram/Web/CLI)
        ↓
┌───────────────────────────────────┐
│     file-manager MCP Server       │
│  ┌──────┐ ┌────────┐ ┌─────────┐ │
│  │ingest│ │classify│ │organize │ │
│  │(OCR) │ │(learn) │ │(archive)│ │
│  └──────┘ └────────┘ └─────────┘ │
└───────────────┬───────────────────┘
                ↓
┌───────────────────────────────────┐
│         ~/FileVault/              │
│  ├── .meta.db (file metadata)     │
│  └── .categories.db (learning)    │
└───────────────┬───────────────────┘
                ↓
┌───────────────────────────────────┐
│    file-dashboard (HTTP API)      │
│    ┌──────────────────────────┐   │
│    │  Web UI (SPA, dark theme) │   │
│    │  · File browser           │   │
│    │  · Media preview          │   │
│    │  · Daily recommendations  │   │
│    │  · Statistics charts      │   │
│    └──────────────────────────┘   │
└───────────────────────────────────┘
```

## Classification Architecture

```
classify_file(path, text, keywords, locale="auto")
        │
        ▼
┌──────────────────────────────────────┐
│  detect_language(text) → zh/en/mixed │
└──────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────┐
│  Load all keyword_weights from       │
│  .categories.db (both zh + en)       │
│                                      │
│  For each active category:           │
│    score += Σ(match_count × weight   │
│              × locale_bonus)         │
│    + extension_match_bonus           │
└──────────────────────────────────────┘
        │
        ▼
    {category, confidence, score_raw, locale_detected}

If confidence < 0.3 → file stays in _inbox with suggestions
```

## Learning Engine Flow

```
User moves file from 任务 → 编程
        │
        ▼
organize_file() calls _maybe_learn_correction()
        │
        ▼
record_correction(filepath, "任务", "编程", keywords)
        │
        ├── For each keyword:
        │     UPDATE keyword_weights
        │     WHERE category_id='task'
        │     SET weight = MAX(weight × 0.7, 0.1)   ← PENALIZE wrong cats
        │
        ├── For each keyword:
        │     UPDATE keyword_weights
        │     WHERE category_id='cat_2359'
        │     SET weight = MIN(weight × 1.3 + 0.2, 3.0)  ← BOOST correct cat
        │
        └── INSERT INTO correction_log (from, to, keywords, timestamp)
```

## Database Schemas

### .meta.db (file metadata)
```sql
files(id, path, original_name, category, file_type, extracted_text,
      keywords, file_size, confidence, importance, starred,
      view_count, created_at, last_accessed, last_modified)
access_log(id, file_path, accessed_at)
```

### .categories.db (learning engine)
```sql
categories(id, name_zh, name_en, extensions, created_at, updated_at,
           sample_count, is_active)
keyword_weights(id, category_id, keyword, locale, weight, source)
correction_log(id, filepath, from_category, to_category, keywords, corrected_at)
```

## File Paths

| Component | Path |
|-----------|------|
| Vault root | `~/FileVault/` |
| Inbox | `~/FileVault/_inbox/` |
| Archive | `~/FileVault/_archive/` |
| File metadata | `~/FileVault/.meta.db` |
| Category data | `~/FileVault/.categories.db` |
| MCP server | `~/.hermes/mcp-servers/file-manager/server.py` |
| Ingest module | `~/.hermes/mcp-servers/file-manager/ingest.py` |
| Classify module | `~/.hermes/mcp-servers/file-manager/classify.py` |
| Organize module | `~/.hermes/mcp-servers/file-manager/organize.py` |
| Dashboard server | `~/.hermes/mcp-servers/file-dashboard/server.py` |
| Dashboard UI | `~/.hermes/mcp-servers/file-dashboard/web/index.html` |
| Session records | `~/.hermes/notes/sessions/20260608-file-manager/` |
