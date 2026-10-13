# lite-work 社区邮件插件：SMTP 发信（HTML / 附件）＋ IMAP 收件箱 / 搜索 / 读单封。
#
# 认证：email_auth 二选一——basic（密码/应用密码）或 oauth2（SASL XOAUTH2 通用
# 实现，预设 outlook / gmail，亦可自定义端点适配任意标准 OAuth2 服务）。
# Outlook 个人版已停用基本认证，只能用 oauth2。
#
# 约束：纯标准库（httpx 可选加速）；冻结宿主可能没捆绑 imaplib，按需回退 _vendor 副本；
# email_send 发送前必过 app.approval_gate；凭据不进日志与返回值；附件限 workspace 内。
# 格式遵循社区 AGENTS.md：ToolPlugin 子类、无参构造、install(kernel) 捕获 app、
# version 与 manifest.json 同步。MIT 许可（跟随本仓库）。
from __future__ import annotations

import asyncio
import base64
import html as _html
import importlib.util
import json
import logging
import os
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Tuple

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

logger = logging.getLogger("litework.email")

DEFAULT_SMTP_PORT = 587
DEFAULT_IMAP_PORT = 993
MAX_BODY_CHARS = 8000
MAX_LIST_ITEMS = 50
DEFAULT_LIST_ITEMS = 20
MAX_ATTACHMENTS = 8
_MAX_ATTACH_BYTES = 20 * 1024 * 1024

DEFAULT_SMTP_TLS = "starttls"
SMTP_TLS_STARTTLS = "starttls"
SMTP_TLS_SSL = "ssl"

AUTH_MODE_BASIC = "basic"
AUTH_MODE_OAUTH2 = "oauth2"
DEFAULT_AUTH_MODE = AUTH_MODE_OAUTH2

OAUTH_FLOW_DEVICE = "device_code"
OAUTH_FLOW_AUTHCODE = "auth_code"
OAUTH_FLOW_MANUAL = "manual"

DEFAULT_TENANT = "consumers"

#: 邮件服务商预设：同时给出 OAuth2 端点与 SMTP/IMAP 服务器参数，选一处即可全通。
#: 端点是模板（{tenant} 占位）；scope 为默认值。所有字段都可被设置页显式填写的值覆盖。
#: smtp_tls：starttls=587 类（明文起、STARTTLS 升级）；ssl=465 类（连接即 TLS）。
#: custom 预设为空，供自建 / 未收录服务自行填写。
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "outlook": {
        "authorize": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "devicecode": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
        "scope": "offline_access https://outlook.office.com/SMTP.Send "
                 "https://outlook.office.com/IMAP.AccessAsUser.All",
        "flow": "device_code",
        "smtp_host": "smtp-mail.outlook.com", "smtp_port": 587,
        "smtp_tls": SMTP_TLS_STARTTLS,
        "imap_host": "outlook.office365.com", "imap_port": 993,
    },
    "gmail": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "devicecode": "https://oauth2.googleapis.com/device/code",
        "scope": "https://mail.google.com/",
        "flow": "auth_code",
        "smtp_host": "smtp.gmail.com", "smtp_port": 465,
        "smtp_tls": SMTP_TLS_SSL,
        "imap_host": "imap.gmail.com", "imap_port": 993,
    },
    "fastmail": {
        "authorize": "https://api.fastmail.com/oauth/authorize",
        "token": "https://api.fastmail.com/oauth/refresh",
        "devicecode": "",
        "scope": "https://www.fastmail.com/dev/maskedemail%20offline_access",
        "flow": "auth_code",
        "smtp_host": "smtp.fastmail.com", "smtp_port": 465,
        "smtp_tls": SMTP_TLS_SSL,
        "imap_host": "imap.fastmail.com", "imap_port": 993,
    },
    "zoho": {
        "authorize": "https://accounts.zoho.com/oauth/v2/auth",
        "token": "https://accounts.zoho.com/oauth/v2/token",
        "devicecode": "",
        "scope": "ZohoMail.messages.ALL ZohoMail.accounts.READ",
        "flow": "auth_code",
        "smtp_host": "smtp.zoho.com", "smtp_port": 465,
        "smtp_tls": SMTP_TLS_SSL,
        "imap_host": "imap.zoho.com", "imap_port": 993,
    },
    "qq": {
        "authorize": "", "token": "", "devicecode": "", "scope": "",
        "flow": "manual",
        "smtp_host": "smtp.qq.com", "smtp_port": 465,
        "smtp_tls": SMTP_TLS_SSL,
        "imap_host": "imap.qq.com", "imap_port": 993,
    },
    "163": {
        "authorize": "", "token": "", "devicecode": "", "scope": "",
        "flow": "manual",
        "smtp_host": "smtp.163.com", "smtp_port": 465,
        "smtp_tls": SMTP_TLS_SSL,
        "imap_host": "imap.163.com", "imap_port": 993,
    },
    "custom": {
        "authorize": "", "token": "", "devicecode": "", "scope": "",
        "flow": "device_code",
        "smtp_host": "", "smtp_port": 587, "smtp_tls": SMTP_TLS_STARTTLS,
        "imap_host": "", "imap_port": 993,
    },
}

DEFAULT_PROVIDER = "outlook"

_HTTP_TIMEOUT = 30
_TOKEN_EXPIRY_SKEW = 120


#: 磁盘配置缓存：设置表单渲染时插件实例还没绑定 app，只能读 config.json
_DISK_CFG: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}

#: IMAP 客户端模块缓存：None=未解析 / False=不可用 / 模块对象=已就绪
_IMAPLIB: Any = None
_IMAPLIB_ERR = ""


def _load_imaplib():
    """取 IMAP 客户端模块：优先标准库，缺失时回退插件自带副本（_vendor/）。

    PyInstaller 冻结宿主只静态分析主程序 import，运行时才加载的插件 import
    看不到——imaplib 因此可能缺席（smtplib 恰好被传递依赖捎带进包）。回退走
    **显式按路径加载**，不把 _vendor 加进 sys.path：imaplib 是通用模块名，
    进 sys.path 会覆盖整个进程的标准库，影响面远超本插件。
    """
    global _IMAPLIB, _IMAPLIB_ERR
    if _IMAPLIB is not None:
        if _IMAPLIB is False:
            raise ImportError(_IMAPLIB_ERR)
        return _IMAPLIB
    try:
        import imaplib as _mod
        _IMAPLIB = _mod
        return _mod
    except ImportError:
        pass
    vendored = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_vendor", "imaplib.py")
    if not os.path.isfile(vendored):
        _IMAPLIB, _IMAPLIB_ERR = False, "宿主未捆绑 imaplib，插件自带副本缺失"
        raise ImportError(_IMAPLIB_ERR)
    spec = importlib.util.spec_from_file_location("litework_email_imaplib", vendored)
    if spec is None or spec.loader is None:
        _IMAPLIB, _IMAPLIB_ERR = False, f"自带 imaplib 无法加载：{vendored}"
        raise ImportError(_IMAPLIB_ERR)
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        _IMAPLIB, _IMAPLIB_ERR = False, f"自带 imaplib 加载失败：{exc}"
        raise ImportError(_IMAPLIB_ERR)
    _IMAPLIB = mod
    return mod


def _own_cfg(app) -> Dict[str, Any]:
    """取插件配置命名空间整块（app.config["email-plugin"]）。"""
    if app is None:
        return {}
    cfg = getattr(app, "config", None) or {}
    own = cfg.get("email-plugin") if isinstance(cfg, dict) else None
    return own if isinstance(own, dict) else {}


def _read_disk_cfg() -> Dict[str, Any]:
    """从磁盘读本插件配置（按 mtime 缓存）。

    渲染设置表单时插件实例尚未绑定 app（宿主 load_plugins 只实例化、不 install），
    拿不到内存配置。只能读 config.json —— 设置表单要按当前认证方式展示不同字段。
    """
    global _DISK_CFG
    path = os.path.join(_config_dir(), "config.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _DISK_CFG["path"] == path and _DISK_CFG["mtime"] == mtime:
        return _DISK_CFG["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        own = data.get("email-plugin") if isinstance(data, dict) else None
        own = own if isinstance(own, dict) else {}
    except Exception:  # noqa: BLE001
        own = {}
    _DISK_CFG.update({"path": path, "mtime": mtime, "data": own})
    return own


def _config_dir() -> str:
    """推断 lite-work 配置目录（插件在 <config_dir>/plugins/<name>/）。"""
    env = os.environ.get("LITEWORK_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


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


def _resolve_from_addr(app, user: str) -> str:
    """解析发件人：填了邮箱就用（可含显示名），否则用账号；两者都无则兜底账号。

    email_from 允许两种写法——纯邮箱，或 `姓名 <邮箱>`。只填姓名（如 "Layne
    Peng"）时不能当发件人地址，否则 From 头里没有地址、SMTP 会拒发。
    """
    raw = str(_cfg(app, "email_from", "") or "").strip()
    if raw:
        name, addr = parseaddr(raw)
        if addr and "@" in addr:      # 含可用的邮箱地址
            return f"{name} <{addr}>" if name else addr
        if "@" in raw:                # 形如裸邮箱但 parseaddr 没拆出（容错）
            return raw
    return user


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


# ---------------------------------------------------------------- OAuth2 通用实现

_oauth_lock = threading.Lock()
_token_cache: Dict[str, Any] = {"key": "", "access": "", "exp": 0.0}
_device_cache: Dict[str, Any] = {}


def _oauth_conf(app) -> Dict[str, Any]:
    """汇总配置：服务商预设 + 用户显式填写值（填了就用填的，能覆盖一切预设）。"""
    provider = str(_cfg(app, "email_oauth_provider", DEFAULT_PROVIDER) or "").strip().lower()
    preset = PROVIDERS.get(provider) or PROVIDERS["custom"]
    tenant = str(_cfg(app, "email_oauth_tenant", DEFAULT_TENANT) or DEFAULT_TENANT).strip()

    def preset_str(key: str) -> str:
        return str(preset.get(key) or "").replace("{tenant}", tenant)

    def pick(preset_value: Any, cfg_key: str) -> str:
        return str(_cfg(app, cfg_key, "") or "").strip() or str(preset_value or "")

    def pick_int(preset_value: Any, cfg_key: str) -> Any:
        raw = str(_cfg(app, cfg_key, "") or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
        return preset_value

    smtp_tls = str(_cfg(app, "email_smtp_tls", "") or "").strip().lower()
    if smtp_tls not in (SMTP_TLS_STARTTLS, SMTP_TLS_SSL):
        preset_tls = str(preset.get("smtp_tls") or DEFAULT_SMTP_TLS).lower()
        smtp_tls = preset_tls if preset_tls in (SMTP_TLS_STARTTLS, SMTP_TLS_SSL) else DEFAULT_SMTP_TLS
    return {
        "provider": provider,
        "tenant": tenant,
        "authorize": pick(preset_str("authorize"), "email_oauth_authorize"),
        "token": pick(preset_str("token"), "email_oauth_token"),
        "devicecode": pick(preset_str("devicecode"), "email_oauth_devicecode"),
        "scope": pick(preset_str("scope"), "email_oauth_scope"),
        "flow": pick(preset.get("flow") or OAUTH_FLOW_DEVICE, "email_oauth_flow"),
        "client_id": str(_cfg(app, "email_oauth_client_id", "") or "").strip(),
        "client_secret": str(_cfg(app, "email_oauth_client_secret", "") or "").strip(),
        "refresh_token": str(_cfg(app, "email_oauth_refresh_token", "") or "").strip(),
        "redirect_uri": str(_cfg(app, "email_oauth_redirect_uri", "") or "").strip(),
        "smtp_host": pick(preset.get("smtp_host"), "email_smtp_host"),
        "smtp_port": pick_int(preset.get("smtp_port") or DEFAULT_SMTP_PORT, "email_smtp_port"),
        "smtp_tls": smtp_tls,
        "imap_host": pick(preset.get("imap_host"), "email_imap_host"),
        "imap_port": pick_int(preset.get("imap_port") or DEFAULT_IMAP_PORT, "email_imap_port"),
    }


def _parse_json(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = json.loads(text)
    except Exception:
        return None, f"响应不是 JSON：{text[:200]}"
    if not isinstance(data, dict):
        return None, f"响应格式异常：{text[:200]}"
    if data.get("error"):
        desc = str(data.get("error_description") or data.get("error"))[:300]
        return None, f"{data.get('error')}: {desc}"
    return data, ""


def _post_form(url: str, data: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], str]:
    """POST 表单换票：httpx 优先（宿主保证捆绑），缺失回退 urllib。"""
    if not url:
        return None, "OAuth2 端点未配置"
    body = urllib.parse.urlencode(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None:
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(url, content=body, headers=headers)
            return _parse_json(resp.text)
        except Exception as exc:
            return None, f"网络错误：{type(exc).__name__}: {exc}"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return _parse_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return _parse_json(exc.read().decode("utf-8", "replace"))
    except Exception as exc:
        return None, f"网络错误：{type(exc).__name__}: {exc}"


def _xoauth2(user: str, token: str) -> str:
    """SASL XOAUTH2 首包：base64("user=" + user + ^A + "auth=Bearer " + token + ^A^A)。"""
    return base64.b64encode(
        f"user={user}\x01auth=Bearer {token}\x01\x01".encode("utf-8")).decode("ascii")


def _open_browser(url: str) -> bool:
    """尽力打开系统浏览器（跨平台）；失败不影响授权流程本身。

    优先 webbrowser 标准库，异常时按平台回退系统命令。冻结宿主里子进程执行
    系统命令比 Python GUI 调用更可靠，故两条路都留。
    """
    if not url:
        return False
    try:
        import webbrowser
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001
        logger.debug("[email] webbrowser 打开失败", exc_info=True)
    cmd = {"darwin": ["open"], "win32": ["cmd", "/c", "start", ""]}.get(
        sys.platform, ["xdg-open"])
    try:
        import subprocess
        subprocess.Popen(cmd + [url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[email] 系统命令打开浏览器失败", exc_info=True)
        return False


def _oauth_refresh(conf: Dict[str, Any]) -> Tuple[str, int, str]:
    """用 refresh_token 换 access_token（refresh_token 长期有效，自动续期）。

    返回 (access_token, 有效秒数, 错误)。
    """
    if not (conf["token"] and conf["client_id"] and conf["refresh_token"]):
        return "", 0, "OAuth2 配置不完整：需 token 端点 / client_id / refresh_token"
    data = {"grant_type": "refresh_token", "client_id": conf["client_id"],
            "refresh_token": conf["refresh_token"]}
    if conf["scope"]:
        data["scope"] = conf["scope"]
    if conf["client_secret"]:
        data["client_secret"] = conf["client_secret"]
    payload, err = _post_form(conf["token"], data)
    if err:
        return "", 0, f"刷新 access_token 失败：{err}"
    token = str((payload or {}).get("access_token") or "")
    if not token:
        return "", 0, "刷新失败：响应中没有 access_token"
    try:
        ttl = int((payload or {}).get("expires_in") or 3600)
    except (TypeError, ValueError):
        ttl = 3600
    return token, ttl, ""


def _access_token(app) -> Tuple[str, str]:
    """取可用 access_token（进程内缓存，临近过期自动刷新）。"""
    conf = _oauth_conf(app)
    key = "|".join([conf["provider"], conf["client_id"], conf["token"],
                    conf["refresh_token"][-12:]])
    now = time.time()
    with _oauth_lock:
        if _token_cache["key"] == key and _token_cache["access"] and _token_cache["exp"] > now:
            return str(_token_cache["access"]), ""
    token, ttl, err = _oauth_refresh(conf)
    if err:
        return "", err
    with _oauth_lock:
        _token_cache.update({"key": key, "access": token,
                             "exp": now + max(ttl - _TOKEN_EXPIRY_SKEW, 60)})
    return token, ""


def _oauth_device_start(conf: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """device code 流程第一步：申请设备码，返回 user_code / verification_uri。"""
    if not conf["devicecode"] or not conf["client_id"]:
        return {}, "device code 流程需要 devicecode 端点与 client_id"
    data = {"client_id": conf["client_id"]}
    if conf["scope"]:
        data["scope"] = conf["scope"]
    payload, err = _post_form(conf["devicecode"], data)
    if err:
        return {}, f"申请设备码失败：{err}"
    return payload or {}, ""


def _oauth_device_poll(conf: Dict[str, Any],
                       device_code: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """device code 流程第二步：单次轮询。

    返回 (payload, 错误, 状态)；状态取值 done / pending / slow_down / error。
    slow_down 由调用方按 RFC 8628 拉长轮询间隔，否则服务端会持续拒绝。
    """
    data = {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": conf["client_id"], "device_code": device_code}
    if conf["client_secret"]:
        data["client_secret"] = conf["client_secret"]
    payload, err = _post_form(conf["token"], data)
    if err:
        low = err.lower()
        if "authorization_pending" in low:
            return None, "", "pending"
        if "slow_down" in low:
            return None, "", "slow_down"
        if "expired_token" in low:
            return None, "设备码已过期，请重新发起 email_oauth_login 获取新代码。", "error"
        if "declined" in low or "access_denied" in low:
            return None, "用户拒绝了授权。", "error"
        return None, err, "error"
    if not (payload or {}).get("access_token"):
        return None, "换票响应缺少 access_token。", "error"
    return payload, "", "done"


def _persist_refresh_token(app, token: str) -> bool:
    """把 refresh_token 写回插件配置（凭据由设置页 schema 声明为 secret）。"""
    try:
        cur = dict((getattr(app, "config", {}) or {}).get("email-plugin") or {})
        cur["email_oauth_refresh_token"] = token
        app.save_config({"email-plugin": cur})
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[email] refresh_token 写回失败", exc_info=True)
        return False


def _resolve_auth_mode(own: Dict[str, Any]) -> str:
    """认证方式：显式配置 > 已有凭据推断 > 默认值。

    表单里「认证方式」的默认值是 oauth2，此处必须一致，否则新用户看到 oauth2
    的字段却按 basic 发信。旧配置只填了密码（无 email_auth）时判为 basic，
    避免升级后突然改用 OAuth2 而发不出信。
    """
    mode = str(own.get("email_auth") or "").strip().lower()
    if mode in (AUTH_MODE_BASIC, AUTH_MODE_OAUTH2):
        return mode
    if str(own.get("email_oauth_refresh_token") or "").strip():
        return AUTH_MODE_OAUTH2
    if str(own.get("email_oauth_client_id") or "").strip():
        return AUTH_MODE_OAUTH2
    if str(own.get("email_password") or "").strip():
        return AUTH_MODE_BASIC
    return DEFAULT_AUTH_MODE


def _auth_material(app) -> Tuple[str, str, str, str]:
    """统一取认证材料：(用户, 密钥, 模式, 错误)。

    basic 模式的密钥是密码；oauth2 模式是 access_token（对上层透明）。
    """
    own = _own_cfg(app)
    user = str(_cfg(app, "email_user", "") or "").strip()
    if not user:
        return "", "", "", "邮箱账号未配置：请在设置页 → 插件 → email-plugin 填写。"
    mode = _resolve_auth_mode(own)
    if mode == AUTH_MODE_OAUTH2:
        token, err = _access_token(app)
        if err:
            return "", "", mode, err
        return user, token, mode, ""
    password = str(_cfg(app, "email_password", "") or "").strip()
    if not password:
        return "", "", mode, "密码未配置：basic 模式需填密码 / 应用密码。"
    return user, password, mode, ""


def _smtp_login(smtp, user: str, secret: str, auth_mode: str) -> None:
    """SMTP 登录：basic 走 LOGIN，oauth2 走 SASL XOAUTH2。"""
    if auth_mode == AUTH_MODE_OAUTH2:
        payload = _xoauth2(user, secret)
        smtp.auth("XOAUTH2", lambda challenge=None: payload)
    else:
        smtp.login(user, secret)


def _imap_login(conn, user: str, secret: str, auth_mode: str) -> None:
    """IMAP 登录：basic 走 LOGIN，oauth2 走 AUTHENTICATE XOAUTH2。"""
    if auth_mode == AUTH_MODE_OAUTH2:
        payload = _xoauth2(user, secret)
        conn.authenticate("XOAUTH2", lambda challenge=None: payload)
    else:
        conn.login(user, secret)


def _smtp_send(smtp_host: str, smtp_port: int, user: str, secret: str,
               from_addr: str, msg: EmailMessage,
               auth_mode: str = AUTH_MODE_BASIC,
               smtp_tls: str = SMTP_TLS_STARTTLS) -> None:
    """同步 SMTP 发送（包进 to_thread）；证书校验走默认 context。

    smtp_tls=starttls（587 类）：明文连接后 STARTTLS 升级；ssl（465 类）：连接即 TLS。
    """
    port = int(smtp_port)
    if smtp_tls == SMTP_TLS_SSL or port == 465:
        with smtplib.SMTP_SSL(smtp_host, port, timeout=30) as smtp:
            _smtp_login(smtp, user, secret, auth_mode)
            smtp.send_message(msg, from_addr=from_addr)
        return
    with smtplib.SMTP(smtp_host, port, timeout=30) as smtp:
        smtp.starttls()
        _smtp_login(smtp, user, secret, auth_mode)
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
    user, secret, mode, err = _auth_material(app)
    if err:
        return [], err
    conf = _oauth_conf(app)
    host, port = conf["imap_host"], conf["imap_port"]
    try:
        imaplib = _load_imaplib()
    except ImportError as exc:
        return [], f"IMAP 不可用：{exc}"
    try:
        import email.parser as _ep
        with imaplib.IMAP4_SSL(host, port, timeout=30) as conn:
            _imap_login(conn, user, secret, mode)
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
            return [], f"IMAP 认证失败（{host}:{port}）：检查账号密码或 OAuth2 授权。"
        return [], f"IMAP 错误：{err}"
    except Exception as e:
        return [], f"IMAP 连接失败（{host}:{port}）：{e}"


def _imap_read(app, mailbox: str, uid: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """同步 IMAP：UID FETCH 完整邮件，解析正文（plain 优先，html 剥离标签）。"""
    user, secret, mode, err = _auth_material(app)
    if err:
        return None, err
    conf = _oauth_conf(app)
    host, port = conf["imap_host"], conf["imap_port"]
    try:
        imaplib = _load_imaplib()
    except ImportError as exc:
        return None, f"IMAP 不可用：{exc}"
    try:
        from email.parser import BytesParser
        with imaplib.IMAP4_SSL(host, port, timeout=30) as conn:
            _imap_login(conn, user, secret, mode)
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
    version = "1.1.0"
    description = (
        "邮件：SMTP 发信（支持 HTML 排版与附件，发送前需用户在审批卡确认）＋ "
        "IMAP 收件箱列表 / 关键词搜索 / 读取单封。认证支持 basic（密码/应用密码）"
        "与 oauth2（SASL XOAUTH2 通用实现；预设 Outlook/Gmail/Fastmail/Zoho/QQ/163，"
        "可自定义端点）；服务器参数随服务商预设自动带出，亦可手填。"
    )

    #: 通用插件 UI 协议。表单按数组顺序平铺渲染，宿主不支持条件显隐，因此这里
    #: 用 @property 按**当前认证方式**动态裁剪字段：oauth2 不显示密码、basic 不显示
    #: 端点；端点/服务器只在「服务商=custom」或 basic 下才出现，避免 20 个字段全平铺。
    #: 实例未绑定 app 时（宿主只实例化不 install）读磁盘 config.json 取当前值。
    @property
    def contributes(self) -> Dict[str, Any]:
        own = _own_cfg(self._app) or _read_disk_cfg()
        mode = _resolve_auth_mode(own)
        provider = str(own.get("email_oauth_provider") or DEFAULT_PROVIDER).strip().lower()
        custom = provider == "custom"

        items: List[Dict[str, Any]] = [
            {"key": "email_auth", "type": "select", "label": "认证方式",
             "options": [AUTH_MODE_OAUTH2, AUTH_MODE_BASIC], "default": DEFAULT_AUTH_MODE,
             "hint": "oauth2＝授权登录（Outlook/Gmail 等已停用密码的服务必选）；"
                     "basic＝用户名+密码/应用密码"},
            {"key": "email_oauth_provider", "type": "select", "label": "服务商",
             "options": sorted(PROVIDERS.keys()), "default": DEFAULT_PROVIDER,
             "hint": "服务器与端点按服务商自动套用；选 custom 才需手填"},
            {"key": "email_user", "type": "str", "label": "邮箱账号",
             "hint": "完整邮箱地址"},
        ]

        if mode == AUTH_MODE_OAUTH2:
            items.append({"key": "email_oauth_client_id", "type": "str",
                          "label": "客户端 ID",
                          "hint": "服务商后台注册应用后获得；填好后运行 email_oauth_login 授权"})
            if provider == "outlook":
                items.append({"key": "email_oauth_tenant", "type": "str",
                              "label": "租户（Outlook）", "default": DEFAULT_TENANT,
                              "hint": "consumers＝个人账号，common＝个人+组织"})
            if custom:
                items += [
                    {"key": "email_oauth_authorize", "type": "str", "label": "授权端点"},
                    {"key": "email_oauth_token", "type": "str", "label": "Token 端点"},
                    {"key": "email_oauth_devicecode", "type": "str", "label": "设备码端点"},
                    {"key": "email_oauth_redirect_uri", "type": "str", "label": "回调地址"},
                    {"key": "email_oauth_scope", "type": "str", "label": "Scope"},
                    {"key": "email_oauth_client_secret", "type": "secret",
                     "label": "客户端密钥（可选）",
                     "hint": "公共客户端留空；机密客户端需填"},
                ]
        else:
            items.append({"key": "email_password", "type": "secret",
                          "label": "密码 / 应用密码",
                          "hint": "多数服务商需先在邮箱设置里开启 SMTP/IMAP 并生成授权码"})

        # 服务器参数：预设已带出，仅 basic 或 custom 需要（此处暴露）
        if mode == AUTH_MODE_BASIC or custom:
            items += [
                {"key": "email_smtp_host", "type": "str", "label": "SMTP 服务器"},
                {"key": "email_smtp_port", "type": "number", "label": "SMTP 端口",
                 "default": DEFAULT_SMTP_PORT},
                {"key": "email_smtp_tls", "type": "select", "label": "SMTP 加密",
                 "options": [SMTP_TLS_STARTTLS, SMTP_TLS_SSL], "default": SMTP_TLS_STARTTLS,
                 "hint": "starttls＝587；ssl＝465"},
                {"key": "email_imap_host", "type": "str", "label": "IMAP 服务器"},
                {"key": "email_imap_port", "type": "number", "label": "IMAP 端口",
                 "default": DEFAULT_IMAP_PORT},
            ]

        items += [
            {"key": "email_from", "type": "str", "label": "发件人地址（可选）",
             "hint": "留空则用邮箱账号"},
            {"key": "email_require_approval", "type": "boolean",
             "label": "发送前需审批（推荐开启）", "default": True,
             "hint": "每次 email_send 弹审批卡，用户确认后才发出"},
        ]
        return {"settings": items}

    def status_from_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """设置页显示的运行状态：按认证方式判断凭据是否齐备。"""
        own = cfg.get(self.name)
        own = own if isinstance(own, dict) else {}
        user = str(own.get("email_user") or "").strip()
        if not user:
            return {"state": "not_started",
                    "reason": "邮箱账号未配置（设置页 → 插件 → email-plugin）"}
        mode = _resolve_auth_mode(own)
        if mode == AUTH_MODE_OAUTH2:
            if not str(own.get("email_oauth_client_id") or "").strip():
                return {"state": "not_started", "reason": "OAuth2 客户端 ID 未配置"}
            if not str(own.get("email_oauth_refresh_token") or "").strip():
                return {"state": "not_started",
                        "reason": "尚未授权（运行 email_oauth_login 完成一次授权）"}
            return {"state": "running"}
        if not str(own.get("email_password") or "").strip():
            return {"state": "not_started", "reason": "basic 模式未填密码 / 应用密码"}
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
            ToolDefinition(
                name="email_oauth_login",
                description=(
                    "OAuth2 授权登录（一次即可，之后自动续期）。device code 流程会返回"
                    "一段代码与网址，用户在浏览器完成授权后本工具自动轮询并保存 refresh_token；"
                    "授权码流程传入 code 换取令牌。仅 oauth2 认证方式需要。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "flow": {"type": "string",
                                 "enum": ["device_code", "auth_code"],
                                 "description": "授权流程（默认取服务商预设）"},
                        "device_code": {"type": "string",
                                        "description": "续领令牌时用（一般无需传，插件会记住待授权的设备码）"},
                        "timeout": {"type": "integer",
                                    "description": "device code 轮询等待秒数（默认 120，上限 300）"},
                        "code": {"type": "string",
                                 "description": "授权码流程：用户在浏览器授权后拿到的 code"},
                    },
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
        if name == "email_oauth_login":
            return await self._oauth_login(args)
        return f"[Error]: 未知工具 {name}"

    # ------------------------------------------------------------ OAuth2 授权

    def _save_token(self, payload: Dict[str, Any]) -> str:
        """保存 refresh_token 到配置，并清空 access_token 缓存。"""
        rt = str(payload.get("refresh_token") or "")
        if not rt:
            return "[Error]: 授权响应中没有 refresh_token（scope 需含 offline_access）。"
        if not _persist_refresh_token(self._app, rt):
            return "[Error]: 授权成功但 refresh_token 写回配置失败，请检查配置写入权限。"
        with _oauth_lock:
            _token_cache.update({"key": "", "access": "", "exp": 0.0})
        return (f"[Email OK]: OAuth2 授权成功，refresh_token 已保存（{len(rt)} 字符）。"
                "后续发信/收信将自动续期，无需重复授权。")

    async def _oauth_login(self, args: Dict[str, Any]) -> str:
        conf = _oauth_conf(self._app)
        flow = str(args.get("flow") or conf["flow"] or OAUTH_FLOW_DEVICE)

        # 授权码流程：用户已拿到 code → 直接换令牌
        code = str(args.get("code") or "").strip()
        if code:
            if not conf["client_id"] or not conf["token"]:
                return ("[Error]: 换令牌需要 client_id 与 token 端点，请先在设置页 → "
                        "插件 → email-plugin 补齐。")
            data = {"grant_type": "authorization_code", "client_id": conf["client_id"],
                    "code": code}
            if conf["redirect_uri"]:
                data["redirect_uri"] = conf["redirect_uri"]
            if conf["client_secret"]:
                data["client_secret"] = conf["client_secret"]
            payload, err = await asyncio.to_thread(_post_form, conf["token"], data)
            if err:
                return f"[Error]: 用授权码换令牌失败：{err}"
            return self._save_token(payload or {})

        if not conf["client_id"]:
            return ("[Error]: OAuth2 未配置 client_id。请先在设置页 → 插件 → email-plugin "
                    "填写［oauth2］客户端 ID（需在服务商后台注册应用并开启邮件权限）。")

        if flow == OAUTH_FLOW_MANUAL:
            return ("[Email]: 该服务商不提供 OAuth2 授权（如 QQ/163，仅支持密码登录）。"
                    "请把「认证方式」改为 basic，并填写「密码 / 应用密码」——"
                    "这类服务商要求先在邮箱设置里开启 SMTP/IMAP 并生成授权码。")

        if flow == OAUTH_FLOW_AUTHCODE:
            if not conf["authorize"]:
                return "[Error]: 未配置授权端点，无法生成授权链接。"
            params = {"client_id": conf["client_id"], "response_type": "code",
                      "scope": conf["scope"], "access_type": "offline",
                      "prompt": "consent"}
            if conf["redirect_uri"]:
                params["redirect_uri"] = conf["redirect_uri"]
            url = f"{conf['authorize']}?{urllib.parse.urlencode(params)}"
            opened = await asyncio.to_thread(_open_browser, url)
            return (f"[Email]: 请在浏览器完成授权"
                    + ("（已自动打开）" if opened else "（未能自动打开，请手动复制下方链接）")
                    + "，然后把拿到的 code 传给本工具（email_oauth_login 的 code 参数）：\n"
                    + url)

        # device code 流程
        pending = _device_cache.get("pending") or {}
        if pending and float(pending.get("expires_at") or 0) < time.time():
            _device_cache.pop("pending", None)   # 过期设备码不能再用
            pending = {}
        device_code = str(args.get("device_code") or pending.get("device_code") or "")
        if device_code:
            try:
                timeout = int(args.get("timeout") or 120)
            except (TypeError, ValueError):
                timeout = 120
            timeout = max(15, min(timeout, 300))
            interval = max(int(pending.get("interval") or 5), 3)
            deadline = time.time() + timeout
            while time.time() < deadline:
                payload, err, status = await asyncio.to_thread(
                    _oauth_device_poll, conf, device_code)
                if status == "error":
                    return f"[Error]: 授权失败：{err}"
                if status == "done":
                    _device_cache.pop("pending", None)
                    return self._save_token(payload or {})
                if status == "slow_down":
                    interval = min(interval + 5, 60)   # RFC 8628：退避后再轮询
                await asyncio.sleep(interval)
            _device_cache["pending"] = dict(pending, device_code=device_code)
            return (f"[Email]: 仍在等待授权（已等待 {timeout}s）。请先在浏览器完成授权，"
                    "完成后再次调用 email_oauth_login 继续领取令牌。")

        # 无待处理设备码 → 申请一个并立即返回，避免工具调用被长时间阻塞
        payload, err = await asyncio.to_thread(_oauth_device_start, conf)
        if err:
            return f"[Error]: {err}"
        dc = str(payload.get("device_code") or "")
        if not dc:
            return f"[Error]: 未拿到 device_code：{str(payload)[:200]}"
        try:
            expires_in = float(payload.get("expires_in") or 900)
        except (TypeError, ValueError):
            expires_in = 900.0
        _device_cache["pending"] = {
            "device_code": dc,
            "interval": payload.get("interval") or 5,
            "expires_at": time.time() + expires_in,
        }
        msg = str(payload.get("message") or "").strip()
        verify = str(payload.get("verification_uri") or payload.get("verification_url") or "")
        minutes = int(expires_in // 60) or 1
        opened = await asyncio.to_thread(_open_browser, verify) if verify else False
        return (f"[Email]: 请完成 OAuth2 授权（{minutes} 分钟内有效）——\n"
                f"1) 浏览器打开：{verify}"
                + ("（已自动为你打开）" if opened else "（未能自动打开，请手动复制）") + "\n"
                f"2) 输入代码：{payload.get('user_code')}\n"
                + (f"3) {msg}\n" if msg else "")
                + "完成后再次调用 email_oauth_login（会自动领取并存下 refresh_token）。")

    # ------------------------------------------------------------ 发信

    async def _send(self, args: Dict[str, Any]) -> str:
        from litework.tools.todos import current_session_id

        user, secret, mode, err = _auth_material(self._app)
        if err:
            return f"[Error]: {err}"

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
        conf = _oauth_conf(self._app)
        smtp_host, smtp_port = conf["smtp_host"], conf["smtp_port"]
        if not smtp_host:
            return ("[Error]: 未确定 SMTP 服务器。请在设置页 → 插件 → email-plugin "
                    "选择服务商，或直接填写 SMTP 服务器地址。")
        from_addr = _resolve_from_addr(self._app, user)

        msg = _build_msg(from_addr, to, cc, bcc, subject, body, html_body, attached)

        try:
            await asyncio.to_thread(
                _smtp_send, smtp_host, smtp_port, user, secret, from_addr, msg,
                mode, conf["smtp_tls"]
            )
        except smtplib.SMTPAuthenticationError:
            if mode == AUTH_MODE_OAUTH2:
                return ("[Error]: SMTP 认证失败（OAuth2）。请确认已运行 email_oauth_login 完成授权，"
                        "且 scope 含 SMTP.Send。")
            return ("[Error]: SMTP 认证失败。Outlook 个人版已停用密码登录，请改用 oauth2；"
                    "其他服务请检查密码 / 应用密码。")
        except (smtplib.SMTPServerDisconnected, ConnectionError, OSError) as e:
            return f"[Error]: SMTP 连接失败（{smtp_host}:{smtp_port}）：{e}"
        except Exception as e:
            return f"[Error]: 发送失败：{e}"

        return (f"[Email OK]: 邮件已发送 → {', '.join(to)} | 主题：{subject}"
                + (f" | 附件：{', '.join(n for _, n in attached) or '(无)'}" if attached else "")
                + f" | 协议 SMTP {smtp_host}:{smtp_port}（{mode}）")

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
