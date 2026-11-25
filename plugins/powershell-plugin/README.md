# powershell-plugin

Windows 上的原生命令执行层：**持久 PowerShell 会话 + Unix 写法自动翻译**。

解决的是两个叠加起来特别难受的问题：命令语法不对（模型写的是类 Unix 写法）＋
每条命令都要新建进程（Windows 上 PowerShell 冷启动 220–400ms）。

| | 宿主 `execute_command`（Windows） | 本插件 `ps_run` |
| --- | --- | --- |
| Shell | `cmd.exe /c` | PowerShell（优先 pwsh 7+，退回 5.1） |
| 每条命令开销 | 每次新建进程，中位数 **343.7ms** | 稳态中位数 **28.4ms**（**12.1×** 更快） |
| Unix 写法 | 全不认 | `ls -la` / `rm -rf` / `cat\|head` / `a && b` / `export` 等自动改写 |
| 编码 | 随控制台代码页走 | 统一 UTF-8（含子进程 `PYTHONUTF8`） |
| 多命令状态 | 每条独立（`cd` 不保留） | 变量 / 函数 / 当前目录在命令之间保持 |
| 超长表格 | 80 列截断 | 4000 列宽，超长输出保留头尾 |

实测数据（Windows 11 / PowerShell 5.1 / 本机，`ps_worker` 直接压测）：

```
一次性进程（每条新建）  n= 8   中位数= 343.7ms   最小= 293.9ms
持久会话 首条（含启动）          682.1ms
持久会话 稳态           n=12   中位数=  28.4ms   最小=   7.3ms
加速比（中位数）：12.1×
```

也就是说：一条命令省约 300ms，一个十几步的任务就省下 3–4 秒的纯等待；
再加上「首次就写对」少掉的 1–2 轮失败重试，体感差距比数字更大。

## 为什么不是覆盖 `execute_command`

宿主的安全中间件把高危命令拦截挂在 `tool_name == "execute_command"` 上。同名覆盖
会让那道防线直接失效，所以本插件**新增** `ps_run` / `ps_session`，并在工具描述里
写明「Windows 上执行命令的首选工具」。代价是宿主针对 `execute_command` 的逐条审批
不会作用到 `ps_run`，因此本插件自己做一遍同口径的高危拒绝
（见 `plugin.py` 的 `_HIGH_RISK` 与 `ps_translate.BLOCKED_PREFIXES`）。

## 工具

### `ps_run`

| 参数 | 说明 |
| --- | --- |
| `command` | 要执行的命令（PowerShell 语法；常见 Unix 写法自动翻译） |
| `timeout` | 超时秒数，默认 120 |
| `cwd` | 工作目录，**必须在工作区内**；不传则沿用会话当前目录 |
| `fresh` | `true` 用一次性干净进程（慢，但在会话被搞脏时用） |
| `background` | `true` 后台执行长命令，返回 `task_id` |

返回格式沿用宿主的段落风格，多一行 `[Translated]` 告诉你哪几处被改写了：

```
[Exit Code]: 0 | [Shell]: Windows PowerShell 5.1（持久会话 pid=30488）
[Translated]: 补 -Encoding UTF8（5.1 默认按 ANSI 解码，UTF-8 文件会乱码）；head → Get-Content/Select-Object（Unix 写法改写）
[STDOUT]:
# AGENTS.md — lite-work-plugins 仓库工作规则
```

### `ps_session`

`action` = `status` / `restart` / `close` / `jobs` / `output`（需 `task_id`）/
`kill`（需 `task_id`）。

## 翻译覆盖哪些写法

| Unix / cmd 写法 | 改写成 | 备注 |
| --- | --- | --- |
| `ls -la` / `dir /b` | `Get-ChildItem -Force` / `Get-ChildItem -Name` | `-l` 无对应语义，丢弃并告知 |
| `rm -rf build` | `Remove-Item -Recurse -Force build` | |
| `cp -r a b` / `mv a b` | `Copy-Item -Recurse a b` / `Move-Item a b` | |
| `mkdir -p a/b` | `New-Item -ItemType Directory -Force a/b` | |
| `touch a.txt` | `New-Item -ItemType File -Force -Path a.txt` | |
| `cat f` | `Get-Content -Encoding UTF8 f` | 5.1 默认按 ANSI 解码，UTF-8 会乱码 |
| `head -n 5 f` / `tail -n 5 f` | `Get-Content -TotalCount/Tail 5 -Encoding UTF8 f` | 管道里改成 `Select-Object -First/Last` |
| `grep -rn pat src` | `Get-ChildItem src -Recurse -File \| Select-String -Pattern pat -CaseSensitive` | 对齐 grep 默认区分大小写 |
| `ps aux \| grep python` | `Get-Process \| Where-Object { "$_" -cmatch 'python' }` | 管道上游是对象，`Select-String` 无效 |
| `find src -name '*.py'` | `Get-ChildItem src -Filter '*.py' -Recurse \| Select-Object -ExpandProperty FullName` | |
| `which python` | `Get-Command python \| Select-Object -First 1 -ExpandProperty Source` | |
| `kill -9 <pid>` | `Stop-Process -Id <pid> -Force` | |
| `wc -l f` / `sort -u f` / `uniq` | `Measure-Object -Line` / `Sort-Object -Unique` / `Get-Unique` | 文件参数不丢 |
| `a && b` / `a \|\| b` | `a; $__psok = $?; if ($__psok) { b; ... }` | 5.1 不支持这两个运算符 |
| `export F=bar` / `F=bar cmd` | `$env:F = 'bar'` | 仅当前会话有效 |
| `unset F` | `Remove-Item Env:F` | |
| `$HOME` / `$USER` / `$TMPDIR` … | `$env:USERPROFILE` / `$env:USERNAME` / `$env:TEMP` | 单引号内的字面量不动 |
| `2>/dev/null` | `2>$null` | |

**不改写**（保守优先）：`git status`、`npm run build`、`python -c ...` 这类本来就是
正确写法的命令原样放过；`sed` / `awk` / `xargs` / `cut` 在管道里没有等价物，返回
可操作提示而不是让 PowerShell 报「无法识别」。

**直接拒绝**（说明原因，不猜）：

- `sudo` / `chmod` / `chown` / `apt` / `brew`：Windows 上没有对应语义；
- `rm -rf /`、`rm -rf ~`、`rm -rf /*`、`rm -rf C:\`：这些在 bash 里可能被挡住，
  但翻译成 `Remove-Item -Recurse -Force` 之后 PowerShell 会**真的执行**——翻译层
  不能把一句安全的废话变成灾难。

## 配置

`config.json` 顶层 `powershell-plugin` 对象（设置页有同名表单项）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `prefer` | `auto` | `auto` / `pwsh`（7+）/ `powershell`（5.1） |
| `timeout` | `120` | 前台命令默认超时（秒） |
| `max_output` | `60000` | STDOUT / STDERR 各自上限，超出保留头尾 |
| `translate` | `true` | 是否启用 Unix → PowerShell 改写 |
| `assume_utf8_files` | `true` | `Get-Content` 是否补 `-Encoding UTF8`（老 GBK 工程请关） |
| `persist_session` | `true` | 关掉则每条命令新建进程（慢但干净） |
| `prewarm` | `true` | 插件装配时后台预热会话，把首次 0.5s 挪到发命令之前 |
| `idle_ttl` | `900` | 会话空闲多久后回收 |
| `encoding` | `utf-8` | worker 输出编码；输出全是问号/乱码时可试 `cp936` |

右栏附带「PowerShell」面板（`contributes.panels`），显示会话 pid、空闲时长、
重启次数与后台任务列表。

## 安装

放进 `~/.lite-work/plugins/powershell-plugin/`，或从设置页「社区插件」安装。
**零第三方依赖**（只用标准库 + 宿主稳定接口），`scripts/check_plugin_deps.py` 应报
「无第三方依赖」。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `plugin.py` | 装配层：工具定义、配置读取、`ps_run` / `ps_session` 实现、面板与设置项 |
| `ps_translate.py` | 翻译层（纯函数，零 PowerShell 依赖）：Unix 写法 → PowerShell、拒绝规则 |
| `ps_worker.py` | 执行层：持久 worker 进程、协议、会话池、后台任务、进程树击杀 |
| `test_powershell_plugin.py` | 单测：翻译层与渲染层离线跑；会话层真机跑（仅 Windows） |

多文件插件形态的前提：宿主 `plugin_loader` 会把插件目录插入 `sys.path`，所以
兄弟模块用**裸 import + 唯一前缀**（`ps_translate` / `ps_worker`），不能用相对导入。

## 测试

```bash
cd plugins/powershell-plugin
python -m unittest test_powershell_plugin -v
```

- 翻译层 / 渲染层 / 装配层：离线，任何平台可跑；
- 会话层：`@skipUnless(IS_WINDOWS)` 真机跑（启动 worker、跑真命令、验超时恢复）。

## 实现要点（改代码前值得知道）

1. **协议走响应文件，不走 stdout。** 命令里的原生命令（python / git / npm）会把
   输出直接写到 worker 的 stdout，不走 PowerShell 对象流。所以响应必须用独立
   文件回传，同时 **worker 的 stdout 必须保持 PIPE 并被后台线程排空**——
   设成 `DEVNULL` 会让这类输出整体丢失（历史 bug，单测
   `test_native_program_output_not_lost` 守着）。
2. **worker.ps1 必须带 UTF-8 BOM，且本体保持纯 ASCII。** Windows PowerShell 5.1
   读取无 BOM 的 `.ps1` 时按系统 ANSI（中文系统 GBK）解码，脚本里的中文注释会被
   拆成替换字符 → 语法错误 → worker 启动即崩（历史 bug，双保险都保留了）。
3. **超时后重建引擎，不能只 `$ps.Stop()`。** `Stop()` 会在管道里留下未取走的
   结果，下一条命令 `EndInvoke` 拿到的是残留，表现为「明明成功的命令没有任何
   输出」。重建的代价是变量与 cwd 丢失，结果里会明说。
4. **`$?` 必须紧跟命令求值。** `a; $__psok = $?` 是唯一正确的写法：任何中间语句
   都会改写 `$?`。`&&` / `||` 编译用「成功标志累加」而不是逐对嵌套，否则
   `a && b || c` 的语义会错。
5. **读响应文件要容忍短暂共享冲突。** 文件出现即代表写完（先写 `.tmp` 再 `Move`），
   但 Windows 上句柄释放有极短窗口，直接 `open()` 会偶发 `PermissionError`。
6. **会话池是进程级单例。** 每个任务都会新建插件实例；会话若挂在实例上，
   「持久」在每次任务装配后就失效，加速效果等于没有。
