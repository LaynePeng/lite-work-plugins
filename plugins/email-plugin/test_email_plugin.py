# 纯逻辑单测：不连真实 SMTP/IMAP（组装/解析/审批/越权/未配置路径均可离线验证）。
# 运行：<主仓库 .venv python> -m unittest plugins.email-plugin.test_email_plugin
import asyncio
import os
import shutil
import importlib.util
import os
import tempfile
import unittest
from email.message import EmailMessage
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "email_plugin_under_test", os.path.join(_HERE, "plugin.py"))
emailplugin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(emailplugin)  # type: ignore[union-attr]


class Approval:
    def __init__(self, ok=True):
        self.ok = ok
        self.last_title = None
        self.last_context = None

    def request_approval(self, title, context, session_id=""):
        self.last_title = title
        self.last_context = context
        fut = asyncio.Future()
        fut.set_result(self.ok)
        return fut


class _App:
    """假 app：可注入 config / workspace / approval_gate。"""

    def __init__(self, config=None, workspace=None, gate=None):
        self.config = config or {}
        self.workspace = workspace or tempfile.mkdtemp(prefix="lw-email-test-")
        self.approval_gate = gate


class EmailPluginBase(unittest.TestCase):
    def setUp(self):
        self.plugin = emailplugin.EmailPlugin()


class BuildMessageTest(EmailPluginBase):
    def test_text_only(self):
        msg = emailplugin._build_msg("me@x.com", ["a@x.com"], [], [], "主题", "正文", None, [])
        self.assertIsInstance(msg, EmailMessage)
        self.assertEqual(msg["Subject"], "主题")
        self.assertEqual(msg.get_content_type(), "text/plain")
        self.assertIn("正文", msg.get_content())

    def test_text_plus_html_alternative(self):
        msg = emailplugin._build_msg(
            "me@x.com", ["a@x.com"], ["cc@x.com"], ["bcc@x.com"],
            "s", "纯文本", "<b>HTML</b>", [],
        )
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        parts = sorted(p.get_content_type() for p in msg.iter_parts())
        self.assertEqual(parts, ["text/html", "text/plain"])
        self.assertIn("<b>HTML</b>", msg.get_payload()[1].get_content())

    def test_attachment_in_subpart(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 x")
            tmp = f.name
        try:
            msg = emailplugin._build_msg("a", ["to@x.com"], [], [], "s", "b", None,
                                         [(tmp, os.path.basename(tmp))])
            self.assertEqual(msg.get_content_type(), "multipart/mixed")
            names = [p.get_filename() for p in msg.iter_parts()]
            self.assertIn(os.path.basename(tmp), names)
        finally:
            os.unlink(tmp)


class UtilsTest(EmailPluginBase):
    def test_decode_mime_header(self):
        out = emailplugin._decode("=?utf-8?B?5L2g5aW977yM5LiW55WM?=")
        self.assertIn("你好", out)

    def test_to_list_variants(self):
        self.assertEqual(emailplugin._to_list("a@x.com, b@x.com"), ["a@x.com", "b@x.com"])
        self.assertEqual(emailplugin._to_list(["a", "  b "]), ["a", "b"])
        self.assertEqual(emailplugin._to_list(None), [])
        self.assertEqual(emailplugin._to_list(""), [])

    def test_imap_date(self):
        self.assertEqual(emailplugin._imap_date("2026-01-05"), "05-Jan-2026")

    def test_resolve_attachment_inside_only(self):
        ws = tempfile.mkdtemp(prefix="lw-email-ws-")
        try:
            with open(os.path.join(ws, "rpt.pdf"), "wb") as f:
                f.write(b"%PDF")
            p = emailplugin._resolve_attachment(SimpleNamespace(workspace=ws), "rpt.pdf")
            self.assertTrue(p is not None and os.path.isfile(p))
            self.assertIsNone(emailplugin._resolve_attachment(SimpleNamespace(workspace=ws), "../../etc/passwd"))
            self.assertIsNone(emailplugin._resolve_attachment(SimpleNamespace(workspace=ws), "C:/Windows/system32/drivers/etc/hosts"))
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class ExecutePathsTest(EmailPluginBase):
    def test_send_unconfigured(self):
        out = asyncio.run(self.plugin._send({"to": "a@x.com", "subject": "s", "body": "b"}))
        self.assertIn("未配置", out)
        self.assertIn("设置页", out)

    def test_send_requires_to_subject_body(self):
        self.plugin._app = _App(config={"email-plugin": {"email_user": "u", "email_password": "p"}})
        out = asyncio.run(self.plugin._send({"subject": "s"}))
        self.assertIn("收件人为空", out)
        out = asyncio.run(self.plugin._send({"to": "a@x.com", "subject": "s", "body": "", "html": ""}))
        self.assertIn("正文为空", out)
        out = asyncio.run(self.plugin._send({"to": "a@x.com"}))
        self.assertIn("主题为空", out)

    def test_send_approval_denied(self):
        gate = Approval(ok=False)
        app = _App(config={"email-plugin": {"email_user": "me@outlook.com",
                           "email_password": "pw", "email_require_approval": True}},
                   workspace=None, gate=gate)
        self.plugin._app = app
        out = asyncio.run(self.plugin._send({"to": "a@x.com", "subject": "测试", "body": "hi"}))
        self.assertIn("拒绝", out)
        self.assertIn("测试", gate.last_title)

    def test_send_approval_context_sanitized(self):
        gate = Approval(ok=False)
        app = _App(config={"email-plugin": {"email_user": "me@outlook.com", "email_password": "pw"}},
                   workspace=None, gate=gate)
        self.plugin._app = app
        asyncio.run(self.plugin._send({"to": "a@x.com", "cc": ["c@x.com"], "subject": "重要", "body": "正文内容"}))
        self.assertIn("重要", gate.last_context)
        self.assertIn("a@x.com", gate.last_context)
        self.assertNotIn("pw", gate.last_context)

    def test_attachment_outside_workspace_blocked(self):
        gate = Approval(ok=True)
        app = _App(config={"email-plugin": {"email_user": "u", "email_password": "p"}}, workspace=None, gate=gate)
        self.plugin._app = app
        out = asyncio.run(self.plugin._send({
            "to": "a@x.com", "subject": "s", "body": "b",
            "attachments": ["../../etc/passwd"],
        }))
        self.assertIn("非法或不在工作区内", out)

    def test_read_bad_uid(self):
        out = asyncio.run(self.plugin._read({"uid": "../etc"}))
        self.assertIn("纯数字", out)


class PluginUiProtocolTest(EmailPluginBase):
    """通用插件 UI 协议：contributes 设置声明 + status_from_config + 嵌套读值。"""

    def test_contributes_settings_schema(self):
        settings = self.plugin.contributes["settings"]
        keys = {s["key"] for s in settings}
        self.assertIn("email_user", keys)
        self.assertIn("email_password", keys)
        # 密码字段必须声明 secret（plugin_settings 打码 + 前端保存跳过未改密文）
        pw = next(s for s in settings if s["key"] == "email_password")
        self.assertTrue(pw.get("secret") or pw.get("type") == "secret")
        # 审批开关默认开启
        approval = next(s for s in settings if s["key"] == "email_require_approval")
        self.assertIs(approval.get("default"), True)

    def test_status_from_config(self):
        st = self.plugin.status_from_config({})
        self.assertEqual(st["state"], "not_started")
        self.assertIn("email-plugin", st["reason"])
        st = self.plugin.status_from_config({"email-plugin": {"email_user": "a@b.com"}})
        self.assertEqual(st["state"], "not_started")
        st = self.plugin.status_from_config(
            {"email-plugin": {"email_user": "a@b.com", "email_password": "pw"}})
        self.assertEqual(st["state"], "running")

    def test_cfg_reads_nested_namespace(self):
        app = SimpleNamespace(config={"email-plugin": {"email_user": "a@b.com"}})
        self.assertEqual(emailplugin._cfg(app, "email_user"), "a@b.com")
        # 嵌套键不存在 → 默认值（不落回顶层平铺键）
        # 键不存在且未传默认 → None；显式默认在调用处传（_cfg 是通用取值）
        self.assertIsNone(emailplugin._cfg(app, "email_smtp_host"))
        self.assertEqual(emailplugin._cfg(app, "email_smtp_host", "h"), "h")
        self.assertIsNone(emailplugin._cfg(None, "email_user"))


if __name__ == "__main__":
    unittest.main()
