# watch-plugin 单测（离线，零网络）：计划读写工具面 + 安全边界。
#
# 🚫 红线：不碰用户真实 ~/.lite-work/schedules.json——全部用内存假 store。
# 运行：python3 -m unittest plugins.watch-plugin.test_watch_plugin
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 桩
def _install_litework_stubs() -> None:
    """宿主包的最小契约替身（幂等补齐，与其它插件的测法一致）。

    用 setdefault 而不是整体跳过：`unittest discover` 里别的测试模块可能已
    注册了 `litework` 包但没补 `litework.core.types`，早先那种
    `if "litework" in sys.modules: return` 会导致本文件直接 ImportError。
    """
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
            return []

        async def execute(self, name, args):
            return ""

    class _ToolDefinition:
        def __init__(self, name="", description="", parameters=None):
            self.name = name
            self.description = description
            self.parameters = parameters or {}

    for name in ("litework", "litework.core", "litework.core.types",
                 "litework.tools", "litework.tools.plugin",
                 "litework.watch"):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            mod.__path__ = []
            sys.modules[name] = mod

    core_types = sys.modules["litework.core.types"]
    if not hasattr(core_types, "ToolDefinition"):
        core_types.ToolDefinition = _ToolDefinition
    tools_plugin = sys.modules["litework.tools.plugin"]
    if not hasattr(tools_plugin, "ToolPlugin"):
        tools_plugin.ToolPlugin = _ToolPlugin

    if "litework.watch.schedule_store" not in sys.modules:
        store_mod = types.ModuleType("litework.watch.schedule_store")

        class Schedule:
            _seq = 0

            def __init__(self, **kw):
                Schedule._seq += 1
                self.id = kw.get("id") or f"sch_{Schedule._seq}"
                self.name = kw.get("name", "")
                self.enabled = kw.get("enabled", True)
                self.schedule_mode = kw.get("schedule_mode", "interval")
                self.interval_seconds = kw.get("interval_seconds", 3600)
                self.time_of_day = kw.get("time_of_day", "09:00")
                self.cron = kw.get("cron", "")
                self.session_id = kw.get("session_id")
                self.workspace = kw.get("workspace", "")
                self.prompt = kw.get("prompt", "")
                self.source = kw.get("source", "")
                self.probe_config = dict(kw.get("probe_config") or {})
                self.permissions = kw.get("permissions", "read")
                self.notify = list(kw.get("notify") or ["in_app"])
                self.catch_up = kw.get("catch_up", "once")
                self.last_run = kw.get("last_run")
                self.last_hash = kw.get("last_hash", "")
                self.last_status = kw.get("last_status", "")
                self.runs = list(kw.get("runs") or [])

            def to_dict(self):
                return dict(self.__dict__)

        store_mod.Schedule = Schedule
        store_mod.ScheduleStore = object
        store_mod.RunRecord = object
        sys.modules["litework.watch.schedule_store"] = store_mod
        sys.modules["litework.watch"].schedule_store = store_mod


_install_litework_stubs()
sys.path.insert(0, _HERE)

import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "watch_plugin_under_test", os.path.join(_HERE, "plugin.py"))
watch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watch)  # type: ignore[union-attr]

Schedule = sys.modules["litework.watch.schedule_store"].Schedule


# ---------------------------------------------------------------- 假宿主


class FakeStore:
    def __init__(self):
        self.items = {}
        self.saved = 0

    def all(self):
        return list(self.items.values())

    def get(self, sid):
        return self.items.get(sid)

    def upsert(self, sched):
        self.items[sched.id] = sched
        self.saved += 1
        return sched

    def delete(self, sid):
        return self.items.pop(sid, None) is not None


class FakeScheduler:
    def __init__(self):
        self.rescheduled = []
        self.fired = []
        self.rec = None

    def reschedule(self, sid=None):
        self.rescheduled.append(sid)

    async def fire(self, sched):
        self.fired.append(sched.id)
        return self.rec or types.SimpleNamespace(status="unchanged",
                                                 detail="盯 2 只，无异动")


class FakeRegistry:
    def __init__(self):
        self.sources = {"astock": {"source_kind": "astock",
                                   "display_name": "A股持仓/自选异动",
                                   "description": "盯持仓与自选股",
                                   "probe_interval_hint": 300,
                                   "config_schema": {
                                       "rules": {"type": "json", "label": "价位规则"},
                                       "codes": "额外盯的代码"}}}

    def get(self, kind):
        return self.sources.get(kind)

    def kinds(self):
        return sorted(self.sources)

    def describe_all(self):
        return list(self.sources.values())


class FakeApp:
    def __init__(self, workspace="/ws/a", with_store=True):
        self.workspace = workspace
        self._store = FakeStore() if with_store else None
        self.watch_scheduler = FakeScheduler()
        self.watch_registry = FakeRegistry()

    @property
    def schedule_store(self):
        if self._store is None:
            raise AttributeError("no store")
        return self._store


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _plugin(app):
    p = watch.WatchPlanPlugin()
    p._app = app
    return p


# ---------------------------------------------------------------- 工具声明


class TestDeclaration(unittest.TestCase):
    def test_all_tools_declared(self):
        p = watch.WatchPlanPlugin()
        names = [t.name for t in p.get_tools()]
        self.assertEqual(sorted(names), sorted([
            "schedule_sources", "schedule_list", "schedule_get",
            "schedule_upsert", "schedule_run", "schedule_delete"]))

    def test_manifest_matches(self):
        """manifest.json 的 tools 必须与 get_tools() 一致（CI 会查）。"""
        import json
        root = os.path.dirname(os.path.dirname(_HERE))
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        entry = [p for p in manifest["plugins"] if p["name"] == "watch-plugin"][0]
        declared = sorted(entry["tools"])
        actual = sorted(t.name for t in watch.WatchPlanPlugin().get_tools())
        self.assertEqual(declared, actual)

    def test_install_captures_app(self):
        app = FakeApp()

        class Kernel:
            def has_service(self, name):
                return name == "app"

            def get_service(self, name):
                return app

        p = watch.WatchPlanPlugin()
        p.install(Kernel())
        self.assertIs(p._app, app)

    def test_install_without_app_service(self):
        class Kernel:
            def has_service(self, name):
                return False

        p = watch.WatchPlanPlugin()
        p.install(Kernel())
        self.assertIsNone(p._app)


# ---------------------------------------------------------------- 数据源


class TestSources(unittest.TestCase):
    def test_lists_config_schema(self):
        out = _run(_plugin(FakeApp()).execute("schedule_sources", {}))
        self.assertIn("astock", out)
        self.assertIn("rules", out, "必须点出 probe_config 的键名")
        self.assertIn("价位规则", out)

    def test_no_registry_degrades(self):
        app = FakeApp()
        app.watch_registry = None
        out = _run(_plugin(app).execute("schedule_sources", {}))
        self.assertIn("[Error]", out)
        self.assertIn("纯定时任务", out)


# ---------------------------------------------------------------- 列出 / 查看


class TestListAndGet(unittest.TestCase):
    def _app_with(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(
            name="A股盯盘", source="astock", interval_seconds=300,
            workspace="/ws/a",
            probe_config={"rules": [{"code": "000651", "price": 36.0,
                                     "op": "lt"}], "min_level": "action"}))
        return app

    def test_list_shows_probe_config(self):
        """核心诉求：模型要能看见「现在盯的是什么」。"""
        out = _run(_plugin(self._app_with()).execute("schedule_list", {}))
        self.assertIn("A股盯盘", out)
        self.assertIn("probe_config", out)
        self.assertIn("000651", out, "规则内容必须可见")
        self.assertIn("min_level", out)

    def test_list_hides_other_workspaces_by_default(self):
        app = self._app_with()
        app.schedule_store.upsert(Schedule(name="别的项目", source="astock",
                                           workspace="/ws/b"))
        out = _run(_plugin(app).execute("schedule_list", {}))
        self.assertNotIn("别的项目", out)
        self.assertIn("其他工作区", out)

    def test_list_can_include_other_workspaces(self):
        app = self._app_with()
        app.schedule_store.upsert(Schedule(name="别的项目", source="astock",
                                           workspace="/ws/b"))
        out = _run(_plugin(app).execute(
            "schedule_list", {"include_other_workspaces": True}))
        self.assertIn("别的项目", out)

    def test_list_filter_by_source(self):
        app = self._app_with()
        app.schedule_store.upsert(Schedule(name="邮件盯梢", source="email",
                                           workspace="/ws/a"))
        out = _run(_plugin(app).execute("schedule_list", {"source": "astock"}))
        self.assertIn("A股盯盘", out)
        self.assertNotIn("邮件盯梢", out)

    def test_list_empty(self):
        out = _run(_plugin(FakeApp()).execute("schedule_list", {}))
        self.assertIn("还没有计划", out)

    def test_list_without_store_degrades(self):
        out = _run(_plugin(FakeApp(with_store=False)).execute("schedule_list", {}))
        self.assertIn("[Error]", out)

    def test_get_by_id(self):
        app = self._app_with()
        sid = app.schedule_store.all()[0].id
        out = _run(_plugin(app).execute("schedule_get", {"id": sid}))
        self.assertIn("计划详情", out)
        self.assertIn("000651", out)

    def test_get_by_name_fragment(self):
        out = _run(_plugin(self._app_with()).execute("schedule_get", {"id": "股盯"}))
        self.assertIn("A股盯盘", out)

    def test_get_ambiguous_reports_ids(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="盯盘A", source="astock"))
        app.schedule_store.upsert(Schedule(name="盯盘B", source="astock"))
        out = _run(_plugin(app).execute("schedule_get", {"id": "盯盘"}))
        self.assertIn("匹配到多条", out)

    def test_get_missing_is_readable(self):
        out = _run(_plugin(FakeApp()).execute("schedule_get", {"id": "nope"}))
        self.assertIn("[Error]", out)
        self.assertIn("找不到", out)


# ---------------------------------------------------------------- 写入


class TestUpsert(unittest.TestCase):
    def test_create(self):
        app = FakeApp()
        out = _run(_plugin(app).execute("schedule_upsert", {
            "name": "新建盯盘", "source": "astock", "interval_seconds": 300,
            "probe_config": {"min_level": "urgent"}}))
        self.assertIn("已创建计划", out)
        s = app.schedule_store.all()[0]
        self.assertEqual(s.name, "新建盯盘")
        self.assertEqual(s.source, "astock")
        self.assertEqual(s.interval_seconds, 300)
        self.assertEqual(s.probe_config, {"min_level": "urgent"})
        self.assertEqual(s.permissions, "read")
        self.assertEqual(s.workspace, "/ws/a", "工作区钉死为当前工作区")
        self.assertEqual(app.watch_scheduler.rescheduled, [s.id])

    def test_workspace_argument_is_ignored(self):
        """安全边界：不能让调用方把探针指到别的目录。"""
        app = FakeApp(workspace="/ws/a")
        _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "source": "astock", "workspace": "/etc"}))
        self.assertEqual(app.schedule_store.all()[0].workspace, "/ws/a")

    def test_unknown_source_rejected(self):
        app = FakeApp()
        out = _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "source": "notexist"}))
        self.assertIn("[Error]", out)
        self.assertIn("未知数据源", out)
        self.assertEqual(app.schedule_store.all(), [], "校验失败不该写盘")

    def test_bad_permission_rejected(self):
        app = FakeApp()
        out = _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "permissions": "read+write"}))
        self.assertIn("[Error]", out)
        self.assertIn("权限档", out)
        self.assertEqual(app.schedule_store.all(), [])

    def test_bad_interval_rejected(self):
        app = FakeApp()
        out = _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "interval_seconds": "abc"}))
        self.assertIn("[Error]", out)
        self.assertEqual(app.schedule_store.all(), [])

    def test_interval_floor(self):
        app = FakeApp()
        _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "interval_seconds": 5}))
        self.assertGreaterEqual(app.schedule_store.all()[0].interval_seconds, 60)

    def test_merge_probe_config_keeps_other_keys(self):
        """最常用场景：只改一条规则，不能把别的键清掉。"""
        app = FakeApp()
        p = _plugin(app)
        _run(p.execute("schedule_upsert", {
            "name": "x", "source": "astock",
            "probe_config": {"min_level": "action", "codes": "600000"}}))
        sid = app.schedule_store.all()[0].id
        _run(p.execute("schedule_upsert", {
            "id": sid, "merge_probe_config": {"min_level": "urgent"}}))
        cfg = app.schedule_store.all()[0].probe_config
        self.assertEqual(cfg["min_level"], "urgent")
        self.assertEqual(cfg["codes"], "600000", "未提到的键必须保留")

    def test_probe_config_replaces(self):
        app = FakeApp()
        p = _plugin(app)
        _run(p.execute("schedule_upsert", {
            "name": "x", "source": "astock",
            "probe_config": {"a": 1, "b": 2}}))
        sid = app.schedule_store.all()[0].id
        _run(p.execute("schedule_upsert", {
            "id": sid, "probe_config": {"c": 3}}))
        self.assertEqual(app.schedule_store.all()[0].probe_config, {"c": 3})

    def test_drop_probe_keys(self):
        app = FakeApp()
        p = _plugin(app)
        _run(p.execute("schedule_upsert", {
            "name": "x", "source": "astock", "probe_config": {"a": 1, "b": 2}}))
        sid = app.schedule_store.all()[0].id
        _run(p.execute("schedule_upsert", {"id": sid, "drop_probe_keys": ["a"]}))
        self.assertEqual(app.schedule_store.all()[0].probe_config, {"b": 2})

    def test_probe_config_json_string_decoded(self):
        """模型常把对象序列化成字符串传进来，不还原就是静默失效。"""
        app = FakeApp()
        _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "source": "astock",
            "probe_config": '{"min_level": "urgent"}'}))
        self.assertEqual(app.schedule_store.all()[0].probe_config,
                         {"min_level": "urgent"})

    def test_probe_config_bad_json_rejected(self):
        app = FakeApp()
        out = _run(_plugin(app).execute("schedule_upsert", {
            "name": "x", "probe_config": "{not json"}))
        self.assertIn("[Error]", out)
        self.assertEqual(app.schedule_store.all(), [])

    def test_update_keeps_id_and_history(self):
        app = FakeApp()
        p = _plugin(app)
        _run(p.execute("schedule_upsert", {"name": "x", "source": "astock"}))
        s = app.schedule_store.all()[0]
        s.last_hash = "abc"
        _run(p.execute("schedule_upsert", {"id": s.id, "enabled": False}))
        keep = app.schedule_store.get(s.id)
        self.assertFalse(keep.enabled)
        self.assertEqual(keep.last_hash, "abc", "修改不能丢指纹基线")
        self.assertEqual(len(app.schedule_store.all()), 1)

    def test_enabled_string_false_is_off(self):
        """`bool("false") == True` 的坑：模型传字符串时必须按语义解析。"""
        app = FakeApp()
        p = _plugin(app)
        _run(p.execute("schedule_upsert", {"name": "x", "source": "astock"}))
        sid = app.schedule_store.all()[0].id
        _run(p.execute("schedule_upsert", {"id": sid, "enabled": "false"}))
        self.assertFalse(app.schedule_store.all()[0].enabled)

    def test_cross_workspace_update_needs_force(self):
        app = FakeApp(workspace="/ws/a")
        app.schedule_store.upsert(Schedule(name="别人", source="astock",
                                           workspace="/ws/b", interval_seconds=120))
        out = _run(_plugin(app).execute("schedule_upsert", {
            "id": "别人", "interval_seconds": 300}))
        self.assertIn("[Error]", out)
        self.assertIn("force", out)
        self.assertEqual(app.schedule_store.all()[0].interval_seconds, 120)

    def test_cross_workspace_update_with_force(self):
        app = FakeApp(workspace="/ws/a")
        app.schedule_store.upsert(Schedule(name="别人", source="astock",
                                           workspace="/ws/b", interval_seconds=120))
        out = _run(_plugin(app).execute("schedule_upsert", {
            "id": "别人", "interval_seconds": 300, "force": True}))
        self.assertIn("已更新", out)
        self.assertEqual(app.schedule_store.all()[0].interval_seconds, 300)

    def test_plain_timer_without_source(self):
        """source 留空＝纯定时任务（到点直接跑 prompt）。"""
        app = FakeApp()
        _run(_plugin(app).execute("schedule_upsert", {
            "name": "每日复盘", "prompt": "总结今天的交易", "time_of_day": "15:30",
            "schedule_mode": "daily"}))
        s = app.schedule_store.all()[0]
        self.assertEqual(s.source, "")
        self.assertEqual(s.schedule_mode, "daily")
        self.assertEqual(s.time_of_day, "15:30")

    def test_without_store_is_explicit(self):
        out = _run(_plugin(FakeApp(with_store=False)).execute(
            "schedule_upsert", {"name": "x"}))
        self.assertIn("[Error]", out)
        self.assertIn("盯梢", out)


# ---------------------------------------------------------------- 立即触发


class TestRun(unittest.TestCase):
    def test_run_returns_record(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        app.watch_scheduler.rec = types.SimpleNamespace(
            status="changed", detail="2 条新触发")
        out = _run(_plugin(app).execute("schedule_run", {"id": "x"}))
        self.assertIn("发现变化", out)
        self.assertIn("2 条新触发", out)
        self.assertEqual(len(app.watch_scheduler.fired), 1)

    def test_unchanged_explains_no_llm(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        out = _run(_plugin(app).execute("schedule_run", {"id": "x"}))
        self.assertIn("没有唤醒 Agent", out)

    def test_error_gives_hint(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        app.watch_scheduler.rec = types.SimpleNamespace(status="error",
                                                        detail="探针异常")
        out = _run(_plugin(app).execute("schedule_run", {"id": "x"}))
        self.assertIn("出错", out)
        self.assertIn("schedule_get", out)

    def test_run_without_scheduler(self):
        app = FakeApp()
        app.watch_scheduler = None
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        out = _run(_plugin(app).execute("schedule_run", {"id": "x"}))
        self.assertIn("[Error]", out)

    def test_run_cross_workspace_needs_force(self):
        app = FakeApp(workspace="/ws/a")
        app.schedule_store.upsert(Schedule(name="别人", source="astock",
                                           workspace="/ws/b"))
        out = _run(_plugin(app).execute("schedule_run", {"id": "别人"}))
        self.assertIn("[Error]", out)
        self.assertEqual(app.watch_scheduler.fired, [])


# ---------------------------------------------------------------- 删除


class TestDelete(unittest.TestCase):
    def test_default_is_preview(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        out = _run(_plugin(app).execute("schedule_delete", {"id": "x"}))
        self.assertIn("预览", out)
        self.assertIn("dry_run=false", out)
        self.assertEqual(len(app.schedule_store.all()), 1, "预览不能真删")

    def test_explicit_delete(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        out = _run(_plugin(app).execute("schedule_delete",
                                        {"id": "x", "dry_run": False}))
        self.assertIn("已删除", out)
        self.assertEqual(app.schedule_store.all(), [])

    def test_delete_recommends_disable(self):
        app = FakeApp()
        app.schedule_store.upsert(Schedule(name="x", source="astock"))
        out = _run(_plugin(app).execute("schedule_delete", {"id": "x"}))
        self.assertIn("enabled=false", out, "应提示暂停优先于删除")

    def test_cross_workspace_delete_blocked(self):
        app = FakeApp(workspace="/ws/a")
        app.schedule_store.upsert(Schedule(name="别人", source="astock",
                                           workspace="/ws/b"))
        out = _run(_plugin(app).execute("schedule_delete",
                                        {"id": "别人", "dry_run": False}))
        self.assertIn("[Error]", out)
        self.assertEqual(len(app.schedule_store.all()), 1)


# ---------------------------------------------------------------- 边界


class TestRobustness(unittest.TestCase):
    def test_unknown_tool(self):
        out = _run(_plugin(FakeApp()).execute("schedule_nope", {}))
        self.assertIn("未知工具", out)

    def test_store_internal_error_contained(self):
        class Boom(FakeApp):
            @property
            def schedule_store(self):
                raise RuntimeError("宿主内部结构变了")

        out = _run(_plugin(Boom()).execute("schedule_list", {}))
        self.assertIn("[Error]", out)

    def test_upsert_failure_reported(self):
        class BadStore(FakeStore):
            def upsert(self, sched):
                raise OSError("磁盘满")

        app = FakeApp()
        app._store = BadStore()
        out = _run(_plugin(app).execute("schedule_upsert", {"name": "x"}))
        self.assertIn("[Error]", out)
        self.assertIn("磁盘满", out)

    def test_no_app_at_all(self):
        out = _run(watch.WatchPlanPlugin().execute("schedule_list", {}))
        self.assertIn("[Error]", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
