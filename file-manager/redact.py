"""
PII Detection & Redaction Engine
100% local — no cloud API calls. Detects Chinese ID cards, passports, 
phone numbers, emails, addresses, and other personally identifiable information.
"""

import re
from typing import List, Dict, Tuple

# ─── PII Detection Patterns ───────────────────────────────────────

PATTERNS = [
    # Chinese Resident ID Card (18 digits, last can be X)
    ("身份证号", re.compile(r'\b(\d{6})(\d{4})(\d{2})(\d{2})(\d{3})[\dXx]\b')),
    
    # Chinese phone number (mainland mobile)
    ("手机号", re.compile(r'\b1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}\b')),
    
    # Email address
    ("邮箱", re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')),
    
    # Passport / Travel permit (various formats)
    ("证件号", re.compile(r'\b[CEGKMSTWcegkmstw]\d{7,8}\b')),
    ("通行证号", re.compile(r'\bCD\d{7}\b')),
    
    # Student ID (various formats)
    ("学号", re.compile(r'\b(?:DC|MC|dc|mc)\d{5,8}\b')),
    ("学号2", re.compile(r'\bD-C\d-\d{4}-\d\b')),
    
    # Chinese address (heuristic: province/city + road/street + number)
    ("地址", re.compile(
        r'((?:河南|河北|北京|上海|广东|深圳|浙江|江苏|山东|四川|湖北|湖南|福建'
        r'|安徽|江西|辽宁|吉林|黑龙江|陕西|山西|甘肃|云南|贵州|海南|台湾'
        r'|内蒙古|广西|西藏|宁夏|新疆|重庆|天津)(?:省|市|自治区|特别行政区)?'
        r'(?:[\u4e00-\u9fff]{1,10}(?:市|区|县|镇|乡|村))?'
        r'(?:[\u4e00-\u9fff]{1,20}(?:路|街|道|巷|弄|号|楼|层|室|单元|院|园|大厦|公寓|小区))'
        r'(?:\d{1,6}[号栋幢座]?\d{0,4}[室号]?)'
        r'(?:[\d一二三四五六七八九十]{1,2}[层楼])?)'
    )),
    
    # University of Macau specific
    ("澳大学号", re.compile(r'\bmc\d{6}\b', re.IGNORECASE)),
    
    # Full name (Chinese characters, 2-4 chars) — heuristic, only in sensitive context
    ("姓名", re.compile(r'(?:姓名|Name)[：:]\s*([\u4e00-\u9fff]{2,4})')),
]

# ─── PII Detection ────────────────────────────────────────────────

def detect_pii(text: str) -> List[Dict]:
    """Detect PII in text. Returns list of {type, original, redacted, span}."""
    findings = []
    for pii_type, pattern in PATTERNS:
        for match in pattern.finditer(text):
            original = match.group(0)
            redacted = _redact_value(pii_type, original, match)
            
            # Avoid duplicates from overlapping patterns
            span = match.span()
            if any(f["span"][0] <= span[0] < f["span"][1] or 
                   f["span"][0] < span[1] <= f["span"][1]
                   for f in findings):
                continue
                    
            findings.append({
                "type": pii_type,
                "original": original,
                "redacted": redacted,
                "span": list(span),
            })
    
    # Sort by position
    findings.sort(key=lambda f: f["span"][0])
    return findings


def _redact_value(pii_type: str, original: str, match: re.Match) -> str:
    """Redact a single PII value, preserving partial info for context."""
    if pii_type == "身份证号":
        # 41102220040318007X → 411022***********X (show first 6, last 1)
        return original[:6] + "**********" + original[-1]
    elif pii_type == "手机号":
        # 13812345678 → 138****5678
        return original[:3] + "****" + original[-4:]
    elif pii_type == "邮箱":
        # abc@example.com → a***@example.com
        parts = original.split("@")
        if len(parts) == 2:
            return parts[0][0] + "***@" + parts[1]
        return "***@***"
    elif pii_type in ("证件号", "通行证号", "学号", "学号2", "澳大学号"):
        # Show first 2, rest masked
        if len(original) > 3:
            return original[:2] + "*" * (len(original) - 3) + original[-1]
        return "***"
    elif pii_type == "地址":
        # 河南省漯河市源汇区... → 河南省漯河市***
        if len(original) > 8:
            return original[:8] + "***"
        return "***地址***"
    elif pii_type == "姓名":
        # 张琳坤 → 张**
        groups = match.groups()
        if groups:
            name = groups[0]
            if len(name) >= 2:
                return f"姓名: {name[0]}{'*' * (len(name) - 1)}"
        return "姓名: ***"
    
    return "[已脱敏]"


def redact_text(text: str) -> Dict:
    """
    Fully redact PII from text. Returns {redacted_text, findings, safe_description}.
    The redacted_text replaces all PII with masked versions.
    The safe_description is a summary safe to store in the description DB.
    """
    findings = detect_pii(text)
    
    # Build redacted text (replace from end to start to preserve spans)
    redacted = text
    for f in reversed(findings):
        start, end = f["span"]
        redacted = redacted[:start] + f["redacted"] + redacted[end:]
    
    # Generate safe description
    safe_desc = _generate_safe_description(findings, text)
    
    return {
        "redacted_text": redacted,
        "findings": findings,
        "pii_count": len(findings),
        "safe_description": safe_desc,
        "has_pii": len(findings) > 0,
    }


def _generate_safe_description(findings: List[Dict], text: str) -> str:
    """Generate a human-readable description without PII."""
    if not findings:
        return ""
    
    desc_parts = []
    pii_types_seen = set()
    
    for f in findings:
        if f["type"] not in pii_types_seen:
            pii_types_seen.add(f["type"])
            desc_parts.append(f["type"])
    
    # Detect document type from context
    has_address = any(f["type"] == "地址" for f in findings)
    has_name = any(f["type"] == "姓名" for f in findings)
    has_student = any(f["type"] in ("学号", "学号2", "澳大学号") for f in findings)
    has_passport = any(f["type"] in ("证件号", "通行证号") for f in findings)
    
    # Context keywords (Chinese)
    context_keywords = []
    if "澳门" in text or "Macau" in text or "澳門" in text:
        context_keywords.append("澳大")
    if "保密" in text or "confidential" in text:
        context_keywords.append("保密")
    if "学生" in text or "student" in text or "Student" in text:
        context_keywords.append("学生")
    if "协议" in text or "agreement" in text:
        context_keywords.append("协议")
    if "声明" in text or "declaration" in text:
        context_keywords.append("声明")
    
    # Build description
    if has_name and has_student:
        desc_parts.append("学生证件")
    if has_name and not has_student:
        desc_parts.append("个人证件")
    if has_passport:
        desc_parts.append("通行证件")
    
    desc = "、".join(desc_parts)
    if context_keywords:
        desc += f" - 含{'、'.join(context_keywords)}相关内容"
    
    return desc


def describe_sensitive_file(text: str, filename: str = "") -> str:
    """
    High-level entry point for sensitive file description.
    Returns a PII-free description string suitable for the descriptions database.
    
    Example:
        Input: "姓名: 张琳坤 身份证号: 41102220040318007X 澳门大学"
        Output: "个人证件、身份证号、姓名 - 含澳大、学生相关内容"
    """
    result = redact_text(text)
    desc = result["safe_description"]
    
    # Add filename hint (without path)
    if filename:
        name_hint = _safe_filename_hint(filename)
        if name_hint:
            desc = f"{name_hint}：{desc}" if desc else name_hint
    
    # If nothing detected but filename has clues
    if not desc:
        if filename:
            return _safe_filename_hint(filename)
        return "敏感文件（未识别到具体类型）"
    
    return desc


def _safe_filename_hint(filename: str) -> str:
    """Extract safe hints from filename without revealing personal info."""
    # Remove personal name patterns (2-3 Chinese chars)
    import os
    basename = os.path.basename(filename)
    stem = os.path.splitext(basename)[0]
    
    # Remove any 18-digit sequences
    stem = re.sub(r'\d{15,18}', '', stem)
    # Remove any 11-digit sequences (phone)
    stem = re.sub(r'\d{11}', '', stem)
    
    # Keep useful keywords
    keywords = []
    for kw in ["保密", "协议", "声明", "澳大", "身份证", "通行证", "学生证", 
               "合同", "NDA", "confidential", "agreement", "passport",
               "澳门", "大学", "研究生", "本科"]:
        if kw in basename:
            keywords.append(kw)
    
    return "、".join(keywords) if keywords else ""
