# 纯逻辑单测：不连真实 SMTP/IMAP（组装/解析/审批/越权/未配置路径均可离线验证）。
# 运行：<主仓库 .venv python> -m unittest plugins.email-plugin.test_email_plugin
import asyncio
import os
import shutil
import sys
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

    def save_config(self, updates):
        """对齐真实 app：按顶层键合并写入（插件只写自己的命名空间）。"""
        self.config.update({k: v for k, v in updates.items() if v is not None})


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


class _NoImaplib:
    """上下文管理器：在 with 内屏蔽标准库 imaplib（模拟未捆绑 imaplib 的冻结宿主）。

    必须覆盖整个测试流程——回退加载发生在调用 `_load_imaplib()` 时，
    若导入阶段结束就恢复 import，测的就是标准库而非 vendored 副本。
    """

    def __enter__(self):
        import builtins
        self._real = builtins.__import__
        self._prev = sys.modules.pop("imaplib", None)
        self._had = self._prev is not None

        def blocked(name, *a, **k):
            if name == "imaplib" or name.startswith("imaplib."):
                raise ImportError("No module named 'imaplib'")
            return self._real(name, *a, **k)

        builtins.__import__ = blocked
        return self

    def __exit__(self, *exc):
        import builtins
        builtins.__import__ = self._real
        sys.modules.pop("imaplib", None)
        if self._had and self._prev is not None:
            sys.modules["imaplib"] = self._prev
        return False

    @staticmethod
    def load_fresh():
        """重新导入插件模块（此时 imaplib 被屏蔽）。"""
        spec = importlib.util.spec_from_file_location(
            "email_plugin_no_imaplib", os.path.join(_HERE, "plugin.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod


class VendoredImaplibTest(unittest.TestCase):
    """冻结宿主可能没捆绑 imaplib（PyInstaller 静态分析看不到插件 import）。

    回归点：模块 import 不得因 imaplib 缺席而失败——否则 contributes 读不出来，
    设置页连「配置」表单都不出现（本 bug 的实际症状）。
    """

    def test_import_survives_missing_imaplib(self):
        with _NoImaplib():
            mod = _NoImaplib.load_fresh()
        self.assertTrue(hasattr(mod, "EmailPlugin"))

    def test_contributes_readable_without_imaplib(self):
        """核心症状：imaplib 缺席时设置表单仍须能渲染。"""
        with _NoImaplib():
            mod = _NoImaplib.load_fresh()
            keys = {s["key"] for s in mod.EmailPlugin().contributes["settings"]}
        self.assertIn("email_user", keys)
        self.assertIn("email_password", keys)

    def test_fallback_loads_vendored_copy(self):
        with _NoImaplib():
            mod = _NoImaplib.load_fresh()
            mod._IMAPLIB = None  # 清缓存，强制走回退
            m = mod._load_imaplib()
            path = os.path.abspath(m.__file__)
        self.assertIn("_vendor", path)
        self.assertTrue(hasattr(m, "IMAP4_SSL"))

    def test_fallback_does_not_pollute_global_state(self):
        """显式按路径加载：不入 sys.modules['imaplib']、不往 sys.path 塞 _vendor。"""
        with _NoImaplib():
            mod = _NoImaplib.load_fresh()
            mod._IMAPLIB = None
            mod._load_imaplib()
            leaked = "imaplib" in sys.modules
            injected = any("_vendor" in p for p in sys.path)
        self.assertFalse(leaked, "vendored 副本不应注册成全局 imaplib")
        self.assertFalse(injected, "_vendor 不应被加进 sys.path")

    def test_missing_both_reports_actionable_error(self):
        """宿主没捆绑 + 无自带副本 → 明确报错，而不是 AttributeError。"""
        with _NoImaplib():
            mod = _NoImaplib.load_fresh()
            mod._IMAPLIB = None
            orig = mod.os.path.isfile
            mod.os.path.isfile = lambda p: False if "_vendor" in p else orig(p)
            try:
                with self.assertRaises(ImportError) as cm:
                    mod._load_imaplib()
                msg = str(cm.exception)
            finally:
                mod.os.path.isfile = orig
        self.assertIn("imaplib", msg)

    def test_imap_tool_reports_unavailable_not_crash(self):
        """连 IMAP 但客户端不可用 → 工具返回可读原因（不抛异常）。"""
        with _NoImaplib():
            mod = _NoImaplib.load_fresh()
            mod._IMAPLIB = None
            plugin = mod.EmailPlugin()
            plugin._app = _App(config={"email-plugin": {
                "email_user": "u@x.com", "email_password": "p"}})
            orig = mod.os.path.isfile
            mod.os.path.isfile = lambda p: False if "_vendor" in p else orig(p)
            try:
                items, err = mod._imap_fetch(plugin._app, "INBOX", ["ALL"], 5)
            finally:
                mod.os.path.isfile = orig
        self.assertEqual(items, [])
        self.assertIn("IMAP 不可用", err)


class AuthModeTest(EmailPluginBase):
    """认证方式：显式配置优先；未配置时按已有凭据推断（兼容旧 basic 配置）。"""

    def test_explicit_mode_wins(self):
        self.assertEqual(emailplugin._resolve_auth_mode({"email_auth": "basic"}), "basic")
        self.assertEqual(emailplugin._resolve_auth_mode({"email_auth": "oauth2"}), "oauth2")

    def test_infer_oauth2_from_refresh_token(self):
        self.assertEqual(
            emailplugin._resolve_auth_mode({"email_oauth_refresh_token": "rt"}), "oauth2")

    def test_infer_basic_when_only_password(self):
        self.assertEqual(
            emailplugin._resolve_auth_mode({"email_password": "pw"}), "basic")

    def test_auth_material_oauth2_missing_config(self):
        app = _App(config={"email-plugin": {"email_user": "u@x.com", "email_auth": "oauth2"}})
        user, secret, mode, err = emailplugin._auth_material(app)
        self.assertEqual((user, secret, mode), ("", "", "oauth2"))
        self.assertIn("OAuth2 配置不完整", err)

    def test_auth_material_basic_ok(self):
        app = _App(config={"email-plugin": {"email_user": "u@x.com", "email_password": "pw"}})
        user, secret, mode, err = emailplugin._auth_material(app)
        self.assertEqual((user, secret, mode, err), ("u@x.com", "pw", "basic", ""))

    def test_auth_material_no_user(self):
        _u, _s, _m, err = emailplugin._auth_material(_App(config={"email-plugin": {}}))
        self.assertIn("邮箱账号未配置", err)


class Xoauth2Test(EmailPluginBase):
    def test_xoauth2_payload_format(self):
        import base64
        payload = emailplugin._xoauth2("me@x.com", "TOKEN")
        raw = base64.b64decode(payload).decode()
        self.assertEqual(raw, "user=me@x.com\x01auth=Bearer TOKEN\x01\x01")


class OauthConfTest(EmailPluginBase):
    def test_outlook_preset_endpoints(self):
        app = _App(config={"email-plugin": {
            "email_oauth_provider": "outlook", "email_oauth_tenant": "consumers"}})
        conf = emailplugin._oauth_conf(app)
        self.assertIn("login.microsoftonline.com/consumers", conf["token"])
        self.assertIn("SMTP.Send", conf["scope"])
        self.assertEqual(conf["flow"], "device_code")

    def test_gmail_preset(self):
        app = _App(config={"email-plugin": {"email_oauth_provider": "gmail"}})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["token"], "https://oauth2.googleapis.com/token")
        self.assertEqual(conf["scope"], "https://mail.google.com/")

    def test_custom_endpoint_override(self):
        app = _App(config={"email-plugin": {
            "email_oauth_provider": "custom",
            "email_oauth_token": "https://idp.example.com/token",
            "email_oauth_scope": "mail.read mail.send"}})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["token"], "https://idp.example.com/token")
        self.assertEqual(conf["scope"], "mail.read mail.send")

    def test_user_value_overrides_preset(self):
        app = _App(config={"email-plugin": {
            "email_oauth_provider": "outlook",
            "email_oauth_token": "https://custom/token"}})
        self.assertEqual(emailplugin._oauth_conf(app)["token"], "https://custom/token")


class ProviderServerParamsTest(EmailPluginBase):
    """通用性核心：服务商预设同时带出 SMTP/IMAP 服务器与加密方式。"""

    def test_gmail_brings_its_own_servers(self):
        app = _App(config={"email-plugin": {"email_oauth_provider": "gmail"}})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["smtp_host"], "smtp.gmail.com")
        self.assertEqual(conf["smtp_port"], 465)
        self.assertEqual(conf["smtp_tls"], "ssl")
        self.assertEqual(conf["imap_host"], "imap.gmail.com")

    def test_outlook_uses_starttls_on_587(self):
        app = _App(config={"email-plugin": {"email_oauth_provider": "outlook"}})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["smtp_port"], 587)
        self.assertEqual(conf["smtp_tls"], "starttls")

    def test_each_preset_has_servers(self):
        for name, preset in emailplugin.PROVIDERS.items():
            if name == "custom":
                continue
            self.assertTrue(preset["smtp_host"], f"{name} 缺 smtp_host")
            self.assertTrue(preset["imap_host"], f"{name} 缺 imap_host")
            self.assertIn(preset["smtp_tls"], ("starttls", "ssl"), name)

    def test_explicit_server_overrides_preset(self):
        app = _App(config={"email-plugin": {
            "email_oauth_provider": "gmail",
            "email_smtp_host": "smtp.example.com",
            "email_smtp_port": "2525"}})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["smtp_host"], "smtp.example.com")
        self.assertEqual(conf["smtp_port"], 2525)

    def test_explicit_tls_mode_overrides_preset(self):
        app = _App(config={"email-plugin": {
            "email_oauth_provider": "gmail", "email_smtp_tls": "starttls"}})
        self.assertEqual(emailplugin._oauth_conf(app)["smtp_tls"], "starttls")

    def test_invalid_tls_value_falls_back_to_preset(self):
        app = _App(config={"email-plugin": {
            "email_oauth_provider": "gmail", "email_smtp_tls": "bogus"}})
        self.assertEqual(emailplugin._oauth_conf(app)["smtp_tls"], "ssl")

    def test_custom_provider_has_no_server(self):
        app = _App(config={"email-plugin": {"email_oauth_provider": "custom"}})
        self.assertEqual(emailplugin._oauth_conf(app)["smtp_host"], "")

    def test_smtp_send_picks_ssl_for_465(self):
        """465 走 SMTP_SSL，587 走 STARTTLS——用替身验证分支。"""
        import smtplib
        seen = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                seen.append(("plain", host, port))
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): seen.append(("starttls",))
            def send_message(self, msg, from_addr=None): pass

        class FakeSSL(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                seen.append(("ssl", host, port))

        orig_plain, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
        orig_login = emailplugin._smtp_login
        smtplib.SMTP, smtplib.SMTP_SSL = FakeSMTP, FakeSSL
        emailplugin._smtp_login = lambda *a, **k: None
        try:
            emailplugin._smtp_send("h", 465, "u", "s", "f@x.com", object(),
                                   "basic", "ssl")
            emailplugin._smtp_send("h", 587, "u", "s", "f@x.com", object(),
                                   "basic", "starttls")
        finally:
            smtplib.SMTP, smtplib.SMTP_SSL = orig_plain, orig_ssl
            emailplugin._smtp_login = orig_login
        self.assertIn(("ssl", "h", 465), seen)
        self.assertIn(("plain", "h", 587), seen)
        self.assertIn(("starttls",), seen)


class OauthLoginToolTest(EmailPluginBase):
    def test_login_without_client_id_reports_actionable_error(self):
        self.plugin._app = _App(config={"email-plugin": {"email_user": "u@x.com"}})
        out = asyncio.run(self.plugin._oauth_login({}))
        self.assertIn("client_id", out)

    def test_device_start_returns_code_and_uri(self):
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_oauth_client_id": "cid"}})
        orig = emailplugin._oauth_device_start
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "ABCD-EFGH",
             "verification_uri": "https://microsoft.com/devicelogin", "interval": 5}, "")
        emailplugin._device_cache.clear()
        try:
            out = asyncio.run(self.plugin._oauth_login({}))
        finally:
            emailplugin._oauth_device_start = orig
            emailplugin._device_cache.clear()
        self.assertIn("ABCD-EFGH", out)
        self.assertIn("devicelogin", out)

    def test_save_token_persists_refresh_token(self):
        app = _App(config={"email-plugin": {"email_user": "u@x.com"}})
        self.plugin._app = app
        out = self.plugin._save_token({"refresh_token": "RT-123"})
        self.assertIn("授权成功", out)
        self.assertEqual(app.config["email-plugin"]["email_oauth_refresh_token"], "RT-123")

    def test_save_token_without_refresh_token(self):
        self.plugin._app = _App(config={"email-plugin": {"email_user": "u@x.com"}})
        out = self.plugin._save_token({"access_token": "AT"})
        self.assertIn("没有 refresh_token", out)


class OauthTokenCacheTest(EmailPluginBase):
    """access_token 缓存与刷新：命中缓存不重复换票，过期自动续期。"""

    def setUp(self):
        super().setUp()
        emailplugin._token_cache.update({"key": "", "access": "", "exp": 0.0})

    def _app(self):
        return _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_client_id": "cid", "email_oauth_refresh_token": "rt"}})

    def test_refresh_uses_server_expires_in(self):
        orig = emailplugin._post_form
        emailplugin._post_form = lambda url, data: (
            {"access_token": "AT", "expires_in": 900}, "")
        try:
            token, ttl, err = emailplugin._oauth_refresh(
                emailplugin._oauth_conf(self._app()))
        finally:
            emailplugin._post_form = orig
        self.assertEqual((token, ttl, err), ("AT", 900, ""))

    def test_access_token_cached(self):
        calls = []
        orig = emailplugin._oauth_refresh

        def fake_refresh(conf):
            calls.append(1)
            return "AT", 3600, ""

        emailplugin._oauth_refresh = fake_refresh
        try:
            app = self._app()
            t1, e1 = emailplugin._access_token(app)
            t2, e2 = emailplugin._access_token(app)
        finally:
            emailplugin._oauth_refresh = orig
            emailplugin._token_cache.update({"key": "", "access": "", "exp": 0.0})
        self.assertEqual((t1, e1), ("AT", ""))
        self.assertEqual((t2, e2), ("AT", ""))
        self.assertEqual(len(calls), 1, "第二次应命中缓存，不再换票")

    def test_expired_token_refreshes_again(self):
        calls = []

        def fake_refresh(conf):
            calls.append(1)
            return "AT-%d" % len(calls), 1, ""

        orig = emailplugin._oauth_refresh
        emailplugin._oauth_refresh = fake_refresh
        try:
            app = self._app()
            emailplugin._access_token(app)
            emailplugin._token_cache["exp"] = 0.0   # 强制过期
            t2, _ = emailplugin._access_token(app)
        finally:
            emailplugin._oauth_refresh = orig
            emailplugin._token_cache.update({"key": "", "access": "", "exp": 0.0})
        self.assertEqual(t2, "AT-2")


class SettingsSchemaTest(EmailPluginBase):
    def test_auth_selector_is_first_and_select_type(self):
        settings = self.plugin.contributes["settings"]
        first = settings[0]
        self.assertEqual(first["key"], "email_auth")
        self.assertEqual(first["type"], "select")
        self.assertIn("oauth2", first["options"])
        self.assertIn("basic", first["options"])

    def test_oauth_secrets_declared_secret(self):
        settings = self.plugin.contributes["settings"]
        for key in ("email_oauth_refresh_token", "email_oauth_client_secret"):
            spec = next(s for s in settings if s["key"] == key)
            self.assertTrue(spec.get("secret") or spec.get("type") == "secret")

    def test_status_oauth2_needs_client_id_then_token(self):
        st = self.plugin.status_from_config(
            {"email-plugin": {"email_user": "u@x.com", "email_auth": "oauth2"}})
        self.assertEqual(st["state"], "not_started")
        self.assertIn("客户端 ID", st["reason"])
        st = self.plugin.status_from_config({"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_client_id": "cid"}})
        self.assertIn("授权", st["reason"])
        st = self.plugin.status_from_config({"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_client_id": "cid", "email_oauth_refresh_token": "rt"}})
        self.assertEqual(st["state"], "running")

    def test_legacy_basic_config_still_running(self):
        st = self.plugin.status_from_config(
            {"email-plugin": {"email_user": "u@x.com", "email_password": "pw"}})
        self.assertEqual(st["state"], "running")


class FromAddressTest(EmailPluginBase):
    """发件人解析：只填显示名不能当发件人（From 头里必须有邮箱地址）。"""

    def _app(self, from_val):
        return _App(config={"email-plugin": {"email_user": "me@x.com",
                                             "email_from": from_val}})

    def test_plain_email(self):
        self.assertEqual(emailplugin._resolve_from_addr(self._app("a@b.com"), "me@x.com"),
                         "a@b.com")

    def test_name_and_email_kept(self):
        out = emailplugin._resolve_from_addr(self._app("Layne <a@b.com>"), "me@x.com")
        _, addr = __import__("email.utils", fromlist=["x"]).parseaddr(out)
        self.assertEqual(addr, "a@b.com")

    def test_display_name_only_falls_back_to_account(self):
        """回归点：真实配置里 email_from='Layne Peng'，曾导致 From 头没有地址。"""
        self.assertEqual(emailplugin._resolve_from_addr(self._app("Layne Peng"), "me@x.com"),
                         "me@x.com")

    def test_empty_falls_back_to_account(self):
        self.assertEqual(emailplugin._resolve_from_addr(self._app(""), "me@x.com"), "me@x.com")

    def test_result_always_has_address(self):
        from email.utils import parseaddr
        for val in ("a@b.com", "Layne <a@b.com>", "Layne Peng", ""):
            out = emailplugin._resolve_from_addr(self._app(val), "me@x.com")
            self.assertTrue(parseaddr(out)[1], f"{val!r} -> {out!r} 缺邮箱地址")


class GeneralizationGuardTest(unittest.TestCase):
    """通用性守卫：服务商相关的字面量只允许出现在 PROVIDERS 预设里。

    回归点：曾把 smtp-mail.outlook.com / outlook.office365.com 写死在发送与
    收信路径，导致换 Gmail 时服务器不跟着走——OAuth 通用化后必须靠预设驱动。
    """

    PROVIDER_LITERALS = ("outlook.", "gmail.", "fastmail.", "zoho.", "qq.com", "163.com")

    def _source_lines(self):
        with open(os.path.join(_HERE, "plugin.py"), encoding="utf-8") as f:
            return f.read().splitlines()

    def test_no_hardcoded_hosts_outside_presets(self):
        lines = self._source_lines()
        # PROVIDERS 字典范围（含其后的自定义预设）
        start = next(i for i, l in enumerate(lines) if l.startswith("PROVIDERS:"))
        end = next(i for i, l in enumerate(lines[start:], start) if l.strip() == "}")
        offenders = []
        for i, line in enumerate(lines):
            if start <= i <= end or line.lstrip().startswith("#"):
                continue
            low = line.lower()
            for lit in self.PROVIDER_LITERALS:
                # 邮箱地址样例（you@outlook.com / u@gmail.com 之类）不算硬编码
                if lit in low and "@" not in low:
                    offenders.append(f"{i+1}: {line.strip()}")
        self.assertEqual(offenders, [], "服务商字面量应只在 PROVIDERS 预设中：\n" +
                         "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()