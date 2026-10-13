# 纯逻辑单测：不连真实 SMTP/IMAP（组装/解析/审批/越权/未配置路径均可离线验证）。
# 运行：<主仓库 .venv python> -m unittest plugins.email-plugin.test_email_plugin
import asyncio
import os
import shutil
import sys
import importlib.util
import os
import tempfile
import time
import unittest
from email.message import EmailMessage
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "email_plugin_under_test", os.path.join(_HERE, "plugin.py"))
emailplugin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(emailplugin)  # type: ignore[union-attr]

# 私有凭据文件隔离：测试一律用内存替身，绝不读写真实 config.local.json
_PRIVATE_STUB: dict = {}
emailplugin._read_private = lambda: dict(_PRIVATE_STUB)
emailplugin._write_private = lambda data: (_PRIVATE_STUB.clear(),
                                           _PRIVATE_STUB.update(data), True)[2]


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
        _PRIVATE_STUB.clear()
        emailplugin._token_cache.update({"key": "", "access": "", "exp": 0.0})
        emailplugin._device_cache.clear()
        self.plugin = emailplugin.EmailPlugin()

    def tearDown(self):
        _PRIVATE_STUB.clear()
        emailplugin._token_cache.update({"key": "", "access": "", "exp": 0.0})
        emailplugin._device_cache.clear()


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
        # 默认 oauth2：显示授权按钮，不出现 basic 的密码字段与 client_id
        self.assertIn("email_oauth_authorize", keys)
        self.assertNotIn("email_oauth_client_id", keys)
        self.assertNotIn("email_password", keys)
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


class OAuthConfigGuardTest(EmailPluginBase):
    """OAuth 配置误填的防御：用户把邮箱填进「租户」是本项目真实踩过的坑。

    Azure 的回包是 `AADSTS900023: Specified tenant identifier '...' is neither
    a valid DNS name, nor a valid external domain` —— 完全不提哪个字段错了。
    这些用例锁住「发请求前就纠正 + 给出可读提示」的行为。
    """

    def _tenant(self, raw):
        return emailplugin._normalize_tenant(raw)

    def test_empty_tenant_falls_back_to_consumers(self):
        tenant, warn = self._tenant("")
        self.assertEqual(tenant, "consumers")
        self.assertEqual(warn, "")

    def test_email_as_tenant_is_corrected_with_readable_hint(self):
        tenant, warn = self._tenant("someone@hotmail.com")
        self.assertEqual(tenant, "consumers")
        # 提示要同时说清「错在哪」和「该怎么做」
        self.assertIn("邮箱", warn)
        self.assertIn("清空", warn)

    def test_url_as_tenant_is_corrected(self):
        tenant, warn = self._tenant("https://login.microsoftonline.com/x")
        self.assertEqual(tenant, "consumers")
        self.assertIn("网址", warn)

    def test_special_tenants_pass_through_lowercased(self):
        for val in ("consumers", "common", "organizations", "CONSUMERS"):
            tenant, warn = self._tenant(val)
            self.assertEqual(warn, "")
            self.assertEqual(tenant, val.lower())

    def test_org_domain_and_guid_pass_through(self):
        for val in ("contoso.onmicrosoft.com",
                    "72f988bf-86f1-41af-91ab-2d7cd011db47"):
            tenant, warn = self._tenant(val)
            self.assertEqual(tenant, val)
            self.assertEqual(warn, "")

    def test_oauth_conf_exposes_tenant_warning(self):
        """误填的 tenant 会体现在 _oauth_conf 结果里（供授权流程提前返回）。"""
        app = SimpleNamespace(config={"email-plugin": {
            "email_oauth_provider": "outlook",
            "email_oauth_tenant": "someone@hotmail.com",
        }})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["tenant"], "consumers")
        self.assertIn("邮箱", conf.get("tenant_warn", ""))
        # 端点里不能再出现邮箱
        self.assertNotIn("hotmail.com", conf["devicecode"])

    def test_oauth_conf_flags_non_guid_client_id(self):
        """Azure 的 client_id 必须是 GUID；填成任意字符串要提前拦下。"""
        app = SimpleNamespace(config={"email-plugin": {
            "email_oauth_provider": "outlook",
            "email_oauth_client_id": "lite-work",
        }})
        conf = emailplugin._oauth_conf(app)
        self.assertIn("GUID", conf.get("client_id_warn", ""))

    def test_oauth_conf_accepts_valid_guid_client_id(self):
        guid = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
        app = SimpleNamespace(config={"email-plugin": {
            "email_oauth_provider": "outlook",
            "email_oauth_client_id": guid,
        }})
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["client_id"], guid)
        self.assertEqual(conf.get("client_id_warn", ""), "")

    def test_device_start_returns_hint_instead_of_hitting_network(self):
        """配置层提示优先于网络请求：不拿一句 Azure 天书让用户猜字段。"""
        conf = emailplugin._oauth_conf(SimpleNamespace(config={"email-plugin": {
            "email_oauth_provider": "outlook",
            "email_oauth_tenant": "someone@hotmail.com",
        }}))
        payload, err = emailplugin._oauth_device_start(conf)
        self.assertEqual(payload, {})
        self.assertIn("邮箱", err)

    def test_azure_errors_are_translated(self):
        cases = [
            ("AADSTS900023: Specified tenant identifier 'a@b.com' is neither...", "租户"),
            ("AADSTS700016: Application with identifier 'x' was not found", "客户端 ID"),
            ("unauthorized_client: not enabled for consumers", "设备码"),
        ]
        for raw, must_contain in cases:
            msg = emailplugin._readable_oauth_error(raw)
            self.assertIn(must_contain, msg)
            # 原始报错保留，便于排查
            self.assertIn(raw[:20], msg)

    def test_unknown_azure_error_passes_through_with_prefix(self):
        msg = emailplugin._readable_oauth_error("something weird happened")
        self.assertIn("something weird happened", msg)

    def test_tenant_field_hint_warns_against_email(self):
        """表单提示要显式写「不能填邮箱」——本次故障就是提示不够明确。"""
        settings = self.plugin.contributes["settings"]
        tenant = next((s for s in settings if s["key"] == "email_oauth_tenant"), None)
        self.assertIsNotNone(tenant, "outlook 应展示租户字段（组织账号用）")
        self.assertIn("邮箱", tenant.get("hint", ""))
        self.assertIn("留空", tenant.get("hint", ""))
        # 占位符比 hint 更靠近输入框，要能提示「留空即个人账号」
        self.assertIn("留空", tenant.get("placeholder", ""))


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
        self.assertIn("email_auth", keys)
        self.assertIn("email_oauth_provider", keys)

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
    """XOAUTH2 首包必须是**原始串**（smtplib.auth / imaplib.authenticate 自动 base64）。

    v1.1.2 前这里预编码过一次 → 双重编码 → 服务器收到乱码：
    IMAP 报 BAD Command Argument Error. 12、SMTP 同样挂。曾被误诊为
    scope 缺失，实为编码 bug。此用例锁死修复。
    """

    def test_returns_raw_string_not_base64(self):
        out = emailplugin._xoauth2("u@x.com", "TOK")
        self.assertEqual(out, "user=u@x.com\x01auth=Bearer TOK\x01\x01")
        # 绝不能是 base64（历史 bug 形态）
        import base64 as _b
        try:
            _b.b64decode(out, validate=True)
            self.fail("_xoauth2 仍返回 base64（双重编码 bug 回归！）")
        except Exception:
            pass  # 正确：原始串不是合法 base64

    def test_raw_then_auto_encode_matches_expected(self):
        import base64 as _b
        raw = emailplugin._xoauth2("u@x.com", "TOK")
        auto = _b.b64encode(raw.encode()).decode()   # 库会对返回值做同样的事
        expected = _b.b64encode(b"user=u@x.com\x01auth=Bearer TOK\x01\x01").decode()
        self.assertEqual(auto, expected)



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
    def test_login_with_builtin_client_id_starts_flow(self):
        """内置 client_id：无需配置即可发起授权（设备码流程，stub 网络）。"""
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "outlook"}})
        orig_start = emailplugin._oauth_device_start
        orig_open = emailplugin._open_browser
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "UC", "expires_in": 900,
             "verification_uri": "https://microsoft.com/link"}, "")
        emailplugin._open_browser = lambda url: True
        emailplugin._device_cache.clear()
        try:
            out = asyncio.run(self.plugin._oauth_login({}))
        finally:
            emailplugin._oauth_device_start = orig_start
            emailplugin._open_browser = orig_open
            emailplugin._device_cache.clear()
        self.assertIn("已自动为你打开", out)
        self.assertIn("UC", out)

    def test_custom_provider_without_client_id_reports_actionable(self):
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "custom"}})
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


class DeviceFlowTest(EmailPluginBase):
    """device code 流程：状态机、退避、过期、异常响应。"""

    def setUp(self):
        super().setUp()
        emailplugin._device_cache.clear()
        self.app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "outlook", "email_oauth_client_id": "cid"}})
        self.plugin._app = self.app

    def tearDown(self):
        emailplugin._device_cache.clear()

    def _stub(self, poll=None, start=None):
        self._orig_poll = emailplugin._oauth_device_poll
        self._orig_start = emailplugin._oauth_device_start
        if poll is not None:
            emailplugin._oauth_device_poll = poll
        if start is not None:
            emailplugin._oauth_device_start = start

    def _restore(self):
        emailplugin._oauth_device_poll = self._orig_poll
        emailplugin._oauth_device_start = self._orig_start

    def _fast_sleep(self):
        """替换 asyncio.sleep + 推进假时钟：否则轮询按真实时间空转，测试要等满超时。"""
        self._orig_sleep = asyncio.sleep
        self._orig_time = emailplugin.time
        self._slept = []
        clock = {"t": self._orig_time.time()}

        async def fake_sleep(sec):
            self._slept.append(sec)
            clock["t"] += sec

        class _FakeTime:
            @staticmethod
            def time():
                return clock["t"]

        emailplugin.time = _FakeTime
        asyncio.sleep = fake_sleep

    def _restore_sleep(self):
        asyncio.sleep = self._orig_sleep
        emailplugin.time = self._orig_time

    def test_pending_then_authorized(self):
        calls = {"n": 0}

        def poll(conf, dc):
            calls["n"] += 1
            if calls["n"] < 2:
                return None, "", "pending"
            return {"access_token": "AT", "refresh_token": "RT"}, "", "done"

        self._fast_sleep()
        self._stub(poll=poll)
        try:
            emailplugin._device_cache["pending"] = {
                "device_code": "DC", "interval": 3, "expires_at": time.time() + 900}
            out = asyncio.run(self.plugin._oauth_login({"timeout": 20}))
        finally:
            self._restore()
            self._restore_sleep()
        self.assertIn("授权成功", out)
        self.assertEqual(self.app.config["email-plugin"]["email_oauth_refresh_token"], "RT")

    def test_pending_times_out_and_resumes(self):
        self._fast_sleep()
        self._stub(poll=lambda conf, dc: (None, "", "pending"))
        try:
            emailplugin._device_cache["pending"] = {
                "device_code": "DC", "interval": 3, "expires_at": time.time() + 900}
            out = asyncio.run(self.plugin._oauth_login({"timeout": 15}))
        finally:
            self._restore()
            self._restore_sleep()
        self.assertIn("仍在等待授权", out)
        self.assertIn("device_code", emailplugin._device_cache.get("pending", {}))
        self.assertTrue(self._slept, "超时期间应有轮询等待")

    def test_slow_down_backs_off_then_succeeds(self):
        """RFC 8628：收到 slow_down 要拉长间隔，而不是当成失败退出。"""
        seq = ["slow_down", "slow_down", "done"]

        def poll(conf, dc):
            s = seq.pop(0)
            if s == "done":
                return {"access_token": "AT", "refresh_token": "RT"}, "", "done"
            return None, "", s

        self._fast_sleep()
        self._stub(poll=poll)
        try:
            emailplugin._device_cache["pending"] = {
                "device_code": "DC", "interval": 3, "expires_at": time.time() + 900}
            out = asyncio.run(self.plugin._oauth_login({"timeout": 60}))
        finally:
            self._restore()
            self._restore_sleep()
        self.assertIn("授权成功", out)
        self.assertEqual(self._slept[:2], [8, 13], "slow_down 应逐次退避")

    def test_expired_token_reports_clearly(self):
        self._stub(poll=lambda conf, dc: (None, "设备码已过期，请重新发起 email_oauth_login 获取新代码。", "error"))
        try:
            emailplugin._device_cache["pending"] = {"device_code": "DC", "interval": 3,
                                                       "expires_at": time.time() + 900}
            out = asyncio.run(self.plugin._oauth_login({"timeout": 15}))
        finally:
            self._restore()
        self.assertIn("过期", out)

    def test_declined_reports_clearly(self):
        self._stub(poll=lambda conf, dc: (None, "用户拒绝了授权。", "error"))
        try:
            emailplugin._device_cache["pending"] = {"device_code": "DC", "interval": 3,
                                                       "expires_at": time.time() + 900}
            out = asyncio.run(self.plugin._oauth_login({"timeout": 15}))
        finally:
            self._restore()
        self.assertIn("拒绝", out)

    def test_expired_pending_is_discarded(self):
        """过期设备码不能继续用，应重新申请并给出新代码。"""
        self._stub(
            start=lambda conf: ({"device_code": "NEW", "user_code": "NEW-CODE",
                                 "verification_uri": "https://x/devicelogin",
                                 "expires_in": 900, "interval": 5}, ""))
        try:
            emailplugin._device_cache["pending"] = {
                "device_code": "OLD", "interval": 3, "expires_at": time.time() - 10}
            out = asyncio.run(self.plugin._oauth_login({"timeout": 15}))
        finally:
            self._restore()
        self.assertIn("NEW-CODE", out)
        self.assertNotIn("OLD", out)

    def test_start_shows_validity_window(self):
        self._stub(start=lambda conf: (
            {"device_code": "DC", "user_code": "UC", "expires_in": 900,
             "verification_uri": "https://x/devicelogin"}, ""))
        try:
            out = asyncio.run(self.plugin._oauth_login({}))
        finally:
            self._restore()
        self.assertIn("15 分钟内有效", out)


class DevicePollStatusTest(unittest.TestCase):
    """轮询状态解析：把各家 OAuth 的 error 码映射成明确状态。"""

    def setUp(self):
        self.conf = {"token": "https://t", "client_id": "cid", "client_secret": ""}
        self._orig = emailplugin._post_form

    def tearDown(self):
        emailplugin._post_form = self._orig

    def _poll(self, response):
        emailplugin._post_form = lambda url, data: response
        return emailplugin._oauth_device_poll(self.conf, "DC")

    def test_pending(self):
        self.assertEqual(self._poll((None, "authorization_pending: x"))[2], "pending")

    def test_slow_down(self):
        self.assertEqual(self._poll((None, "slow_down: x"))[2], "slow_down")

    def test_expired(self):
        p, err, st = self._poll((None, "expired_token: x"))
        self.assertEqual(st, "error")
        self.assertIn("过期", err)

    def test_declined(self):
        p, err, st = self._poll((None, "access_denied: x"))
        self.assertEqual(st, "error")
        self.assertIn("拒绝", err)

    def test_done_requires_access_token(self):
        """响应没有 access_token 不能当成成功。"""
        p, err, st = self._poll(({"refresh_token": "RT"}, ""))
        self.assertEqual(st, "error")
        self.assertIn("access_token", err)

    def test_done(self):
        p, err, st = self._poll(({"access_token": "AT", "refresh_token": "RT"}, ""))
        self.assertEqual(st, "done")
        self.assertEqual(p["access_token"], "AT")


class AuthCodeFlowTest(EmailPluginBase):
    """授权码流程：链接生成、code 换票、缺配置时的可读报错。"""

    def setUp(self):
        super().setUp()
        self.app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "gmail", "email_oauth_client_id": "cid"}})
        self.plugin._app = self.app

    def test_generates_authorize_url(self):
        out = asyncio.run(self.plugin._oauth_login({}))
        # 无 redirect_uri 配置时：auth_code 流程要么给出可操作指引，
        # 要么给出明确错误说明缺什么——不能是含糊失败。
        self.assertTrue(
            "accounts.google.com" in out or "回调地址" in out or "redirect" in out,
            f"输出既无授权链接也无明确指引: {out[:120]}")
        self.assertIn("client_id=cid", out)
        self.assertIn("access_type=offline", out)   # 缺它拿不到 refresh_token

    def test_code_exchange_saves_token(self):
        orig = emailplugin._post_form
        emailplugin._post_form = lambda url, data: (
            {"access_token": "AT", "refresh_token": "RT-G"}, "")
        try:
            out = asyncio.run(self.plugin._oauth_login({"code": "AUTH-CODE"}))
        finally:
            emailplugin._post_form = orig
        self.assertIn("授权成功", out)
        self.assertEqual(self.app.config["email-plugin"]["email_oauth_refresh_token"], "RT-G")

    def test_code_exchange_without_client_id_is_actionable(self):
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "custom"}})
        out = asyncio.run(self.plugin._oauth_login({"code": "X"}))
        self.assertIn("client_id", out)

    def test_manual_flow_tells_user_to_use_basic(self):
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "qq", "email_oauth_client_id": "cid"}})
        out = asyncio.run(self.plugin._oauth_login({}))
        self.assertIn("basic", out)
        self.assertIn("授权码", out)


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
        """密钥字段必须声明 secret（打码 + 前端跳过未改动）。"""
        self.plugin._app = _App(config={"email-plugin": {
            "email_auth": "basic", "email_user": "u@x.com"}})
        settings = self.plugin.contributes["settings"]
        pw = next(s for s in settings if s["key"] == "email_password")
        self.assertTrue(pw.get("secret") or pw.get("type") == "secret")

    def test_custom_provider_exposes_client_secret_as_secret(self):
        self.plugin._app = _App(config={"email-plugin": {
            "email_auth": "oauth2", "email_oauth_provider": "custom"}})
        settings = self.plugin.contributes["settings"]
        spec = next(s for s in settings if s["key"] == "email_oauth_client_secret")
        self.assertTrue(spec.get("secret") or spec.get("type") == "secret")

    def test_status_oauth2_unauthorized_then_running(self):
        st = self.plugin.status_from_config(
            {"email-plugin": {"email_user": "u@x.com", "email_auth": "oauth2",
                              "email_oauth_provider": "outlook"}})
        self.assertEqual(st["state"], "not_started")
        self.assertIn("授权", st["reason"])
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


class DynamicSchemaTest(EmailPluginBase):
    """动态设置表单：按认证方式/服务商裁剪字段，避免字段过多与逻辑矛盾。"""

    def _keys(self, cfg):
        self.plugin._app = _App(config={"email-plugin": cfg})
        return [s["key"] for s in self.plugin.contributes["settings"]]

    def test_oauth2_hides_password_field(self):
        keys = self._keys({"email_auth": "oauth2", "email_oauth_provider": "outlook"})
        self.assertNotIn("email_password", keys)
        self.assertIn("email_oauth_authorize", keys)      # 授权按钮在
        self.assertNotIn("email_oauth_client_id", keys)   # client_id 已内置，不再显示

    def test_basic_hides_oauth_endpoints(self):
        keys = self._keys({"email_auth": "basic"})
        self.assertIn("email_password", keys)
        self.assertNotIn("email_oauth_client_id", keys)
        self.assertNotIn("email_oauth_authorize", keys)

    def test_preset_provider_hides_manual_endpoints(self):
        """预设服务商已带出端点，不该再让用户手填。"""
        keys = self._keys({"email_auth": "oauth2", "email_oauth_provider": "outlook"})
        for k in ("email_oauth_token", "email_oauth_devicecode", "email_oauth_scope"):
            self.assertNotIn(k, keys)

    def test_custom_provider_shows_endpoints(self):
        keys = self._keys({"email_auth": "oauth2", "email_oauth_provider": "custom"})
        for k in ("email_oauth_token", "email_oauth_devicecode",
                  "email_oauth_authorize", "email_oauth_scope"):
            self.assertIn(k, keys)

    def test_custom_provider_hides_outlook_tenant(self):
        keys = self._keys({"email_auth": "oauth2", "email_oauth_provider": "custom"})
        self.assertNotIn("email_oauth_tenant", keys)

    def test_field_count_is_reasonable(self):
        """回归点：全平铺曾达 20 个字段，用户反馈过于复杂。"""
        oauth_keys = self._keys({"email_auth": "oauth2", "email_oauth_provider": "outlook"})
        basic_keys = self._keys({"email_auth": "basic"})
        self.assertLessEqual(len(oauth_keys), 8, f"oauth2 字段过多：{oauth_keys}")
        self.assertLessEqual(len(basic_keys), 11, f"basic 字段过多：{basic_keys}")

    def test_core_fields_always_present(self):
        for cfg in ({"email_auth": "oauth2"}, {"email_auth": "basic"}, {}):
            keys = self._keys(cfg)
            for k in ("email_auth", "email_oauth_provider", "email_user",
                      "email_require_approval"):
                self.assertIn(k, keys, f"{cfg} 缺 {k}")

    def test_reads_disk_config_when_app_missing(self):
        """宿主渲染设置时插件未绑定 app，必须能读磁盘配置决定字段集。"""
        import json as _json
        ws = tempfile.mkdtemp(prefix="lw-cfg-")
        cfg_dir = os.path.join(ws, "cfg")
        os.makedirs(os.path.join(cfg_dir, "plugins", "email-plugin"), exist_ok=True)
        with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as f:
            _json.dump({"email-plugin": {"email_auth": "basic"}}, f)
        orig = emailplugin._config_dir
        emailplugin._config_dir = lambda: cfg_dir
        emailplugin._DISK_CFG.update({"path": "", "mtime": 0.0, "data": {}})
        try:
            plug = emailplugin.EmailPlugin()      # 不设 _app
            keys = [s["key"] for s in plug.contributes["settings"]]
        finally:
            emailplugin._config_dir = orig
            emailplugin._DISK_CFG.update({"path": "", "mtime": 0.0, "data": {}})
            shutil.rmtree(ws, ignore_errors=True)
        self.assertIn("email_password", keys)
        self.assertNotIn("email_oauth_client_id", keys)


class OauthBrowserTest(EmailPluginBase):
    """授权时自动打开浏览器（用户期望『弹出网页 approve』）。"""

    def test_device_flow_opens_browser(self):
        opened = []
        orig_open = emailplugin._open_browser
        orig_start = emailplugin._oauth_device_start
        emailplugin._open_browser = lambda url: (opened.append(url), True)[1]
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "UC", "expires_in": 900,
             "verification_uri": "https://microsoft.com/devicelogin"}, "")
        emailplugin._device_cache.clear()
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "outlook", "email_oauth_client_id": "cid"}})
        try:
            out = asyncio.run(self.plugin._oauth_login({}))
        finally:
            emailplugin._open_browser = orig_open
            emailplugin._oauth_device_start = orig_start
            emailplugin._device_cache.clear()
        self.assertEqual(opened, ["https://microsoft.com/devicelogin"])
        self.assertIn("已自动为你打开", out)

    def test_open_failure_still_shows_link(self):
        orig_open = emailplugin._open_browser
        orig_start = emailplugin._oauth_device_start
        emailplugin._open_browser = lambda url: False
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "UC", "expires_in": 900,
             "verification_uri": "https://microsoft.com/devicelogin"}, "")
        emailplugin._device_cache.clear()
        self.plugin._app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "outlook", "email_oauth_client_id": "cid"}})
        try:
            out = asyncio.run(self.plugin._oauth_login({}))
        finally:
            emailplugin._open_browser = orig_open
            emailplugin._oauth_device_start = orig_start
            emailplugin._device_cache.clear()
        self.assertIn("未能自动打开", out)
        self.assertIn("https://microsoft.com/devicelogin", out)

    def test_open_browser_empty_url_is_safe(self):
        self.assertFalse(emailplugin._open_browser(""))


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


class SettingsActionTest(EmailPluginBase):
    """设置页「立即授权」按钮：同步发起 + 后台轮询 + 磁盘落令牌。"""

    def setUp(self):
        super().setUp()
        self.plugin._ACTION_STATE.clear()

    def tearDown(self):
        self.plugin._ACTION_STATE.clear()

    def _cfg(self, extra=None):
        return {"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "outlook", **(extra or {})}}

    def test_rejects_unknown_action_key(self):
        out = self.plugin.settings_action("whatever", self._cfg())
        self.assertFalse(out["ok"])

    def test_rejects_basic_mode(self):
        out = self.plugin.settings_action(
            "email_oauth_authorize", {"email-plugin": {
                "email_user": "u@x.com", "email_auth": "basic"}})
        self.assertFalse(out["ok"])
        self.assertIn("oauth2", out["message"])

    def test_rejects_missing_user(self):
        out = self.plugin.settings_action(
            "email_oauth_authorize", {"email-plugin": {
                "email_auth": "oauth2", "email_oauth_provider": "outlook"}})
        self.assertFalse(out["ok"])
        self.assertIn("邮箱账号", out["message"])

    def test_start_opens_browser_and_returns_code(self):
        opened = []
        orig_start, orig_open = emailplugin._oauth_device_start, emailplugin._open_browser
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "CODE99", "expires_in": 900,
             "interval": 1, "verification_uri": "https://microsoft.com/link"}, "")
        emailplugin._open_browser = lambda url: opened.append(url) or True
        try:
            out = self.plugin.settings_action("email_oauth_authorize", self._cfg())
        finally:
            emailplugin._oauth_device_start = orig_start
            emailplugin._open_browser = orig_open
            self.plugin._ACTION_STATE.clear()
        self.assertTrue(out["ok"])
        self.assertEqual(opened, ["https://microsoft.com/link"])
        self.assertIn("CODE99", out["message"])

    def test_running_state_shows_progress_message(self):
        self.plugin._ACTION_STATE["auth"] = {"running": True}
        out = self.plugin.settings_action("email_oauth_authorize", self._cfg())
        self.assertTrue(out["ok"])
        self.assertIn("进行中", out["message"])

    def test_running_state_repeats_code(self):
        """运行中重复点击必须**原样回显同一组码**。

        前端是覆盖式渲染这条消息：若再点时只回「进行中」，用户一旦点第二次
        就再也看不到码（实测反馈「码只出现一次、要点好几次」）。
        """
        self.plugin._ACTION_STATE["auth"] = {
            "running": True, "user_code": "CODE99",
            "verify": "https://microsoft.com/link"}
        out = self.plugin.settings_action("email_oauth_authorize", self._cfg())
        self.assertTrue(out["ok"])
        self.assertIn("CODE99", out["message"])
        self.assertIn("https://microsoft.com/link", out["message"])

    def test_start_records_code_for_later_clicks(self):
        """首次点击后状态里要留下码，供后续点击回显。"""
        orig_start, orig_open = emailplugin._oauth_device_start, emailplugin._open_browser
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "KEEP42", "expires_in": 900,
             "interval": 30, "verification_uri": "https://x/link"}, "")
        emailplugin._open_browser = lambda url: True
        try:
            first = self.plugin.settings_action("email_oauth_authorize", self._cfg())
            again = self.plugin.settings_action("email_oauth_authorize", self._cfg())
        finally:
            emailplugin._oauth_device_start = orig_start
            emailplugin._open_browser = orig_open
            self.plugin._ACTION_STATE.clear()
        self.assertIn("KEEP42", first["message"])
        self.assertIn("KEEP42", again["message"], "二次点击必须还能看到同一个码")

    def test_done_state_reports_success(self):
        self.plugin._ACTION_STATE["auth"] = {"running": False, "done": True}
        out = self.plugin.settings_action("email_oauth_authorize", self._cfg())
        self.assertTrue(out["ok"])
        self.assertIn("授权成功", out["message"])

    def test_poll_saves_token_to_disk(self):
        """后台轮询拿到令牌 → 磁盘写回 + 状态翻转（端到端，stub 网络）。"""
        import json as _json
        ws = tempfile.mkdtemp(prefix="lw-act-")
        cfg_dir = os.path.join(ws, "cfg")
        os.makedirs(os.path.join(cfg_dir, "plugins", "email-plugin"), exist_ok=True)
        cfg_path = os.path.join(cfg_dir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            _json.dump({"email-plugin": {"email_user": "u@x.com", "email_auth": "oauth2"},
                        "other-plugin": {"keep": 1}}, f)

        orig_dir = emailplugin._config_dir
        orig_start = emailplugin._oauth_device_start
        orig_poll = emailplugin._oauth_device_poll
        orig_open = emailplugin._open_browser
        emailplugin._config_dir = lambda: cfg_dir
        emailplugin._oauth_device_start = lambda conf: (
            {"device_code": "DC", "user_code": "UC", "expires_in": 300,
             "interval": 1, "verification_uri": "https://x"}, "")
        emailplugin._oauth_device_poll = lambda conf, dc: (
            {"access_token": "AT", "refresh_token": "RT-DISK"}, "", "done")
        emailplugin._open_browser = lambda url: True
        emailplugin._DISK_CFG.update({"path": "", "mtime": 0.0, "data": {}})
        try:
            out = self.plugin.settings_action("email_oauth_authorize",
                                              _json.load(open(cfg_path)))
            # 等后台线程完成（轮询立即 done）
            for _ in range(50):
                if self.plugin._ACTION_STATE.get("auth", {}).get("done"):
                    break
                time.sleep(0.05)
            with open(cfg_path, encoding="utf-8") as f:
                saved = _json.load(f)
            done_state = bool(self.plugin._ACTION_STATE.get("auth", {}).get("done"))
        finally:
            emailplugin._config_dir = orig_dir
            emailplugin._oauth_device_start = orig_start
            emailplugin._oauth_device_poll = orig_poll
            emailplugin._open_browser = orig_open
            emailplugin._DISK_CFG.update({"path": "", "mtime": 0.0, "data": {}})
            shutil.rmtree(ws, ignore_errors=True)
            self.plugin._ACTION_STATE.clear()
        self.assertTrue(out["ok"])
        self.assertTrue(done_state, "后台轮询应完成并翻转状态")
        self.assertEqual(saved["email-plugin"]["email_oauth_refresh_token"], "RT-DISK")
        self.assertEqual(saved["other-plugin"], {"keep": 1}, "兄弟插件配置不得被抹掉")

    def test_builtin_client_id_used_for_outlook(self):
        """outlook 内置 client_id：不填也能解析到公共客户端 ID。"""
        app = _App(config=self._cfg())
        conf = emailplugin._oauth_conf(app)
        self.assertEqual(conf["client_id"],
                         emailplugin.PUBLIC_CLIENT_IDS["outlook"])

class PrivateTokenStoreTest(unittest.TestCase):
    """私有凭据文件：宿主覆盖 config.json 竞态下的令牌兜底（真实文件 IO）。"""

    def test_write_and_read_roundtrip(self):
        """真实文件 IO（绕开模块级 stub，直测磁盘函数）。"""
        import json as _json
        ws = tempfile.mkdtemp(prefix="lw-priv-")
        path = os.path.join(ws, "config.local.json")

        def _real_write(data):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            return True

        def _real_read():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = _json.load(f)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}

        orig_w, orig_r = emailplugin._write_private, emailplugin._read_private
        emailplugin._write_private = _real_write
        emailplugin._read_private = _real_read
        try:
            self.assertTrue(_real_write({"refresh_token": "RT-X"}))
            self.assertEqual(_real_read(), {"refresh_token": "RT-X"})
            self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")
        finally:
            emailplugin._write_private = orig_w
            emailplugin._read_private = orig_r
            shutil.rmtree(ws, ignore_errors=True)

    def test_read_missing_returns_empty(self):
        orig_path = emailplugin._private_path
        emailplugin._private_path = lambda: "/nonexistent/dir/config.local.json"
        try:
            self.assertEqual(emailplugin._read_private(), {})
        finally:
            emailplugin._private_path = orig_path

    def test_private_token_wins_over_config(self):
        """私有文件的令牌优先于 config.json（后者会被宿主覆盖）。"""
        app = _App(config={"email-plugin": {
            "email_user": "u@x.com", "email_auth": "oauth2",
            "email_oauth_provider": "outlook",
            "email_oauth_refresh_token": "STALE-IN-CONFIG"}})
        stub = {"refresh_token": "FRESH-PRIVATE"}
        orig = emailplugin._read_private
        emailplugin._read_private = lambda: stub
        try:
            conf = emailplugin._oauth_conf(app)
        finally:
            emailplugin._read_private = orig
        self.assertEqual(conf["refresh_token"], "FRESH-PRIVATE")

    def test_persist_writes_both_stores(self):
        """_persist_refresh_token：私有文件为主，config.json 为副本。"""
        app = _App(config={"email-plugin": {"email_user": "u@x.com"}})
        ok = emailplugin._persist_refresh_token(app, "RT-BOTH")
        self.assertTrue(ok)
        self.assertEqual(_PRIVATE_STUB.get("refresh_token"), "RT-BOTH")
        self.assertEqual(app.config["email-plugin"]["email_oauth_refresh_token"], "RT-BOTH")
        _PRIVATE_STUB.clear()

    def setUp(self):
        _PRIVATE_STUB.clear()

    def tearDown(self):
        _PRIVATE_STUB.clear()

if __name__ == "__main__":
    unittest.main()
