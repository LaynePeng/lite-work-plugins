# lite-work 社区邮件插件：SMTP 发信（含 HTML 排版 / 附件）＋ IMAP 收件箱 / 搜索 / 读单封。
#
# 设计要点：
# - 默认面向 Hotmail/Outlook（SMTP smtp-mail.outlook.com:587 STARTTLS /
#   IMAP outlook.office365.com:993 SSL），也兼容任意标准 SMTP/IMAP 服务
# - email_send 发送前必须过审批门（app.approval_gate）；凭据不写日志、不进返回值
# - 纯标准库（smtplib / imaplib / email / html.parser），无需 wheels
# - 配置从 app.config 读取（设置页「邮件」区块写入 email_* 键）
# - 单一账号；附件限 workspace 内路径
#
# 格式遵循社区 AGENTS.md：ToolPlugin 子类、无参构造、install(kernel) 捕获 app、
# version 与 manifest.json 同步。MIT 许可（跟随本仓库）。
from __future__ import annotations

import asyncio
import html as _html
import imaplib
import logging
import os
import re
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

logger = logging.getLogger("litework.email")

DEFAULT_SMTP_HOST = "smtp-mail.outlook.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_IMAP_HOST = "outlook.office365.com"
DEFAULT_IMAP_PORT = 993
MAX_BODY_CHARS = 8000
MAX_LIST_ITEMS = 50
DEFAULT_LIST_ITEMS = 20
MAX_ATTACHMENTS = 8
_MAX_ATTACH_BYTES = 20 * 1024 * 1024


def _cfg(app, key: str, default: Any = None) -> Any:
    """读插件配置：app.config["email-plugin"][key]（设置页的插件配置表单
    经通用插件 UI 协议写入该嵌套命名空间，见 contributes["settings"]）。"""
    if app is None:
        return default
    cfg = getattr(app, "config", None) or {}
    own = cfg.get("email-plugin")
    if not isinstance(own, dict):
        return default
    return own.get(key, default)


def _decode(s: Any) -> str:
    """RFC2047 头解码（=?utf-8?...?= 等），失败回退原文。"""
    if not s:
        return ""
    try:
        parts = decode_header(s)
        out = []
        for data, enc in parts:
            if isinstance(data, bytes):
                out.append(data.decode(enc or "utf-8", "replace"))
            else:
                out.append(data)
        return "".join(out)
    except Exception:
        return str(s)


def _to_list(v: Any) -> List[str]:
    """兼容字符串/数组入参的收件人解析（逗号分隔亦可）。"""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def _resolve_attachment(app, rel: str) -> Optional[str]:
    """附件路径：仅允许 workspace 内（安全；发送动作本身已过审批）。"""
    if app is None or not rel:
        return None
    ws = os.path.abspath(getattr(app, "workspace", "") or "")
    if not ws:
        return None
    target = os.path.abspath(os.path.join(ws, rel))
    if not (target == ws or target.startswith(ws + os.sep)):
        return None
    return target if os.path.isfile(target) else None


def _smtp_send(smtp_host: str, smtp_port: int, user: str, password: str,
               from_addr: str, msg: EmailMessage) -> None:
    """同步 SMTP 发送（包进 to_thread）；STARTTLS 优先，验证服务器证书。"""
    with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg, from_addr=from_addr)


def _build_msg(from_addr, to, cc, bcc, subject, body, html_body, attached):
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    if body and html_body:
        msg.set_content(body)
        msg.add_alternative(html_body, subtype="html")
    elif html_body:
        msg.set_content(html_body, subtype="html")
    else:
        msg.set_content(body)
    for fpath, fname in attached:
        with open(fpath, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype="octet-stream",
                           filename=fname)
    return msg


def _imap_fetch(app, mailbox: str, criteria: List[str],
                max_items: int) -> Tuple[List[Dict[str, Any]], str]:
    """同步 IMAP：SEARCH + FETCH 头字段，返回 (列表, 错误消息)。"""
    user = str(_cfg(app, "email_user", "") or "").strip()
    password = str(_cfg(app, "email_password", "") or "").strip()
    if not user or not password:
        return [], "邮件账号未配置：请先在设置页 → 插件 → email-plugin 填写邮箱与应用密码。"
    host = str(_cfg(app, "email_imap_host", DEFAULT_IMAP_HOST) or DEFAULT_IMAP_HOST)
    port = int(_cfg(app, "email_imap_port", DEFAULT_IMAP_PORT) or DEFAULT_IMAP_PORT)
    try:
        import email.parser as _ep
        with imaplib.IMAP4_SSL(host, port, timeout=30) as conn:
            conn.login(user, password)
            conn.select(mailbox or "INBOX")
            typ, data = conn.search(None, *criteria)
            if typ != "OK":
                return [], f"IMAP 搜索失败：{data}"
            uids = (data[0] or b"").decode().split() or []
            uids = uids[-max_items:]  # 最新 max 封
            items = []
            for uid in uids:
                try:
                    typ, d = conn.uid("fetch", uid,
                        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                except Exception:
                    continue
                if typ != "OK" or not d or not isinstance(d[0], tuple):
                    continue
                msg = _ep.BytesParser().parsebytes(d[0][1])
                items.append({
                    "uid": uid,
                    "from": _decode(msg.get("From") or ""),
                    "subject": _decode(msg.get("Subject") or "") or "(无主题)",
                    "date": _decode(msg.get("Date") or "") or "",
                })
            return items, ""
    except imaplib.IMAP4.error as e:
        err = str(e)
        if "authentication" in err.lower():
            return [], f"IMAP 认证失败：检查邮箱与应用密码（{host}:{port}）。"
        return [], f"IMAP 错误：{err}"
    except Exception as e:
        return [], f"IMAP 连接失败（{host}:{port}）：{e}"


def _imap_read(app, mailbox: str, uid: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """同步 IMAP：UID FETCH 完整邮件，解析正文（plain 优先，html 剥离标签）。"""
    user = str(_cfg(app, "email_user", "") or "").strip()
    password = str(_cfg(app, "email_password", "") or "").strip()
    if not user or not password:
        return None, "邮件账号未配置：请先在设置页 → 插件 → email-plugin 填写邮箱与应用密码。"
    host = str(_cfg(app, "email_imap_host", DEFAULT_IMAP_HOST) or DEFAULT_IMAP_HOST)
    port = int(_cfg(app, "email_imap_port", DEFAULT_IMAP_PORT) or DEFAULT_IMAP_PORT)
    try:
        from email.parser import BytesParser
        with imaplib.IMAP4_SSL(host, port, timeout=30) as conn:
            conn.login(user, password)
            conn.select(mailbox or "INBOX")
            typ, d = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                return None, f"读取失败：UID {uid} 不存在或已删除。"
            msg = BytesParser().parsebytes(d[0][1])
            body = ""
            html_part = ""
            for part in msg.walk():
                ctype = part.get_content_type()
                cdisp = str(part.get("Content-Disposition") or "")
                if "attachment" in cdisp:
                    continue
                try:
                    payload = part.get_payload(decode=True)
                except Exception:
                    continue
                if payload is None:
                    continue
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
                if ctype == "text/plain" and not body:
                    body = text.strip()
                elif ctype == "text/html" and not html_part:
                    html_part = text
            if not body and html_part:
                body = _html.unescape(re.sub(r"<[^>]+>", "", html_part)).strip()
            body = re.sub(r"\n{3,}", "\n\n", body)[:MAX_BODY_CHARS]
            return {
                "uid": uid,
                "from": _decode(msg.get("From") or ""),
                "to": _decode(msg.get("To") or ""),
                "cc": _decode(msg.get("Cc") or ""),
                "subject": _decode(msg.get("Subject") or "") or "(无主题)",
                "date": _decode(msg.get("Date") or "") or "",
                "body": body,
            }, ""
    except imaplib.IMAP4.error as e:
        return None, f"IMAP 错误：{e}"
    except Exception as e:
        return None, f"IMAP 连接失败（{host}:{port}）：{e}"


class EmailPlugin(ToolPlugin):
    """邮件工具：发信（HTML 排版 + 附件，发送前审批）＋ IMAP 收件箱/搜索/读单封。"""

    name = "email-plugin"
    version = "1.0.1"
    description = (
        "邮件：SMTP 发信（支持 HTML 排版与附件，发送前需用户在审批卡确认）＋ "
        "IMAP 收件箱列表 / 关键词搜索 / 读取单封（Hotmail/Outlook 默认配置，"
        "账号在设置页 → 插件 → email-plugin 填写）。"
    )

    #: 通用插件 UI 协议：设置页据此自动渲染配置表单（仅安装本插件后出现）；
    #: 值经 POST /api/config 写入 config["email-plugin"][key] 嵌套命名空间。
    contributes = {
        "settings": [
            {"key": "email_user", "type": "str", "label": "邮箱账号",
             "hint": "完整邮箱地址，如 you@hotmail.com"},
            {"key": "email_password", "type": "secret", "label": "密码 / 应用密码",
             "hint": "Hotmail/Outlook 开启两步验证后需用「应用密码」而非登录密码"},
            {"key": "email_smtp_host", "type": "str", "label": "SMTP 服务器",
             "default": "smtp-mail.outlook.com"},
            {"key": "email_smtp_port", "type": "number", "label": "SMTP 端口",
             "default": 587},
            {"key": "email_imap_host", "type": "str", "label": "IMAP 服务器",
             "default": "outlook.office365.com"},
            {"key": "email_imap_port", "type": "number", "label": "IMAP 端口",
             "default": 993},
            {"key": "email_from", "type": "str", "label": "发件人地址（可选）",
             "hint": "留空则用邮箱账号"},
            {"key": "email_require_approval", "type": "boolean",
             "label": "发送前需审批（推荐开启）", "default": True,
             "hint": "每次 email_send 弹审批卡，用户确认后才发出"},
        ],
    }

    def status_from_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """设置页显示的运行状态：账号未配置 → 未启动 + 原因。"""
        own = cfg.get(self.name)
        own = own if isinstance(own, dict) else {}
        user = str(own.get("email_user") or "").strip()
        password = str(own.get("email_password") or "").strip()
        if not user or not password:
            return {"state": "not_started",
                    "reason": "邮箱账号未配置（设置页 → 插件 → email-plugin）"}
        return {"state": "running"}

    def __init__(self) -> None:
        self._app = None

    def install(self, kernel) -> None:
        try:
            if kernel.has_service("app"):
                self._app = kernel.get_service("app")
        except Exception:
            self._app = None
        super().install(kernel)

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="email_send",
                description=(
                    "发送电子邮件。发送前会弹出审批卡需用户确认；账号在设置页 → "
                    "插件 → email-plugin 配置（默认 Hotmail/Outlook）。支持纯文本与 "
                    "HTML 排版正文（html 由你生成）、附件（限工作区内文件路径）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": ["string", "array"], "description": "收件人邮箱（逗号分隔或数组）", "items": {"type": "string"}},
                        "cc": {"type": ["string", "array"], "description": "抄送（可选）", "items": {"type": "string"}},
                        "bcc": {"type": ["string", "array"], "description": "密送（可选）", "items": {"type": "string"}},
                        "subject": {"type": "string", "description": "邮件主题"},
                        "body": {"type": "string", "description": "纯文本正文（必填；提供 html 时可同为降级）"},
                        "html": {"type": "string", "description": "HTML 排版正文（可选，客户端优先展示）"},
                        "attachments": {"type": "array", "description": "附件文件名列表（工作区内路径）", "items": {"type": "string"}},
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
            ToolDefinition(
                name="email_list",
                description=(
                    "列出收件箱邮件（IMAP；默认 INBOX，最新在前）。可按发件人/主题关键词/"
                    "未读/日期范围过滤。返回每封的 uid/发件人/主题/日期。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "description": "IMAP 文件夹（默认 INBOX）"},
                        "from": {"type": "string", "description": "按发件人过滤"},
                        "subject": {"type": "string", "description": "按主题关键词过滤"},
                        "unread": {"type": "boolean", "description": "仅未读"},
                        "since": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                        "until": {"type": "string", "description": "截止日期 YYYY-MM-DD"},
                        "max": {"type": "integer", "description": "返回条数（上限 50）"},
                    },
                },
            ),
            ToolDefinition(
                name="email_read",
                description=(
                    "读取收件箱中某封邮件的完整内容（含正文）。uid 取自 email_list 的返回。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "folder": {"type": "string", "description": "IMAP 文件夹（默认 INBOX）"},
                        "uid": {"type": "string", "description": "邮件 UID（由 email_list 返回）"},
                    },
                    "required": ["uid"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "email_send":
            return await self._send(args)
        if name == "email_list":
            return await self._list(args)
        if name == "email_read":
            return await self._read(args)
        return f"[Error]: 未知工具 {name}"

    # ------------------------------------------------------------ 发信

    async def _send(self, args: Dict[str, Any]) -> str:
        from litework.tools.todos import current_session_id

        user = str(_cfg(self._app, "email_user", "") or "").strip()
        password = str(_cfg(self._app, "email_password", "") or "").strip()
        if not user or not password:
            return ("[Error]: 邮件账号未配置。请先在设置页 → 插件 → email-plugin 填写邮箱与应用密码："
                    "SMTP/IMAP 默认已按 Hotmail/Outlook 填好。")

        to = _to_list(args.get("to"))
        cc = _to_list(args.get("cc"))
        bcc = _to_list(args.get("bcc"))
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        html_body = str(args.get("html") or "").strip() or None
        atts = _to_list(args.get("attachments"))
        if not to:
            return "[Error]: 收件人为空（to 必填）。"
        if not subject:
            return "[Error]: 主题为空（subject 必填）。"
        if not body and not html_body:
            return "[Error]: 正文为空（body / html 至少填一个）。"

        attached = []
        for rel in atts[:MAX_ATTACHMENTS]:
            p = _resolve_attachment(self._app, rel)
            if p is None:
                return f"[Error]: 附件路径非法或不在工作区内：{rel}"
            attached.append((p, os.path.basename(p)))

        # ---------- 审批（发送前必过；email_require_approval 默认开启） ----------
        gate = getattr(self._app, "approval_gate", None)
        try:
            require = bool(_cfg(self._app, "email_require_approval", True))
        except Exception:
            require = True
        if require:
            if gate is None:
                return "[Error]: 审批门不可用，无法发送邮件（发送审批已开启）。"
            session_id = current_session_id.get("")
            context = (
                f"收件人：{', '.join(to) or '(无)'}"
                + (f"\n抄送：{', '.join(cc) or '(无)'}" if cc else "")
                + f"\n主题：{subject}"
                + (f"\n正文：{body[:240]}{'…' if len(body) > 240 else ''}" if body else "\n正文：HTML（审批卡以纯文本摘要为主）")
                + (f"\n附件：{', '.join(n for _, n in attached) or '(无)'}" if attached else "")
                + "\n\n批准后该邮件将被立即发送。"
            )
            future = gate.request_approval(f"发送邮件：{subject}", context, session_id=session_id)
            try:
                ok = await future
            except Exception as e:
                return f"[Error]: 审批等待异常：{e}"
            if not ok:
                return "[Denied]: 用户拒绝发送该邮件。"

        # ---------- 组装与发送 ----------
        smtp_host = str(_cfg(self._app, "email_smtp_host", DEFAULT_SMTP_HOST) or DEFAULT_SMTP_HOST)
        smtp_port = int(_cfg(self._app, "email_smtp_port", DEFAULT_SMTP_PORT) or DEFAULT_SMTP_PORT)
        from_addr = str(_cfg(self._app, "email_from", "") or "").strip() or user

        msg = _build_msg(from_addr, to, cc, bcc, subject, body, html_body, attached)

        try:
            await asyncio.to_thread(
                _smtp_send, smtp_host, smtp_port, user, password, from_addr, msg
            )
        except smtplib.SMTPAuthenticationError:
            return ("[Error]: SMTP 认证失败。Hotmail/Outlook 普通密码可能被拒——"
                    "请在微软账户启用两步验证后生成「应用密码」填入插件设置。")
        except (smtplib.SMTPServerDisconnected, ConnectionError, OSError) as e:
            return f"[Error]: SMTP 连接失败（{smtp_host}:{smtp_port}）：{e}"
        except Exception as e:
            return f"[Error]: 发送失败：{e}"

        return (f"[Email OK]: 邮件已发送 → {', '.join(to)} | 主题：{subject}"
                + (f" | 附件：{', '.join(n for _, n in attached) or '(无)'}" if attached else "")
                + f" | 协议 SMTP {smtp_host}:{smtp_port}")

    # ------------------------------------------------------------ 收信

    async def _list(self, args: Dict[str, Any]) -> str:
        folder = str(args.get("folder") or "INBOX").strip() or "INBOX"
        try:
            raw_max = int(args.get("max") or DEFAULT_LIST_ITEMS)
        except (TypeError, ValueError):
            raw_max = DEFAULT_LIST_ITEMS
        max_items = max(1, min(raw_max, MAX_LIST_ITEMS))

        criteria = ["ALL"]
        if args.get("unread"):
            criteria = ["UNSEEN"]
        frm = str(args.get("from") or "").strip()
        if frm:
            criteria += ["FROM", f'"{frm}"']
        subj = str(args.get("subject") or "").strip()
        if subj:
            criteria += ["SUBJECT", f'"{subj}"']
        try:
            if args.get("since"):
                criteria += ["SINCE", _imap_date(str(args["since"]).strip())]
            if args.get("until"):
                criteria += ["BEFORE", _imap_date(str(args["until"]).strip())]
        except ValueError:
            return "[Error]: 日期格式应为 YYYY-MM-DD。"

        items, err = await asyncio.to_thread(_imap_fetch, self._app, folder, criteria, max_items)
        if err:
            return f"[Error]: {err}"
        if not items:
            return f"[Email]: {folder} 中没有匹配的邮件（条件：{' '.join(criteria)}）。"
        lines = [f"[Email]: {folder} 共找到 {len(items)} 封（最新在前，uid 供 email_read 使用）："]
        for it in items:
            lines.append(f"- uid={it['uid']} | {it['date']} | {it['from']} | {it['subject']}")
        return "\n".join(lines)

    async def _read(self, args: Dict[str, Any]) -> str:
        folder = str(args.get("folder") or "INBOX").strip() or "INBOX"
        uid = str(args.get("uid") or "").strip()
        if not uid or not re.fullmatch(r"\d+", uid):
            return "[Error]: uid 必须是 email_list 返回的纯数字 UID。"
        item, err = await asyncio.to_thread(_imap_read, self._app, folder, uid)
        if err:
            return f"[Error]: {err}"
        if item is None:
            return "[Error]: 未读到该邮件。"
        return (
            f"[Email]: 主题：{item['subject']}\n"
            f"发件人：{item['from']}\n"
            f"收件人：{item['to']}（抄送 {item['cc'] or '(无)'}）\n"
            f"日期：{item['date']}\n"
            f"---- 正文 ----\n{item['body']}"
        )


def _imap_date(d: str) -> str:
    """'YYYY-MM-DD' → IMAP 日期 'dd-Mon-yyyy'（英文缩略月）。"""
    from datetime import datetime
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d-%b-%Y")
