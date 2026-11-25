# lite-work 社区插件 powershell-plugin 的翻译层（纯函数，零 PowerShell 依赖）。
#
# 为什么需要这一层
# ---------------------------------------------------------------------------
# Windows 上 Agent 写出的命令绝大多数是「类 Unix 写法」：`ls -la`、`rm -rf`、
# `cat f | head -n 5`、`a && b`、`export FOO=bar`。PowerShell 5.1 与它们的关系是
# 「部分别名 + 参数名不兼容 + `&&` 直接语法错误」：
#
#   PS> ls -la            → A parameter cannot be found that matches 'la'
#   PS> rm -rf x          → A parameter cannot be found that matches 'rf'
#   PS> echo a && echo b  → The token '&&' is not a valid statement separator
#   PS> head -n 5 f       → The term 'head' is not recognized
#
# 于是「第一次跑命令」几乎必然失败，模型再改一版、再失败，成功率与速度双输。
# 这一层把这些**能确定语义**的写法改写成等价的 PowerShell，并把每处改写记进
# notes 随结果返回——模型能看到「我改了什么」，不需要靠猜。
#
# 设计红线（改这个文件前先读）
# ---------------------------------------------------------------------------
# 1. **保守优先**：只改写语义无歧义的写法。拿不准就原样放过，让 PowerShell
#    自己报错——报错可修，改错会静默产生错误的副作用（写错文件、删错目录）。
# 2. **只改结构，不改参数值**：绝不碰引号里的内容、变量值、路径字面量。
# 3. **纯函数**：不读环境、不启动进程、不碰文件系统。单测可以完全离线跑。
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

#: Unix 环境变量 → Windows 对应项。
#:
#: 只收「在 Windows 上有确定对应物」的少数几个。`$HOME` 这类写法在 PowerShell
#: 里是**未定义变量**（静默变成空串，比报错更糟：`cd $HOME` 会退到默认目录），
#: 所以必须改写；而任意 `$FOO` 无法判断是环境变量还是脚本变量，一律不动。
ENV_ALIASES = {
    "HOME": "env:USERPROFILE",
    "USER": "env:USERNAME",
    "USERNAME": "env:USERNAME",
    "PATH": "env:PATH",
    "SHELL": "env:COMSPEC",
    "TMPDIR": "env:TEMP",
    "LANG": "env:LANG",
}

#: 明确拒绝的写法：Windows 上没有对应语义，与其让 PowerShell 报一句
#: 「无法识别 sudo」，不如直接说明原因（模型据此换方案而不是换拼写）。
BLOCKED_PREFIXES = {
    "sudo": "Windows 没有 sudo（无 POSIX 提权模型）。需要管理员权限时请让用户在管理员终端里操作。",
    "chown": "Windows 无 chown 语义（ACL 由 icacls / 属性页管理）。",
    "chmod": "Windows 无 chmod 语义；可执行权限由扩展名与 ACL 决定。",
    "apt": "Windows 无 apt；请用 winget / choco / scoop（若已安装）。",
    "apt-get": "Windows 无 apt-get；请用 winget / choco / scoop（若已安装）。",
    "brew": "Windows 无 brew（除非 WSL）；请用 winget / choco / scoop。",
}

#: 递归删除的危险目标：改名之后它们就**从「bash 里失败」变成「PowerShell 里成功」**。
#:
#: 这是翻译层最需要小心的地方：`rm -rf /` 在 bash 里会被 `--no-preserve-root` 挡住，
#: 但改写成 `Remove-Item -Recurse -Force /` 之后，`/` 在 PowerShell 里会解析成
#: **当前盘的根目录**——原本失败的写法变成了真正执行的灾难。`~` 同理（PowerShell
#: 里 `~` 就是家目录，而 bash 的 `rm -rf ~` 至少需要用户亲手敲）。
#: 所以这不是「过滤」，而是「不许把一条安全的废话改写成危险命令」。
DANGEROUS_TARGETS = {
    "/", "\\", "/*", "\\*", "/*.*", ".", "..", "~", "~/", "~\\", "~/*",
    "$HOME", "${HOME}", "$env:USERPROFILE", "$env:SystemRoot", "C:\\", "C:/",
}

#: 递归删除的通配/根路径形态：`/*`、`./*`、`C:\*` 之类。
#:
#: 为什不能用「看起来像开关就当开关」：`rm -rf /*` 里的 `/*` 以 `/` 开头，
#: 会被 `_flags_and_args` 当成 cmd 风格开关吃掉，于是危险目标被误认为“没有目标”。
#: 安全判定必须看**全部** token，不能只看看参。
_DANGER_TARGET_RE = re.compile(
    r"^(?:\.?[\\/])*(?:\*|\.\*|\*\.\*)?$")


def _is_dangerous_target(token: str) -> bool:
    """这个 token 是不是「递归删除就会造成灾难」的目标。"""
    plain = _unquote(token).strip()
    if not plain:
        return False
    if plain in DANGEROUS_TARGETS:
        return True
    # 盘符根：C: / C:\ / C:/*
    if re.fullmatch(r"[A-Za-z]:[\\/]*(?:\*)?", plain):
        return True
    # 纯通配 / 当前目录通配：* / .* / /* / ./* / \* 等
    return bool(_DANGER_TARGET_RE.match(plain) and "*" in plain)

#: `cd` 之外最常用的「能确定等价物」的首命令改写表。
#: 每项：命令名 → 处理函数(tokens, ctx) → 新脚本片段。
_Rewrite = Callable[[List[str], "Ctx"], Tuple[str, List[str]]]


@dataclass
class Ctx:
    """改写上下文：让处理函数知道自己在哪个位置、有哪些配置。"""

    #: 是否管道的第一段（首段命令的 `head` 取行，末段的 `head` 取对象）
    first: bool = True
    #: 是否假定文本文件是 UTF-8（决定 Get-Content 是否加 -Encoding）
    assume_utf8: bool = True
    #: 改写备注收集器（只追加，不读）
    notes: List[str] = field(default_factory=list)


@dataclass
class Translation:
    """一次翻译的结果。"""

    script: str
    notes: List[str] = field(default_factory=list)
    #: 非空表示拒绝执行（附原因）；此时 script 为空
    blocked: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.notes)


# ---------------------------------------------------------------- 词法：分段与分词


def _split_top_level(text: str, ops: Tuple[str, ...]) -> Tuple[List[str], List[str]]:
    """按 `ops` 在**最外层**切分，返回 (片段, 操作符) 且 len(片段) == len(操作符)+1。

    照顾三种会让「看似顶层」变「其实在内部」的情况：引号、括号/子表达式、
    反引号转义。`"a && b"` 是一个参数、`$(a && b)` 是子表达式，都不能切。
    """
    parts: List[str] = []
    found: List[str] = []
    buf: List[str] = []
    quote = ""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "`" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0:
            hit = next((op for op in ops if text.startswith(op, i)), "")
            if hit:
                parts.append("".join(buf))
                found.append(hit)
                buf = []
                i += len(hit)
                continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts, found


def _split_tokens(segment: str) -> List[str]:
    """把一段命令切成 token（保留引号原文），只用于「看页首命令与开关」。

    不是为了重新拼装命令——改写函数只输出新片段，没被改写的 token 原样带走，
    所以这里的引号处理只要保证**不切错边界**即可。
    """
    tokens: List[str] = []
    buf: List[str] = []
    quote = ""
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if quote:
            buf.append(ch)
            if ch == "`" and quote == '"' and i + 1 < n:
                buf.append(segment[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        # 管道/重定向/分隔符本身也该独立成 token（`cat f|head` 没有空格）
        if ch in "|;":
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _quote(value: str) -> str:
    """PowerShell 单引号字面量（内部 `'` 双写）。"""
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------- 首命令改写表


def _flags_and_args(tokens: List[str]) -> Tuple[List[str], List[str]]:
    """拆出（紧跟在命令名后的开关，其余参数）。

    只吃「连续的、以 `-`/`/` 开头」的 token 作为开关，遇到第一个普通参数就停。
    这样 `grep -rn pat path` 的 `pat`/`path` 不会被误当开关，而
    `ls -la dir -R` 的尾随 `-R` 会留在参数里原样带走（不猜它的语义）。
    """
    flags: List[str] = []
    i = 0
    while i < len(tokens) and tokens[i].startswith(("-", "/")) and len(tokens[i]) > 1:
        flags.append(tokens[i])
        i += 1
    return flags, tokens[i:]


def _expand_short_flags(flags: List[str]) -> List[str]:
    """把 `-la` / `-rf` 这类合并短开关拆成 `['-l', '-a']` / `['-r', '-f']`。

    只拆**全小写**的纯 ASCII 字母且不含 `=` 的（`-rf`、`-la`、`-rn`）。
    大写开头的参数（`-Name`、`-Recurse`、`-Force`）是 PowerShell 风格的长
    参数名，拆开就变成四个无效开关——`-Name` 变 `-N -a -m -e` 会让
    "本来能跑的命令"直接报错。Unix 短开关的约定就是小写字母，划界线可靠。
    """
    out: List[str] = []
    for flag in flags:
        body = flag[1:]
        if flag.startswith("-") and len(body) > 1 and body.isascii() and body.isalpha() \
                and body.islower():
            out.extend("-" + c for c in body)
        else:
            out.append(flag)
    return out


def _rw_ls(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    """`ls`/`dir` 的 Unix 开关 → Get-ChildItem。"""
    flags, rest = _flags_and_args(tokens[1:])
    keep: List[str] = []
    force = False
    notes: List[str] = []
    for flag in _expand_short_flags(flags):
        low = flag.lower()
        if low in ("-l", "-h", "-1", "-lh", "-t", "-n"):
            notes.append(f"{flag} 无对应语义，已丢弃")
        elif low in ("-a", "-all"):
            force = True
        elif low in ("-la", "-al", "-lha"):
            force = True
            notes.append(f"{flag} 无对应语义，只保留 -Force")
        elif low.startswith("--"):
            notes.append(f"{flag} 无对应语义，已丢弃")
        elif low == "/b":                     # cmd 的 `dir /b`
            keep.append("-Name")
        else:
            keep.append(flag)
    cmd = "Get-ChildItem" + (" -Force" if force else "") + "".join(
        f" {k}" if k else "" for k in keep)
    return cmd, rest, notes


def _rw_remove(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    """`rm -rf` → Remove-Item -Recurse -Force（`-r`/`-f` 是前缀，PS 认）。"""
    flags, rest = _flags_and_args(tokens[1:])
    recurse = force = False
    notes: List[str] = []
    for flag in _expand_short_flags(flags):
        low = flag.lower()
        if low in ("-r", "-recurse"):
            recurse = True
        elif low in ("-f", "-force"):
            force = True
        elif low.startswith("--"):
            notes.append(f"{flag} 无对应语义，已丢弃")
        else:
            notes.append(f"{flag} 无对应语义，已丢弃")
    cmd = "Remove-Item"
    if recurse:
        cmd += " -Recurse"
    if force:
        cmd += " -Force"
    return cmd, rest, notes

def _rw_copy_move(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    verb = "Copy-Item" if tokens[0].lower() in ("cp", "copy") else "Move-Item"
    flags, rest = _flags_and_args(tokens[1:])
    recurse = force = False
    notes: List[str] = []
    for flag in _expand_short_flags(flags):
        low = flag.lower()
        if low in ("-r", "-recurse", "-a"):
            recurse = True
        elif low in ("-f", "-force"):
            force = True
        else:
            notes.append(f"{flag} 无对应语义，已丢弃")
    cmd = verb
    if recurse and verb == "Copy-Item":
        cmd += " -Recurse"
    if force:
        cmd += " -Force"
    return cmd, rest, notes


def _rw_mkdir(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    flags, rest = _flags_and_args(tokens[1:])
    parents = any(f.lower() in ("-p", "--parents") for f in flags)
    dropped = [f for f in flags if f.lower() not in ("-p", "--parents")]
    notes = [f"{f} 无对应语义，已丢弃" for f in dropped]
    cmd = "New-Item -ItemType Directory" + (" -Force" if parents else "")
    return cmd, rest, notes


def _rw_cat(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    """`cat f` → Get-Content。

    5.1 的 Get-Content 默认按**系统 ANSI 代码页**解码，读 UTF-8 文件会得到乱码
    （实测：`涓\ue15f枃琛?`）。所以默认补 `-Encoding UTF8`；老 GBK 工程可以关掉
    （配置项 assume_utf8_files=false）。
    """
    flags, rest = _flags_and_args(tokens[1:])
    notes: List[str] = []
    keep = []
    for flag in flags:
        if flag.lower() in ("-n",):
            notes.append("-n 无对应语义，已丢弃（cat 不分页）")
        else:
            keep.append(flag)
    has_encoding = any(f.lower().startswith("-encoding") for f in keep)
    cmd = "Get-Content"
    if ctx.assume_utf8 and not has_encoding:
        cmd += " -Encoding UTF8"
        notes.append("补 -Encoding UTF8（5.1 默认按 ANSI 解码，UTF-8 文件会乱码）")
    for k in keep:
        cmd += f" {k}"
    return cmd, rest, notes


def _count_from_args(tokens: List[str], ctx: Ctx) -> Tuple[Optional[int], List[str], List[str]]:
    """解析 `head/tail` 的行数：`-n 5`、`-5`、`-n5`、`--lines=5`；缺省 10。"""
    count: Optional[int] = None
    rest: List[str] = []
    notes: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low == "-n" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            count = int(tokens[i + 1])
            i += 2
            continue
        if re.fullmatch(r"-n\d+", low):
            count = int(low[2:])
            i += 1
            continue
        if re.fullmatch(r"-\d+", tok):
            count = int(tok[1:])
            i += 1
            continue
        if low.startswith("--lines="):
            body = tok.split("=", 1)[1]
            if body.isdigit():
                count = int(body)
            i += 1
            continue
        if low == "-f":
            notes.append("-f（持续跟随）在 PowerShell 无对应语义，已丢弃")
            i += 1
            continue
        rest.append(tok)
        i += 1
    return count, rest, notes


def _rw_head(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    count, rest, notes = _count_from_args(tokens[1:], ctx)
    n = 10 if count is None else count
    if ctx.first:
        return f"Get-Content -TotalCount {n}" + (
            " -Encoding UTF8" if ctx.assume_utf8 else ""), rest, notes
    return f"Select-Object -First {n}", rest, notes


def _rw_tail(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    count, rest, notes = _count_from_args(tokens[1:], ctx)
    n = 10 if count is None else count
    if ctx.first:
        return f"Get-Content -Tail {n}" + (
            " -Encoding UTF8" if ctx.assume_utf8 else ""), rest, notes
    return f"Select-Object -Last {n}", rest, notes


def _rw_grep(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    """`grep` → 文件场景 Select-String / 管道场景 Where-Object。

    两种场景必须分开处理：
    - **有文件/递归**：Select-String 天然输出 `文件:行号:内容`，语义最贴近 grep。
    - **管道里**（`ps aux | grep python`、`git log | grep fix`）：上游可能是
      **对象**而不是文本行。`Select-String` 对对象会调到 ToString()（实测
      `Get-Process | Select-String python` 什么都匹不上），必须用
      `Where-Object { \"$_\" -match ... }`——它对文本行与对象都能工作。

    大小写：grep 默认**区分**大小写，所以文件场景补 `-CaseSensitive`，
    管道场景用 `-cmatch`。给了 -i 时两者都改成不区分。
    """
    flags, rest = _flags_and_args(tokens[1:])
    recursive = case_insensitive = files_only = invert = False
    notes: List[str] = []
    for flag in _expand_short_flags(flags):
        low = flag.lower()
        if low in ("-r", "-recursive"):
            recursive = True
        elif low in ("-i", "-ignore-case"):
            case_insensitive = True
        elif low in ("-n", "-line-number"):
            pass                                    # Select-String 自带行号
        elif low in ("-l", "-files-with-matches"):
            files_only = True
        elif low in ("-v", "-invert-match"):
            invert = True
        elif low in ("-e",):
            pass
        else:
            notes.append(f"{flag} 无对应语义，已丢弃")
    if not rest:
        return "Select-String", [], notes + ["grep 缺少模式，未改写"]
    pattern = rest[0]
    paths = rest[1:]

    if not ctx.first:
        # 管道场景：对上游对象/文本行都成立的过滤。
        #
        # 两个必须注意的细节（都实测过）：
        # ① pattern 必须引起来：`-cmatch python` 会被解析成「变量 $python」→
        #    报 "You must provide a value expression following '-cmatch'"；
        # ② `"$_"` 把对象转成字符串再匹配：上游可能是 Get-Process 的对象
        #    （直接 -cmatch 会拿 ProcessName 以外的属性去比），也可能是文本行。
        operator = "-cnotmatch" if not case_insensitive else "-notmatch"
        if not invert:
            operator = "-cmatch" if not case_insensitive else "-match"
        notes.append("管道内的 grep → Where-Object 过滤（Select-String 只认文本，"
                     "对对象无效）")
        literal = pattern if pattern[:1] in "'\"" else _quote(pattern)
        return f'Where-Object {{ "$_" {operator} {literal} }}', [], notes

    cmd = "Select-String -Pattern " + pattern
    if not case_insensitive:
        cmd += " -CaseSensitive"
    if recursive:
        target = paths[0] if paths else "."
        cmd = f"Get-ChildItem -LiteralPath {target} -Recurse -File | " + cmd
    elif paths:
        cmd += " -Path " + ",".join(paths)
    if files_only:
        cmd += " | Select-Object -ExpandProperty Path -Unique"
    return cmd, [], notes


def _rw_find(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    """`find <path> -name '*.py'` → Get-ChildItem（最常用的三个开关）。"""
    args = tokens[1:]
    path = "."
    name = ""
    file_only = False
    depth = ""
    notes: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        low = tok.lower()
        if low == "-name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
            continue
        if low == "-type" and i + 1 < len(args):
            file_only = args[i + 1].lower() in ("f", "file")
            i += 2
            continue
        if low == "-maxdepth" and i + 1 < len(args):
            if args[i + 1].isdigit():
                depth = str(max(0, int(args[i + 1]) - 1))
            i += 2
            continue
        if not tok.startswith("-") and path == ".":
            path = tok
        else:
            notes.append(f"find 的 {tok} 无对应语义，已丢弃")
        i += 1
    cmd = f"Get-ChildItem -LiteralPath {path}"
    if name:
        cmd += f" -Filter {name}"
    cmd += " -Recurse"
    if file_only:
        cmd += " -File"
    if depth:
        cmd += f" -Depth {depth}"
    return cmd + " | Select-Object -ExpandProperty FullName", [], notes


def _rw_which(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    if len(tokens) < 2:
        return "Get-Command", [], []
    notes: List[str] = []
    if len(tokens) > 2:
        notes.append("which 只取第一个参数（其余忽略）")
    return (f"Get-Command {tokens[1]} -ErrorAction SilentlyContinue "
            f"| Select-Object -First 1 -ExpandProperty Source"), [], notes


def _rw_ps(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    """`ps aux` / `ps -ef` / `ps` → Get-Process（管道到 grep 走 Select-String）。"""
    notes = ["ps → Get-Process"] if len(tokens) > 1 else []
    return "Get-Process", [], notes


def _rw_kill(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    flags, rest = _flags_and_args(tokens[1:])
    force = any(f.lower() in ("-9", "-kill", "-sigkill") for f in flags)
    if not rest:
        return "Stop-Process", [], ["kill 缺少目标进程"]
    return ("Stop-Process -Id " + " ".join(rest) + (" -Force" if force else "")), [], []


def _rw_touch(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    _, rest = _flags_and_args(tokens[1:])
    if not rest:
        return "New-Item -ItemType File -Force", [], ["touch 缺少文件名"]
    return "New-Item -ItemType File -Force -Path " + " ".join(rest), [], []


def _rw_time(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    inner = " ".join(tokens[1:]).strip()
    if not inner:
        return "Measure-Command", [], ["time 缺少命令"]
    return f"Measure-Command {{ {inner} }}", [], [
        "time → Measure-Command：只输出耗时，命令自身的输出会进 $null"]


def _rw_wc(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    flags, rest = _flags_and_args(tokens[1:])
    which = "-Line"
    for flag in _expand_short_flags(flags):
        low = flag.lower()
        if low in ("-w", "-word"):
            which = "-Word"
        elif low in ("-c", "-char", "-chars"):
            which = "-Character"
    measure = (f"Measure-Object {which} | Select-Object -ExpandProperty "
               f"{which.lstrip('-')}s")
    if rest:
        # `wc -l file`：文件参数不能被丢掉（丢了下游统计的是空管道）
        path = rest[0] if len(rest) == 1 else ""
        if not path:
            notes = ["wc 只统计第一个文件（其余忽略）"]
            path = rest[0]
        else:
            notes = []
        return (f"Get-Content -LiteralPath {path}"
                + (" -Encoding UTF8" if ctx.assume_utf8 else "") + " | " + measure,
                [], notes)
    return measure, [], []


def _rw_sort(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    flags, rest = _flags_and_args(tokens[1:])
    unique = any(f.lower() in ("-u", "-unique") for f in flags)
    reverse = any(f.lower() in ("-r", "-reverse") for f in flags)
    cmd = "Sort-Object"
    if unique:
        cmd += " -Unique"
    if reverse:
        cmd += " -Descending"
    if rest:
        # `sort file`：先读文件再排序（丢了文件参数只会排出空结果）
        path = rest[0]
        notes = [f"sort 其余参数已忽略（{len(rest) - 1} 个）"] if len(rest) > 1 else []
        return (f"Get-Content -LiteralPath {path}"
                + (" -Encoding UTF8" if ctx.assume_utf8 else "") + " | " + cmd,
                [], notes)
    return cmd, [], []


def _rw_uniq(tokens: List[str], ctx: Ctx) -> Tuple[str, List[str]]:
    return "Get-Unique", [], []


#: 改写目标的名字（备注文案用）。
#:
#: 不能“取改写结果的第一个词”——很多时候改写产出的是管道
#: （`wc -l f` → `Get-Content f | Measure-Object`），第一个词是取数据而非目标，
#: 写出来就是“wc → Get-Content”这种误导性备注。
TARGET_LABEL: Dict[str, str] = {
    "ls": "Get-ChildItem", "dir": "Get-ChildItem", "ll": "Get-ChildItem",
    "rm": "Remove-Item", "del": "Remove-Item",
    "cp": "Copy-Item", "copy": "Copy-Item", "mv": "Move-Item", "move": "Move-Item",
    "mkdir": "New-Item -ItemType Directory", "touch": "New-Item -ItemType File",
    "cat": "Get-Content", "head": "Get-Content/Select-Object",
    "tail": "Get-Content/Select-Object", "grep": "Select-String/Where-Object",
    "find": "Get-ChildItem", "which": "Get-Command", "ps": "Get-Process",
    "kill": "Stop-Process", "time": "Measure-Command",
    "wc": "Measure-Object", "sort": "Sort-Object", "uniq": "Get-Unique",
}

#: 首段命令改写表。键统一小写。
STAGE_COMMANDS: Dict[str, _Rewrite] = {    "ls": _rw_ls,
    "dir": _rw_ls,
    "ll": _rw_ls,
    "rm": _rw_remove,
    "del": _rw_remove,
    "cp": _rw_copy_move,
    "move": _rw_copy_move,
    "mv": _rw_copy_move,
    "mkdir": _rw_mkdir,
    "cat": _rw_cat,
    "head": _rw_head,
    "tail": _rw_tail,
    "grep": _rw_grep,
    "find": _rw_find,
    "which": _rw_which,
    "ps": _rw_ps,
    "kill": _rw_kill,
    "touch": _rw_touch,
    "time": _rw_time,
    "wc": _rw_wc,
    "sort": _rw_sort,
    "uniq": _rw_uniq,
}

#: 只在前段是「取数据」时才改写的管道阶段（避免把用户自己的 Sort-Object 又包一层）。
PIPE_ONLY_COMMANDS = frozenset({"grep", "head", "tail", "wc", "sort", "uniq"})

#: 改写后会把**对象**写进输出流的写操作：真实 shell 里这些命令成功时是静默的，
#: 但 PowerShell 的 New-Item 会把新建的项打印出来（一次 `mkdir -p a/b` 会刷出
#: 一整张目录表）。给它们补 `| Out-Null` 才能对齐用户的预期。
#:
#: 为什么 `| Out-Null` 而不是 `$null = ...`：前者不改变 `$?` 的判定对象
#: （管道成功与否取决于上游命令），实测 `Remove-Item <不存在> | Out-Null; $?`
#: 仍是 False，`&&` 链的短路语义因此不受影响。
SILENT_OUTPUT_COMMANDS = frozenset({
    "mkdir", "touch", "rm", "del", "erase", "cp", "copy", "mv", "move",
})

#: 管道里明确没有等价物的命令：给出可操作提示，而不是让 PowerShell 报「无法识别」。
PIPE_UNSUPPORTED = {
    "xargs": "管道里没有 xargs 等价物；请改为 ForEach-Object { ... } 或直接写完整命令。",
    "sed": "管道里没有 sed；简单替换用 -replace 运算符（如 $s -replace 'a','b'）。",
    "awk": "管道里没有 awk；请用 ForEach-Object / Select-Object 组合，或改用 python。",
    "cut": "管道里没有 cut；请用 Select-Object 或 (-split $_) 组合。",
}

_ENV_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S*)\s*")


# ---------------------------------------------------------------- 段内改写


def _rewrite_env_prefix(script: str, notes: List[str]) -> str:
    """`FOO=bar cmd` / `FOO=bar` → `$env:FOO='bar'; cmd`。

    bash 的「前缀赋值」在 PowerShell 里没有语法等价物，必须显式设置环境变量。
    只改**段首**的一串 `NAME=value`（`cmd FOO=bar` 这种参数不动）。
    """
    assigns: List[str] = []
    rest = script
    while True:
        match = _ENV_ASSIGN.match(rest)
        if not match:
            break
        name, value = match.group(1), _unquote(match.group(2))
        assigns.append(f"$env:{name} = {_quote(value)}")
        rest = rest[match.end():]
    if not assigns:
        return script
    notes.append("前缀赋值 → $env:NAME（PowerShell 无 bash 式行内赋值）")
    tail = rest.strip()
    return "; ".join(assigns + ([tail] if tail else []))


def _rewrite_devnull(script: str) -> Tuple[str, List[str]]:
    """`/dev/null` 重定向 → `$null`（Windows 上 /dev/null 不存在）。"""
    notes: List[str] = []
    out = script
    if "/dev/null" in out:
        out = re.sub(r"([0-9*]?)>>?\s*/dev/null", lambda m: f"{m.group(1)}>$null"
                     if ">>" not in m.group(0) else f"{m.group(1)}>>$null", out)
        out = out.replace("/dev/null", "$null")
        notes.append("/dev/null → $null（Windows 无该设备）")
    return out, notes


def _rewrite_env_vars(script: str) -> Tuple[str, List[str]]:
    """把 `$HOME` 这类已知 Unix 环境变量改成 `$env:X`（跳过单引号内文本）。"""
    if "$" not in script:
        return script, []
    notes: List[str] = []
    replaced: List[str] = []
    pattern = re.compile(r"\$(?:\{)?([A-Za-z_][A-Za-z0-9_]*)(?:\})?")

    def fix(chunk: str) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1)
            target = ENV_ALIASES.get(name)
            if not target:
                return m.group(0)
            # `$env:HOME`、`$HOME_PATH`（已带作用域或只是前缀相似）不动
            after = chunk[m.end():m.end() + 1]
            before = chunk[max(0, m.start() - 1):m.start()]
            if after == ":" or before in (":", "\\", "$", "@"):
                return m.group(0)
            replaced.append(f"${name}→${target}")
            return f"${target}"

        return pattern.sub(repl, chunk)

    # 单引号段是字面量：绝不替换
    out_parts: List[str] = []
    for index, part in enumerate(re.split(r"('(?:[^']|'')*')", script)):
        if index % 2 == 1:
            out_parts.append(part)
        else:
            out_parts.append(fix(part))
    if replaced:
        notes.append("环境变量改写：" + "、".join(sorted(set(replaced))))
    return "".join(out_parts), notes


def _translate_stage(stage: str, ctx: Ctx) -> Tuple[str, List[str]]:
    """改写管道里的一段（首段或后续段）。未命中改写表则原样返回。"""
    stage = stage.strip()
    if not stage:
        return stage, []
    notes: List[str] = []
    stage = _rewrite_env_prefix(stage, notes)
    # `export FOO=bar` / `unset FOO`：bash 内建，PowerShell 必须换写法
    export_match = re.match(r"^\s*export\s+(.+)$", stage, re.IGNORECASE)
    if export_match:
        body = export_match.group(1).strip()
        assignments = [a for a in body.split() if "=" in a]
        if not assignments:
            return "''", notes + ["export 单个变量名在 PowerShell 无等价操作，已忽略"]
        pairs = []
        for item in assignments:
            name, _, value = item.partition("=")
            pairs.append(f"$env:{name.strip()} = {_quote(_unquote(value.strip()))}")
        notes.append("export → $env:NAME（仅当前会话有效）")
        return "; ".join(pairs), notes
    unset_match = re.match(r"^\s*unset\s+(\S+)", stage, re.IGNORECASE)
    if unset_match:
        notes.append("unset → Remove-Item Env:（仅当前会话有效）")
        return f"Remove-Item Env:{unset_match.group(1)} -ErrorAction SilentlyContinue", notes

    tokens = _split_tokens(stage)
    if not tokens:
        return stage, notes
    head = tokens[0].lower()
    if any(ch in head for ch in ("/", "\\")) or head.startswith("."):
        return stage, notes                       # 路径形态（./script.sh）不猜
    handler = STAGE_COMMANDS.get(head)
    if handler is None:
        return stage, notes
    if not ctx.first and head not in PIPE_ONLY_COMMANDS:
        # 管道后续段的 `ls`/`rm` 等不是「取数据」，保持原样更安全
        return stage, notes
    if not ctx.first and head in PIPE_UNSUPPORTED:
        return stage, notes
    try:
        rewritten, rest, extra = handler(tokens, ctx)
    except Exception:                             # noqa: BLE001 - 改写失败绝不影响执行
        return stage, notes
    notes.extend(extra)
    if not extra:
        # 每个改写都必须留痕：模型据此知道「我写的和实际跑的不一样」，
        # 下一次就能直接写对。漏掉这条等于让改写变成静默行为。
        target = TARGET_LABEL.get(head) or (rewritten.split()[0] if rewritten.split() else rewritten)
        notes.append(f"{tokens[0]} → {target}（Unix 写法改写）")
    tail = (" " + " ".join(rest)) if rest else ""
    command = f"{rewritten}{tail}"
    if head in SILENT_OUTPUT_COMMANDS and ctx.first and "|" not in command:
        # 写操作成功时本该静默：不吞掉 New-Item/Copy-Item 返回的对象，
        # 结果里会多出整张「目录表」，模型会以为命令输出了这些东西。
        command += " | Out-Null"
    return command, notes


def _translate_pipeline(segment: str, ctx: Ctx) -> Tuple[str, List[str]]:
    """按 `;` 切语句、按 `|` 切管道，逐段改写。

    必须先切 `;`：`export FOO=bar; echo $FOO` 是一个 segment（`;` 不是 `&&`/`||`），
    若整段交给 `export` 处理，`echo $FOO` 会被当成多余的赋值项丢掉——实测踩过。
    `;` 在引号/子表达式里时不会被切（`_split_top_level` 保证）。
    """
    statements, _ = _split_top_level(segment, (";",))
    notes: List[str] = []
    out_statements: List[str] = []
    for statement in statements:
        new_statement, statement_notes = _translate_statements(statement, ctx)
        notes.extend(statement_notes)
        out_statements.append(new_statement)
    return "; ".join(out_statements), notes


def _translate_statements(statement: str, ctx: Ctx) -> Tuple[str, List[str]]:
    """一段不含 `;` 的语句：按 `|` 切管道并逐段改写。"""
    stages, _ = _split_top_level(statement, ("|",))
    notes: List[str] = []
    out: List[str] = []
    for index, stage in enumerate(stages):
        stage_ctx = Ctx(first=index == 0, assume_utf8=ctx.assume_utf8)
        stripped = stage.strip()
        head = _split_tokens(stripped)[0].lower() if stripped else ""
        if index > 0 and head in PIPE_UNSUPPORTED:
            notes.append(PIPE_UNSUPPORTED[head])
            ctx.notes.append(PIPE_UNSUPPORTED[head])
            out.append(stripped)
            continue
        new, stage_notes = _translate_stage(stage, stage_ctx)
        notes.extend(stage_notes)
        out.append(new)
    return " | ".join(out), notes


def _compile_chain(segments: List[str], ops: List[str], notes: List[str]) -> str:
    """把 `&&` / `||` 链编译成 `if ($?)` 结构（5.1 没有这两个运算符）。

    用「成功标志累加」而不是逐对嵌套：`a && b || c` 的 bash 语义是
    「a 成功才跑 b；a 或 b 失败就跑 c」，累加器版本一次覆盖所有组合，
    嵌套版本在 `||` 之后接 `&&` 时会把本该继续的链断掉。

    注意 `$?` 必须**紧跟**在命令之后求值：任何中间语句（包括 `$__psok = $?`
    本身）都会改写 `$?`，所以赋值与它捕获的命令写在同一条语句里。
    """
    if not ops:
        return segments[0]
    notes.append("&& / || → if ($?) 分支（PowerShell 5.1 不支持这两个运算符）")
    head = segments[0].strip()
    # $? 捕获的是「上一条语句」的结果：执行首段后立即取，不能隔语句
    parts = [f"{head}; $__psok = $?"]
    for op, seg in zip(ops, segments[1:]):
        seg = seg.strip()
        if not seg:
            continue
        if op == "&&":
            parts.append(f"if ($__psok) {{ {seg}; $__psok = $? }}")
        else:
            parts.append(f"if (-not $__psok) {{ {seg}; $__psok = $? }}")
    return "; ".join(parts)


# ---------------------------------------------------------------- 对外入口


def destructive_reason(command: str) -> str:
    """递归删除的危险目标检查（返回原因，空串 = 放行）。

    为什么必须在翻译**之前**看原文：`rm -rf ~` / `rm -rf $HOME` 在 bash 里
    可能被用户自己的 alias 或权限拦住，但改写成 `Remove-Item -Recurse -Force ~`
    后 PowerShell 会真的删掉家目录——翻译层不能制造这种升级。拒绝时给出
    可操作提示，而不是让模型猜“为什么不行”。
    """
    segments, _ = _split_top_level(command, ("&&", "||", ";"))
    for segment in segments:
        stages, _ = _split_top_level(segment, ("|",))
        for stage in stages:
            tokens = _split_tokens(stage.strip())
            if not tokens:
                continue
            head = tokens[0].lower()
            if head not in ("rm", "del", "erase", "rmdir", "rd", "remove-item"):
                continue
            # 注意：这里**不**用 _flags_and_args 分离看参——`rm -rf /*` 里的 `/*`
            # 以 `/` 开头，会被当成 cmd 风格开关丢掉，导致危险目标被误判为无目标。
            # 安全判定必须扫描全部 token。
            lowered = [t.lower() for t in tokens[1:]]
            recursive = any(
                t in ("-r", "-recurse", "-rf", "-fr", "-rfi", "-s", "/s")
                or (t.startswith("-") and "r" in t and t[1:].isalpha() and t[1:].islower())
                for t in lowered)
            if not recursive:
                continue
            for token in tokens[1:]:
                if token.startswith("-"):
                    continue
                if _is_dangerous_target(token):
                    plain = _unquote(token).strip()
                    return (f"递归删除的目标 {plain!r} 是根目录/家目录/通配路径。"
                            f"翻译成 PowerShell 后它会真的执行（bash 里可能被挡住），"
                            f"故拒绝。要删具体目录请写明确路径，如 `rm -rf build`。")
    return ""


def translate(command: str, *, assume_utf8: bool = True) -> Translation:
    """把一条（可能是 Unix 写法的）命令翻译成 PowerShell 脚本。

    返回值里的 notes 是**给模型看的**：它据此知道自己的命令被改写了哪几处，
    下一次就能直接写对，而不是反复试探。
    """
    raw = (command or "").strip()
    if not raw:
        return Translation(script="", blocked="命令为空")

    first_token = _split_tokens(_split_top_level(raw, ("&&", "||"))[0][0])
    if first_token:
        head = first_token[0].lower()
        if head in BLOCKED_PREFIXES:
            return Translation(script="", blocked=BLOCKED_PREFIXES[head])

    danger = destructive_reason(raw)
    if danger:
        return Translation(script="", blocked=danger)

    notes: List[str] = []
    text, null_notes = _rewrite_devnull(raw)
    notes.extend(null_notes)
    text, env_notes = _rewrite_env_vars(text)
    notes.extend(env_notes)

    ctx = Ctx(assume_utf8=assume_utf8, notes=notes)
    raw_segments, ops = _split_top_level(text, ("&&", "||"))
    segments: List[str] = []
    for segment in raw_segments:
        new_seg, seg_notes = _translate_pipeline(segment, ctx)
        segments.append(new_seg)
        notes.extend(n for n in seg_notes if n not in notes)
    script = _compile_chain(segments, ops, notes)
    deduped: List[str] = []
    for note in notes:
        if note not in deduped:
            deduped.append(note)
    return Translation(script=script.strip(), notes=deduped)
