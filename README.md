# HeFileLib — Hermes File Agent

智能文件管理系统，基于 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 框架。
自动提取文件内容 → 分类 → 组织 → 提供 Web 仪表板浏览。

## 功能

- **自动提取** — 支持 txt, pdf, docx, pptx, xlsx, ipynb, zip，图片通过 Telegram 描述/vision_analyze/OCR 获取文本
- **智能分类** — jieba 分词 + SQLite 动态类别 + 关键词权重 + 学习纠正
- **文件分组** — 四种策略：连续编号、版本变体、同根不同格式、日期前缀
- **Web 仪表板** — 深色主题，登录认证（PBKDF2），分类浏览/搜索/图片预览/统计
- **描述数据库** — FTS5 全文索引，每个文件+文件夹可写中文描述

## 结构

```
file-manager/       MCP Server — 提取、分类、组织、分组
file-dashboard/     HTTP 服务器 — Web 仪表板 + 9 个 API
skills/             Hermes Agent Skill 定义
```

## 安装

```bash
pip install -r file-manager/requirements.txt
pip install -r file-dashboard/requirements.txt  # 如有
```

## MCP Server 配置

在 Hermes 的 `~/.hermes/config.yaml` 中添加：

```yaml
mcp_servers:
  file-manager:
    command: "python3"
    args: ["/path/to/hefilelib_hermes_FileAgent/file-manager/server.py"]
  file-dashboard:
    command: "python3"
    args: ["/path/to/hefilelib_hermes_FileAgent/file-dashboard/server.py", "--password", "your-password"]
```

## 使用

### Hermes Agent 中

```
「帮我整理 ~/Documents 的所有文件」
「找股票相关的文件」
「这个文件是什么」
「打开仪表板」
```

### 直接调用

```bash
# 启动仪表板
python3 file-dashboard/server.py --password mypass --port 8765

# 分类文件
python3 file-manager/classify.py --file /path/to/file.jpg
```

## 安全

- 登录使用 PBKDF2-SHA256 哈希密码
- HttpOnly Session Cookie
- CSP (Content-Security-Policy)
- CSRF Token 双重提交 Cookie 验证
- 防路径穿越 (safe_resolve)
- 防 XSS（前端 esc 转义）、防 SQL 注入

## License

MIT
