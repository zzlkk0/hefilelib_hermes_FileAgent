#!/usr/bin/env python3
"""
File Manager MCP Server
Provides tools for file ingestion, classification, organization, and archiving.

Run: python server.py
"""

import sys
import os
import json

# Add parent to path for local imports
sys.path.insert(0, os.path.dirname(__file__))

from ingest import extract_text, extract_batch
from classify import (
    classify_file, get_categories, get_categories_list, add_category, add_custom_category,
    update_category, delete_category, add_keywords, get_keywords, remove_keyword,
    record_correction, get_learning_stats, suggest_categories,
)
from redact import redact_text as redact_text_fn
from organize import (
    ensure_vault, organize_file, archive_cleanup,
    search_files, get_stats, get_recommendations,
    update_importance, toggle_star, record_access,
    record_correction_wrapper, get_learning_stats_wrapper, suggest_categories_wrapper,
    detect_file_groups, group_files, ungroup_files,
    describe, search_descriptions, list_descriptions, delete_description,
    ingest_sensitive,
)

# MCP server (uses mcp package)
try:
    from mcp.server import Server, NotificationOptions
    from mcp.server.stdio import stdio_server
    from mcp.server.models import InitializationCapabilities
    from mcp.types import Tool, TextContent
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    print("Warning: 'mcp' package not installed. Run: pip install mcp", file=sys.stderr)


server = Server("file-manager")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="ensure_vault",
            description="Create the FileVault directory structure and initialize metadata database.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="ingest_file",
            description="Extract text content and keywords from a file (txt, pdf, docx, pptx, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="batch_ingest",
            description="Extract text from all files in a directory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory path"}
                },
                "required": ["directory"]
            }
        ),
        Tool(
            name="classify_file",
            description="Classify a file into a category based on its content and filename.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"},
                    "extracted_text": {"type": "string", "description": "Pre-extracted text (if available)"},
                    "extracted_keywords": {"type": "array", "items": {"type": "string"}, "description": "Pre-extracted keywords"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="organize_file",
            description="Move a file to its category folder in the vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"},
                    "category": {"type": "string", "description": "Target category (学习/备忘/提醒/生活/任务/财务/职务)"}
                },
                "required": ["filepath", "category"]
            }
        ),
        Tool(
            name="search_files",
            description="Search files by keyword across names, content, and categories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="get_stats",
            description="Get vault statistics: file counts, sizes, categories, top viewed.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="get_recommendations",
            description="Get daily file recommendations: important, discovery, overdue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max items per section (default 5)"}
                }
            }
        ),
        Tool(
            name="archive_cleanup",
            description="Archive files not accessed in N days (default 180).",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Age threshold in days (default 180)"}
                }
            }
        ),
        Tool(
            name="get_categories",
            description="Get all category definitions and their keyword rules.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="add_custom_category",
            description="Add a custom file category with keywords.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Category name"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Keyword triggers"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "File extensions (e.g., ['.xlsx'])"}
                },
                "required": ["name", "keywords"]
            }
        ),
        Tool(
            name="update_importance",
            description="Set importance score for a file (0.0-1.0).",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"},
                    "importance": {"type": "number", "description": "Importance score 0-1"}
                },
                "required": ["filepath", "importance"]
            }
        ),
        Tool(
            name="toggle_star",
            description="Toggle starred status for a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="record_access",
            description="Record a file access (updates view count and last_accessed).",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"}
                },
                "required": ["filepath"]
            }
        ),
        # ── New: Learning engine & category management ──
        Tool(
            name="get_categories_list",
            description="List all categories with full metadata (id, names, sample_count, keyword_count, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "locale": {"type": "string", "description": "Language: 'zh', 'en', or 'auto' (default zh)"}
                }
            }
        ),
        Tool(
            name="add_category",
            description="Add a new custom category with keywords. The system will learn from user corrections over time.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Category display name"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Keyword triggers for this category"},
                    "extensions": {"type": "array", "items": {"type": "string"}, "description": "File extensions (e.g., ['.xlsx'])"},
                    "locale": {"type": "string", "description": "Language of keywords: 'zh' or 'en' (default zh)"}
                },
                "required": ["name"]
            }
        ),
        Tool(
            name="update_category",
            description="Update a category (rename, activate/deactivate, change extensions).",
            inputSchema={
                "type": "object",
                "properties": {
                    "cat_id": {"type": "string", "description": "Category ID (e.g., 'study', 'finance')"},
                    "name_zh": {"type": "string", "description": "New Chinese name"},
                    "name_en": {"type": "string", "description": "New English name"},
                    "is_active": {"type": "integer", "description": "1=active, 0=deactivated"},
                    "extensions": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["cat_id"]
            }
        ),
        Tool(
            name="delete_category",
            description="Soft-delete a category (deactivates it; files remain).",
            inputSchema={
                "type": "object",
                "properties": {
                    "cat_id": {"type": "string", "description": "Category ID to deactivate"}
                },
                "required": ["cat_id"]
            }
        ),
        Tool(
            name="add_keywords",
            description="Add keywords to an existing category to improve classification.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cat_id": {"type": "string", "description": "Category ID"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords to add"},
                    "locale": {"type": "string", "description": "Language: 'zh' or 'en' (default zh)"}
                },
                "required": ["cat_id", "keywords"]
            }
        ),
        Tool(
            name="get_keywords",
            description="Get all keywords for a category with their learned weights.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cat_id": {"type": "string", "description": "Category ID"}
                },
                "required": ["cat_id"]
            }
        ),
        Tool(
            name="remove_keyword",
            description="Remove a keyword from a category.",
            inputSchema={
                "type": "object",
                "properties": {
                    "cat_id": {"type": "string", "description": "Category ID"},
                    "keyword": {"type": "string", "description": "Keyword to remove"}
                },
                "required": ["cat_id", "keyword"]
            }
        ),
        Tool(
            name="record_correction",
            description="Learn from a user correction: when a file is manually reclassified, the engine adjusts keyword weights.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the file"},
                    "from_category": {"type": "string", "description": "Previous category"},
                    "to_category": {"type": "string", "description": "New category"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "Keywords from the file (optional, auto-loaded if omitted)"}
                },
                "required": ["filepath", "from_category", "to_category"]
            }
        ),
        Tool(
            name="get_learning_stats",
            description="Get learning engine stats: total corrections, learned keywords, top-corrected categories.",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="suggest_categories",
            description="Analyze uncategorized inbox files and suggest new categories based on keyword clustering.",
            inputSchema={
                "type": "object",
                "properties": {
                    "min_files": {"type": "integer", "description": "Min files in inbox to trigger (default 5)"},
                    "locale": {"type": "string", "description": "Language: 'zh' or 'en' (default zh)"}
                }
            }
        ),
        Tool(
            name="detect_language",
            description="Detect the primary language of text content (returns 'zh', 'en', or 'mixed').",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to analyze"}
                },
                "required": ["text"]
            }
        ),
        # ── File Grouping / Batch Collation ──
        Tool(
            name="detect_file_groups",
            description="Scan a directory and detect files that should be grouped together (same title pattern, sequential numbering, version variants, etc.). Returns suggested groups with confidence scores.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory to scan for file groups"},
                    "min_group_size": {"type": "integer", "description": "Minimum files to form a group (default 2)"}
                },
                "required": ["directory"]
            }
        ),
        Tool(
            name="group_files",
            description="Move a list of files into a subfolder together. Use after detect_file_groups to execute a grouping suggestion, or to manually group files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_list": {"type": "array", "items": {"type": "string"}, "description": "List of file paths to group"},
                    "folder_name": {"type": "string", "description": "Name for the new subfolder"},
                    "source_dir": {"type": "string", "description": "Parent directory (auto-detected if omitted)"}
                },
                "required": ["file_list", "folder_name"]
            }
        ),
        Tool(
            name="ungroup_files",
            description="Reverse a grouping: move all files out of a subfolder back to the parent and delete the empty folder.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_path": {"type": "string", "description": "Path to the subfolder to ungroup"}
                },
                "required": ["folder_path"]
            }
        ),
        # ── Description Database (v2.3) ──
        Tool(
            name="describe",
            description="Set or get a human-readable description for any file or folder. Call with a description text to set it, or without to read the current description back.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to a file or folder"},
                    "description": {"type": "string", "description": "Optional: text description to set. Omit to read the current description."}
                },
                "required": ["path"]
            }
        ),
        Tool(
            name="search_descriptions",
            description="Search across all file/folder descriptions. Supports multi-keyword AND queries (split by space). Works for both Chinese and English.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (supports FTS5 syntax: AND, OR, *, quotes)"},
                    "limit": {"type": "integer", "description": "Max results (default 50)"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="list_descriptions",
            description="List all descriptions, optionally filtered by directory or category name (e.g., '生活', '学习').",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Filter to paths under this directory"},
                    "category": {"type": "string", "description": "Shorthand: filter to ~/FileVault/<category>/"}
                }
            }
        ),
        Tool(
            name="delete_description",
            description="Remove a description entry for a path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path whose description to delete"}
                },
                "required": ["path"]
            }
        ),
        # ── Sensitive File Security (v2.4) ──
        Tool(
            name="ingest_sensitive",
            description="Process a sensitive file (ID cards, passports, NDAs). Detects PII locally, generates a redacted description that is safe to store and search. The original file content NEVER enters any database or cloud API. Use this instead of regular ingest_file when the file contains personal data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path to the sensitive file"},
                    "ocr_text": {"type": "string", "description": "Pre-extracted text (from Telegram description, local OCR, or manual input). The raw text will be redacted before storage."}
                },
                "required": ["filepath"]
            }
        ),
        Tool(
            name="redact_text",
            description="Detect and redact personally identifiable information (PII) from text. Runs 100% locally — text is never sent to any cloud API. Returns redacted version and list of findings. Supports Chinese ID cards, passports, phone numbers, emails, addresses, student IDs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to scan for PII and redact"}
                },
                "required": ["text"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    try:
        if name == "ensure_vault":
            result = ensure_vault()

        elif name == "ingest_file":
            result = extract_text(arguments["filepath"])

        elif name == "batch_ingest":
            result = extract_batch(arguments["directory"])

        elif name == "classify_file":
            result = classify_file(
                filepath=arguments["filepath"],
                extracted_text=arguments.get("extracted_text", ""),
                extracted_keywords=arguments.get("extracted_keywords")
            )

        elif name == "organize_file":
            result = organize_file(
                filepath=arguments["filepath"],
                category=arguments["category"]
            )

        elif name == "search_files":
            result = search_files(arguments["query"])

        elif name == "get_stats":
            result = get_stats()

        elif name == "get_recommendations":
            result = get_recommendations(limit=arguments.get("limit", 5))

        elif name == "archive_cleanup":
            result = archive_cleanup(days=arguments.get("days", 180))

        elif name == "get_categories":
            result = get_categories()

        elif name == "add_custom_category":
            result = add_custom_category(
                name=arguments["name"],
                keywords=arguments["keywords"],
                extensions=arguments.get("extensions")
            )

        elif name == "update_importance":
            result = update_importance(
                filepath=arguments["filepath"],
                importance=arguments["importance"]
            )

        elif name == "toggle_star":
            result = toggle_star(arguments["filepath"])

        elif name == "record_access":
            record_access(arguments["filepath"])
            result = {"status": "recorded", "path": arguments["filepath"]}

        # ── New: Learning engine & category management ──
        elif name == "get_categories_list":
            result = get_categories_list(locale=arguments.get("locale", "zh"))

        elif name == "add_category":
            result = add_category(
                name=arguments["name"],
                keywords=arguments.get("keywords"),
                extensions=arguments.get("extensions"),
                locale=arguments.get("locale", "zh")
            )

        elif name == "update_category":
            kwargs = {k: v for k, v in arguments.items() if k != "cat_id"}
            result = update_category(arguments["cat_id"], **kwargs)

        elif name == "delete_category":
            result = delete_category(arguments["cat_id"])

        elif name == "add_keywords":
            result = add_keywords(
                cat_id=arguments["cat_id"],
                keywords=arguments["keywords"],
                locale=arguments.get("locale", "zh")
            )

        elif name == "get_keywords":
            result = get_keywords(arguments["cat_id"])

        elif name == "remove_keyword":
            result = remove_keyword(arguments["cat_id"], arguments["keyword"])

        elif name == "record_correction":
            result = record_correction_wrapper(
                filepath=arguments["filepath"],
                from_category=arguments["from_category"],
                to_category=arguments["to_category"]
            )

        elif name == "get_learning_stats":
            result = get_learning_stats_wrapper()

        elif name == "suggest_categories":
            result = suggest_categories_wrapper(
                min_files=arguments.get("min_files", 5),
                locale=arguments.get("locale", "zh")
            )

        elif name == "detect_language":
            from classify import detect_language
            result = {"locale": detect_language(arguments["text"]), "text_length": len(arguments["text"])}

        # ── File Grouping ──
        elif name == "detect_file_groups":
            result = detect_file_groups(
                directory=arguments["directory"],
                min_group_size=arguments.get("min_group_size", 2)
            )

        elif name == "group_files":
            result = group_files(
                file_list=arguments["file_list"],
                folder_name=arguments["folder_name"],
                source_dir=arguments.get("source_dir")
            )

        elif name == "ungroup_files":
            result = ungroup_files(arguments["folder_path"])

        # ── Description Database (v2.3) ──
        elif name == "describe":
            result = describe(
                path=arguments["path"],
                description=arguments.get("description")
            )

        elif name == "search_descriptions":
            result = search_descriptions(
                query=arguments["query"],
                limit=arguments.get("limit", 50)
            )

        elif name == "list_descriptions":
            result = list_descriptions(
                directory=arguments.get("directory"),
                category=arguments.get("category")
            )

        elif name == "delete_description":
            result = delete_description(arguments["path"])

        # ── Sensitive File Security (v2.4) ──
        elif name == "ingest_sensitive":
            result = ingest_sensitive(
                filepath=arguments["filepath"],
                ocr_text=arguments.get("ocr_text", "")
            )

        elif name == "redact_text":
            result = redact_text_fn(arguments["text"])

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    """Run the MCP server over stdio."""
    if not HAS_MCP:
        print("FATAL: 'mcp' package required. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    capabilities = InitializationCapabilities(
        tools={},
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            capabilities,
            raise_exceptions=False
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
