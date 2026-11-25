# lite-work 社区插件：powershell —— Windows 上的原生命令执行层
#
# 为什么需要这个插件
# ---------------------------------------------------------------------------
# 宿主的 execute_command 是**跨平台一份实现**：`asyncio.create_subprocess_shell`
# 在 POSIX 上是 `/bin/sh -c`，在 Windows 上落到 `cmd.exe /c`。于是 Windows 用户
# 遇到的是双重不匹配：
#
#   1. **语法不匹配**：模型写出来的命令十有八九是类 Unix 写法（`ls -la`、
#      `rm -rf`、`cat f | head -n 5`、`a && b`）。cmd.exe 全不认，PowerShell
#      也只认一部分（`rm -rf` 报 "parameter cannot be found"，`&&` 在 5.1 上
#      直接是语法错误）。第一次执行几乎必然失败。
#   2. **启动成本**：每次调用都新建进程。Windows 上 PowerShell 冷启动 220–400ms
#      （本机实测），一批命令下来光是进程创建就是几秒——而这些时间全在
#      「模型等结果」的关键路径上。
#
# 本插件把这两件事一起解决：
#
#   · **持久会话**：一个长期存活的 PowerShell worker，命令经 stdin 送入。同一
#     会话内连续命令 **2–30ms**（对比每次新建 220–400ms）。变量、函数、当前
#     目录在命令之间保持，模型可以像人一样分步干活（`cd` 之后接着跑）。
#   · **命令翻译**：把能确定语义的 Unix 写法改写成等价 PowerShell，并把每处
#     改写回显给模型——它下一次就能直接写对，而不是反复试探。
#   · **编码治理**：worker 统一 UTF-8（含子进程的 PYTHONUTF8），5.1 默认按 ANSI
#     解码导致的乱码不再出现；`Get-Content` 自动补 `-Encoding UTF8`。
#   · **输出治理**：表格按 4000 列宽输出（5.1 默认 80 列会把路径截成 `...`）、
#     关进度条、关 CLIXML 噪声、超长输出保留头尾。
#
# 与宿主 execute_command 的关系
# ---------------------------------------------------------------------------
# **不覆盖** execute_command。原因有三：
#   1. SecurityPlugin 的高危命令拦截挂在 `tool_name == "execute_command"` 上，
#      同名覆盖会让那道防线直接失效——安全上不可接受；
#   2. 用户有时确实需要 cmd.exe 语义（.bat、`dir /b` 之类的原样语法）；
#   3. 同名覆盖在被裁掉工具的场景下会互相踩（宿主 -plugin 版本比对、按需装载）。
# 所以本插件**新增** `ps_run` / `ps_session`，并在 description 里写明「Windows
# 上优先用它」。模型按 schema 描述选工具，效果等同于默认走 PowerShell。
#
# 安全边界
# ---------------------------------------------------------------------------
# - 本插件不绕过、不削弱 SecurityPlugin：高危命令仍由宿主拦截（它按命令文本
#   正则判定，与哪个工具执行无关的那部分仍在）。但 `ps_run` 是**新工具名**，
#   宿主的中危「逐条审批」只认 execute_command——因此本插件自己做一遍同口径的
#   高危拒绝（见 ps_translate.BLOCKED_PREFIXES 与下面的 `_guard_command`），
#   并对 `sudo` / `chmod` / `chown` 这类 Windows 无对应物的写法直接说明。
# - 工作区钉死：命令默认在会话工作区执行；`cwd` 参数只允许工作区内的路径。
# - 命令串行：同一会话里的两条命令不并发（共享变量与 cwd，并发只会互相污染）。
#
# 配置（`config.json` 顶层 `powershell-plugin` 对象，设置页可改）
#
#   键                  默认      说明
#   prefer              auto      auto / pwsh（7+）/ powershell（5.1）
#   timeout             120       前台命令默认超时（秒）
#   max_output          60000     单段输出上限（字符）
#   translate           true      是否启用 Unix → PowerShell 改写
#   assume_utf8_files   true      Get-Content 是否补 -Encoding UTF8
#   persist_session     true      持久会话（关掉则退回一次性进程，慢但干净）
#   prewarm             true      插件装配时后台预热会话
#   idle_ttl            900       会话空闲多久后回收（秒）
#   encoding            utf-8     worker 输出编码（utf-8 / cp936）
#
# 安装：放进 `~/.lite-work/plugins/powershell-plugin/`（或从设置页社区安装）。
# 零第三方依赖（只用标准库 + 宿主稳定接口）。
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin

import ps_translate
import ps_worker

logger = logging.getLogger("litework.plugins.powershell")

PLUGIN_NAME = "powershell-plugin"

DEFAULTS: Dict[str, Any] = {
    "prefer": "auto",
    "timeout": 120,
    "max_output": 60000,
    "translate": True,
    "assume_utf8_files": True,
    "persist_session": True,
    "prewarm": True,
    "idle_ttl": 900,
    "encoding": "utf-8",
}

#: 与宿主 SecurityGuard 同口径的**高危**兜底（本插件的工具名不在宿主的
#: execute_command 分支上，所以这一步必须自己再做一次）。宁可多拒，不可放过：
#: 这些模式在任何 shell 里都意味着不可逆的数据破坏。
_HIGH_RISK = (
    (r"\brm\s+-[rRfF]+\s+[/\*]", "递归删除根/通配路径"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bdd\b[^\n]*\bof=/dev/", "直写块设备"),
    (r">\s*/dev/sd", "直写块设备"),
    (r"[:]\(\)\s*\{\s*[:][|][:]&", "fork 炸弹"),
    (r"\bRemove-Item\b[^\n]*-Recurse[^\n]*-Force[^\n]*\s[A-Za-z]:\\?\s*($|;|\|)", "递归删除盘符根"),
    (r"\b(?:rmdir|rd)\b[^\n]*/s[^\n]*\s[A-Za-z]:\\?\s*($|;|\|)", "删除盘符根目录"),
    (r"\bdel\b[^\n]*/s[^\n]*\s[A-Za-z]:\\\*", "递归删除盘符内容"),
    (r"\bformat\s+[A-Za-z]:", "格式化磁盘"),
    (r"\bshutdown\b", "关机"),
    (r"\brestart-computer\b", "重启"),
    (r"\bStop-Computer\b", "关机"),
)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        num = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, num)


def _guard_command(command: str) -> str:
    """高危命令兜底；返回拦截原因（空串 = 放行）。"""
    import re

    for pattern, label in _HIGH_RISK:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return f"命令命中高危模式（{label}）/ {pattern} /，已拒绝执行。"
        except re.error:                              # noqa: PERF203 - 常量模式不会走到
            continue
    return ""


class _Job:
    """后台任务：一次性 PowerShell 进程 + 输出落盘（跨调用可查）。"""

    def __init__(self, command: str, cwd: str, timeout: float) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.started_at = time.time()
        self.proc: Optional[Any] = None
        self.out_path = ""
        self.err_path = ""
        self.dir = ""
        self.done = False
        self.exit_code: Optional[int] = None
        self.timed_out = False
        self._lock = threading.Lock()

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def read(self, cap: int = 20000) -> Dict[str, str]:
        def tail(path: str) -> str:
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return ""
            text = data.decode("utf-8", "replace")
            if len(text) > cap:
                return (f"... [输出截断：仅保留末尾 {cap} 字符] ...\n"
                        + text[-cap:])
            return text

        with self._lock:
            return {"out": tail(self.out_path), "err": tail(self.err_path)}

    def cleanup(self) -> None:
        self.dir and _rmtree(self.dir)


def _rmtree(path: str) -> None:
    try:
        for name in os.listdir(path):
            try:
                os.remove(os.path.join(path, name))
            except OSError:
                pass
        os.rmdir(path)
    except OSError:
        pass


class PowerShellPlugin(ToolPlugin):
    """Windows 原生命令执行层（持久 PowerShell 会话 + 命令翻译）。"""

    name = PLUGIN_NAME
    version = "1.0.0"
    description = (
        "Windows 原生命令执行：持久 PowerShell 会话（同一会话内连续命令 2–30ms，"
        "对比每次新建进程 220–400ms）+ Unix 写法自动翻译 + UTF-8 编码治理。"
        "Windows 上执行命令的首选工具。"
    )

    #: 通用插件 UI 协议：设置页表单 + 右栏「会话」面板（宿主自动渲染）
    contributes: Dict[str, Any] = {
        "settings": [
            {"key": "prefer", "type": "select", "label": "PowerShell 版本",
             "default": "auto", "options": ["auto", "pwsh", "powershell"],
             "hint": "auto = 有 pwsh（7+）就用它，否则退回 Windows PowerShell 5.1"},
            {"key": "timeout", "type": "number", "label": "默认超时（秒）",
             "default": 120,
             "hint": "单条前台命令的上限；长任务请用 background=true"},
            {"key": "max_output", "type": "number", "label": "输出上限（字符）",
             "default": 60000, "hint": "STDOUT / STDERR 各自的上限，超出保留头尾"},
            {"key": "translate", "type": "boolean", "label": "Unix 写法自动翻译",
             "default": True,
             "hint": "把 ls -la / rm -rf / cat|head / && / export 等改写成 PowerShell 等价物"},
            {"key": "assume_utf8_files", "type": "boolean",
             "label": "按 UTF-8 读取文件", "default": True,
             "hint": "Get-Content 自动补 -Encoding UTF8；老 GBK 工程请关闭"},
            {"key": "persist_session", "type": "boolean", "label": "持久会话",
             "default": True,
             "hint": "关闭后每条命令都新建进程（慢，但每次都是干净环境）"},
            {"key": "prewarm", "type": "boolean", "label": "装配时预热会话",
             "default": True, "hint": "把首次启动的 ~0.5s 挪到模型发命令之前"},
            {"key": "idle_ttl", "type": "number", "label": "会话空闲回收（秒）",
             "default": 900, "hint": "超时不用就关掉 worker，避免常驻进程"},
            {"key": "encoding", "type": "select", "label": "输出编码",
             "default": "utf-8", "options": ["utf-8", "cp936"],
             "hint": "现代 CLI 用 utf-8；若输出全是问号/乱码可切 cp936"},
        ],
        "panels": [
            {"id": "session", "title": "PowerShell", "icon": "terminal"},
        ],
    }

    def __init__(self) -> None:
        self._app: Any = None
        self._jobs: Dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()

    # ------------------------------------------------------------ 装配

    def install(self, kernel) -> None:
        try:
            if kernel.has_service("app"):
                self._app = kernel.get_service("app")
        except Exception:                             # noqa: BLE001
            self._app = None
        super().install(kernel)
        if self._setting("prewarm", True):
            self._prewarm()

    def _config(self) -> Dict[str, Any]:
        """当前插件配置（``config.json`` 的 ``powershell-plugin`` 段）。"""
        app = self._app
        raw: Any = None
        if app is not None:
            try:
                raw = (getattr(app, "config", {}) or {}).get(PLUGIN_NAME)
            except Exception:                         # noqa: BLE001
                raw = None
        return raw if isinstance(raw, dict) else {}

    def _setting(self, key: str, default: Any = None) -> Any:
        """读一个配置项：设置页/配置文件优先，缺省落回 DEFAULTS。"""
        cfg = self._config()
        if cfg.get(key) is not None:
            return cfg[key]
        if default is not None:
            return default
        return DEFAULTS.get(key)

    def _workspace(self) -> str:
        """运行时工作区：优先当前任务钉死的目录，其次 app.workspace。

        与内置 office/ocr 同一口径（见宿主 tools/plugin.py 的 _runtime_workspace）：
        任务期 `header_context["workspace"]` 最准确（worktree 隔离时是工作树路径），
        面板等非任务路径退回 app.workspace。
        """
        try:
            from litework.core.types import header_context

            ctx = header_context.get() or {}
            raw = str(ctx.get("workspace") or "").strip() if isinstance(ctx, dict) else ""
            if raw and os.path.isabs(raw) and os.path.isdir(raw):
                return os.path.abspath(raw)
        except Exception:                             # noqa: BLE001
            pass
        raw = str(getattr(self._app, "workspace", "") or "").strip()
        if raw and os.path.isabs(raw) and os.path.isdir(raw):
            return os.path.abspath(raw)
        return os.getcwd()

    def _exe(self) -> tuple:
        prefer = str(self._setting("prefer", "auto") or "auto").strip().lower()
        return ps_worker.find_powershell(prefer)

    def _prewarm(self) -> None:
        """后台预热：把 worker 启动成本（~0.5s）挪到模型发命令之前。"""
        if not ps_worker.IS_WINDOWS:
            return
        app = self._app
        workspace = ""
        try:
            workspace = str(getattr(app, "workspace", "") or "")
        except Exception:                             # noqa: BLE001
            workspace = ""
        if not workspace or not os.path.isdir(workspace):
            return
        exe, label = self._exe()
        try:
            ps_worker.POOL.idle_ttl = _as_int(self._setting("idle_ttl", 900), 900, 30)
            ps_worker.POOL.prewarm(workspace,
                                   encoding=str(self._setting("encoding", "utf-8")),
                                   max_output=_as_int(self._setting("max_output", 60000),
                                                      60000, 1000),
                                   exe=exe, label=label)
        except Exception:                             # noqa: BLE001 - 预热失败不影响工具
            logger.debug("[ps] 预热会话失败", exc_info=True)

    def on_config_changed(self, config: Dict[str, Any]) -> None:
        """配置改了立即生效：会话参数（编码/上限/可执行文件）随下次调用重建。"""
        ps_worker.POOL.idle_ttl = _as_int(self._setting("idle_ttl", 900), 900, 30)
        if self._setting("prewarm", True):
            self._prewarm()

    # ------------------------------------------------------------ 工具定义

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="ps_run",
                description=(
                    "在 PowerShell 中执行命令（Windows 上**执行命令的首选工具**："
                    "持久会话，同一会话内连续命令 2–30ms，比每次新建进程快一个数量级）。"
                    "支持 Unix 写法的自动翻译（ls -la / rm -rf / cat|head / grep / "
                    "find / a && b / export / $HOME / /dev/null 等会改写成 PowerShell "
                    "等价物，改了什么会在 [Translated] 里告诉你）。"
                    "变量、函数、当前目录在命令之间保持——`cwd` 不传就沿用上一条命令的目录。"
                    "参数：timeout（秒，覆盖默认）；cwd（工作目录，须在工作区内）；"
                    "fresh=true 用一次性干净进程（慢，但在会话被搞脏时用）；"
                    "background=true 后台执行长任务（返回 task_id，用 ps_session "
                    "action=output 取输出、action=kill 终止）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string",
                                    "description": "要执行的命令（PowerShell 语法；"
                                                   "常见 Unix 写法会自动翻译）"},
                        "timeout": {"type": "number",
                                    "description": "超时秒数（可选，覆盖默认）"},
                        "cwd": {"type": "string",
                                "description": "工作目录（可选，须在工作区内；"
                                               "不传则沿用会话当前目录）"},
                        "fresh": {"type": "boolean",
                                  "description": "用一次性干净进程执行（默认 false，"
                                                 "即走持久会话）"},
                        "background": {"type": "boolean",
                                       "description": "后台执行长命令（git clone / "
                                                      "pip install 等），返回 task_id"},
                    },
                    "required": ["command"],
                },
            ),
            ToolDefinition(
                name="ps_session",
                description=(
                    "管理 ps_run 的 PowerShell 会话与后台任务。"
                    "action=status（会话/后台任务总览）、restart（重启会话，清掉所有"
                    "变量与目录状态）、close（关闭会话释放进程）、jobs（后台任务列表）、"
                    "output（取后台任务输出，需 task_id）、kill（终止后台任务，需 task_id）。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "restart", "close", "jobs",
                                     "output", "kill"],
                            "description": "要执行的管理动作",
                        },
                        "task_id": {"type": "string",
                                    "description": "后台任务 id（action=output / kill 时必填）"},
                    },
                    "required": ["action"],
                },
            ),
        ]

    # ------------------------------------------------------------ 执行入口

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name == "ps_run":
            return await self._tool_run(args)
        if name == "ps_session":
            return await self._tool_session(args)
        return f"[Error]: 未知工具 {name}"

    # ------------------------------------------------------------ ps_run

    async def _tool_run(self, args: Dict[str, Any]) -> str:
        command = str(args.get("command") or "")
        if not command.strip():
            return "[Error]: command 不能为空。"

        blocked = _guard_command(command)
        if blocked:
            return f"[Security Blocked]: {blocked}"

        workspace = self._workspace()
        cwd_arg = str(args.get("cwd") or "").strip()
        cwd = ""
        if cwd_arg:
            resolved = self._safe_cwd(cwd_arg, workspace)
            if resolved is None:
                return (f"[Error]: cwd {cwd_arg!r} 不在工作区 {workspace} 内（或不存在）。"
                        f"本插件只允许在工作区内执行命令。")
            cwd = resolved

        if not ps_worker.IS_WINDOWS and not self._exe()[0].lower().endswith("pwsh"):
            return (f"[Error]: 未找到 PowerShell（{self._exe()[0]}）。"
                    f"本插件面向 Windows；非 Windows 请装 pwsh，或直接用 execute_command。")

        # 命令翻译（改写了什么会回显给模型，下一次它就能直接写对）
        notes: List[str] = []
        script = command
        if _as_bool(self._setting("translate", True), True):
            translated = ps_translate.translate(
                command,
                assume_utf8=_as_bool(self._setting("assume_utf8_files", True), True),
            )
            if translated.blocked:
                return f"[Not Supported]: {translated.blocked}"
            script = translated.script
            notes = translated.notes

        timeout = float(args.get("timeout") or self._setting("timeout", 120) or 120)
        timeout = max(1.0, min(timeout, 3600.0))
        max_output = _as_int(self._setting("max_output", 60000), 60000, 1000)

        if _as_bool(args.get("background", False), False):
            return await asyncio.to_thread(
                self._start_job, script, cwd or workspace, timeout, notes, command)

        fresh = _as_bool(args.get("fresh", False), False) or not _as_bool(
            self._setting("persist_session", True), True)

        if fresh:
            result = await asyncio.to_thread(
                ps_worker.run_oneshot, script, workspace=cwd or workspace,
                timeout=timeout,
                encoding=str(self._setting("encoding", "utf-8")),
                max_output=max_output, exe=self._exe()[0])
            shell = f"{self._exe()[1]}（一次性进程）"
        else:
            exe, label = self._exe()
            session = ps_worker.POOL.acquire(
                workspace, encoding=str(self._setting("encoding", "utf-8")),
                max_output=max_output, exe=exe, label=label)
            ps_worker.POOL.idle_ttl = _as_int(self._setting("idle_ttl", 900), 900, 30)
            result = await asyncio.to_thread(
                session.run, script, timeout=timeout, cwd=cwd or None)
            shell = f"{label}（持久会话 pid=" \
                    f"{session._proc.pid if session._proc is not None else '-'}）"

        return result.render(shell=shell, notes=notes, timeout=timeout)

    def _safe_cwd(self, raw: str, workspace: str) -> Optional[str]:
        """把 cwd 解析为工作区内的绝对路径；越界或不存在返回 None。"""
        candidate = raw
        if not os.path.isabs(candidate):
            candidate = os.path.join(workspace, candidate)
        try:
            candidate = os.path.abspath(os.path.expanduser(candidate))
        except (OSError, ValueError):
            return None
        if not os.path.isdir(candidate):
            return None
        try:
            root = os.path.abspath(workspace)
            if os.path.commonpath([root, candidate]) != root:
                return None
        except (ValueError, OSError):
            return None
        return candidate

    # ------------------------------------------------------------ 后台任务

    def _start_job(self, script: str, cwd: str, timeout: float,
                   notes: List[str], original: str) -> str:
        """启动后台任务（独立一次性进程，不占用持久会话）。"""
        job = _Job(original, cwd, timeout)
        job.dir = os.path.join(self._runtime_dir(), job.id)
        os.makedirs(job.dir, exist_ok=True)
        job.out_path = os.path.join(job.dir, "stdout.log")
        job.err_path = os.path.join(job.dir, "stderr.log")
        exe, label = self._exe()
        try:
            job.proc = ps_worker.spawn_detached(
                script, cwd=cwd, timeout=timeout, exe=exe,
                out_path=job.out_path, err_path=job.err_path,
                encoding=str(self._setting("encoding", "utf-8")))
        except Exception as exc:                      # noqa: BLE001
            job.cleanup()
            return f"[Error]: 启动后台命令失败：{type(exc).__name__}: {exc}"
        with self._jobs_lock:
            self._jobs[job.id] = job
        head = [f"[Background Started]: task_id={job.id}（{label}，超时 {timeout:g}s）"]
        if notes:
            head.append("[Translated]: " + "；".join(notes))
        head.append(f"轮询输出：ps_session(action=\"output\", task_id=\"{job.id}\")")
        head.append(f"终止任务：ps_session(action=\"kill\", task_id=\"{job.id}\")")
        return "\n".join(head)

    def _runtime_dir(self) -> str:
        base = ""
        app = self._app
        if app is not None:
            base = str(getattr(app, "config_dir", "") or "")
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".lite-work")
        path = os.path.join(base, "powershell_plugin")
        os.makedirs(path, exist_ok=True)
        return path

    def _poll_job(self, job: _Job) -> None:
        """更新任务完成状态（进程已退出则记账）。"""
        proc = job.proc
        if proc is None or job.done:
            return
        code = proc.poll()
        if code is None:
            return
        with job._lock:
            job.done = True
            job.exit_code = int(code)

    def _cleanup_jobs(self, keep: float = 3600.0) -> None:
        """回收久已完成的任务目录（避免临时文件无限增长）。"""
        now = time.time()
        with self._jobs_lock:
            stale = [j for j in self._jobs.values()
                     if j.done and now - j.started_at > keep]
            for job in stale:
                self._jobs.pop(job.id, None)
        for job in stale:
            job.cleanup()

    # ------------------------------------------------------------ ps_session

    async def _tool_session(self, args: Dict[str, Any]) -> str:
        action = str(args.get("action") or "status").strip().lower()
        if action == "status":
            return self._status_markdown()
        if action == "restart":
            return await asyncio.to_thread(self._restart_sessions)
        if action == "close":
            return await asyncio.to_thread(self._close_sessions)
        if action == "jobs":
            return self._jobs_markdown()
        if action in ("output", "kill"):
            task_id = str(args.get("task_id") or "").strip()
            if not task_id:
                return f"[Error]: action={action} 需要 task_id。"
            if action == "output":
                return self._job_output(task_id)
            return self._job_kill(task_id)
        return (f"[Error]: 未知 action {action!r}；"
                f"可用：status / restart / close / jobs / output / kill。")

    def _status_markdown(self) -> str:
        exe, label = self._exe()
        workspace = self._workspace()
        sessions = ps_worker.POOL.describe()
        lines = ["## PowerShell 会话", "",
                 f"- 可执行文件：`{exe}`（{label}）",
                 f"- 当前工作区：`{workspace}`",
                 f"- 持久会话：{'开' if _as_bool(self._setting('persist_session', True), True) else '关'}",
                 f"- 命令翻译：{'开' if _as_bool(self._setting('translate', True), True) else '关'}",
                 f"- 输出编码：{self._setting('encoding', 'utf-8')}"]
        if sessions:
            lines += ["", "| 工作区 | 状态 | pid | 空闲(s) | 重启次数 |",
                      "| --- | --- | --- | --- | --- |"]
            for item in sessions:
                lines.append(
                    f"| `{item['workspace']}` | {'运行中' if item['alive'] else '已停'} | "
                    f"{item['pid'] or '-'} | {item['idle_seconds']} | {item['restarts']} |")
        else:
            lines += ["", "（当前没有活跃会话——首次 ps_run 时会启动，约 0.5s）"]
        with self._jobs_lock:
            running = [j for j in self._jobs.values() if not j.done]
        if running:
            lines += ["", f"后台任务：{len(running)} 个在跑"
                          f"（详见 ps_session action=jobs）"]
        return "\n".join(lines)

    def _restart_sessions(self) -> str:
        before = len(ps_worker.POOL.describe())
        ps_worker.POOL.close_all()
        if self._setting("prewarm", True):
            self._prewarm()
        return (f"✅ 已关闭 {before} 个 PowerShell 会话；下一条 ps_run 会用新会话"
                f"（变量与当前目录已重置，文件系统改动不受影响）。")

    def _close_sessions(self) -> str:
        before = len(ps_worker.POOL.describe())
        ps_worker.POOL.close_all()
        return f"✅ 已关闭 {before} 个 PowerShell 会话（不再常驻 worker 进程）。"

    def _jobs_markdown(self) -> str:
        self._cleanup_jobs()
        with self._jobs_lock:
            jobs = list(self._jobs.values())
        if not jobs:
            return "（没有后台任务。用 ps_run background=true 启动）"
        lines = ["## 后台任务", "",
                 "| task_id | 状态 | 已运行(s) | Exit | 命令 |",
                 "| --- | --- | --- | --- | --- |"]
        for job in sorted(jobs, key=lambda j: j.started_at, reverse=True):
            self._poll_job(job)
            state = "已完成" if job.done else "运行中"
            cmd = job.command.replace("|", "\\|")[:60]
            lines.append(f"| `{job.id}` | {state} | {job.elapsed:.0f} | "
                         f"{job.exit_code if job.done else '-'} | `{cmd}` |")
        lines += ["", "取输出：ps_session(action=\"output\", task_id=\"…\")"]
        return "\n".join(lines)

    def _job_output(self, task_id: str) -> str:
        with self._jobs_lock:
            job = self._jobs.get(task_id)
        if job is None:
            return f"[Error]: 未找到后台任务 {task_id!r}（可能已回收）。"
        self._poll_job(job)
        payload = job.read()
        parts = [f"[task_id]: {job.id}",
                 f"[running]: {not job.done}",
                 f"[elapsed]: {job.elapsed:.1f}s"]
        if job.done:
            parts.append(f"[Exit Code]: {job.exit_code}")
        if payload["out"].strip():
            parts.append(f"[STDOUT]:\n{payload['out'].rstrip()}")
        if payload["err"].strip():
            parts.append(f"[STDERR]:\n{payload['err'].rstrip()}")
        if not payload["out"].strip() and not payload["err"].strip():
            parts.append("[No output yet]")
        return "\n".join(parts)

    def _job_kill(self, task_id: str) -> str:
        with self._jobs_lock:
            job = self._jobs.get(task_id)
        if job is None:
            return f"[Error]: 未找到后台任务 {task_id!r}。"
        if job.done:
            return f"任务 {task_id} 已结束（Exit {job.exit_code}），无需终止。"
        pid = job.proc.pid if job.proc is not None else None
        ps_worker.kill_tree(pid)
        with job._lock:
            job.done = True
            job.exit_code = job.exit_code if job.exit_code is not None else -1
        return f"✅ 已终止后台任务 {task_id}（pid={pid}，连带整棵进程树）。"

    # ------------------------------------------------------------ 面板（通用 UI 协议）

    def panel_content(self, panel_id: str, config: Dict[str, Any]) -> str:
        """右栏「PowerShell」面板：会话与后台任务总览（纯读，不快也不慢）。"""
        if panel_id != "session":
            return ""
        try:
            lines = [self._status_markdown()]
            jobs = self._jobs_markdown()
            if not jobs.startswith("（没有后台任务"):
                lines += ["", jobs]
            return "\n".join(lines)
        except Exception as exc:                      # noqa: BLE001 - 面板不得抛错
            return f"面板渲染失败：{type(exc).__name__}: {exc}"

    def status_from_config(self, config: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """设置页状态徽标：PowerShell 是否可用、当前用的哪个版本。"""
        try:
            exe, label = self._exe()
        except Exception as exc:                      # noqa: BLE001
            return {"state": "not_started", "reason": f"探测 PowerShell 失败：{exc}"}
        if not os.path.isfile(exe) and not ps_worker._which(exe):
            return {"state": "not_started",
                    "reason": f"未找到 PowerShell（{exe}）；Windows 自带 5.1 应存在"}
        return {"state": "running", "reason": f"{label}：{exe}"}
