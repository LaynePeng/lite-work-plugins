# powershell-plugin 单测：翻译层（纯逻辑，离线）+ 会话层（真机，仅在 Windows 跑）。
#
# 🚫 红线：不碰用户真实配置文件；真机会话只在本目录下的临时目录里跑命令。
# 运行：python -m unittest plugins.powershell-plugin.test_powershell_plugin
#      （目录名带连字符无法当包导入时：cd 到插件目录后 python -m unittest test_powershell_plugin）
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 桩：宿主接口
def _install_litework_stubs() -> None:
    """宿主包最小契约替身（幂等补齐，与仓库其它插件的测法一致）。

    用 setdefault 语义而不是整体跳过：`unittest discover` 里别的测试模块可能
    已注册了 `litework` 包但没补 `litework.core.types`。
    """
    for name in ("litework", "litework.core", "litework.core.types",
                 "litework.tools", "litework.tools.plugin"):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            mod.__path__ = []
            sys.modules[name] = mod

    class _ToolDefinition:
        def __init__(self, name="", description="", parameters=None):
            self.name = name
            self.description = description
            self.parameters = parameters or {}

    class _ToolPlugin:
        name = "plugin"
        version = ""
        description = ""
        removed_tools = []
        contributes = {}
        read_only_tools = []

        def install(self, kernel):
            pass

        def get_tools(self):
            return []

        async def execute(self, name, args):
            return ""

    core_types = sys.modules["litework.core.types"]
    if not hasattr(core_types, "ToolDefinition"):
        core_types.ToolDefinition = _ToolDefinition
    tools_plugin = sys.modules["litework.tools.plugin"]
    if not hasattr(tools_plugin, "ToolPlugin"):
        tools_plugin.ToolPlugin = _ToolPlugin


_install_litework_stubs()
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


import ps_translate as T  # noqa: E402
import ps_worker  # noqa: E402
import plugin as P  # noqa: E402


# ---------------------------------------------------------------- 翻译层（纯逻辑）


class TranslateTests(unittest.TestCase):
    """Unix 写法 → PowerShell 的改写规则。

    每条用例都同时断言「改成了什么」与「有没有留痕」：留痕是给模型看的，
    漏掉留痕等于让改写变成静默行为，模型下次还会写错。
    """

    def assertScript(self, command: str, expected: str) -> str:
        result = T.translate(command)
        self.assertFalse(result.blocked, f"{command!r} 不该被拒绝：{result.blocked}")
        self.assertEqual(result.script, expected, f"{command!r} 改写结果不符")
        return result.script

    # ---- 目录列举
    def test_ls(self) -> None:
        self.assertEqual(self.assertScript("ls", "Get-ChildItem"), "Get-ChildItem")
        self.assertIn("Get-ChildItem -Force", self.assertScript("ls -la", "Get-ChildItem -Force"))
        self.assertTrue(T.translate("ls -la").notes)

    def test_ls_keeps_path(self) -> None:
        self.assertEqual(T.translate("ls -la src").script, "Get-ChildItem -Force src")

    def test_cmd_dir_b(self) -> None:
        self.assertEqual(T.translate("dir /b").script, "Get-ChildItem -Name")

    # ---- 删除 / 复制 / 移动
    def test_rm_flags(self) -> None:
        self.assertEqual(T.translate("rm -rf build").script,
                         "Remove-Item -Recurse -Force build | Out-Null")
        self.assertEqual(T.translate("rm -f a.txt").script,
                         "Remove-Item -Force a.txt | Out-Null")
        self.assertEqual(T.translate("rm a.txt").script,
                         "Remove-Item a.txt | Out-Null")

    def test_cp_mv(self) -> None:
        self.assertEqual(T.translate("cp -r a b").script,
                         "Copy-Item -Recurse a b | Out-Null")
        self.assertEqual(T.translate("mv a b").script, "Move-Item a b | Out-Null")

    def test_mkdir_p(self) -> None:
        self.assertEqual(T.translate("mkdir -p build/out").script,
                         "New-Item -ItemType Directory -Force build/out | Out-Null")
        self.assertEqual(T.translate("mkdir build").script,
                         "New-Item -ItemType Directory build | Out-Null")

    def test_touch(self) -> None:
        self.assertEqual(T.translate("touch a.txt").script,
                         "New-Item -ItemType File -Force -Path a.txt | Out-Null")

    # ---- 读取 / 分页
    def test_cat_adds_utf8(self) -> None:
        """5.1 的 Get-Content 默认按 ANSI 解码，UTF-8 文件会乱码。"""
        self.assertEqual(T.translate("cat a.txt").script, "Get-Content -Encoding UTF8 a.txt")

    def test_cat_respects_existing_encoding(self) -> None:
        self.assertEqual(T.translate("cat -Encoding UTF8 a.txt").script,
                         "Get-Content -Encoding UTF8 a.txt")

    def test_cat_without_utf8_assumption(self) -> None:
        self.assertEqual(T.translate("cat a.txt", assume_utf8=False).script, "Get-Content a.txt")

    def test_head_first_stage_reads_file(self) -> None:
        self.assertEqual(T.translate("head -n 20 app.log").script,
                         "Get-Content -TotalCount 20 -Encoding UTF8 app.log")

    def test_head_default_count(self) -> None:
        self.assertIn("-TotalCount 10", T.translate("head app.log").script)

    def test_head_short_count_forms(self) -> None:
        self.assertIn("-TotalCount 7", T.translate("head -7 app.log").script)
        self.assertIn("-TotalCount 7", T.translate("head -n7 app.log").script)
        self.assertIn("-TotalCount 7", T.translate("head --lines=7 app.log").script)

    def test_tail(self) -> None:
        self.assertEqual(T.translate("tail -n 3 app.log").script,
                         "Get-Content -Tail 3 -Encoding UTF8 app.log")

    def test_head_in_pipeline_selects_objects(self) -> None:
        script = T.translate("cat a.txt | head -n 5").script
        self.assertTrue(script.startswith("Get-Content"), script)
        self.assertIn("Select-Object -First 5", script)

    # ---- 搜索
    def test_grep_file_uses_select_string(self) -> None:
        script = T.translate("grep -n 'def main' a.py").script
        self.assertIn("Select-String", script)
        self.assertIn("-Pattern 'def main'", script)
        # grep 默认区分大小写，必须显式对齐（Select-String 默认不区分）
        self.assertIn("-CaseSensitive", script)

    def test_grep_ignore_case_drops_case_sensitive(self) -> None:
        script = T.translate("grep -i error app.log").script
        self.assertNotIn("-CaseSensitive", script)

    def test_grep_recursive(self) -> None:
        script = T.translate("grep -rn TODO src").script
        self.assertIn("Get-ChildItem", script)
        self.assertIn("-Recurse", script)
        self.assertIn("Select-String", script)

    def test_grep_in_pipeline_uses_where_object(self) -> None:
        """Select-String 只认文本；管道上游可能是对象（Get-Process）。"""
        script = T.translate("ps aux | grep python").script
        self.assertIn("Where-Object", script)
        self.assertNotIn("Select-String", script)
        # pattern 必须被引起来，否则 PowerShell 把它当变量名
        self.assertIn("'python'", script)

    def test_find(self) -> None:
        script = T.translate("find src -name '*.py'").script
        self.assertIn("Get-ChildItem", script)
        self.assertIn("-Filter '*.py'", script)
        self.assertIn("-Recurse", script)

    def test_which(self) -> None:
        script = T.translate("which python").script
        self.assertIn("Get-Command python", script)

    # ---- 进程
    def test_ps(self) -> None:
        self.assertEqual(T.translate("ps aux").script, "Get-Process")

    def test_kill(self) -> None:
        self.assertEqual(T.translate("kill -9 1234").script, "Stop-Process -Id 1234 -Force")

    # ---- 计数 / 排序（文件参数不能被丢）
    def test_wc_keeps_file_argument(self) -> None:
        script = T.translate("wc -l file.txt").script
        self.assertIn("file.txt", script)
        self.assertIn("Measure-Object -Line", script)

    def test_sort_keeps_file_argument(self) -> None:
        script = T.translate("sort -u names.txt").script
        self.assertIn("names.txt", script)
        self.assertIn("Sort-Object -Unique", script)

    # ---- 逻辑组合
    def test_and_operator(self) -> None:
        script = T.translate("echo a && echo b").script
        self.assertIn("$__psok = $?", script)
        self.assertIn("if ($__psok) { echo b", script)
        # 首段不能重复出现（曾经因切片偏移重复执行首段）
        self.assertEqual(script.count("echo a"), 1, script)

    def test_or_operator(self) -> None:
        script = T.translate("cmd /c exit 3 || echo fallback").script
        self.assertIn("if (-not $__psok)", script)
        self.assertIn("echo fallback", script)

    def test_chain_and_or(self) -> None:
        script = T.translate("echo a && echo b || echo c").script
        self.assertEqual(script.count("echo a"), 1, script)
        self.assertIn("echo b", script)
        self.assertIn("echo c", script)

    def test_quoted_ampersand_untouched(self) -> None:
        """引号里的 `&&` 是一个参数，不能被当成运算符切开。"""
        self.assertEqual(T.translate("echo 'a && b'").script, "echo 'a && b'")
        self.assertEqual(T.translate('echo "a && b"').script, 'echo "a && b"')

    # ---- 环境变量
    def test_export(self) -> None:
        self.assertEqual(T.translate("export FOO=bar").script, "$env:FOO = 'bar'")

    def test_export_multiple(self) -> None:
        self.assertEqual(T.translate("export A=1 B=2").script,
                         "$env:A = '1'; $env:B = '2'")

    def test_export_does_not_swallow_following_statement(self) -> None:
        """`;` 后的语句必须保留：整段交给 export 会把 `echo $FOO` 当成多余赋值丢掉。"""
        script = T.translate("export FOO=demo; echo $env:FOO").script
        self.assertEqual(script, "$env:FOO = 'demo'; echo $env:FOO")

    def test_semicolon_statements_translated_independently(self) -> None:
        script = T.translate("ls -la; rm -rf build").script
        self.assertIn("Get-ChildItem", script)
        self.assertIn("Remove-Item -Recurse -Force build", script)

    def test_semicolon_inside_quotes_untouched(self) -> None:
        self.assertEqual(T.translate("echo 'a; b'").script, "echo 'a; b'")

    def test_env_prefix_assignment(self) -> None:
        self.assertEqual(T.translate("FOO=bar python x.py").script,
                         "$env:FOO = 'bar'; python x.py")

    def test_unset(self) -> None:
        script = T.translate("unset FOO").script
        self.assertIn("Remove-Item Env:FOO", script)

    def test_unix_env_alias(self) -> None:
        self.assertEqual(T.translate("echo $HOME").script, "echo $env:USERPROFILE")

    def test_env_alias_skips_single_quotes(self) -> None:
        """单引号里是字面量：改了会改变输出内容。"""
        self.assertEqual(T.translate("echo '$HOME literal'").script, "echo '$HOME literal'")

    def test_env_alias_skips_scoped_name(self) -> None:
        self.assertEqual(T.translate("echo $env:PATH").script, "echo $env:PATH")

    def test_dev_null(self) -> None:
        script = T.translate("cat x 2>/dev/null || echo none").script
        self.assertIn("2>$null", script)
        self.assertNotIn("/dev/null", script)

    # ---- 拒绝
    def test_blocked_sudo(self) -> None:
        result = T.translate("sudo rm x")
        self.assertTrue(result.blocked)
        self.assertIn("sudo", result.blocked)

    def test_blocked_chmod(self) -> None:
        self.assertTrue(T.translate("chmod +x run.sh").blocked)

    def test_blocked_destructive_rm(self) -> None:
        """`rm -rf /` 在 bash 里被挡，但改写成 Remove-Item 后 PowerShell 会真的执行。"""
        for command in ("rm -rf /", "rm -rf ~", "rm -rf /*", "rm -rf C:\\",
                        "rm -rf $HOME", "rm -rf ./*"):
            with self.subTest(command=command):
                self.assertTrue(T.translate(command).blocked, command)

    def test_safe_rm_allowed(self) -> None:
        for command in ("rm -rf build", "rm -rf ./dist", "rm -f a.txt"):
            with self.subTest(command=command):
                self.assertFalse(T.translate(command).blocked, command)

    def test_non_recursive_rm_of_dot_allowed(self) -> None:
        # 不带 -r 的 `rm ./*` 只删文件、不递归，风险面小得多，不拦
        self.assertFalse(T.translate("rm ./*").blocked)

    # ---- 不改写的场景（保守）
    def test_passthrough_native_commands(self) -> None:
        for command in ("git status --short", "npm run build", "python -c \"print(1)\""):
            with self.subTest(command=command):
                result = T.translate(command)
                self.assertEqual(result.script, command)
                self.assertEqual(result.notes, [])

    def test_empty_command(self) -> None:
        self.assertTrue(T.translate("").blocked)

    def test_every_rewrite_is_reported(self) -> None:
        """改写必须留痕：模型靠它知道「我写的和实际跑的不一样」。"""
        cases = ["ls", "rm -rf build", "cat a.txt", "grep x a.txt",
                 "find . -name '*.py'", "which python", "ps aux",
                 "kill -9 1", "touch a", "mkdir -p d", "cp -r a b",
                 "time echo hi", "wc -l a", "sort -u a", "uniq"]
        for command in cases:
            with self.subTest(command=command):
                self.assertTrue(T.translate(command).notes, f"{command!r} 改写未留痕")


class TokenizerTests(unittest.TestCase):
    """词法边界：切错边界的代价是把参数当开关，或反过来把开关当参数。"""

    def test_split_top_level_ignores_quotes(self) -> None:
        parts, ops = T._split_top_level('a "x && y" && b', ("&&",))
        self.assertEqual(len(parts), 2, parts)
        self.assertEqual(ops, ["&&"])
        self.assertIn('"x && y"', parts[0])

    def test_split_top_level_respects_nesting(self) -> None:
        parts, ops = T._split_top_level("$(a && b) && c", ("&&",))
        self.assertEqual(len(parts), 2, parts)

    def test_split_tokens_keeps_pipe(self) -> None:
        self.assertEqual(T._split_tokens("cat f|head"), ["cat", "f", "|", "head"])

    def test_expand_short_flags(self) -> None:
        self.assertEqual(T._expand_short_flags(["-la"]), ["-l", "-a"])
        # 大写开头是 PowerShell 长参数名，拆开就变成无效开关（-Name → -N -a -m -e）
        self.assertEqual(T._expand_short_flags(["-Name"]), ["-Name"])
        self.assertEqual(T._expand_short_flags(["-Recurse"]), ["-Recurse"])
        self.assertEqual(T._expand_short_flags(["--lines=5"]), ["--lines=5"])


# ---------------------------------------------------------------- 结果渲染


class RenderTests(unittest.TestCase):
    def test_render_basic(self) -> None:
        result = ps_worker.CmdResult(code=0, out="hi", ms=5)
        text = result.render(shell="PowerShell 5.1")
        self.assertIn("[Exit Code]: 0", text)
        self.assertIn("[Shell]: PowerShell 5.1", text)
        self.assertIn("[STDOUT]:\nhi", text)

    def test_render_translation_notes(self) -> None:
        result = ps_worker.CmdResult(code=0, out="x")
        text = result.render(notes=["ls → Get-ChildItem"])
        self.assertIn("[Translated]: ls → Get-ChildItem", text)

    def test_render_timeout_and_session_lost(self) -> None:
        result = ps_worker.CmdResult(code=124, timed_out=True, session_lost=True)
        text = result.render(timeout=3)
        self.assertIn("[Timed Out]", text)
        self.assertIn("[Session]", text)
        self.assertIn("[No output]", text)

    def test_render_strips_surrounding_blank_lines(self) -> None:
        """PowerShell 的表格输出固定带前导空行，原样回给模型会被误读成两块内容。"""
        text = ps_worker.CmdResult(code=0, out="\n\n  table\n\n").render()
        self.assertIn("[STDOUT]:\ntable", text)

    def test_render_strips_padded_trailing_spaces(self) -> None:
        """表格行被 pad 到 4000 列：一次 ls 能刷出几十 KB 全空格（实测）。"""
        padded = "Mode  Name" + " " * 3900 + "\n" + "d---- src" + " " * 3900
        text = ps_worker.CmdResult(code=0, out=padded).render()
        self.assertNotIn(" " * 100, text)
        self.assertIn("Mode  Name", text)
        self.assertIn("d---- src", text)

    def test_render_explains_negative_exit_code(self) -> None:
        text = ps_worker.CmdResult(code=-1, out="rows").render()
        self.assertIn("[Note]: Exit Code -1", text)


class ClampTests(unittest.TestCase):
    def test_short_text_untouched(self) -> None:
        text, cut = ps_worker._clamp("abc", 100)
        self.assertEqual(text, "abc")
        self.assertFalse(cut)

    def test_long_text_keeps_head_and_tail(self) -> None:
        text, cut = ps_worker._clamp("A" * 500 + "Z", 100)
        self.assertTrue(cut)
        self.assertIn("输出截断", text)
        self.assertTrue(text.startswith("A"))
        self.assertTrue(text.endswith("Z"))


# ---------------------------------------------------------------- 插件装配（离线）


class FakeApp:
    def __init__(self, workspace: str, config_dir: str, config=None) -> None:
        self.workspace = workspace
        self.config_dir = config_dir
        self.config = config or {}


class FakeKernel:
    def __init__(self, app) -> None:
        self.app = app

    def has_service(self, name: str) -> bool:
        return name == "app"

    def get_service(self, name: str):
        return self.app


class PluginSurfaceTests(unittest.TestCase):
    """工具面与配置语义（不启动任何进程）。"""

    def make(self, config=None, workspace=None):
        self._tmp = tempfile.mkdtemp(prefix="ps_plugin_test_")
        app = FakeApp(workspace or self._tmp, self._tmp, config or {})
        instance = P.PowerShellPlugin()
        instance._app = app
        return instance

    def test_tool_names_stable(self) -> None:
        instance = self.make()
        self.assertEqual([t.name for t in instance.get_tools()],
                         ["ps_run", "ps_session"])

    def test_tool_schemas_have_required_fields(self) -> None:
        instance = self.make()
        for tool in instance.get_tools():
            self.assertTrue(tool.description, tool.name)
            self.assertEqual(tool.parameters.get("type"), "object")
            self.assertIn("properties", tool.parameters)

    def test_settings_schema_covered_by_defaults(self) -> None:
        """每个可配置项都必须有默认值，否则读配置会落空。"""
        for spec in P.PowerShellPlugin.contributes["settings"]:
            self.assertIn(spec["key"], P.DEFAULTS, spec["key"])

    def test_setting_reads_config_then_default(self) -> None:
        instance = self.make({"powershell-plugin": {"timeout": 7}})
        self.assertEqual(instance._setting("timeout"), 7)
        self.assertEqual(instance._setting("max_output"), P.DEFAULTS["max_output"])
        # 显式 default 只在配置缺该键时生效
        self.assertEqual(instance._setting("idle_ttl", 99), 99)
        self.assertEqual(instance._setting("timeout", 99), 7)

    def test_tool_names_match_manifest(self) -> None:
        import json
        manifest_path = os.path.join(_HERE, "manifest.json")
        self.assertTrue(os.path.isfile(manifest_path), "缺 manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["name"], P.PowerShellPlugin.name)
        self.assertEqual(data["version"], P.PowerShellPlugin.version)
        declared = set(data.get("tools") or [])
        actual = {t.name for t in self.make().get_tools()}
        self.assertEqual(declared, actual)

    def test_guard_blocks_high_risk(self) -> None:
        for command in ("rm -rf /", "mkfs /dev/sda", "shutdown /s",
                        "Remove-Item -Recurse -Force C:\\", "format C:"):
            with self.subTest(command=command):
                self.assertTrue(P._guard_command(command), command)

    def test_guard_allows_ordinary_commands(self) -> None:
        for command in ("git status", "npm test", "rm -rf build", "ls -la"):
            with self.subTest(command=command):
                self.assertEqual(P._guard_command(command), "")

    def test_safe_cwd_rejects_outside_workspace(self) -> None:
        instance = self.make()
        workspace = instance._app.workspace
        self.assertIsNone(instance._safe_cwd("C:\\Windows", workspace)
                          if os.name == "nt" else instance._safe_cwd("/etc", workspace))

    def test_safe_cwd_accepts_relative_inside(self) -> None:
        instance = self.make()
        workspace = instance._app.workspace
        os.makedirs(os.path.join(workspace, "sub"), exist_ok=True)
        self.assertEqual(instance._safe_cwd("sub", workspace),
                         os.path.join(workspace, "sub"))

    def test_read_only_tools_not_declared(self) -> None:
        """ps_run 能执行命令，绝不能声明为只读（无人值守档会因此放行它）。"""
        self.assertEqual(list(P.PowerShellPlugin.read_only_tools), [])


# ---------------------------------------------------------------- 真机会话（仅 Windows）


@unittest.skipUnless(ps_worker.IS_WINDOWS, "持久会话用例只在 Windows 上有意义")
class SessionIntegrationTests(unittest.TestCase):
    """真机跑 worker：验证的是「协议 + 行为」，不是翻译（翻译已离线覆盖）。

    这些用例比纯逻辑慢（每个用例一次 worker 启动，约 0.5s），但它们是
    本插件唯一能证明「命令真的能跑、输出真的能拿到」的地方——历史上有两个
    致命 bug（stdout 被 DEVNULL 丢掉、.ps1 按 ANSI 解码导致 worker 起不来）
    都只能靠这一层抓出来。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp(prefix="ps_live_test_")
        cls.session = ps_worker.PsSession(cls._tmp)
        cls.session.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.session.stop()

    def run_ok(self, script: str, timeout: float = 30.0) -> ps_worker.CmdResult:
        result = self.session.run(script, timeout=timeout)
        self.assertFalse(result.session_lost, result.err)
        return result

    def test_basic_output(self) -> None:
        result = self.run_ok("echo hello")
        self.assertEqual(result.code, 0)
        self.assertIn("hello", result.out)

    def test_native_program_output_not_lost(self) -> None:
        """原生命令的 stdout 不进 PowerShell 对象流，必须仍能拿到（曾因 DEVNULL 全丢）。"""
        result = self.run_ok("hostname")
        self.assertTrue(result.out.strip(), "原生命令输出丢失")

    def test_nonzero_exit_code_propagates(self) -> None:
        result = self.run_ok("cmd /c exit 7")
        self.assertEqual(result.code, 7)

    def test_powershell_error_captured(self) -> None:
        result = self.run_ok("Get-ChildItem -LiteralPath Z:\\no-such-dir-xyz")
        self.assertNotEqual(result.code, 0)
        self.assertTrue(result.err.strip(), "错误信息丢失")

    def test_session_state_persists(self) -> None:
        """变量在命令之间保持——这是「分步干活」的前提。"""
        self.run_ok("$ps_test_var = 41")
        result = self.run_ok("$ps_test_var + 1")
        self.assertIn("42", result.out)

    def test_cwd_persists(self) -> None:
        os.makedirs(os.path.join(self._tmp, "sub"), exist_ok=True)
        self.run_ok("Set-Location -LiteralPath 'sub'")
        result = self.run_ok("(Get-Location).Path")
        self.assertIn("sub", result.out)
        self.run_ok(f"Set-Location -LiteralPath '{self._tmp}'")

    def test_unicode_roundtrip(self) -> None:
        result = self.run_ok("'中文输出 ok'")
        self.assertIn("中文输出", result.out)

    def test_timeout_then_session_usable(self) -> None:
        """超时后会话要被重建，且**下一条命令的输出必须还在**（曾经会丢）。"""
        result = self.session.run("Start-Sleep -Seconds 30", timeout=1.0)
        self.assertTrue(result.timed_out or result.session_lost)
        follow = self.session.run("echo after-timeout", timeout=20)
        self.assertIn("after-timeout", follow.out)

    def test_translated_pipeline_end_to_end(self) -> None:
        """翻译 + 执行串起来验一次：写文件 → cat | head。"""
        target = os.path.join(self._tmp, "sample.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("line1\nline2\nline3 中文\n")
        translated = T.translate(f"cat '{target}' | head -n 2")
        result = self.run_ok(translated.script)
        self.assertIn("line1", result.out)
        self.assertIn("line2", result.out)
        self.assertNotIn("line3", result.out)
        self.assertTrue(translated.notes)

    def test_translated_and_chain_end_to_end(self) -> None:
        translated = T.translate("echo first && echo second")
        result = self.run_ok(translated.script)
        self.assertIn("first", result.out)
        self.assertIn("second", result.out)

    def test_translated_or_chain_short_circuits(self) -> None:
        """`||` 只在左侧失败时执行右侧——翻译若丢掉这个语义就是行为变更。"""
        failed = self.run_ok(
            T.translate("cmd /c exit 3 || echo fallback").script)
        self.assertIn("fallback", failed.out)
        ok = self.run_ok(
            T.translate("cmd /c exit 0 || echo fallback").script)
        self.assertNotIn("fallback", ok.out)


@unittest.skipUnless(ps_worker.IS_WINDOWS, "一次性进程用例只在 Windows 上有意义")
class OneShotTests(unittest.TestCase):
    def test_oneshot_runs_and_reports_code(self) -> None:
        result = ps_worker.run_oneshot("echo oneshot; exit 3",
                                       workspace=tempfile.gettempdir(), timeout=60)
        self.assertIn("oneshot", result.out)
        self.assertEqual(result.code, 3)

    def test_oneshot_does_not_keep_state(self) -> None:
        """一次性进程的语义就是干净环境：变量不该跨调用。"""
        tmp = tempfile.gettempdir()
        ps_worker.run_oneshot("$oneshot_var = 1", workspace=tmp, timeout=60)
        result = ps_worker.run_oneshot("if ($oneshot_var) { 'leaked' } else { 'clean' }",
                                       workspace=tmp, timeout=60)
        self.assertIn("clean", result.out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
