---
name: file-manager
description: Use when the user uploads a file and wants it ingested with OCR/text extraction, classified by domain into folder categories (学习, 备忘, 提醒, 生活, 任务, 财务, 职务, etc.), organized into ~/FileVault/, or archived. Classification is backed by a dynamic SQLite learning engine that improves from user corrections. Also use for category management, keyword tuning, learning stats, category suggestions, bulk file cleanup, and metadata queries.
version: 2.3.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [file-management, ocr, classification, archive, organization, learning-engine, multi-language]
    related_skills: [ocr-and-documents, hierarchical-memory, file-dashboard]
---

# File Manager — 智能文件管家 v2

## Overview

Automated file pipeline: upload → extract → classify → organize → archive. Classification is powered by a **dynamic learning engine** stored in `~/FileVault/.categories.db` (SQLite). The engine starts with 7 seed categories (bilingual zh/en) and improves from every user correction — penalizing keywords that led to wrong classifications and boosting those that lead to correct ones. 

See `references/architecture.md` for the full system design.

## When to Use

- **User uploads a file** → run full ingest → classify → organize pipeline
- **User asks "where is file X?"** → search by keyword in metadata index
- **User wants cleanup** → archive (180-day threshold)
- **User reclassifies a file** → automatically triggers learning (record_correction)
- **User asks for category suggestions** → suggest_categories() from inbox clustering
- **User adds/edits categories** → add_category, update_category, add_keywords
- **User asks "how's the classifier doing?"** → get_learning_stats
- **User asks "what's in my vault?"** → get_stats by category
- **User asks "what is file X about?"** → describe(path) to read the human-written description
- **User wants to add a file summary** → describe(path, "text") to write a description
- **User says "find files about X"** → search_descriptions("X") to search the description database
- **User wants to browse descriptions** → list_descriptions(category="生活") to see all in a category

Don't use for: one-off file reads, manual file ops not involving the vault.

---

## Core Workflow

### 1. Ingest (extract content)

Run `ingest_file` via MCP. Supported types: txt, md, csv, json, yaml, xml, html, log, py, js, ts, sh, css, toml, ini, cfg, ipynb (Jupyter notebooks — parses JSON to extract code + markdown cells), pdf (pymupdf/marker), docx (python-docx), pptx (python-pptx). Chinese keywords via jieba.

### 2. Classify (dynamic, multi-language)

Classification uses **weighted keyword matching** from `~/.hermes/mcp-servers/file-manager/classify.py`. Key features:

- **Dynamic categories:** Stored in SQLite, not hardcoded. Add/remove freely.
- **Weighted keywords:** Each keyword has a learned weight (0.1–3.0). Seed keywords start at 1.0; user-added at 1.5; learned weights adjust from corrections.
- **Multi-language:** Keywords stored per locale (zh/en). `classify_file` with `locale="auto"` detects document language and matches relevant keywords.
- **Bilingual seed categories:** 7 defaults with zh + en keywords each.

To classify:
```
classify_file(filepath, extracted_text, extracted_keywords, locale="auto")
→ {category, category_id, confidence, score_raw, locale_detected, suggestions}
```

If confidence < 0.3, the file stays in `_inbox/` with `suggestions` for user review.

### 3. Organize (with learning)

`organize_file(filepath, category, learn=True)` moves the file. If `learn=True` (default) and the file previously had a different category, it **automatically calls `record_correction()`** to adjust keyword weights.

### 4. Archive

`archive_cleanup(days=180)` moves stale files to `_archive/`.

---

## Category Management (new in v2)

See `references/keyword-expansion.md` for a log of keywords added per category with context.

```
get_categories_list(locale="zh")    → list all categories with metadata
add_category(name, keywords, ext)   → create new category (persisted to DB)
update_category(cat_id, **fields)   → rename, activate/deactivate
delete_category(cat_id)             → soft-delete (is_active=0)
add_keywords(cat_id, keywords)      → add keywords with weight 1.5
get_keywords(cat_id)                → list keywords with current weights
remove_keyword(cat_id, keyword)     → remove a keyword
```

## Learning Engine (new in v2)

```
record_correction(filepath, from, to, keywords)
  → Penalizes ~from~ keywords (weight × 0.7)
  → Boosts ~to~ keywords (weight × 1.3 + 0.2, capped at 3.0)
  → Logs to correction_log table

get_learning_stats()
  → {total_corrections, learned_keywords, top_corrected_categories}

suggest_categories(min_files=5)
  → Clusters inbox keywords via co-occurrence
  → Returns top-3 proposed new categories with sample keywords
```

---

## MCP Tools (34 total — v2.3)

### Core pipeline
| Tool | Description |
|------|-------------|
| `ensure_vault` | Init vault + metadata DB |
| `ingest_file` | Extract text + keywords |
| `batch_ingest` | Ingest all files in a directory |
| `classify_file` | Classify with locale support |
| `organize_file` | Move + auto-learn correction |
| `archive_cleanup` | Archive stale files |

### Category management
| `get_categories` | Legacy (dict format) |
| `get_categories_list` | Full metadata list |
| `add_category` | Create category |
| `update_category` | Modify category |
| `delete_category` | Soft-delete |
| `add_keywords` | Add keywords |
| `get_keywords` | List with weights |
| `remove_keyword` | Remove keyword |

### Learning engine
| `record_correction` | Feed correction to learner |
| `get_learning_stats` | Learning statistics |
| `suggest_categories` | Propose new categories |

### Description database (v2.3)
| `describe` | Set/get human-readable description for any file or folder |
| `search_descriptions` | Keyword search across descriptions (AND logic, zh+en) |
| `list_descriptions` | List all descriptions, filter by directory or category |
| `delete_description` | Remove a description entry |

### Search & stats
| `search_files` | Keyword search |
| `get_stats` | Vault statistics |
| `get_recommendations` | Daily recommendations |
| `update_importance` | Set importance score |
| `toggle_star` | Toggle starred |
| `record_access` | Log file view |

### File grouping (batch collation)
| `detect_file_groups` | Scan dir for files that belong together (sequential, version, same-stem) |
| `group_files` | Move file list into a subfolder |
| `ungroup_files` | Reverse grouping, delete empty folder |

### Utility
| `detect_language` | Detect zh/en/mixed |
| `add_custom_category` | Legacy alias for add_category |

---

## Real Classification Walkthrough

**Text file example** (e.g., "2024年度发票汇总.pdf"):
```
ingest → text: "发票号码 INV-2024-0881 金额 ¥128,800"
keywords → [2024, 发票, INV, 报销, 金额]
classify → 财务 (conf 0.56, matches: 发票 + 报销)
organize → ~/FileVault/财务/
```

---

## Image File Handling (Vision Pipeline)

**Source directory:** Telegram images arrive at `~/.hermes/image_cache/` with hashed filenames (`img_<hex>.ext`). After classification, transfer them to `~/FileVault/<category>/` with descriptive names.

Image files (.jpg/.png/.webp/.bmp/.gif) have no extractable text — classification needs a **vision description** first. There are two sources:

### Source 1: Telegram Image Descriptions (auto, zero-latency)
When a user sends an image via Telegram, the platform auto-generates a detailed description. It appears in the message as:
```
[The user sent an image~ Here's what I can see:
This is a three-dimensional CAD model of WALL-E...
...SolidWorks...coordinate axis...]
```
**Always check for this first** — it's already available, no extra API call needed. Use it directly as `extracted_text` for classification.

### Source 2: Hermes vision_analyze (on-demand, one API call)
When the Telegram description is insufficient or the file comes from another platform:
```python
vision_analyze(image_url=filepath, question="详细描述这个图片的领域、内容、用途")
# Returns {success: true, analysis: "This is a BCD decoder circuit..."}
```
The `analysis` field becomes the `extracted_text` for classification.

**How vision_analyze works:**
- If your model has **native vision** (GPT-4V, Claude 3, Gemini) → image pixels are injected directly into the context
- If not (e.g., deepseek) → Hermes falls back to an **auxiliary vision model** that returns a text description
- Result is the same: you get a detailed text description of the image

### Decision Tree for Images

```
Image uploaded
│
├─ Telegram description available?
│  └─ YES → Use it as extracted_text ✅ (no API cost)
│
└─ NO → Call vision_analyze(image, question)
   └─ Use analysis as extracted_text ✅
        ↓
   Run jieba on description → keywords
   Run classify_file(text=description, keywords=...) → category
   If confidence < 0.3 → offer to add keywords or create category
        ↓
   organize_file → move to target folder
```

### Real Example

**WALL-E CAD model image:**
```
1. Telegram description: "3D CAD model of WALL-E, SolidWorks, mechanical engineering..."
2. classify_file(text=description) → 学习 (conf 0.02, only "design" matched)
3. Low confidence → user asked to add CAD keywords to "学习"
4. add_keywords("study", ["CAD","SolidWorks","建模","3D打印","机械","engineering"])
5. Re-classify → 学习 (conf 0.13, matches: CAD, SolidWorks, mechanical, engineering) ✅
6. organize_file → ~/FileVault/学习/
```

**BCD circuit simulator image:**
```
1. Called vision_analyze → "Deeds Digital Circuit Simulator, BCD decoder, digital electronics..."
2. classify_file(text=description) → 学习 (conf 0.25, matches: simulation, circuit, digital, logic) ✅
3. organize_file → ~/FileVault/学习/
```

### Multi-Modal Sources Summary

| Source | When | Cost | Quality |
|--------|------|------|---------|
| Telegram auto-desc | User sends image via Telegram | Zero | Good (general domain + objects) |
| vision_analyze | Any image, any platform | 1 API call | Detailed (custom question) |
| OCR (pymupdf) | Image contains text | Local CPU | Extracts embedded text only |

**Priority:** Telegram desc → vision_analyze → OCR (only if text-heavy image)

---

## File Grouping (new in v2.1)

When user sends multiple files sharing a title pattern:
```
detect_file_groups(directory)
  → Finds clusters: sequential numbering, version variants, same-stem formats
  
Ask user: "Found 'Photo' group (3 files). Put in subfolder?"
Yes → group_files(files, "Photo")
No  → process individually

Reverse: ungroup_files(folder_path) moves files back, deletes empty folder
```

---

## Description Database (new in v2.3)

Every file AND folder in FileVault can have a human-readable description stored in `.meta.db`. This acts as a lookup table — you can quickly answer "what is this file about?" without re-reading its content.

### Writing descriptions

```
describe("/path/to/file.jpg", "手机股票APP截图：8只持仓，当日盈利+33")
→ {action: "set", path: "...", description: "..."}

# Also works for folders:
describe("/path/to/FileVault/生活", "日常生活类：旅行、美食、出行")
```

**When to use:**
- After `organize_file` succeeds → write a 1-sentence Chinese description
- User asks "这个文件是什么" → `describe(path)` to read it back
- New folder created → describe it

### Reading descriptions

```
describe("/path/to/file.jpg")
→ {action: "get", description: "手机股票APP截图...", updated_at: "..."}
```

### Searching descriptions

```
search_descriptions("股票")
→ {total_hits: 2, results: [{path, description}, ...]}

# Multi-keyword AND search (split by space):
search_descriptions("澳门 保密")
→ Files whose descriptions contain BOTH "澳门" AND "保密"
```

### Listing by category

```
list_descriptions(category="生活")
→ All descriptions under ~/FileVault/生活/

list_descriptions()
→ All descriptions in the entire vault
```

### Deleting

```
delete_description(path) → removes the entry
```

### Auto-describe convention

When classifying and organizing a new file, the agent should **automatically write a description** using the classification context:

```python
# After organize_file succeeds:
describe(dst_path, f"{context_summary}。{category_note}")
```

Example: `"携程APP截图：大连宜客宜家酒店预订页面，海景大床房。旅游出行相关"`

---

## Directory Structure

```
~/FileVault/
├── _inbox/           ← New files land here
├── 学习/             ← Seed + user categories
├── 备忘/
├── 提醒/
├── 生活/
├── 任务/
├── 财务/
├── 职务/
├── _archive/         ← Auto-archived
├── .meta.db          ← SQLite: file metadata + access log + descriptions
└── .categories.db    ← SQLite: categories + keyword_weights + correction_log
```

---

## Common Pitfalls

1. **Forgetting deps.** `pymupdf`, `python-docx`, `python-pptx`, `jieba`, `mcp` — `pip install -r requirements.txt`.

2. **Large PDF → marker-pdf.** Use pymupdf first; fall back to marker only when extracted text < 50 chars.

3. **Classify before ingest.** Always run ingest first.

4. **Keyword case-sensitivity.** `add_category` and `add_keywords` auto-lowercase keywords. If you manually insert in DB, ensure lowercase.

5. **Learning only fires on reclassification.** `organize_file` auto-learns when a file moves between categories. Manual `record_correction` also available.

6. **Categories DB vs Meta DB.** `.meta.db` tracks files; `.categories.db` tracks categories + keyword weights. Don't mix them up.

7. **User-added keywords start at weight 1.5.** Seed keywords are 1.0. This gives user additions an initial edge, then the learner adjusts from there.

8. **Test contamination.** Keywords added during testing persist in `.categories.db`. After adding test categories/keywords, delete `~/FileVault/.categories.db` and restart to get a clean slate. Otherwise test keywords (e.g., "git" for "编程") cause false positives on real files.

9. **Image classification needs vision first.** Images have no extractable text. See the **"Image File Handling"** section above for the full pipeline: Telegram auto-descriptions → vision_analyze → OCR fallback. Always get a description before classify_file on images.

9b. **Telegram images live in ~/.hermes/image_cache/.** When a user sends an image via Telegram, the raw file is stored at `~/.hermes/image_cache/img_<hex>.ext` (NOT in FileVault). After classification, move it to `~/FileVault/<category>/` with a descriptive filename (e.g., `飞机窗景_云海.jpg`). Always check image_cache first when the user asks for images they previously sent — don't search FileVault only.

11. **Multiple images in one message → batch acknowledge.** When the user sends several images at once, process them all but acknowledge the batch: "收到3张图，分别是X、Y、Z，已分类"。This prevents the user from thinking images were lost. Process individually (each gets its own classify → organize), but present results as one batch summary. If one image triggers a low-confidence classification, flag it inline in the batch summary rather than stopping the whole pipeline. The classifier uses `kw in text` (substring match), not word-boundary matching. This means "plan" in "airplane" triggers the 任务 category. When adding English keywords, prefer longer unambiguous forms (e.g., "airline" over "air") and be aware that short seed keywords like "plan", "work", "note" can cause false positives in compound words. When a false match occurs, add stronger competing keywords rather than removing seed keywords.

12. **Auto-describe after organize.** Always call `describe(dst_path)` with a 1-sentence Chinese summary after successfully organizing a file. This builds up the lookup table automatically — no extra user action needed. Description should capture what the file IS (content summary) and optionally why it's in this category. For images, use the vision description as the base; for documents, summarize the extracted text.

13. **Search descriptions uses AND logic.** `search_descriptions("澳门 保密")` returns only files whose descriptions contain BOTH terms. Use single keywords for broader results. The search uses SQLite `LIKE`, so `%` wildcards work if needed.

---

## Verification Checklist

- [ ] `~/FileVault/` + all category subdirs exist
- [ ] `.meta.db` + `.categories.db` initialized
- [ ] `classify_file` with `locale="auto"` detects language correctly
- [ ] `organize_file` auto-learns corrections when reclassifying
- [ ] `get_learning_stats` shows correction count
- [ ] `suggest_categories` returns meaningful proposals when inbox has files
- [ ] `get_categories_list` returns English display names
- [ ] After user sends images: files transferred from `~/.hermes/image_cache/` to `~/FileVault/` with descriptive names
- [ ] Multi-image batches: acknowledged as batch, results summarized together
- [ ] `describe` tool: can set and read descriptions for files and folders
- [ ] `search_descriptions`: returns correct results for zh/en keywords
- [ ] `list_descriptions(category="生活")`: returns only subset
- [ ] All organized files have auto-written descriptions
