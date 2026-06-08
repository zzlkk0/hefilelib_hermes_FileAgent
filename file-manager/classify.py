"""
File Manager MCP Server — classify module v2
Dynamic categories with learning engine and multi-language support.

Architecture:
  - Categories stored in SQLite (learned from user behavior)
  - 7 seed categories as initial defaults
  - Weighted keywords: matched via DB-stored weights, adjusted by corrections
  - Learning: record_correction() updates weights based on user reclassifications
  - Suggestions: suggest_categories() clusters uncategorized files to propose new cats
  - Multi-language: category display names + keywords localised (zh/en/auto-detect)
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional
from collections import Counter, defaultdict

VAULT_ROOT = Path.home() / "FileVault"
META_DB = VAULT_ROOT / ".meta.db"
CAT_DB = VAULT_ROOT / ".categories.db"  # dedicated DB for category learning

# ─── Seed categories (Chinese-first, with English equivalents) ─────

SEED_CATEGORIES = [
    {
        "id": "study",
        "name_zh": "学习",
        "name_en": "Study",
        "keywords_zh": ["学习", "教程", "课程", "笔记", "论文", "教材", "考试", "复习", "习题", "答案",
                         "作业", "练习", "知识点", "课件", "讲义", "学术", "研究", "博士", "硕士", "本科"],
        "keywords_en": ["tutorial", "course", "lecture", "study", "learn", "textbook", "paper", "arxiv",
                          "homework", "exam", "thesis", "dissertation", "syllabus"],
        "extensions": [".pdf", ".epub", ".mobi"],
    },
    {
        "id": "memo",
        "name_zh": "备忘",
        "name_en": "Memo",
        "keywords_zh": ["备忘", "便签", "灵感", "摘录", "引用", "代码片段", "记住", "参考", "速查"],
        "keywords_en": ["snippet", "memo", "note", "cheatsheet", "checklist", "quickref", "reference"],
        "extensions": [],
    },
    {
        "id": "reminder",
        "name_zh": "提醒",
        "name_en": "Reminder",
        "keywords_zh": ["截止", "提醒", "预约", "会议", "日程", "行程", "到期", "过期", "邀请函"],
        "keywords_en": ["deadline", "due", "appointment", "calendar", "meeting", "schedule", "reminder"],
        "extensions": [],
    },
    {
        "id": "life",
        "name_zh": "生活",
        "name_en": "Life",
        "keywords_zh": ["菜谱", "食谱", "健康", "旅行", "购物", "穿搭", "家居", "运动", "锻炼", "健身",
                         "宠物", "美食", "餐厅", "旅游", "摄影", "手工"],
        "keywords_en": ["recipe", "health", "travel", "shopping", "fashion", "home", "gym", "pet", "food",
                          "restaurant", "photo", "diy", "cooking"],
        "extensions": [],
    },
    {
        "id": "task",
        "name_zh": "任务",
        "name_en": "Task",
        "keywords_zh": ["项目", "需求", "进度", "报告", "周报", "月报", "计划", "任务", "开发", "部署",
                         "上线", "测试", "发布", "迭代"],
        "keywords_en": ["project", "requirement", "progress", "report", "plan", "task", "todo",
                          "prd", "spec", "roadmap", "milestone", "sprint", "deploy", "release"],
        "extensions": [],
    },
    {
        "id": "finance",
        "name_zh": "财务",
        "name_en": "Finance",
        "keywords_zh": ["发票", "账单", "收据", "合同", "报税", "工资", "银行", "流水", "报销", "预算",
                         "贷款", "保险", "理财", "投资", "税务"],
        "keywords_en": ["invoice", "bill", "receipt", "contract", "tax", "salary", "bank", "transaction",
                          "expense", "budget", "loan", "insurance", "investment"],
        "extensions": [".xlsx", ".xls", ".csv"],
    },
    {
        "id": "work",
        "name_zh": "职务",
        "name_en": "Work",
        "keywords_zh": ["述职", "绩效", "考核", "晋升", "简历", "面试", "入职", "工作", "汇报", "纪要",
                         "周报", "日报", "总结", "规划"],
        "keywords_en": ["performance", "review", "promotion", "resume", "cv", "interview", "onboarding",
                          "work", "presentation", "minutes", "report"],
        "extensions": [],
    },
]

# ─── Database init ───────────────────────────────────────────────────

def _init_cat_db():
    """Initialize the categories learning database."""
    CAT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CAT_DB))

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id TEXT PRIMARY KEY,
            name_zh TEXT NOT NULL,
            name_en TEXT NOT NULL,
            extensions TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            sample_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS keyword_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id TEXT NOT NULL,
            keyword TEXT NOT NULL,
            locale TEXT DEFAULT 'zh',
            weight REAL DEFAULT 1.0,
            source TEXT DEFAULT 'default',
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS correction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT,
            from_category TEXT,
            to_category TEXT,
            keywords TEXT,
            corrected_at TEXT DEFAULT (datetime('now'))
        );

        -- Index for fast keyword lookup
        CREATE INDEX IF NOT EXISTS idx_kw_cat ON keyword_weights(category_id, keyword);
        CREATE INDEX IF NOT EXISTS idx_kw_locale ON keyword_weights(locale);
        CREATE INDEX IF NOT EXISTS idx_corr_cat ON correction_log(to_category);
    """)
    conn.commit()

    # Seed if empty
    cur = conn.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        _seed_categories(conn)

    conn.close()


def _seed_categories(conn):
    """Insert the 7 default categories with keywords."""
    for cat in SEED_CATEGORIES:
        conn.execute(
            "INSERT INTO categories (id, name_zh, name_en, extensions) VALUES (?, ?, ?, ?)",
            (cat["id"], cat["name_zh"], cat["name_en"], json.dumps(cat["extensions"]))
        )
        for kw in cat["keywords_zh"]:
            conn.execute(
                "INSERT INTO keyword_weights (category_id, keyword, locale, weight, source) VALUES (?, ?, 'zh', 1.0, 'seed')",
                (cat["id"], kw)
            )
        for kw in cat["keywords_en"]:
            conn.execute(
                "INSERT INTO keyword_weights (category_id, keyword, locale, weight, source) VALUES (?, ?, 'en', 1.0, 'seed')",
                (cat["id"], kw)
            )
    conn.commit()


# ─── User language detection ─────────────────────────────────────────

# Simple heuristic: count CJK characters vs ASCII
def detect_language(text: str) -> str:
    """Detect primary language of text. Returns 'zh', 'en', or 'mixed'."""
    if not text:
        return "zh"  # default to Chinese per user preference
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff')
    ascii_chars = sum(1 for c in text if c.isascii() and c.isalpha())
    if cjk > ascii_chars:
        return "zh"
    elif ascii_chars > cjk * 2:
        return "en"
    return "mixed"


# ─── Category CRUD ───────────────────────────────────────────────────

def get_categories(locale: str = "zh") -> dict:
    """
    Return all active categories with localised display names.
    locale: 'zh' (显示中文名), 'en' (show English names), 'auto' (detect from user)
    """
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name_zh, name_en, extensions, sample_count, is_active FROM categories WHERE is_active = 1 ORDER BY id"
    ).fetchall()
    conn.close()

    result = {}
    name_field = "name_zh" if locale in ("zh", "auto") else "name_en"
    for r in rows:
        result[r[name_field]] = {
            "id": r["id"],
            "display_name": r[name_field],
            "name_zh": r["name_zh"],
            "name_en": r["name_en"],
            "extensions": json.loads(r["extensions"] or "[]"),
            "sample_count": r["sample_count"],
        }
    return result


def get_categories_list(locale: str = "zh") -> list[dict]:
    """Return categories as a list with full metadata."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM categories WHERE is_active = 1 ORDER BY sample_count DESC, id ASC"
    ).fetchall()

    result = []
    name_field = "name_zh" if locale in ("zh", "auto") else "name_en"
    for r in rows:
        result.append({
            "id": r["id"],
            "display_name": r[name_field],
            "name_zh": r["name_zh"],
            "name_en": r["name_en"],
            "extensions": json.loads(r["extensions"] or "[]"),
            "sample_count": r["sample_count"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "keyword_count": _count_keywords(conn, r["id"]),
        })
    conn.close()
    return result


def _count_keywords(conn, category_id: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM keyword_weights WHERE category_id = ?", (category_id,)
    )
    return cur.fetchone()[0]


def add_category(name: str, keywords: list[str] = None, extensions: list[str] = None,
                 locale: str = "zh") -> dict:
    """
    Add a new category. name is the display name; generates id from name.
    Keywords are assigned to the detected locale.
    """
    _init_cat_db()
    cat_id = re.sub(r'[^a-z0-9_]', '_', name.lower().replace(" ", "_"))[:32]
    if not cat_id or cat_id.startswith("_"):
        cat_id = f"cat_{abs(hash(name)) % 10000}"

    conn = sqlite3.connect(str(CAT_DB))

    # Check if exists
    cur = conn.execute("SELECT id FROM categories WHERE id = ?", (cat_id,))
    if cur.fetchone():
        conn.close()
        return {"error": f"Category '{cat_id}' already exists", "duplicate": True}

    name_en = name  # fallback
    name_zh = name
    if locale == "zh":
        name_en = _translate_to_en(name)
    else:
        name_zh = name  # keep as-is for now

    conn.execute(
        "INSERT INTO categories (id, name_zh, name_en, extensions) VALUES (?, ?, ?, ?)",
        (cat_id, name_zh, name_en, json.dumps(extensions or []))
    )

    if keywords:
        for kw in keywords:
            conn.execute(
                "INSERT INTO keyword_weights (category_id, keyword, locale, weight, source) VALUES (?, ?, ?, 1.5, 'user')",
                (cat_id, kw.strip().lower(), locale)
            )
    conn.commit()
    conn.close()

    return {
        "status": "added",
        "id": cat_id,
        "display_name": name,
        "keywords_count": len(keywords or []),
    }


def _translate_to_en(name_zh: str) -> str:
    """Simple fallback: return pinyin-like or original."""
    basic_map = {
        "学习": "Study", "备忘": "Memo", "提醒": "Reminder", "生活": "Life",
        "任务": "Task", "财务": "Finance", "职务": "Work",
    }
    return basic_map.get(name_zh, name_zh)


def update_category(cat_id: str, **kwargs) -> dict:
    """Update category metadata (name_zh, name_en, is_active, extensions)."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))

    allowed = {"name_zh", "name_en", "is_active", "extensions"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        conn.close()
        return {"error": "No valid fields to update"}

    if "extensions" in updates:
        updates["extensions"] = json.dumps(updates["extensions"])

    updates["updated_at"] = "datetime('now')"

    sets = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [cat_id]
    conn.execute(f"UPDATE categories SET {sets} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return {"status": "updated", "id": cat_id}


def delete_category(cat_id: str) -> dict:
    """Soft-delete a category (set is_active=0)."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))
    conn.execute("UPDATE categories SET is_active = 0, updated_at = datetime('now') WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()
    return {"status": "deactivated", "id": cat_id}


def add_keywords(cat_id: str, keywords: list[str], locale: str = "zh") -> dict:
    """Add keywords to an existing category."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))
    added = 0
    for kw in keywords:
        kw_clean = kw.strip().lower()
        if not kw_clean:
            continue
        # Check duplicate
        cur = conn.execute(
            "SELECT id FROM keyword_weights WHERE category_id = ? AND keyword = ? AND locale = ?",
            (cat_id, kw_clean, locale)
        )
        if not cur.fetchone():
            conn.execute(
                    "INSERT INTO keyword_weights (category_id, keyword, locale, weight, source) VALUES (?, ?, ?, 1.5, 'user')",
                    (cat_id, kw_clean.lower() if kw_clean else "", locale)
                )
            added += 1
    conn.commit()
    conn.close()
    return {"status": "added", "category_id": cat_id, "keywords_added": added}


# ─── Classification engine ───────────────────────────────────────────

def classify_file(filepath: str, extracted_text: str = "",
                  extracted_keywords: list[str] = None,
                  locale: str = "zh") -> dict:
    """
    Classify a file using weighted keyword matching from the learning DB.
    Returns {category, confidence, reason, suggestions, locale_detected}.
    """
    _init_cat_db()

    path = Path(filepath)
    filename = path.name.lower()
    ext = path.suffix.lower()

    if extracted_keywords is None:
        extracted_keywords = []

    # Detect document language for better keyword matching
    doc_locale = detect_language(extracted_text or filename)
    if locale == "auto":
        locale = doc_locale

    # Build search text
    text_lower = extracted_text.lower()
    all_text = filename + " " + text_lower + " " + " ".join(extracted_keywords).lower()

    # Load all keywords with weights from DB
    conn = sqlite3.connect(str(CAT_DB))
    conn.row_factory = sqlite3.Row

    # Get all active categories
    cats = conn.execute("SELECT id, name_zh, name_en, extensions FROM categories WHERE is_active = 1").fetchall()

    # Load keyword weights (match both zh and en locales for broad coverage)
    kw_rows = conn.execute(
        "SELECT category_id, keyword, locale, weight FROM keyword_weights"
    ).fetchall()
    conn.close()

    # Scoring per category
    scores = {}
    reasons = {}

    for cat in cats:
        score = 0.0
        matches = []
        cat_extensions = set(json.loads(cat["extensions"] or "[]"))

        # Extension match
        if ext in cat_extensions:
            score += 2.0
            matches.append(f"extension {ext}")

        # Weighted keyword matching
        for kw_row in kw_rows:
            if kw_row["category_id"] != cat["id"]:
                continue

            kw = kw_row["keyword"]
            if kw in all_text:
                weight = kw_row["weight"]
                # Bonus for locale match
                locale_bonus = 1.2 if kw_row["locale"] == doc_locale else 1.0

                if kw in filename:
                    score += 3.0 * weight * locale_bonus
                    matches.append(f"filename '{kw}' (w={weight:.1f})")
                elif kw in " ".join(extracted_keywords).lower():
                    score += 2.0 * weight * locale_bonus
                    matches.append(f"keyword '{kw}' (w={weight:.1f})")
                else:
                    score += 1.0 * weight * locale_bonus

        scores[cat["id"]] = score
        reasons[cat["id"]] = matches

    if not scores:
        return {
            "category": "_inbox",
            "confidence": 0.0,
            "reason": "No categories defined",
            "suggestions": [],
            "locale_detected": doc_locale,
        }

    best_id = max(scores, key=scores.get)
    best_score = scores[best_id]

    # Get display name
    best_cat = next((c for c in cats if c["id"] == best_id), None)
    best_display = best_cat["name_zh"] if best_cat and locale in ("zh", "auto") else (
        best_cat["name_en"] if best_cat else best_id
    )

    # Confidence: normalize against a reasonable ceiling
    # Ceiling = (total_keywords_for_this_cat * 3 * avg_weight)
    cat_kw_count = sum(1 for kw in kw_rows if kw["category_id"] == best_id)
    ceiling = max(cat_kw_count * 2.5, 5)  # at least ceiling of 5
    confidence = min(best_score / ceiling, 1.0) if best_score > 0 else 0.0

    # Suggestions for low confidence
    suggestions = []
    if confidence < 0.3:
        sorted_cats = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        for cat_id, s in sorted_cats[:3]:
            if s > 0 and cat_id != best_id:
                c = next((c for c in cats if c["id"] == cat_id), None)
                if c:
                    suggestions.append(c["name_zh"] if locale in ("zh", "auto") else c["name_en"])

    if best_score == 0:
        return {
            "category": "_inbox",
            "confidence": 0.0,
            "reason": "No keyword matches — needs manual classification",
            "suggestions": suggestions,
            "locale_detected": doc_locale,
        }

    return {
        "category": best_display,
        "category_id": best_id,
        "confidence": round(confidence, 2),
        "reason": "; ".join(reasons[best_id][:5]),
        "suggestions": suggestions,
        "locale_detected": doc_locale,
        "score_raw": round(best_score, 1),
    }


# ─── Learning engine ─────────────────────────────────────────────────

def record_correction(filepath: str, from_category: str, to_category: str,
                      keywords: list[str] = None) -> dict:
    """
    Learn from a user correction: when user moves a file from category A to B.
    1. Reduce weights of keywords that pointed to A
    2. Increase weights of keywords that match B
    3. Log the correction for analysis
    """
    _init_cat_db()

    if keywords is None:
        keywords = []

    conn = sqlite3.connect(str(CAT_DB))

    # Find category IDs
    cur = conn.execute(
        "SELECT id FROM categories WHERE name_zh = ? OR name_en = ? OR id = ?",
        (from_category, from_category, from_category)
    )
    from_row = cur.fetchone()
    from_id = from_row[0] if from_row else None

    cur = conn.execute(
        "SELECT id FROM categories WHERE name_zh = ? OR name_en = ? OR id = ?",
        (to_category, to_category, to_category)
    )
    to_row = cur.fetchone()
    to_id = to_row[0] if to_row else None

    # Detect locale of keywords
    kw_locale = detect_language(" ".join(keywords)) if keywords else "zh"

    # Penalize keywords that led to wrong classification
    if from_id and keywords:
        for kw in keywords:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            # Reduce weight for wrong-category keywords
            conn.execute(
                """UPDATE keyword_weights SET weight = MAX(weight * 0.7, 0.1), source = 'learned'
                   WHERE category_id = ? AND keyword = ? AND locale = ?""",
                (from_id, kw_clean, kw_locale)
            )
            # If not found, insert with low weight (this keyword exists but isn't for this cat)
            cur = conn.execute(
                "SELECT id FROM keyword_weights WHERE category_id = ? AND keyword = ?",
                (from_id, kw_clean)
            )
            if not cur.fetchone():
                conn.execute(
                    "INSERT INTO keyword_weights (category_id, keyword, locale, weight, source) VALUES (?, ?, ?, 0.3, 'corrected_down')",
                    (from_id, kw_clean, kw_locale)
                )

    # Reward keywords that point to correct classification
    if to_id and keywords:
        for kw in keywords:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            cur = conn.execute(
                "SELECT id, weight FROM keyword_weights WHERE category_id = ? AND keyword = ? AND locale = ?",
                (to_id, kw_clean, kw_locale)
            )
            row = cur.fetchone()
            if row:
                # Boost existing keyword (cap at 3.0)
                new_weight = min(row[1] * 1.3 + 0.2, 3.0)
                conn.execute(
                    "UPDATE keyword_weights SET weight = ?, source = 'learned' WHERE id = ?",
                    (new_weight, row[0])
                )
            else:
                # New keyword — start with weight 1.5
                conn.execute(
                    "INSERT INTO keyword_weights (category_id, keyword, locale, weight, source) VALUES (?, ?, ?, 1.5, 'learned')",
                    (to_id, kw_clean, kw_locale)
                )

    # Update sample count
    if to_id:
        conn.execute(
            "UPDATE categories SET sample_count = sample_count + 1, updated_at = datetime('now') WHERE id = ?",
            (to_id,)
        )

    # Log the correction
    conn.execute(
        "INSERT INTO correction_log (filepath, from_category, to_category, keywords) VALUES (?, ?, ?, ?)",
        (filepath, from_category, to_category, json.dumps(keywords or []))
    )

    conn.commit()
    conn.close()

    return {
        "status": "learned",
        "from": from_category,
        "to": to_category,
        "keywords_processed": len(keywords or []),
    }


def get_learning_stats() -> dict:
    """Return statistics about the learning engine."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))

    cur = conn.execute("SELECT COUNT(*) FROM correction_log")
    total_corrections = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM keyword_weights WHERE source = 'learned'")
    learned_keywords = cur.fetchone()[0]

    cur = conn.execute(
        "SELECT to_category, COUNT(*) as cnt FROM correction_log GROUP BY to_category ORDER BY cnt DESC LIMIT 10"
    )
    top_categories = [{"category": r[0], "corrections": r[1]} for r in cur.fetchall()]

    conn.close()
    return {
        "total_corrections": total_corrections,
        "learned_keywords": learned_keywords,
        "top_corrected_categories": top_categories,
    }


# ─── Category suggestion engine ──────────────────────────────────────

def suggest_categories(min_files: int = 5, locale: str = "zh") -> list[dict]:
    """
    Analyze uncategorized files (_inbox) and suggest new categories based on keyword clustering.
    Returns suggestions with proposed name and sample keywords.
    """
    _init_cat_db()

    conn = sqlite3.connect(str(META_DB))
    cur = conn.execute(
        "SELECT path, original_name, keywords FROM files WHERE category = '_inbox'"
    )
    inbox_files = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    conn.close()

    if len(inbox_files) < min_files:
        return [{"status": "insufficient_data", "message": f"Need at least {min_files} files in _inbox (currently {len(inbox_files)})"}]

    # Collect all keywords from inbox files
    all_keywords = []
    for _, _, kw_str in inbox_files:
        try:
            kws = json.loads(kw_str) if kw_str else []
        except (json.JSONDecodeError, TypeError):
            kws = []
        all_keywords.extend(kws)

    if not all_keywords:
        return [{"status": "no_keywords", "message": "No keywords extracted from inbox files yet. Run ingest first."}]

    # Frequency analysis
    kw_counter = Counter(all_keywords)

    # Filter: only keywords appearing in 3+ files, not common stop words
    stop_words = {"the", "a", "an", "is", "of", "to", "in", "and", "的", "了", "是", "在", "和", "2024", "2025", "2026"}
    frequent = {kw: cnt for kw, cnt in kw_counter.most_common(30)
                if cnt >= 3 and kw.lower() not in stop_words and len(kw) >= 2}

    if not frequent:
        return [{"status": "no_clusters", "message": "No clear keyword clusters found in inbox files."}]

    # Simple clustering: group keywords that co-occur in the same files
    clusters = _cluster_keywords(inbox_files, frequent)

    suggestions = []
    for i, cluster in enumerate(clusters[:3]):  # top 3 suggestions
        if len(cluster["keywords"]) < 3:
            continue
        # Suggest a name from the top keywords
        proposed_name = " · ".join(cluster["keywords"][:3])
        suggestions.append({
            "rank": i + 1,
            "proposed_name": proposed_name,
            "top_keywords": cluster["keywords"][:10],
            "file_count": cluster["file_count"],
            "sample_files": cluster["sample_files"][:3],
        })

    return suggestions if suggestions else [{"status": "no_clusters", "message": "No significant clusters found."}]


def _cluster_keywords(inbox_files, frequent_kw: dict[str, int]) -> list[dict]:
    """Simple co-occurrence clustering of keywords."""
    kw_list = list(frequent_kw.keys())

    # Build co-occurrence matrix
    cooccur = defaultdict(lambda: defaultdict(int))
    file_sets = defaultdict(set)

    for filepath, fname, kw_str in inbox_files:
        try:
            kws = set(json.loads(kw_str) or [])
        except (json.JSONDecodeError, TypeError):
            kws = set()
        file_kws = kws & set(kw_list)
        for kw in file_kws:
            file_sets[kw].add(filepath)
        for kw1 in file_kws:
            for kw2 in file_kws:
                if kw1 < kw2:
                    cooccur[kw1][kw2] += 1

    # Greedy clustering
    visited = set()
    clusters = []

    for kw in sorted(kw_list, key=lambda k: frequent_kw[k], reverse=True):
        if kw in visited:
            continue
        cluster_kws = [kw]
        cluster_files = set(file_sets[kw])
        visited.add(kw)

        for other in kw_list:
            if other in visited:
                continue
            # If co-occurs with any cluster member
            if any(cooccur[min(k, other)][max(k, other)] >= 2 for k in cluster_kws):
                cluster_kws.append(other)
                cluster_files |= file_sets[other]
                visited.add(other)

        if len(cluster_kws) >= 3:
            clusters.append({
                "keywords": cluster_kws,
                "file_count": len(cluster_files),
                "sample_files": [f[1] for f in inbox_files[:3]],
            })

    return sorted(clusters, key=lambda c: c["file_count"], reverse=True)


# ─── Keyword management ──────────────────────────────────────────────

def get_keywords(cat_id: str) -> list[dict]:
    """Get all keywords for a category with weights."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT keyword, locale, weight, source FROM keyword_weights WHERE category_id = ? ORDER BY weight DESC, locale",
        (cat_id,)
    ).fetchall()
    conn.close()
    return [{"keyword": r["keyword"], "locale": r["locale"], "weight": round(r["weight"], 2), "source": r["source"]} for r in rows]


def remove_keyword(cat_id: str, keyword: str) -> dict:
    """Remove a keyword from a category."""
    _init_cat_db()
    conn = sqlite3.connect(str(CAT_DB))
    conn.execute("DELETE FROM keyword_weights WHERE category_id = ? AND keyword = ?", (cat_id, keyword))
    conn.commit()
    conn.close()
    return {"status": "removed", "category_id": cat_id, "keyword": keyword}


# Alias for backward compatibility
def add_custom_category(name: str, keywords: list[str] = None, extensions: list[str] = None) -> dict:
    return add_category(name, keywords, extensions)
