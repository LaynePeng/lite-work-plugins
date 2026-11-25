# lite-work 社区插件 powershell-plugin 的执行层。
#
# 它解决什么
# ---------------------------------------------------------------------------
# 宿主的 execute_command 走 `asyncio.create_subprocess_shell`，在 Windows 上落
# 到 cmd.exe：每次调用都要付一次进程启动成本，而且拿到的语法不是 PowerShell。
# 本层做两件事：
#
#   1. **持久会话**：一个长期存活的 powershell.exe，命令通过 stdin 送进去、
#      结果从临时文件取回。同一会话内连续命令 2–30ms，对比每次新建进程
#      220–400ms（本机实测），这是「快」的来源；副作用是变量、函数、当前
#      目录在命令之间保持——与真实 shell 一致，模型能据此分步干活。
#   2. **原始进程兜底**：persist_session=false 时退回一次性
#      `-EncodedCommand` 进程。慢，但每次都是干净环境，适合「上一条命令把
#      shell 搞脏了」的场景。
#
# 协议（一行一个 JSON，返回写文件——不共用 stdout，避免命令自身的输出
# 干扰协议；实测命令里的原生程序写 stdout 也不会污染响应）：
#
#   请求  {"b64": "<UTF-8 命令的 base64>", "resp": "<响应文件>",
#          "cwd": "<可选，工作目录>", "timeout_ms": 60000, "max_chars": 60000}
#   响应  {"code": 0, "out": "...", "err": "...", "ms": 12,
#          "cwd": "C:\\x", "timed_out": false, "truncated": false}
#
# 为什么命令要 base64：stdin 的编码取决于控制台代码页，中文命令会被按
# GBK 解码成乱码。base64 是纯 ASCII，绕开整个问题。
#
# 编码策略：worker 把 [Console]::OutputEncoding 设为 UTF-8，并给子进程注入
# PYTHONUTF8=1 / PYTHONIOENCODING=utf-8。现代 CLI（git / node / python /
# ripgrep）输出 UTF-8，解码正确；少数老 Windows 工具（tasklist / ipconfig）
# 仍按代码页输出，会看到替换字符——需要时可切 output_encoding=system。
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("litework.plugins.powershell")

IS_WINDOWS = os.name == "nt"

#: 结果头里的退出码，供 out/code 使用
TIMEOUT_EXIT_CODE = 124

#: worker 的 PowerShell 脚本。
#:
#: 关键点（每一条都对应一次实测失败）：
#: - 用 `[powershell]::Create()` + 复用 Runspace：`Invoke-Expression` 拿不到
#:   独立的输出流，也没法按次超时中止。
#: - `BeginInvoke` + `AsyncWaitHandle.WaitOne(ms)`：单条命令超时能中止，
#:   而不是把整个会话拖死。
#: - 每次请求前后清空 Streams：否则上一条命令的 Write-Error 会重复出现在
#:   后面每一条的结果里（实测踩过）。
#: - 响应写 `$resp.tmp` 再 Move：文件出现即为写完整，Python 侧不需要重试解析。
WORKER_PS1 = r'''
param([string]$Encoding = 'utf-8')
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
if ($Encoding -eq 'utf-8') {
    try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
    try { $OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
} else {
    try { [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936) } catch {}
}
try { $PSDefaultParameterValues['Out-File:Encoding'] = 'utf8' } catch {}
try { $PSDefaultParameterValues['Out-String:Width'] = 4000 } catch {}

$rs = [runspacefactory]::CreateRunspace()
$rs.ThreadOptions = 'ReuseThread'
$rs.Open()
$ps = [powershell]::Create()
$ps.Runspace = $rs
# Session start directory (used to restore location after the engine is rebuilt
# on timeout; otherwise the location drifts to the process start directory).
$startCwd = (Get-Location).ProviderPath
$null = $rs.SessionStateProxy.SetVariable('ErrorActionPreference', 'Continue')
$null = $rs.SessionStateProxy.SetVariable('ProgressPreference', 'SilentlyContinue')
$null = $rs.SessionStateProxy.SetVariable('PSDefaultParameterValues', $PSDefaultParameterValues)
function Read-Frame([string]$text, [int]$cap) {
    if ($null -eq $text) { return @{ text = ''; truncated = $false } }
    if ($text.Length -le $cap) { return @{ text = $text; truncated = $false } }
    $head = [int]($cap * 0.7)
    $tail = $cap - $head
    return @{
        text = $text.Substring(0, $head) + "`n... [output truncated: $($text.Length - $cap) chars omitted] ...`n" + $text.Substring($text.Length - $tail)
        truncated = $true
    }
}

function Join-Errors($stream) {
    $lines = @()
    foreach ($item in $stream) {
        $m = ''
        try {
            if ($item.ErrorDetails -and $item.ErrorDetails.Message) { $m = $item.ErrorDetails.Message }
            else { $m = $item.Exception.Message }
        } catch { $m = [string]$item }
        if ($m) { $lines += $m.Trim() }
    }
    return ($lines -join "`n")
}

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ($line.Trim().Length -eq 0) { continue }

    $req = $null
    try { $req = $line | ConvertFrom-Json } catch { continue }
    $cmdText = ''
    try { $cmdText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string]$req.b64)) } catch { }
    if (-not $cmdText) { continue }

    $cap = 60000
    if ($req.max_chars) { $cap = [int]$req.max_chars }
    $budget = 120000
    if ($req.timeout_ms) { $budget = [int]$req.timeout_ms }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $outText = ''
    $errText = ''
    $code = 0
    $timedOut = $false
    $failed = $false

    $null = $rs.SessionStateProxy.SetVariable('LASTEXITCODE', $null)
    if ($req.cwd) {
        try { Set-Location -LiteralPath ([string]$req.cwd) -ErrorAction Stop }
        catch { $errText = "cwd unavailable: $($req.cwd): $($_.Exception.Message)" }
    }

    try {
        $ps.Commands.Clear()
        [void]$ps.AddScript($cmdText)
        $handle = $ps.BeginInvoke()
        if (-not $handle.AsyncWaitHandle.WaitOne($budget)) {
            $timedOut = $true
            # Rebuild the engine after a timeout instead of only calling $ps.Stop():
            # Stop() leaves un-consumed results in the pipeline, so the next
            # EndInvoke returns the previous command's leftovers and the following
            # successful command appears to produce no output at all. Cost: session
            # variables and cwd are lost - consistent with 'the command was killed'.
            try { $ps.Stop() } catch {}
            try { $ps.Dispose() } catch {}
            try { $rs.Close(); $rs.Dispose() } catch {}
            $rs = [runspacefactory]::CreateRunspace()
            $rs.ThreadOptions = 'ReuseThread'
            $rs.Open()
            $ps = [powershell]::Create()
            $ps.Runspace = $rs
            $null = $rs.SessionStateProxy.SetVariable('ErrorActionPreference', 'Continue')
            $null = $rs.SessionStateProxy.SetVariable('ProgressPreference', 'SilentlyContinue')
            $null = $rs.SessionStateProxy.SetVariable('PSDefaultParameterValues', $PSDefaultParameterValues)
            try { Set-Location -LiteralPath $req.cwd -ErrorAction SilentlyContinue } catch {}
            if (-not $req.cwd) { try { Set-Location -LiteralPath $script:startCwd } catch {} }
            $timeoutMsg = "Command timed out after $([math]::Round($budget / 1000, 1))s and was stopped; the session was rebuilt, so variables and the current directory reset (filesystem changes are unaffected)."
            $errText = $timeoutMsg
            $code = 124
        } else {
            $res = $ps.EndInvoke($handle)
            if ($res -and $res.Count -gt 0) { $outText = ($res | Out-String -Width 4000) }
        }
    } catch {
        $failed = $true
        try { $errText = $_.Exception.Message } catch {}
    }
    if ($ps.Streams.Error.Count -gt 0) {
        $joined = Join-Errors $ps.Streams.Error
        if ($joined) { $errText = (($joined + "`n" + $errText)).Trim("`n") }
    }
    if ($ps.Streams.Warning.Count -gt 0) {
        $w = ($ps.Streams.Warning | ForEach-Object { "WARNING: $_" }) -join "`n"
        $errText = ($errText + "`n" + $w).Trim("`n")
    }
    if ($ps.Streams.Information.Count -gt 0) {
        $info = ($ps.Streams.Information | ForEach-Object { "$_" }) -join "`n"
        if ($info) { $outText = ($outText + "`n" + $info).Trim("`n") }
    }
    $ps.Streams.Error.Clear()
    $ps.Streams.Warning.Clear()
    $ps.Streams.Information.Clear()
    $ps.Streams.Verbose.Clear()
    $ps.Streams.Debug.Clear()
    $ps.Streams.Progress.Clear()

    if (-not $timedOut) {
        $lec = $rs.SessionStateProxy.GetVariable('LASTEXITCODE')
        if ($null -ne $lec) { $code = [int]$lec }
        elseif ($failed -or $ps.HadErrors) { $code = 1 }
        else { $code = 0 }
    }
    $ps.HadErrors = $false
    $sw.Stop()

    $outFrame = Read-Frame $outText $cap
    $errFrame = Read-Frame $errText $cap
    $cwdNow = ''
    try { $cwdNow = (Get-Location).ProviderPath } catch {}
    $payload = [pscustomobject]@{
        code       = $code
        out        = [string]$outFrame.text
        err        = [string]$errFrame.text
        ms         = [int]$sw.ElapsedMilliseconds
        cwd        = [string]$cwdNow
        timed_out  = $timedOut
        truncated  = [bool]($outFrame.truncated -or $errFrame.truncated)
    }
    try {
        $json = $payload | ConvertTo-Json -Compress -Depth 3
        $tmp = [string]$req.resp + '.tmp'
        [IO.File]::WriteAllText($tmp, $json)
        Move-Item -LiteralPath $tmp -Destination ([string]$req.resp) -Force
    } catch {
        try {
            [IO.File]::WriteAllText([string]$req.resp, '{"code":1,"err":"response write failed","out":"","ms":0,"cwd":"","timed_out":false,"truncated":false}')
        } catch {}
    }
}
try { $ps.Dispose(); $rs.Close() } catch {}
'''

#: 每次命令前注入的脚本前缀（一次性进程模式用）。
ONESHOT_PREAMBLE = (
    "$ErrorActionPreference='Continue';$ProgressPreference='SilentlyContinue';"
    "try{[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)}catch{};"
    "try{$OutputEncoding=[Text.UTF8Encoding]::new($false)}catch{};"
    "try{$PSDefaultParameterValues['Out-File:Encoding']='utf8'}catch{};"
)


# ---------------------------------------------------------------- 结果类型


@dataclass
class CmdResult:
    """一条命令的执行结果（前台/后台/超时/失败都用它）。"""

    code: int = 0
    out: str = ""
    err: str = ""
    ms: int = 0
    cwd: str = ""
    timed_out: bool = False
    truncated: bool = False
    #: 会话已不可用（进程死了/超时被杀），下次调用会重启
    session_lost: bool = False

    def render(self, *, shell: str = "", notes: List[str] = None,
               timeout: float = 0.0) -> str:
        """渲染成工具返回值（保持宿主 execute_command 的段落格式）。"""
        parts: List[str] = []
        head = f"[Exit Code]: {self.code}"
        if shell:
            head += f" | [Shell]: {shell}"
        parts.append(head)
        if notes:
            parts.append("[Translated]: " + "；".join(notes))
        if self.timed_out:
            parts.append(f"[Timed Out]: 命令超过 {timeout:g}s 被终止；"
                         f"下一步可用更长 timeout 或 background=true 重试。")
        if self.session_lost:
            parts.append("[Session]: 会话已重启（上一条命令超时或进程异常）。"
                         "变量与当前目录已重置，文件系统改动不受影响。")
        # 首尾空白统一收掉：PowerShell 的表格输出前面固定带空行，
        # 原样回给模型会让它以为「空行 + 表格」是两个内容块。
        out = _tidy(self.out)
        err = _tidy(self.err)
        if out:
            parts.append(f"[STDOUT]:\n{out}")
        if err:
            parts.append(f"[STDERR]:\n{err}")
        if not out and not err:
            parts.append("[No output]")
        if self.code == -1 and out:
            # 原生命令被 `Select-Object -First` 提前关管道时，$LASTEXITCODE 会被
            # 置为 -1——这是 PowerShell 的已知行为，不是命令本身失败。
            parts.append("[Note]: Exit Code -1 通常表示上游原生命令因管道提前关闭而终止"
                         "（Select-Object -First 的副作用），不代表命令失败。")
        return "\n".join(parts)


# ---------------------------------------------------------------- 进程工具


def _popen_flags() -> Dict[str, Any]:
    """子进程额外参数：Windows 下不弹黑框。"""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def kill_tree(pid: Optional[int]) -> None:
    """杀整棵进程树（命令超时时必须带走孙进程，否则 npm/pyinstaller 留孤儿）。"""
    if not pid or pid <= 0:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}))
        else:
            import signal

            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    except (OSError, subprocess.SubprocessError):
        logger.debug("[ps] kill_tree(%s) 未命中", pid)


def find_powershell(prefer: str = "auto") -> Tuple[str, str]:
    """定位 PowerShell 可执行文件，返回 (路径, 说明)。

    优先 pwsh（PowerShell 7+，支持 `&&`/`||`、默认 UTF-8、启动更快），
    退回 Windows PowerShell 5.1（Windows 自带，一定存在）。
    """
    candidates: List[Tuple[str, str]] = []
    if prefer in ("auto", "pwsh"):
        candidates.append(("pwsh", "PowerShell 7+"))
    if prefer in ("auto", "powershell", "windows-powershell"):
        candidates.append(("powershell", "Windows PowerShell 5.1"))
    for exe, label in candidates:
        path = _which(exe)
        if path:
            return path, label
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    fallback = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0",
                            "powershell.exe")
    if os.path.isfile(fallback):
        return fallback, "Windows PowerShell 5.1"
    return "powershell", "Windows PowerShell"


def _which(exe: str) -> str:
    from shutil import which

    found = which(exe) or ""
    if found:
        return found
    if exe == "powershell" and IS_WINDOWS:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        path = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0",
                            "powershell.exe")
        return path if os.path.isfile(path) else ""
    if exe == "pwsh" and IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            path = os.path.join(base, "PowerShell", "7", "pwsh.exe")
            if os.path.isfile(path):
                return path
    return ""


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """给 PowerShell 与它拉起的子进程准备环境变量。

    - `PYTHONUTF8` / `PYTHONIOENCODING`：让 Python 子进程按 UTF-8 输出，
      否则中文在管道里必然乱码（本机实测：不加这两项 python 打出 GBK）。
    - `TERM=dumb` / `NO_COLOR=1`：关掉 ANSI 色码与进度条，工具输出更干净。
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("TERM", "dumb")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("GIT_PAGER", "cat")
    env.setdefault("PAGER", "cat")
    if extra:
        env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


# ---------------------------------------------------------------- 持久会话


class PsSession:
    """一个长期存活的 PowerShell 会话（单 worker 进程，串行执行）。

    线程安全：`run()` 全程持锁。命令之间本来就该串行（共享变量与 cwd），
    并发调用只会互相污染，所以宁可排队等待，并在等不到时明确报忙。
    """

    def __init__(self, workspace: str, *, encoding: str = "utf-8",
                 max_output: int = 60000, exe: str = "", label: str = "") -> None:
        self.workspace = os.path.abspath(workspace) if workspace else os.getcwd()
        self.encoding = encoding
        self.max_output = max_output
        self.exe = exe or find_powershell()[0]
        self.label = label or "PowerShell"
        self._proc: Optional[subprocess.Popen] = None
        self._dir: str = ""
        self._lock = threading.RLock()
        self._seq = 0
        self.last_used = 0.0
        self.restarts = 0
        self._stderr_path = ""
        self._stderr_fp = None
        self._stdout_tail = b""
    # ------------------------------------------------------------ 生命周期

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """启动 worker（已存活则跳过）。启动本身约 0.5s，故配合 prewarm 使用。"""
        with self._lock:
            if self.alive:
                return
            self._cleanup_locked()
            self._dir = tempfile.mkdtemp(prefix="litework_ps_")
            script_path = os.path.join(self._dir, "worker.ps1")
            # ⚠ 必须带 UTF-8 BOM：Windows PowerShell 5.1 读取**无 BOM** 的 .ps1 时
            # 按系统 ANSI 代码页（中文系统是 GBK）解码，脚本里的非 ASCII 字符
            # 会被拆成替换字符，直接产生语法错误、worker 启动即崩——实测踩过。
            # 同时脚本本体保持纯 ASCII（双保险：改编码表也跑得动）。
            with open(script_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
                f.write(WORKER_PS1)
            self._stderr_path = os.path.join(self._dir, "worker.stderr")
            self._stderr_fp = open(self._stderr_path, "wb")
            args = [self.exe, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", script_path]
            if self.encoding and self.encoding.lower() not in ("utf-8", "utf8"):
                args += ["-Encoding", self.encoding]
            try:
                self._proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    # stdout 必须是 PIPE：**原生命令的 stdout 直接继承 worker 的
                    # stdout**，不会进 PowerShell 对象流（EndInvoke 只收对象）。
                    # 设成 DEVNULL 会让 `python -c print(...)` / `git status`
                    # 的输出整体丢失——实测踩过，是本插件最贵的那个 bug。
                    # 协议本身走响应文件，所以这里的数据只用于排障：后台线程
                    # 持续排空丢弃（不排空会让 pipe 满、子进程阻塞）。
                    stdout=subprocess.PIPE,
                    stderr=self._stderr_fp,
                    cwd=self.workspace,
                    env=child_env(),
                    **_popen_flags(),
                )
            except OSError as exc:
                self._proc = None
                logger.warning("[ps] 启动 PowerShell worker 失败: %s", exc)
                raise
            self._start_drain(self._proc)
            logger.info("[ps] worker 已启动 pid=%s exe=%s", self._proc.pid, self.exe)

    @staticmethod
    def _start_drain(proc: subprocess.Popen) -> None:
        """后台排空 worker 的 stdout，防 pipe 写满卡住子进程。

        数据本身不需要：响应走响应文件，原生命令的输出已由 PowerShell 侧的
        `$res | Out-String` 收集。这里只保留末尾一小段，进程异常退出时可用于
        排障（比如 PowerShell 自己的崩溃信息）。
        """

        def _drain() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    buf = self._stdout_tail + chunk
                    self._stdout_tail = buf[-4096:]
            except (OSError, ValueError):
                pass

        thread = threading.Thread(target=_drain, name="ps-drain", daemon=True)
        thread.start()

    def stdout_tail(self, limit: int = 800) -> str:
        """worker 原始 stdout 的尾部（排障用）。"""
        return self._stdout_tail.decode("utf-8", "replace")[-limit:].strip()

    def stop(self) -> None:
        with self._lock:
            self._cleanup_locked()

    def _cleanup_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.poll() is None and proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
            except OSError:
                pass
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # 关掉两条管道：排空线程持有 stdout，不关会在解释器退出时报
            # ResourceWarning（单测里能看到），长期运行则泄漏句柄。
            for stream in (proc.stdout, proc.stdin, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        if self._stderr_fp is not None:
            try:
                self._stderr_fp.close()
            except OSError:
                pass
            self._stderr_fp = None
        if self._dir and os.path.isdir(self._dir):
            for name in os.listdir(self._dir):
                try:
                    os.remove(os.path.join(self._dir, name))
                except OSError:
                    pass
            try:
                os.rmdir(self._dir)
            except OSError:
                pass
            self._dir = ""

    def stderr_tail(self, limit: int = 800) -> str:
        """worker 的诊断尾巴（进程异常退出时排障用）。

        优先 stderr；stderr 为空（PowerShell 把解析错误等写到别处的情况）
        回退到 stdout 尾巴——两条都为空的诊断信息对排障毫无用处。
        """
        if self._stderr_path:
            try:
                with open(self._stderr_path, "rb") as f:
                    data = f.read()
                text = data.decode("utf-8", "replace").strip()
                if text:
                    return text[-limit:]
            except OSError:
                pass
        return self.stdout_tail(limit)

    # ------------------------------------------------------------ 执行

    def run(self, script: str, *, timeout: float = 60.0,
            cwd: Optional[str] = None, lock_timeout: float = 30.0) -> CmdResult:
        """在会话里执行一段脚本。

        cwd 只在**首次**与显式指定时下发；不指定则沿用会话当前目录——
        这样模型 `cd` 之后的下一条命令自然在同一个目录里（shell 语义）。
        """
        acquired = self._lock.acquire(timeout=max(1.0, lock_timeout))
        if not acquired:
            return CmdResult(
                code=1, err="另一条命令正在同一 PowerShell 会话里执行（命令串行执行）；"
                            "请等待上一条完成，或用 background=true 并行。")
        try:
            if not self.alive:
                self.start()
            self._seq += 1
            rid = f"r{self._seq}_{uuid.uuid4().hex[:6]}"
            resp_path = os.path.join(self._dir, rid + ".json")
            payload = {
                "b64": base64.b64encode(script.encode("utf-8")).decode("ascii"),
                "resp": resp_path,
                "timeout_ms": int(max(1.0, timeout) * 1000),
                "max_chars": int(self.max_output),
            }
            if cwd:
                payload["cwd"] = os.path.abspath(cwd)
            line = json.dumps(payload, ensure_ascii=True) + "\n"
            started = time.perf_counter()
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
            except (OSError, ValueError, AssertionError) as exc:
                self.restarts += 1
                tail = self.stderr_tail()
                return CmdResult(code=1, session_lost=True,
                                 err=f"写入 PowerShell 会话失败：{exc}"
                                     + (f"\n[worker stderr]:\n{tail}" if tail else ""),
                                 ms=int((time.perf_counter() - started) * 1000))
            result = self._await_response(resp_path, timeout, started)
            self.last_used = time.time()
            return result
        finally:
            try:
                self._lock.release()
            except RuntimeError:
                pass

    def _await_response(self, resp_path: str, timeout: float,
                        started: float) -> CmdResult:
        """等响应文件出现。超时 → 杀整棵进程树（会话下次自动重建）。"""
        deadline = started + max(1.0, timeout) + 2.0
        delay = 0.002
        while True:
            if os.path.exists(resp_path):
                break
            if time.perf_counter() > deadline:
                self._kill_after_timeout()
                return CmdResult(code=TIMEOUT_EXIT_CODE, timed_out=True,
                                 session_lost=True,
                                 err=f"命令超过 {timeout:g}s 未完成，worker 已被终止；"
                                     f"变量与当前目录已重置。",
                                 ms=int((time.perf_counter() - started) * 1000))
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                tail = self.stderr_tail()
                self._proc = None
                return CmdResult(code=1, session_lost=True,
                                 err="PowerShell worker 意外退出。"
                                     + (f"\n[worker stderr]:\n{tail}" if tail else ""),
                                 ms=int((time.perf_counter() - started) * 1000))
            time.sleep(delay)
            if delay < 0.02:
                delay += 0.002

        data = _read_shared(resp_path)
        try:
            os.remove(resp_path)
        except OSError:
            pass
        if data is None:
            return CmdResult(code=1,
                             err="读取结果失败：响应文件被占用（重试后仍无法读取）。")
        try:
            obj = json.loads(data)
        except ValueError:
            return CmdResult(code=1, err="结果解析失败（worker 协议异常）。\n" + data[:500])
        return CmdResult(
            code=int(obj.get("code") or 0),
            out=str(obj.get("out") or ""),
            err=str(obj.get("err") or ""),
            ms=int(obj.get("ms") or 0),
            cwd=str(obj.get("cwd") or ""),
            timed_out=bool(obj.get("timed_out")),
            truncated=bool(obj.get("truncated")),
        )

    def _kill_after_timeout(self) -> None:
        """超时处置：杀整棵树并丢弃会话（比 `$ps.Stop()` 更彻底）。"""
        self.restarts += 1
        with self._lock:
            self._cleanup_locked()


# ---------------------------------------------------------------- 一次性进程


def run_oneshot(script: str, *, workspace: str, timeout: float = 60.0,
                encoding: str = "utf-8", max_output: int = 60000,
                exe: str = "") -> CmdResult:
    """一次性进程执行（每次干净环境；比持久会话慢约一个进程启动时间）。"""
    exe = exe or find_powershell()[0]
    preamble = ONESHOT_PREAMBLE
    if encoding and encoding.lower() not in ("utf-8", "utf8"):
        preamble = ONESHOT_PREAMBLE.replace(
            "[Text.UTF8Encoding]::new($false)",
            "[Text.Encoding]::GetEncoding(936)"
        )
    full = preamble + script + "\nexit $(if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 })\n"
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    args = [exe, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    started = time.perf_counter()
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            args, cwd=os.path.abspath(workspace) if workspace else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, env=child_env(), **_popen_flags())
        out_b, err_b = proc.communicate(timeout=max(1.0, timeout))
        code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        if proc is not None:
            kill_tree(proc.pid)
            try:
                proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
        out_b, err_b, code, timed_out = b"", b"", TIMEOUT_EXIT_CODE, True
    except OSError as exc:
        return CmdResult(code=1, err=f"启动 PowerShell 失败：{exc}")
    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    out, cut_out = _clamp(out, max_output)
    err, cut_err = _clamp(err, max_output)
    return CmdResult(code=int(code or 0), out=out, err=err,
                     ms=int((time.perf_counter() - started) * 1000),
                     timed_out=timed_out, truncated=cut_out or cut_err,
                     session_lost=timed_out)


def spawn_detached(script: str, *, cwd: str, timeout: float, exe: str = "",
                   out_path: str, err_path: str,
                   encoding: str = "utf-8") -> subprocess.Popen:
    """启动**后台**命令：独立一次性进程，输出直接落盘。

    为什么后台任务不复用持久会话：会话里的命令是串行的（共享变量与 cwd），
    一条 `pip install` 会把会话占住几分钟，期间任何前台命令都只能排队——
    那正是后台模式要解决的问题。独立进程的代价是丢会话变量，可以接受
    （长任务本来就不该依赖会话状态）。

    超时由 `wait_job` 侧兜：这里只负责起进程与重定向。
    """
    exe = exe or find_powershell()[0]
    preamble = ONESHOT_PREAMBLE
    if encoding and encoding.lower() not in ("utf-8", "utf8"):
        preamble = ONESHOT_PREAMBLE.replace(
            "[Text.UTF8Encoding]::new($false)",
            "[Text.Encoding]::GetEncoding(936)"
        )
    full = preamble + script + "\nexit $(if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 })\n"
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    args = [exe, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    out_fp = open(out_path, "wb")
    err_fp = open(err_path, "wb")
    try:
        proc = subprocess.Popen(
            args, cwd=os.path.abspath(cwd) if cwd else None,
            stdout=out_fp, stderr=err_fp, stdin=subprocess.DEVNULL,
            env=child_env(), **_popen_flags())
    except OSError:
        out_fp.close()
        err_fp.close()
        raise
    # 句柄已交给子进程（Windows 上子进程有独立句柄）：父侧副本立刻关闭，
    # 否则后台任务结束时文件句柄会被父进程一直占着，读出来可能是空。
    out_fp.close()
    err_fp.close()
    return proc


def _read_shared(path: str, attempts: int = 12) -> Optional[str]:
    """读一个「另一个进程刚写完」的文件，容忍短暂共享冲突。

    为什么要重试：PowerShell 用 `[IO.File]::WriteAllText` + `Move-Item` 落盘，
    文件只要**出现**就说明写完了；但 Windows 上 Move 之后源/目标的句柄释放
    有一个极短的窗口，此时 Python 侧 `open()` 会拿到
    `PermissionError: [Errno 13]`。实测在繁体中文输出（内容较长、写得慢）时
    偶发——这会把一条成功的命令报成「读取结果失败」，比慢一点糟得多。

    读到空文件也当作“还没写完”重试：Move 是原子的，出现空文件意味着读到的是
    中间态（WriteAllText 先建文件再写内容的实现细节）。
    """
    delay = 0.002
    for attempt in range(max(1, attempts)):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8-sig", "replace")
            if text.strip():
                return text
        except (PermissionError, FileNotFoundError, OSError):
            pass
        if attempt + 1 < attempts:
            time.sleep(delay)
            delay = min(delay * 1.6, 0.05)
    return None


def _tidy(text: str) -> str:
    """收掉行尾空白与首尾空行——但**保留行内空白**。

    为什么必须做：PowerShell 的表格渲染会把每行 pad 到 Out-String 的宽度
    （为了不截断长路径，worker 里设的是 4000 列）。直接回给模型的结果里，
    每行后面能跟上 3900 个空格——既浪费 token 又让结果不可读（实测：一次
    `ls` 输出 ≈ 40KB，全是空格）。只 rstrip 不加任何其它处理，表格对齐不受影响。
    """
    if not text:
        return ""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip("\n").strip()


def _clamp(text: str, cap: int) -> Tuple[str, bool]:
    if cap <= 0 or len(text) <= cap:
        return text, False
    head = int(cap * 0.7)
    tail = cap - head
    omitted = len(text) - cap
    return (text[:head] + f"\n... [输出截断：省略 {omitted} 字符] ...\n"
            + text[-tail:]), True


# ---------------------------------------------------------------- 会话池


class SessionPool:
    """按 (工作区, 编码, 上限) 复用会话；超过空闲 TTL 自动回收。"""

    def __init__(self, idle_ttl: float = 900.0) -> None:
        self.idle_ttl = idle_ttl
        self._sessions: Dict[str, PsSession] = {}
        self._lock = threading.RLock()
        self._warm_threads: Dict[str, threading.Thread] = {}

    @staticmethod
    def _key(workspace: str, encoding: str, max_output: int) -> str:
        return f"{os.path.abspath(workspace or '.')}|{encoding}|{max_output}"

    def acquire(self, workspace: str, *, encoding: str = "utf-8",
                max_output: int = 60000, exe: str = "",
                label: str = "") -> PsSession:
        self.reap()
        key = self._key(workspace, encoding, max_output)
        with self._lock:
            session = self._sessions.get(key)
            if session is not None and session.alive:
                return session
            if session is not None:
                session.stop()
            session = PsSession(workspace, encoding=encoding,
                                max_output=max_output, exe=exe, label=label)
            self._sessions[key] = session
            return session

    def prewarm(self, workspace: str, *, encoding: str = "utf-8",
                max_output: int = 60000, exe: str = "", label: str = "") -> None:
        """后台预热会话：把 ~0.5s 的启动成本挪到模型发命令之前。

        幂等：同一 key 只有一个预热线程；失败只记日志（预热失败不该影响工具）。
        """
        key = self._key(workspace, encoding, max_output)
        with self._lock:
            runner = self._warm_threads.get(key)
            if runner is not None and runner.is_alive():
                return
            session = self._sessions.get(key)
            if session is not None and session.alive:
                return

            def _warm() -> None:
                try:
                    self.acquire(workspace, encoding=encoding,
                                 max_output=max_output, exe=exe, label=label).start()
                except Exception as exc:              # noqa: BLE001 - 预热绝不外抛
                    logger.debug("[ps] 预热失败: %s", exc)

            thread = threading.Thread(target=_warm, name="ps-prewarm", daemon=True)
            self._warm_threads[key] = thread
            thread.start()

    def reap(self, force: bool = False) -> int:
        """回收空闲超时的会话，返回被关闭的数量。"""
        now = time.time()
        closed = 0
        with self._lock:
            for key, session in list(self._sessions.items()):
                idle = now - (session.last_used or now)
                if force or (not session.alive and idle > 30) or idle > self.idle_ttl:
                    try:
                        session.stop()
                    except Exception:             # noqa: BLE001
                        logger.debug("[ps] 回收会话失败", exc_info=True)
                    self._sessions.pop(key, None)
                    closed += 1
        return closed

    def close_all(self) -> None:
        self.reap(force=True)

    def describe(self) -> List[Dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.items())
        out: List[Dict[str, Any]] = []
        for key, session in sessions:
            workspace = key.split("|", 1)[0]
            out.append({
                "workspace": workspace,
                "alive": session.alive,
                "pid": session._proc.pid if session._proc is not None else None,
                "idle_seconds": round(time.time() - (session.last_used or time.time()), 1),
                "restarts": session.restarts,
            })
        return out


#: 进程级单例：会话要跨插件实例（每个任务都会新建插件实例）复用，
#: 否则「持久会话」在每次任务装配后就失效，加速效果等于没有。
POOL = SessionPool()


def shutdown() -> None:
    """退出时关掉所有 worker（避免留下常驻 powershell 进程）。"""
    try:
        POOL.close_all()
    except Exception:                                  # noqa: BLE001
        logger.debug("[ps] 关闭会话池失败", exc_info=True)
