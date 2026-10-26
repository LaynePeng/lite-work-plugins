"""盯梢通知：复用 email-plugin 的发信能力（不自建 SMTP）。

**为什么这么设计**（先看这段，别重复踩坑）：

❌ **错误做法 1：让盯梢的 Agent 调用 `email_send` 工具**
   盯梢任务用 `unattended_profile` 启动，`litework/core/permissions.py` 的
   `UNATTENDED_PROFILES` 只保留 `read`/`web`/`interactive` 三个域，而
   `email_send` 属 `misc` 域 → **被从工具面移除**。Agent 连工具都看不到，
   不是"卡在审批等超时"（那是表象）——所以这条路根本走不通。

❌ **错误做法 2：自己写一套 SMTP**
   会丢掉 email-plugin 已经实现的关键能力：
   ① **OAuth2**（XOAUTH2 + 刷新令牌）——Outlook/Gmail 个人版已停用密码认证，
      自建 SMTP 对这些用户直接不可用；
   ② 端口/TLS 判断、`From/To/Cc` 组装、授权码与登录密码的区分提示。
   结果是"配过邮件插件的人还要再配一遍"，且部分账号永远配不通。

✅ **本模块做法：直接 import email-plugin 的模块级函数**
   插件之间不能调**工具**，但可以共享**代码**。email-plugin 的
   `_auth_material()`（统一取账号+密码/access_token）与 `_smtp_send()` /
   `_build_msg()`（发信）都是纯函数，不依赖工具注册表，也不走审批门。
   于是：用户只需在 email-plugin 里配一次邮箱，盯梢通知自动复用，
   OAuth2 账号同样可用。

**降级**：email-plugin 未安装/不可导入时，`available()` 返回 False，
插件照常工作（只是不发通知），并给出"装 email-plugin"的可操作提示。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("litework.astock")

#: 每小时的邮件上限（防抖：接口抖动不该变成邮件轰炸）。
MAX_MAILS_PER_HOUR = 12
#: 同一 (code, kind) 信号在多少秒内不重复发。
DEDUP_SECONDS = 6 * 3600

#: email-plugin 的模块名（导入到 sys.modules 时用的唯一键）
_EMAIL_MODULE_KEY = "litework_plugin_email_shared"


def _find_email_plugin() -> Optional[str]:
    """定位 email-plugin 的 plugin.py（用户插件目录优先，其次仓库内）。"""
    candidates: List[str] = []
    # ① 用户插件目录 ~/.lite-work/plugins/email-plugin/plugin.py
    cfg_dir = os.environ.get("LITEWORK_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".lite-work")
    candidates.append(os.path.join(cfg_dir, "plugins", "email-plugin", "plugin.py"))
    # ② 与 astock-plugin 同级的兄弟目录（开发态仓库内）
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "email-plugin", "plugin.py"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _load_email_plugin() -> Optional[Any]:
    """导入 email-plugin 模块（进程内缓存）。

    用 `spec_from_file_location` 按文件加载——与宿主加载插件的方式一致，
    且不依赖 sys.path 里恰好有 email-plugin 目录。

    **已知约束**：email-plugin 模块顶层 `from litework.core.types import ...`，
    因此必须在**宿主进程内**导入（那里 litework 包可用）。在宿主之外
    （如纯离线单测）导入会失败——此时记负结果并让通知静默降级，
    绝不让盯梢因此报错。单测通过注入假模块覆盖真实发信路径。
    """
    if _EMAIL_MODULE_KEY in sys.modules:
        return sys.modules.get(_EMAIL_MODULE_KEY)
    path = _find_email_plugin()
    if not path:
        sys.modules[_EMAIL_MODULE_KEY] = None      # 记负结果，避免反复扫盘
        logger.info("[astock] 未找到 email-plugin，邮件通知不可用")
        return None
    try:
        spec = importlib.util.spec_from_file_location(_EMAIL_MODULE_KEY, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_EMAIL_MODULE_KEY] = module
        spec.loader.exec_module(module)            # type: ignore[union-attr]
        return module
    except Exception as exc:
        # 导入失败（宿主包缺失/依赖缺失/文件损坏）不能让盯梢挂掉。
        # **不缓存负结果**：宿主包可能是稍后才可用（如测试注入 sys.modules），
        # 缓存了会导致本进程内永远无法恢复。
        sys.modules.pop(_EMAIL_MODULE_KEY, None)
        logger.info("[astock] email-plugin 暂不可用（%s），邮件通知降级",
                    type(exc).__name__)
        return None


@dataclass
class MailConfig:
    """邮件通知配置（阈值来自本插件设置页，凭据来自 email-plugin）。"""

    enabled: bool = False
    min_level: str = "urgent"          # urgent / action / info
    mail_to: List[str] = field(default_factory=list)
    #: 收件人留空时发给 email-plugin 的发件人自己（"提醒自己"是最常见用法）
    fallback_to_self: bool = True

    def ready(self) -> Tuple[bool, str]:
        if not self.enabled:
            return False, "未启用邮件通知（设置页可开启）"
        if not module_available():
            return False, "未安装/未找到 email-plugin，无法发信"
        if not self.mail_to and not self.fallback_to_self:
            return False, "未配置收件人"
        return True, ""


class Notifier:
    """发信 + 限流 + 去重。实例挂在插件上（跨 probe 复用状态）。"""

    def __init__(self) -> None:
        self._last_sent: Dict[str, float] = {}
        self._sent_times: List[float] = []

    # -------------------------------------------------- 限流 / 去重

    def should_send(self, code: str, kind: str,
                    now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else now
        key = f"{code}|{kind}"
        last = self._last_sent.get(key)
        if last is not None and now - last < DEDUP_SECONDS:
            left = int((DEDUP_SECONDS - (now - last)) / 60)
            return False, f"同一信号 {left} 分钟内已提醒过"
        self._sent_times = [t for t in self._sent_times if now - t < 3600]
        if len(self._sent_times) >= MAX_MAILS_PER_HOUR:
            return False, f"已达每小时上限 {MAX_MAILS_PER_HOUR} 封"
        return True, ""

    def mark_sent(self, code: str, kind: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._last_sent[f"{code}|{kind}"] = now
        self._sent_times.append(now)

    def reset_signal(self, code: str, kind: str) -> None:
        """信号恢复正常后清除记录，下次再触发会重新提醒。"""
        self._last_sent.pop(f"{code}|{kind}", None)

    # -------------------------------------------------- 发信（复用 email-plugin）

    def send(self, cfg: MailConfig, app: Any, subject: str, body: str,
             html: str = "") -> Tuple[bool, str]:
        """通过 email-plugin 发信。返回 (成功, 说明)，**绝不抛异常**。"""
        mod = _load_email_plugin()
        if mod is None:
            return False, "未找到 email-plugin，无法发信"
        try:
            user, secret, auth_mode, err = mod._auth_material(app)
            if err:
                return False, f"email-plugin 凭据不可用：{err}"
            if not user or not secret:
                return False, "email-plugin 未配置邮箱账号或凭据"

            to = list(cfg.mail_to)
            if not to and cfg.fallback_to_self:
                to = [user]          # 留空 = 发给自己（最常见的"提醒自己"用法）
            if not to:
                return False, "未配置收件人"

            own = mod._own_cfg(app) if hasattr(mod, "_own_cfg") else {}
            host = str((own or {}).get("smtp_host") or "smtp.qq.com")
            port = int((own or {}).get("smtp_port") or 465)
            tls = str((own or {}).get("smtp_tls") or "ssl")
            from_addr = str((own or {}).get("email_from") or user)

            msg = mod._build_msg(from_addr, to, [], [], subject, body, html, [])
            mod._smtp_send(host, port, user, secret, from_addr, msg,
                           auth_mode=auth_mode, smtp_tls=tls)
            return True, f"已发送至 {', '.join(to)}"
        except Exception as exc:
            name = type(exc).__name__
            hint = ""
            if "Auth" in name or "auth" in str(exc).lower():
                hint = "（多数邮箱需用「授权码」而非登录密码）"
            logger.warning("[astock] 邮件发送失败: %s", exc)
            return False, f"发送失败：{name}: {exc}{hint}"


def module_available() -> bool:
    """email-plugin 是否可用（设置页展示用）。"""
    return _load_email_plugin() is not None


# ---------------------------------------------------------------- 配置合成


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}


def build_mail_config(own: Dict[str, Any]) -> MailConfig:
    """从插件设置页配置合成 MailConfig（凭据不由本插件管理）。"""
    own = own or {}
    to_raw = own.get("mail_to") or ""
    if isinstance(to_raw, str):
        to_list = [x.strip() for x in to_raw.replace("，", ",").split(",") if x.strip()]
    elif isinstance(to_raw, list):
        to_list = [str(x).strip() for x in to_raw if str(x).strip()]
    else:
        to_list = []
    return MailConfig(
        enabled=_as_bool(own.get("email_notify_enabled"), False),
        min_level=str(own.get("email_min_level") or "urgent").lower(),
        mail_to=to_list,
        fallback_to_self=_as_bool(own.get("email_to_self_if_empty"), True),
    )


# ---------------------------------------------------------------- 渲染


_LEVEL_RANK = {"info": 0, "action": 1, "urgent": 2}
_LEVEL_LABEL = {"urgent": "🔴 紧急", "action": "🟠 需关注", "info": "⚪ 提示"}


def render_mail(entries: List[Dict[str, Any]], as_of: str = "") -> Tuple[str, str]:
    """信号列表 → (主题, 纯文本正文)。"""
    urgent = [e for e in entries if e.get("severity") == "urgent"]
    head = "🔴 紧急" if urgent else "🟠 需关注"
    names = "、".join(e.get("name") or e.get("code") or "?" for e in entries[:3])
    subject = f"[A股助手]{head} {names}" + ("…" if len(entries) > 3 else "")

    lines = [f"盯盘发现 {len(entries)} 条信号（{as_of}）", ""]
    for e in entries[:20]:
        lines.append(f"{_LEVEL_LABEL.get(e.get('severity'), '')} "
                     f"{e.get('name') or ''}({e.get('code')})")
        lines.append(f"  {e.get('title') or ''}")
        if e.get("detail"):
            lines.append(f"  {e['detail']}")
        if e.get("suggestion"):
            lines.append(f"  → {e['suggestion']}")
        lines.append("")
    lines.append("—— 以上为按既定规则算出的提醒，仅供参考，不构成投资建议。")
    lines.append("若你已挂条件单，券商会自动执行；本邮件只提醒【计划外】的变化。")
    return subject, "\n".join(lines)


def render_mail_html(entries: List[Dict[str, Any]], as_of: str = "") -> str:
    """HTML 版正文（手机上分色块更易读）。"""
    color = {"urgent": "#d93025", "action": "#e37400", "info": "#5f6368"}
    rows = []
    for e in entries[:20]:
        c = color.get(e.get("severity"), "#5f6368")
        rows.append(
            f'<div style="margin:0 0 14px;padding:10px 12px;border-left:4px solid {c};'
            f'background:#fafafa;border-radius:4px">'
            f'<div style="font-weight:600;color:{c}">'
            f'{_LEVEL_LABEL.get(e.get("severity"), "")} '
            f'{_esc(e.get("name") or "")}({_esc(e.get("code") or "")})</div>'
            f'<div style="margin:4px 0;color:#202124">{_esc(e.get("title") or "")}</div>'
            + (f'<div style="color:#5f6368;font-size:13px">{_esc(e.get("detail") or "")}</div>'
               if e.get("detail") else "")
            + (f'<div style="margin-top:6px;color:#1a73e8">→ {_esc(e.get("suggestion") or "")}</div>'
               if e.get("suggestion") else "")
            + "</div>")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'max-width:640px;margin:0 auto;padding:16px">'
        f'<h3 style="margin:0 0 4px">盯盘发现 {len(entries)} 条信号</h3>'
        f'<div style="color:#5f6368;font-size:13px;margin-bottom:16px">{_esc(as_of)}</div>'
        + "".join(rows)
        + '<hr style="border:none;border-top:1px solid #eee;margin:16px 0">'
          '<div style="color:#80868b;font-size:12px;line-height:1.6">'
          '以上为按既定规则算出的提醒，仅供参考，不构成投资建议。<br>'
          '若你已挂条件单，券商会自动执行；本邮件只提醒【计划外】的变化。</div>'
          "</div>")


def _esc(text: Any) -> str:
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))
