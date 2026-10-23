# 纯逻辑单测（离线，零网络）：0.9.0 迟延治理（P0-P4）的行为回归。
#
# 🚫 红线：不连真实 SystemOne 端点、不读真实 config.local.json / config.json。
#   HTTP 一律走 `_client_factory` 的内存替身；配置走内存 dict。
# 运行：python3 -m unittest plugins.jev.test_jev_plugin
import asyncio
import importlib.util
import json
import os
import sys
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 桩（须在导入插件前装好）
def _install_httpx_stub() -> None:
    """插件模块级 `import httpx`（打包态可能叫 httpx2，插件已有兜底导入）。

    测试环境两者都没有 → 打最小契约桩：只提供异常类；客户端一律由
    `plugin._client_factory` 替换成内存替身，绝不发真实请求。
    """
    if "httpx" in sys.modules:
        return
    stub = types.ModuleType("httpx")

    class HTTPStatusError(Exception):
        def __init__(self, message="", request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    for name in ("ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
                 "TimeoutException", "PoolTimeout"):
        setattr(stub, name, type(name, (Exception,), {}))
    stub.HTTPStatusError = HTTPStatusError
    stub.AsyncClient = object          # 真实使用处一律被 _client_factory 替换
    stub.Client = object
    sys.modules["httpx"] = stub


def _install_litework_stubs() -> None:
    """插件模块级 import litework.*；测试环境没有主程序包，按最小契约打桩。"""
    if "litework" in sys.modules:
        return

    class _Plugin:
        name = "plugin"
        contributes = {}

        def install(self, kernel):
            pass

    class _ToolPlugin(_Plugin):
        version = ""
        description = ""
        removed_tools = []

        def get_tools(self):
            raise NotImplementedError

        async def execute(self, name, args):
            raise NotImplementedError

        def install(self, kernel):
            return None

    class _Message:
        def __init__(self, role="user", content=None, **kw):
            self.role = role
            self.content = content
            for k, v in kw.items():
                setattr(self, k, v)

    class _ToolDefinition:
        def __init__(self, name, description="", parameters=None, **kw):
            self.name = name
            self.description = description
            self.parameters = parameters or {}

    class _Kernel:
        def __init__(self, session_id="s1"):
            self.ctx = types.SimpleNamespace(session_id=session_id, metadata={},
                                             services={}, messages=[])

        def has_service(self, name):
            return name in self.ctx.services

        def get_service(self, name):
            return self.ctx.services[name]

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    lite = _mod("litework")
    core = _mod("litework.core", kernel=None, types=None)
    _mod("litework.core.kernel", Kernel=_Kernel)
    _mod("litework.core.types", Message=_Message, ToolDefinition=_ToolDefinition,
         Plugin=_Plugin, Context=types.SimpleNamespace)
    tools = _mod("litework.tools")
    _mod("litework.tools.plugin", ToolPlugin=_ToolPlugin)
    _mod("litework.core.context_manager", ContextManager=object)
    lite.core = core
    lite.tools = tools
    # 插件里 `from litework.core.kernel import Kernel` 依赖包属性链
    core.kernel = sys.modules["litework.core.kernel"]
    core.types = sys.modules["litework.core.types"]
    tools.plugin = sys.modules["litework.tools.plugin"]


_install_httpx_stub()
_install_litework_stubs()

_SPEC = importlib.util.spec_from_file_location(
    "jev_plugin_under_test", os.path.join(_HERE, "plugin.py"))
jevplugin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jevplugin)  # type: ignore[union-attr]


# ---------------------------------------------------------------- HTTP 替身
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise jevplugin.httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=types.SimpleNamespace(),
                response=types.SimpleNamespace(status_code=self.status_code))

    def json(self):
        return self._payload


class FakeClient:
    """内存 HTTP 客户端：按 payload 里的 questions 生成 answers。"""

    def __init__(self, server, timeout=None):
        self.server = server
        self.timeout = timeout
        self.closed = False

    async def post(self, url, json=None, headers=None):
        return await self.server.handle(json, headers)

    async def aclose(self):
        self.closed = True


class FakeServer:
    """判定服务替身：可注入延迟、错误序列、HTTP 状态码，并按问题客观回答。

    支持三种问题类型（与真实网关契约一致）：
      noul   → {"noul", "why"}（由 answers["injection"/"evidence"] 决定）
      choice → {"choice", "confidence", "probabilities", "why"}
      score  → {"score", "confidence", "legend"}
    """

    def __init__(self, answers=None, delay=0.0, errors=0, status=None,
                 choice=None, confidence=0.9, probabilities=None,
                 omit_noul=False):
        self.answers = answers or {}
        self.delay = delay
        self.errors = errors            # 前 N 次抛瞬时错误（ConnectError）
        self.status = status            # 非 None 时一律返回该 HTTP 状态
        self.choice = choice            # choice 类问题的回答
        self.confidence = confidence
        self.probabilities = probabilities or {"放行": 0.1, "请人确认": 0.1, "拦截": 0.8}
        self.omit_noul = omit_noul      # 模拟响应缺 noul 字段（无效响应）
        self.calls = []                 # 每次调用的 payload
        self.clients = []

    def factory(self, timeout_s):
        client = FakeClient(self, timeout=timeout_s)
        self.clients.append(client)
        return client

    async def handle(self, payload, headers):
        self.calls.append({"payload": payload, "headers": headers})
        if self.errors > 0:
            self.errors -= 1
            raise jevplugin.httpx.ConnectError("boom")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status is not None:
            return _Resp({}, status=self.status)
        qs = (payload or {}).get("questions") or {}
        answers = {}
        for key, q in qs.items():
            kind = str(q.get("type") or "")
            instr = str(q.get("instructions") or "")
            if kind == "choice":
                answers[key] = {"choice": self.choice, "confidence": self.confidence,
                                "probabilities": dict(self.probabilities),
                                "why": "选择替身"}
            elif kind == "score":
                answers[key] = {"score": 2, "confidence": self.confidence,
                                "legend": {"0": "低", "1": "中", "2": "高"},
                                "why": "打分替身"}
            elif self.omit_noul:
                answers[key] = {"why": "（故意不给 noul）"}
            elif "提示注入" in instr:
                answers[key] = {"noul": self.answers.get("injection", 0.1)}
            else:
                answers[key] = {"noul": self.answers.get("evidence", 0.9),
                                "why": "证据替身"}
        return _Resp({"model": (payload or {}).get("model", "jev-test"),
                      "answers": answers})


# ---------------------------------------------------------------- 管道替身
class FakePipeline:
    """洋葱模型管道（与 core 的 Pipeline 同语义，够用于测试）。"""

    def __init__(self):
        self._mws = []

    def use(self, mw):
        self._mws.append(mw)

    async def run(self, ctx, data):
        async def dispatch(i, d):
            if i >= len(self._mws):
                return d
            async def nxt(nd=None):
                return await dispatch(i + 1, d if nd is None else nd)
            return await self._mws[i](ctx, d, nxt)
        return await dispatch(0, data)


class FakeKernel:
    def __init__(self, session_id="s1"):
        self.ctx = types.SimpleNamespace(session_id=session_id, metadata={},
                                        services={}, messages=[])
        self.before_llm = FakePipeline()
        self.before_tool = FakePipeline()
        self.after_tool = FakePipeline()
        self.before_finish = FakePipeline()

    def has_service(self, name):
        return name in self.ctx.services

    def get_service(self, name):
        return self.ctx.services[name]


class FakeApp:
    def __init__(self, jev_cfg=None):
        self.config = {"jev": dict(jev_cfg or {})}
        self.config_dir = None      # None → 不镜像、不写盘（测试隔离）

    def save_config(self, updates):
        self.config.update(updates)


def msg(role, content):
    return types.SimpleNamespace(role=role, content=content)


def make_cfg(**over):
    raw = {"api_key": "k", "model": "jev-test", "screen_enabled": True,
           "verify_enabled": True, "cache_ttl_s": 60}
    raw.update(over)
    return jevplugin.JevConfig(raw)


# ---------------------------------------------------------------- 基类
class JevTestBase(unittest.TestCase):
    def setUp(self):
        jevplugin.cache_clear()
        jevplugin._PENDING_NOTES.clear()
        jevplugin._BG_TASKS.clear()
        for key in jevplugin.STATS:
            jevplugin.STATS[key] = 0
        jevplugin._RECENT.clear()
        # 私有凭据文件与磁盘兜底一律隔离
        jevplugin._read_private = lambda: {}
        jevplugin._write_private = lambda data: None
        jevplugin._read_disk_jev = lambda config_dir: {}
        self.server = FakeServer()
        jevplugin._client_factory = self.server.factory
        self.kernel = FakeKernel()
        # 默认把两个付费开关打开——单独测开关行为时各自覆盖配置
        self.app = FakeApp({"api_key": "k", "model": "jev-test",
                            "screen_enabled": True, "verify_enabled": True})
        self.kernel.ctx.services["app"] = self.app

    def tearDown(self):
        # 关键：取消仍在飞的后台判定任务——否则它的 await 会在下一条用例里
        # 解析到**新的** _client_factory，把请求记到下一个替身服务器上（跨用例污染）。
        for task in list(jevplugin._BG_TASKS):
            task.cancel()
        jevplugin._BG_TASKS.clear()
        jevplugin._PENDING_NOTES.clear()
        jevplugin.cache_clear()

    async def _drain(self):
        await jevplugin.wait_bg_tasks(timeout=2.0)


# ================================================================ P0：不阻塞
class TestScreenAsync(JevTestBase):
    def test_after_tool_returns_without_waiting_for_http(self):
        """核心断言：判定慢也不阻塞工具返回（原实现的病根）。"""
        self.server.delay = 0.5
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)
        data = {"toolName": "read_file", "result": "x" * 500}

        async def go():
            started = time.time()
            out = await self.kernel.after_tool.run(self.kernel.ctx, data)
            elapsed = time.time() - started
            self.assertLess(elapsed, 0.15, "after_tool 不应等待判定完成")
            self.assertEqual(out["result"], data["result"], "异步模式不改写当前结果")
            await self._drain()
        asyncio.run(go())
        self.assertGreaterEqual(len(self.server.calls), 1, "判定仍应在后台真的发出")

    def test_async_hit_queues_note_for_next_turn(self):
        self.server.answers = {"injection": 0.95}
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)
        data = {"toolName": "webfetch", "result": "evil " * 200}

        async def go():
            await self.kernel.after_tool.run(self.kernel.ctx, data)
            await self._drain()
        asyncio.run(go())
        notes = jevplugin._drain_notes("s1")
        self.assertEqual(len(notes), 1)
        self.assertIn("提示注入", notes[0])
        self.assertTrue(any(e.get("kind") == "screen" for e in jevplugin._RECENT))

    def test_sync_mode_still_isolates(self):
        """screen_async_enabled=false 时保留 0.8.x 的当场隔离语义。"""
        self.server.answers = {"injection": 0.95}
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["async_screen_enabled"] = False
        plugin._install_screen(self.kernel)
        data = {"toolName": "read_file", "result": "evil " * 200}

        async def go():
            return await self.kernel.after_tool.run(self.kernel.ctx, data)
        out = asyncio.run(go())
        self.assertIn("[Jev Screen]", out["result"])

    def test_clean_result_queues_nothing(self):
        self.server.answers = {"injection": 0.02}
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)
        data = {"toolName": "read_file", "result": "y" * 400}

        async def go():
            await self.kernel.after_tool.run(self.kernel.ctx, data)
            await self._drain()
        asyncio.run(go())
        self.assertEqual(jevplugin.pending_note_count(), 0)

    def test_self_source_skipped(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)
        data = {"toolName": "read_file",
                "result": "File: plugins/jev/plugin.py\n" + "z" * 400}

        async def go():
            await self.kernel.after_tool.run(self.kernel.ctx, data)
            await self._drain()
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 0, "自家内容不该发判定请求")

    def test_screen_max_per_turn_gate(self):
        self.app.config["jev"]["screen_max_per_turn"] = 2
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)

        async def go():
            for i in range(5):
                await self.kernel.after_tool.run(
                    self.kernel.ctx, {"toolName": "read_file",
                                      "result": f"body{i} " * 100})
            await self._drain()
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 2)

    def test_inline_gate_reset_per_turn(self):
        self.app.config["jev"]["screen_max_per_turn"] = 1
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)
        plugin._install_turn_guard(self.kernel)

        async def go():
            await self.kernel.after_tool.run(
                self.kernel.ctx, {"toolName": "read_file", "result": "a " * 200})
            await self.kernel.after_tool.run(
                self.kernel.ctx, {"toolName": "read_file", "result": "b " * 200})
            await self.kernel.before_llm.run(self.kernel.ctx, [msg("user", "hi")])
            await self.kernel.after_tool.run(
                self.kernel.ctx, {"toolName": "read_file", "result": "c " * 200})
            await self._drain()
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 2, "新一轮应重置成本闸门")

    def test_bg_concurrency_cap_drops_instead_of_queueing(self):
        self.server.delay = 0.2
        self.app.config["jev"]["max_concurrency"] = 2
        jevplugin._BG_MAX_INFLIGHT = 2
        try:
            plugin = jevplugin.JevPlugin()
            plugin._install_screen(self.kernel)

            async def go():
                for i in range(4):
                    await self.kernel.after_tool.run(
                        self.kernel.ctx, {"toolName": "read_file",
                                          "result": f"body{i} " * 200})
                await self._drain()
            asyncio.run(go())
            self.assertLessEqual(jevplugin.STATS["dropped"], 2)
        finally:
            jevplugin._BG_MAX_INFLIGHT = 8


# ================================================================ P1：合并 + 缓存
class TestTurnGuardMerge(JevTestBase):
    def _claim_turns(self):
        """末尾是 assistant 的完成声称（触发 Verify，不触发 Screen 消息判定）。"""
        long_user = "请帮我做一件事，细节如下：" + "细节" * 60
        return [msg("system", "sys"),
                msg("user", long_user),
                msg("assistant", "工具跑完了，测试通过，已完成。")]

    def _claim_then_user_turns(self):
        """末尾是用户消息、而其前一条 assistant 声称完成：

        这是 Screen（消息注入）与 Verify（补核验）**同一轮共存**的真实形态——
        也就是 0.8.x 里两个 before_llm 钩子各发一次 HTTP 的那一轮。
        """
        return [msg("system", "sys"),
                msg("user", "先做点事"),
                msg("assistant", "工具跑完了，测试通过，已完成。"),
                msg("user", "后续说明：" + "细节" * 40)]

    def test_screen_and_verify_share_one_request(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_then_user_turns()

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 1, "同轮只应发一次请求")
        qs = self.server.calls[0]["payload"]["questions"]
        self.assertEqual(set(qs), {"q1", "q2"}, "两个问题应合并进同一请求")

    def test_screen_only_does_not_ask_evidence(self):
        """只有用户消息、没有完成声称时，不应多问一个无意义的证据问题。"""
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = [msg("system", "sys"), msg("user", "普通消息：" + "内容" * 40)]

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        self.assertEqual(set(self.server.calls[0]["payload"]["questions"]), {"q1"})

    def test_no_screen_when_assistant_is_last(self):
        """用户消息在前几轮已判过（且有缓存）：完成声称那一轮不必重判消息。"""
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_turns()

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        qs = self.server.calls[0]["payload"]["questions"]
        self.assertEqual(set(qs), {"q1"}, "只应问证据核验")

    def test_verify_only_claim_sends_single_question(self):
        self.app.config["jev"]["screen_enabled"] = False
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_turns()

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(set(self.server.calls[0]["payload"]["questions"]), {"q1"})

    def test_verify_warning_appended_on_insufficient_evidence(self):
        self.server.answers = {"evidence": 0.1}
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_turns()

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        out = asyncio.run(go())
        self.assertIn("[verify-warning]", out[0].content)

    def test_note_injected_when_no_system_message(self):
        """payload 里没有 system 消息时，结论前插为合成 system——绝不能静默丢掉。"""
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        jevplugin._queue_note("s1", "结论-A")
        turns = [msg("user", "只看这一条")]

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        out = asyncio.run(go())
        self.assertEqual(str(out[0].role), "system")
        self.assertIn("结论-A", out[0].content)
        self.assertEqual(jevplugin.pending_note_count(), 0)

    def test_msg_injection_short_circuits_turn(self):
        self.server.answers = {"injection": 0.95}
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_then_user_turns()

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        fa = self.kernel.ctx.metadata.get("final_answer")
        self.assertIsNotNone(fa)
        self.assertIn("Jev Screen", fa["content"])

    def test_no_short_circuit_when_clean(self):
        self.server.answers = {"injection": 0.05}
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_then_user_turns()
        asyncio.run(self.kernel.before_llm.run(self.kernel.ctx, turns))
        self.assertIsNone(self.kernel.ctx.metadata.get("final_answer"))

    def test_msg_threshold_boundary_is_exclusive(self):
        """0.79 < 0.8 → 不短路；0.80 = 0.8 → 短路（阈值语义必须精确）。"""
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_then_user_turns()

        self.server.answers = {"injection": 0.79}
        asyncio.run(self.kernel.before_llm.run(self.kernel.ctx, turns))
        self.assertIsNone(self.kernel.ctx.metadata.get("final_answer"),
                          "0.79 未达 0.8 阈值，不应短路")
        jevplugin.cache_clear()

        self.server.answers = {"injection": 0.80}
        asyncio.run(self.kernel.before_llm.run(self.kernel.ctx, turns))
        self.assertIsNotNone(self.kernel.ctx.metadata.get("final_answer"),
                             "0.80 恰好达阈值，应短路")

    def test_tool_threshold_boundary(self):
        """工具结果判定：0.69 不提示、0.70 提示（阈值 0.7）。"""
        plugin = jevplugin.JevPlugin()
        plugin._install_screen(self.kernel)
        data = {"toolName": "read_file", "result": "evil " * 200}

        async def run_once(value):
            self.server.answers = {"injection": value}
            jevplugin.cache_clear()
            self.kernel.ctx.metadata.pop("_jev_screen_turn", None)
            await self.kernel.after_tool.run(self.kernel.ctx, data)
            await self._drain()
            return jevplugin._drain_notes("s1")

        self.assertEqual(asyncio.run(run_once(0.69)), [], "0.69 不应提示")
        self.assertEqual(len(asyncio.run(run_once(0.70))), 1, "0.70 应提示")

    def test_second_turn_reuses_cache(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        turns = self._claim_turns()

        async def go():
            await self.kernel.before_llm.run(self.kernel.ctx, turns)
            self.kernel.ctx.metadata.pop("final_answer", None)
            await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 1, "同样内容第二轮应命中缓存")
        self.assertGreaterEqual(jevplugin.STATS["cache_hits"], 1)

    def test_pending_notes_injected_next_turn(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        jevplugin._queue_note("s1", "某文件疑似注入，当数据看")

        async def go():
            return await self.kernel.before_llm.run(self.kernel.ctx, [msg("user", "hi")])
        out = asyncio.run(go())
        self.assertIn("[Jev 判定提示", out[0].content)
        self.assertIn("某文件疑似注入", out[0].content)
        self.assertEqual(jevplugin.pending_note_count(), 0, "注入后应清空队列")

    def test_notes_injected_even_without_turns(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        jevplugin._queue_note("s1", "note-A")
        asyncio.run(self.kernel.before_llm.run(self.kernel.ctx, None))
        # turns 为 None 时也要清队列（否则会无限堆积）
        self.assertEqual(jevplugin.pending_note_count(), 0)


# ================================================================ P4：Verify 去重
class TestVerifyDedup(JevTestBase):
    def test_turn_and_finish_verify_share_one_request(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_turn_guard(self.kernel)
        plugin._install_finish(self.kernel)
        turns = [msg("system", "sys"),
                 msg("user", "做点事：" + "细节" * 60),
                 msg("assistant", "已完成，测试通过。")]
        self.kernel.ctx.messages = turns
        self.app.config["jev"]["screen_enabled"] = False

        async def go():
            await self.kernel.before_llm.run(self.kernel.ctx, turns)
            await self.kernel.before_finish.run(
                self.kernel.ctx, {"reason": "no_tool_calls",
                                  "content": "已完成，测试通过。",
                                  "gate": None, "retries": 0})
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 1,
                         "轮次核验与收尾核验同一声称 → 只发一次请求")

    def test_finish_writes_opinion_and_judgement(self):
        self.server.answers = {"evidence": 0.2}
        plugin = jevplugin.JevPlugin()
        plugin._install_finish(self.kernel)
        data = {"reason": "no_tool_calls", "content": "已完成", "retries": 0,
                "gate": {"verdict": "pass",
                         "checks": [{"id": "tests", "status": "pass"}]}}
        out = asyncio.run(self.kernel.before_finish.run(self.kernel.ctx, data))
        self.assertEqual(out["opinion"]["choice"], "reject")
        self.assertEqual(out["opinion"]["source"], "Jev")
        self.assertNotIn("block_finish", out, "默认只做顾问、不该阻断收尾")
        points = {e.get("point") for e in jevplugin._RECENT}
        self.assertIn("finish", points)
        self.assertIn("finish-gate", points)

    def test_finish_enforce_only_when_configured(self):
        self.server.answers = {"evidence": 0.2}
        self.app.config["jev"]["verify_finish_enforce"] = True
        plugin = jevplugin.JevPlugin()
        plugin._install_finish(self.kernel)
        data = {"reason": "no_tool_calls", "content": "已完成", "retries": 0}
        out = asyncio.run(self.kernel.before_finish.run(self.kernel.ctx, data))
        self.assertTrue(out["block_finish"])
        self.assertIn("证据不足", out["reminder"])

    def test_finish_fail_open_on_error(self):
        self.server.errors = 99
        plugin = jevplugin.JevPlugin()
        plugin._install_finish(self.kernel)
        data = {"reason": "no_tool_calls", "content": "已完成", "retries": 0}
        out = asyncio.run(self.kernel.before_finish.run(self.kernel.ctx, data))
        self.assertNotIn("opinion", out, "判不了就该静默放行")

    def test_finish_skips_when_not_claiming_done(self):
        plugin = jevplugin.JevPlugin()
        plugin._install_finish(self.kernel)
        data = {"reason": "no_tool_calls", "content": "我先看下代码", "retries": 0}
        asyncio.run(self.kernel.before_finish.run(self.kernel.ctx, data))
        self.assertEqual(len(self.server.calls), 0)


# ================================================================ P2：超时/重试
class TestTransport(JevTestBase):
    def test_default_timeout_and_attempts_are_tight(self):
        cfg = jevplugin.JevConfig({"api_key": "k"})
        self.assertEqual(cfg.timeout_ms, 1200)
        self.assertEqual(cfg.max_attempts, 2)
        self.assertTrue(cfg.async_screen)
        self.assertEqual(cfg.screen_max_per_turn, 3)
        self.assertEqual(jevplugin.DEFAULT_VERIFY_TIMEOUT_MS, 2500)

    def test_transient_error_retried_once_then_success(self):
        self.server.errors = 1
        cfg = make_cfg()
        data = asyncio.run(jevplugin.post_systemone_async(
            cfg, {"state": "s", "model": "m", "questions": {}}))
        self.assertIn("answers", data)
        self.assertEqual(len(self.server.calls), 2)

    def test_retries_exhausted_raises_and_counts_error(self):
        self.server.errors = 99
        cfg = make_cfg()
        with self.assertRaises(Exception):
            asyncio.run(jevplugin.post_systemone_async(
                cfg, {"state": "s", "model": "m", "questions": {}}))
        self.assertEqual(len(self.server.calls), 2, "默认最多 2 次尝试")
        self.assertEqual(jevplugin.STATS["errors"], 1)

    def test_verify_uses_longer_timeout(self):
        cfg = make_cfg()
        asyncio.run(jevplugin.post_systemone_async(
            cfg, {"state": "s", "model": "m", "questions": {}},
            timeout_ms=cfg.verify_timeout_ms))
        self.assertEqual(self.server.clients[0].timeout, 2.5)

    def test_missing_credential_raises(self):
        cfg = jevplugin.JevConfig({"model": "m"})
        with self.assertRaises(RuntimeError):
            asyncio.run(jevplugin.post_systemone_async(cfg, {"state": "s"}))

    def test_client_side_error_not_retried(self):
        """400/401/422 是确定性失败（配置/契约问题）→ 立即抛出，不浪费重试。"""
        self.server.status = 400
        cfg = make_cfg()
        with self.assertRaises(jevplugin.httpx.HTTPStatusError):
            asyncio.run(jevplugin.post_systemone_async(
                cfg, {"state": "s", "model": "m", "questions": {}}))
        self.assertEqual(len(self.server.calls), 1, "4xx 不应重试")

    def test_429_is_retried(self):
        self.server.status = 429
        cfg = make_cfg()
        with self.assertRaises(jevplugin.httpx.HTTPStatusError):
            asyncio.run(jevplugin.post_systemone_async(
                cfg, {"state": "s", "model": "m", "questions": {}}))
        self.assertEqual(len(self.server.calls), 2, "429 属瞬时错误，应重试到上限")

    def test_missing_noul_treated_as_untrusted(self):
        """响应缺 noul = 无效响应 → 视为未采信（不能当 0.0 用成假高置信）。"""
        self.server.omit_noul = True
        cfg = make_cfg()
        noul, why, reused = asyncio.run(jevplugin._judge_noul(
            cfg, "state", "问题", cache_kind="t"))
        self.assertIsNone(noul)
        self.assertFalse(reused)

    def test_invalid_noul_never_cached(self):
        self.server.omit_noul = True
        cfg = make_cfg()
        asyncio.run(jevplugin._judge_noul(cfg, "state", "问题", cache_kind="t"))
        self.assertEqual(len(jevplugin._CACHE), 0, "无效响应不应进缓存")

    def test_clients_are_closed(self):
        cfg = make_cfg()
        asyncio.run(jevplugin.post_systemone_async(
            cfg, {"state": "s", "model": "m", "questions": {}}))
        self.assertTrue(all(c.closed for c in self.server.clients))

    def test_async_transport_does_not_touch_threadpool(self):
        """回归：0.8.x 用 asyncio.to_thread + time.sleep，会占线程池。"""
        cfg = make_cfg()
        seen = []
        orig = asyncio.to_thread

        async def spy(fn, *a, **kw):
            seen.append(fn)
            return await orig(fn, *a, **kw)

        asyncio.to_thread = spy
        try:
            asyncio.run(jevplugin.post_systemone_async(
                cfg, {"state": "s", "model": "m", "questions": {}}))
        finally:
            asyncio.to_thread = orig
        self.assertEqual(seen, [], "判定链路不应再走线程池")


# ================================================================ 缓存 / 队列
class TestCacheAndNotes(JevTestBase):
    def test_cache_ttl_expiry(self):
        key = jevplugin._cache_key("k", "v", "m")
        jevplugin._cache_put(key, {"noul": 0.5}, ttl_s=0.05)
        self.assertIsNotNone(jevplugin._cache_get(key))
        time.sleep(0.08)
        self.assertIsNone(jevplugin._cache_get(key))

    def test_cache_zero_ttl_disabled(self):
        key = jevplugin._cache_key("k", "v", "m")
        jevplugin._cache_put(key, {"noul": 0.5}, ttl_s=0)
        self.assertIsNone(jevplugin._cache_get(key))

    def test_cache_key_includes_model_and_content(self):
        a = jevplugin._cache_key("k", "same", "m1")
        b = jevplugin._cache_key("k", "same", "m2")
        c = jevplugin._cache_key("k", "other", "m1")
        self.assertEqual(len({a, b, c}), 3)

    def test_cache_is_bounded(self):
        for i in range(jevplugin._CACHE_MAX + 20):
            jevplugin._cache_put(f"k{i}", {"noul": 0.5}, ttl_s=60)
        self.assertLessEqual(len(jevplugin._CACHE), jevplugin._CACHE_MAX)

    def test_judge_noul_second_call_hits_cache_without_network(self):
        """_judge_noul 自身的缓存短路（变异测试暴露的缺口：原只被间接覆盖）。"""
        cfg = make_cfg()
        first = asyncio.run(jevplugin._judge_noul(
            cfg, "same-state", "问题", cache_kind="t"))
        calls_after_first = len(self.server.calls)
        second = asyncio.run(jevplugin._judge_noul(
            cfg, "same-state", "问题", cache_kind="t"))
        self.assertEqual(len(self.server.calls), calls_after_first,
                         "第二次不应再发网络请求")
        self.assertEqual(first[0], second[0])
        self.assertFalse(first[2], "首次为真判")
        self.assertTrue(second[2], "第二次应标记为复用缓存")

    def test_judge_noul_different_state_is_new_call(self):
        cfg = make_cfg()
        asyncio.run(jevplugin._judge_noul(cfg, "state-A", "问题", cache_kind="t"))
        asyncio.run(jevplugin._judge_noul(cfg, "state-B", "问题", cache_kind="t"))
        self.assertEqual(len(self.server.calls), 2, "不同内容应各自判定")

    def test_notes_fifo_and_limit(self):
        for i in range(jevplugin._NOTES_MAX + 3):
            jevplugin._queue_note("s", f"n{i}")
        notes = jevplugin._drain_notes("s")
        self.assertEqual(len(notes), jevplugin._NOTES_MAX)
        self.assertEqual(notes[-1], f"n{jevplugin._NOTES_MAX + 2}")
        self.assertEqual(jevplugin.pending_note_count("s"), 0)

    def test_notes_are_per_session(self):
        jevplugin._queue_note("a", "for-a")
        jevplugin._queue_note("b", "for-b")
        self.assertEqual(jevplugin._drain_notes("a"), ["for-a"])
        self.assertEqual(jevplugin._drain_notes("b"), ["for-b"])

    def test_spawn_bg_without_loop_is_safe(self):
        async def coro():
            return 1
        self.assertFalse(jevplugin._spawn_bg(coro(), label="t"))
        self.assertEqual(jevplugin.STATS["dropped"], 1)

    def test_bg_exception_is_swallowed(self):
        async def bad():
            raise ValueError("nope")

        async def go():
            jevplugin._spawn_bg(bad(), label="bad")
            await self._drain()
        asyncio.run(go())
        self.assertGreaterEqual(jevplugin.STATS["errors"], 1)


# ================================================================ 门禁 / 工具
class TestGateAndTools(JevTestBase):
    def test_gate_cache_hit_avoids_second_request(self):
        self.server.choice = "放行"
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "execute_command", "args": {"command": "rm -rf /tmp/x"}}

        async def go():
            await self.kernel.before_tool.run(self.kernel.ctx, data)
            await self.kernel.before_tool.run(self.kernel.ctx, data)
        asyncio.run(go())
        self.assertEqual(len(self.server.calls), 1, "同一工具+参数应命中缓存")

    def test_gate_high_risk_block_cancels_call(self):
        """唯一硬拦条件：choice=拦截 且 confidence ≥ 0.95。"""
        self.server.choice, self.server.confidence = "拦截", 0.97
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "execute_command", "args": {"command": "rm -rf /"}}
        out = asyncio.run(self.kernel.before_tool.run(self.kernel.ctx, data))
        self.assertTrue(out["cancel"])
        self.assertIn("风险极高", out["reason"])
        self.assertEqual(out["judge_opinion"]["choice"], "block")
        self.assertEqual(jevplugin.STATS["blocked"], 1)

    def test_gate_block_below_0_95_only_advises(self):
        """0.94 的拦截判定**不得** cancel——单向棘轮的边界必须精确。"""
        self.server.choice, self.server.confidence = "拦截", 0.94
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "execute_command", "args": {"command": "rm -rf /"}}
        out = asyncio.run(self.kernel.before_tool.run(self.kernel.ctx, data))
        self.assertNotIn("cancel", out)
        self.assertEqual(out["judge_opinion"]["level"], "高风险")
        self.assertEqual(jevplugin.STATS["blocked"], 0)

    def test_gate_low_confidence_not_adopted(self):
        """置信低于阈值 → 不采信、不附加意见、只计数。"""
        self.server.choice, self.server.confidence = "拦截", 0.2
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "write_file", "args": {"filePath": "a", "content": "x"}}
        out = asyncio.run(self.kernel.before_tool.run(self.kernel.ctx, data))
        self.assertNotIn("judge_opinion", out)
        self.assertNotIn("cancel", out)
        self.assertEqual(jevplugin.STATS["skipped_untrusted"], 1)

    def test_gate_allow_only_advises(self):
        """放行判定也走同一路径：附意见、但不 cancel。"""
        self.server.choice, self.server.confidence = "放行", 0.9
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "apply_search_replace",
                "args": {"filePath": "a", "searchBlock": "x", "replaceBlock": "y"}}
        out = asyncio.run(self.kernel.before_tool.run(self.kernel.ctx, data))
        self.assertNotIn("cancel", out)
        self.assertEqual(out["judge_opinion"]["choice"], "allow")
        self.assertEqual(out["judge_opinion"]["level"], "低风险")

    def test_gate_fail_open_counts_error(self):
        self.server.errors = 99
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "execute_command", "args": {"command": "ls"}}
        out = asyncio.run(self.kernel.before_tool.run(self.kernel.ctx, data))
        self.assertFalse(out.get("cancel"), "判不了就该让行")
        self.assertNotIn("judge_opinion", out)

    def test_gate_ignores_non_gated_tools(self):
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["auto_gate_enabled"] = True
        plugin._install_gate(self.kernel)
        data = {"toolName": "read_file", "args": {"filePath": "a"}}
        asyncio.run(self.kernel.before_tool.run(self.kernel.ctx, data))
        self.assertEqual(len(self.server.calls), 0)

    def test_tool_execute_uses_async_transport(self):
        plugin = jevplugin.JevPlugin()
        plugin._kernel = self.kernel
        self.server.answers = {"injection": 0.1}
        out = asyncio.run(plugin.execute("jev_score", {
            "state": "这段代码改动", "instructions": "有多危险",
            "criteria": ["低", "中", "高"]}))
        self.assertIn("Jev 判断", out)

    def test_tool_execute_fail_open_message(self):
        plugin = jevplugin.JevPlugin()
        plugin._kernel = self.kernel
        self.server.errors = 99
        out = asyncio.run(plugin.execute("jev_choice", {
            "state": "s", "instructions": "i", "options": ["a", "b"]}))
        self.assertIn("Jev 暂不可用", out)

    def test_direct_answer_short_circuit(self):
        plugin = jevplugin.JevPlugin()
        self.app.config["jev"]["direct_answer_enabled"] = True
        self.app.config["jev"]["direct_answer_confidence"] = 0.5
        plugin._install_direct_answer(self.kernel)
        turns = [msg("user", "/jev 是否 天空是蓝色的")]

        async def go():
            await self.kernel.before_llm.run(self.kernel.ctx, turns)
        asyncio.run(go())
        self.assertIsNotNone(self.kernel.ctx.metadata.get("final_answer"))


# ================================================================ 装配 / 设置
class TestInstallAndSettings(JevTestBase):
    def _hooks(self, plugin):
        return (len(self.kernel.before_llm._mws), len(self.kernel.after_tool._mws),
                len(self.kernel.before_tool._mws), len(self.kernel.before_finish._mws))

    def test_defaults_install_no_hooks(self):
        self.app.config["jev"] = {"api_key": "k", "model": "jev-test"}
        plugin = jevplugin.JevPlugin()
        plugin.install(self.kernel)          # 配置里没有任何能力开关
        self.assertEqual(self._hooks(plugin), (0, 0, 0, 0))
        self.assertEqual(plugin.status, "running")

    def test_screen_verify_install_single_turn_hook(self):
        self.app.config["jev"].update({"screen_enabled": True, "verify_enabled": True})
        plugin = jevplugin.JevPlugin()
        plugin.install(self.kernel)
        before_llm, after_tool, before_tool, before_finish = self._hooks(plugin)
        self.assertEqual(before_llm, 1, "Screen+Verify 应共用 1 个 before_llm 钩子")
        self.assertEqual(after_tool, 1)
        self.assertEqual(before_tool, 0)
        self.assertEqual(before_finish, 1)

    def test_missing_credential_means_not_started(self):
        self.app.config["jev"] = {"model": "jev-test"}
        plugin = jevplugin.JevPlugin()
        plugin.install(self.kernel)
        self.assertEqual(plugin.status, "not_started")
        self.assertIn("凭证", plugin.status_reason)

    def test_settings_cover_every_cost_knob(self):
        keys = {s["key"] for s in jevplugin.JevPlugin.contributes["settings"]}
        for key in ("async_screen_enabled", "screen_skip_self", "screen_max_per_turn",
                    "verify_timeout_ms", "cache_ttl_s", "max_attempts",
                    "max_concurrency", "timeout_ms"):
            self.assertIn(key, keys)
        # P3：每个会发网络的开关都要有 hint 说明代价
        cost_keys = ("auto_gate_enabled", "screen_enabled", "verify_enabled",
                     "verify_finish_enforce", "skill_recommend_enabled",
                     "compact_decider_enabled", "direct_answer_enabled")
        by_key = {s["key"]: s for s in jevplugin.JevPlugin.contributes["settings"]}
        for key in cost_keys:
            self.assertIn("代价", by_key[key].get("hint", ""),
                          f"{key} 的 hint 应写明每轮代价")

    def test_cfg_defaults_do_not_enable_paid_features(self):
        cfg = jevplugin.JevConfig({"api_key": "k"})
        self.assertTrue(cfg.tools_enabled)      # 工具面零轮代价，保持默认开
        for flag in ("auto_gate_enabled", "direct_answer", "skill_recommend",
                     "screen", "verify", "verify_finish_enforce", "compact_decider"):
            self.assertFalse(getattr(cfg, flag), f"{flag} 应默认关")

    def test_config_knob_parsing_and_fallbacks(self):
        cfg = jevplugin.JevConfig({"api_key": "k",
                                   "screen_max_per_turn": "bad",
                                   "cache_ttl_s": "bad",
                                   "max_attempts": 0,
                                   "verify_timeout_ms": 10,
                                   "max_concurrency": "0"})
        self.assertEqual(cfg.screen_max_per_turn, 3)      # 非法值回默认
        self.assertEqual(cfg.cache_ttl_s, jevplugin.DEFAULT_CACHE_TTL_S)
        self.assertEqual(cfg.max_attempts, jevplugin.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(cfg.verify_timeout_ms, 200)      # 下限钳制
        self.assertEqual(cfg.max_concurrency, 1)

    def test_old_core_without_before_finish_still_installs(self):
        """老核心（无 before_finish）只降级收尾核验，其余能力照常、绝不抛错。"""
        del self.kernel.before_finish
        self.app.config["jev"].update({"api_key": "k", "verify_enabled": True,
                                       "screen_enabled": True})
        plugin = jevplugin.JevPlugin()
        plugin.install(self.kernel)          # 不应抛 AttributeError
        self.assertEqual(plugin.status, "running")
        self.assertEqual(len(self.kernel.before_llm._mws), 1,
                         "老核心下轮次核验仍应可用")
        self.assertFalse(hasattr(self.kernel, "before_finish"))

    def test_verify_never_registers_second_before_llm_hook(self):
        """P4 回归：Verify 不得再挂第二个 before_llm（0.8.x 的病根）。"""
        self.app.config["jev"].update({"api_key": "k", "verify_enabled": True})
        plugin = jevplugin.JevPlugin()
        plugin.install(self.kernel)
        self.assertEqual(len(self.kernel.before_llm._mws), 1)

    def test_panel_content_still_renders(self):
        self.app.config["jev"].update({"api_key": "k", "model": "jev-test"})
        plugin = jevplugin.JevPlugin()
        raw = plugin.panel_content("judgements", self.app.config)
        data = json.loads(raw)
        self.assertEqual(data["type"], "lite-tree")
        self.assertEqual(data["status"]["text"], "运行中")

    def test_version_synced_with_manifest(self):
        manifest = json.load(open(os.path.join(_HERE, "..", "..", "manifest.json"),
                                  encoding="utf-8"))
        entry = next(p for p in manifest["plugins"] if p["name"] == "jev")
        self.assertEqual(entry["version"], jevplugin.JevPlugin.version)


if __name__ == "__main__":
    unittest.main(verbosity=2)
