"""`astock_plan` 单测：自动创建/维护宿主盯梢计划。

**为什么这个模块必须重点测**：它是「说了要盯盘、结果什么都没发生」这类
静默失败的根因所在。而且它伸手进宿主内部属性（`app.schedule_store`），
宿主改结构时必须退回手动模式而不是炸掉插件——这两点都只能用测试锁住。

🚫 红线：不写用户真实 `~/.lite-work/schedules.json`。全部用内存假 store。
"""
from __future__ import annotations

import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _install_litework_stubs() -> None:
    """宿主包的极简替身（离线跑）。

    只补 `astock_plan` 真正用到的东西：`litework.watch.schedule_store.Schedule`。
    与主测试文件的做法一致（都是从文件加载兄弟模块 + 打桩依赖）。
    """
    if "litework" not in sys.modules:
        lw = types.ModuleType("litework")
        lw.__path__ = []                       # 让它看起来是包
        sys.modules["litework"] = lw
    for name, attrs in (
        ("litework.watch", {}),
        ("litework.core", {}),
        ("litework.tools", {}),
    ):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            mod.__path__ = []
            sys.modules[name] = mod
        for k, v in attrs.items():
            setattr(mod, k, v)

    if "litework.watch.schedule_store" not in sys.modules:
        store_mod = types.ModuleType("litework.watch.schedule_store")

        class Schedule:
            _seq = 0

            def __init__(self, **kw):
                Schedule._seq += 1
                self.id = f"sch_{Schedule._seq}"
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

        store_mod.Schedule = Schedule
        store_mod.ScheduleStore = object
        store_mod.RunRecord = object
        sys.modules["litework.watch.schedule_store"] = store_mod
        sys.modules["litework.watch"].schedule_store = store_mod


_install_litework_stubs()
sys.path.insert(0, _HERE)

import astock_plan as plan                      # noqa: E402


class FakeStore:
    """内存版 ScheduleStore：只实现本模块用到的方法。"""

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

    def reschedule(self, sid):
        self.rescheduled.append(sid)


class FakeApp:
    def __init__(self, with_store=True, with_scheduler=True):
        self._store = FakeStore() if with_store else None
        self.watch_scheduler = FakeScheduler() if with_scheduler else None

    @property
    def schedule_store(self):
        if self._store is None:
            raise AttributeError("no store")
        return self._store


class TestEnsurePlan(unittest.TestCase):
    def test_creates_when_missing(self):
        app = FakeApp()
        res = plan.ensure_plan(app, "/ws/a", interval=300)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "created")
        scheds = app.schedule_store.all()
        self.assertEqual(len(scheds), 1)
        s = scheds[0]
        self.assertEqual(s.source, "astock")
        self.assertEqual(s.interval_seconds, 300)
        self.assertEqual(s.workspace, "/ws/a")
        self.assertEqual(s.permissions, "read")
        self.assertTrue(s.enabled)
        # 必须通知调度器重算，否则新计划要等下一 tick 才生效
        self.assertEqual(app.watch_scheduler.rescheduled, [s.id])

    def test_idempotent_no_duplicate(self):
        app = FakeApp()
        plan.ensure_plan(app, "/ws/a", interval=300)
        res = plan.ensure_plan(app, "/ws/a", interval=300)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "existing")
        self.assertEqual(len(app.schedule_store.all()), 1,
                         "重复启动盯盘不该建出第二条计划")

    def test_repairs_disabled_plan(self):
        """暂停后再启动，必须复用原计划（保留运行历史/指纹基线）。"""
        app = FakeApp()
        first = plan.ensure_plan(app, "/ws/a", interval=300)
        sid = first["schedule_id"]
        plan.set_enabled(app, False)
        self.assertFalse(app.schedule_store.get(sid).enabled)

        res = plan.ensure_plan(app, "/ws/a", interval=300)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "repaired")
        self.assertEqual(res["schedule_id"], sid, "必须复用同一个 id")
        self.assertTrue(app.schedule_store.get(sid).enabled)
        self.assertEqual(len(app.schedule_store.all()), 1)

    def test_repairs_wrong_interval_and_workspace(self):
        app = FakeApp()
        sid = plan.ensure_plan(app, "/ws/a", interval=300)["schedule_id"]
        s = app.schedule_store.get(sid)
        s.interval_seconds = 3600          # 用户手改成 1 小时（几乎等于没盯）
        s.workspace = "/ws/wrong"          # 盯错工作区 = 盯错台账

        res = plan.ensure_plan(app, "/ws/a", interval=300)
        self.assertEqual(res["action"], "repaired")
        s = app.schedule_store.get(sid)
        self.assertEqual(s.interval_seconds, 300)
        self.assertEqual(s.workspace, "/ws/a")

    def test_interval_floor(self):
        app = FakeApp()
        res = plan.ensure_plan(app, "/ws", interval=5)   # 太小 → 抬到 60
        s = app.schedule_store.get(res["schedule_id"])
        self.assertGreaterEqual(s.interval_seconds, 60)

    def test_probe_cfg_merged_not_replaced(self):
        app = FakeApp()
        sid = plan.ensure_plan(app, "/ws", interval=300,
                               probe_cfg={"stop_loss_pct": 8})["schedule_id"]
        plan.ensure_plan(app, "/ws", interval=300,
                         probe_cfg={"take_profit_pct": 15})
        s = app.schedule_store.get(sid)
        self.assertEqual(s.probe_config.get("stop_loss_pct"), 8)
        self.assertEqual(s.probe_config.get("take_profit_pct"), 15)


class TestDegradation(unittest.TestCase):
    """拿不到宿主能力时必须如实降级，不能假装成功。"""

    def test_no_app(self):
        res = plan.ensure_plan(None, "/ws")
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "unavailable")
        self.assertIn("拿不到", res["message"])

    def test_no_store(self):
        res = plan.ensure_plan(FakeApp(with_store=False), "/ws")
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "unavailable")

    def test_store_read_error_is_contained(self):
        class Boom(FakeApp):
            @property
            def schedule_store(self):
                raise RuntimeError("宿主内部结构变了")

        res = plan.ensure_plan(Boom(), "/ws")
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "unavailable")

    def test_no_scheduler_still_creates(self):
        """没有调度器也要把计划写进去（下一 tick 会自动带出来）。"""
        app = FakeApp(with_scheduler=False)
        res = plan.ensure_plan(app, "/ws", interval=300)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(app.schedule_store.all()), 1)

    def test_upsert_failure_reported(self):
        class BadStore(FakeStore):
            def upsert(self, sched):
                raise OSError("磁盘满")

        app = FakeApp()
        app._store = BadStore()
        res = plan.ensure_plan(app, "/ws")
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "error")


class TestDiagnose(unittest.TestCase):
    """体检结论必须能指出**卡在哪一环**（三者任一没配好症状都一样）。"""

    def test_plugin_off(self):
        app = FakeApp()
        plan.ensure_plan(app, "/ws")
        d = plan.diagnose(app, now_watching=False, only_hours=True)
        self.assertIn("开关是关闭", d["verdict"])
        self.assertTrue(d["actions"])

    def test_no_plan(self):
        app = FakeApp()
        d = plan.diagnose(app, now_watching=True, only_hours=True)
        self.assertFalse(d["plan_ok"])
        self.assertIn("还没有盯梢计划", d["verdict"])

    def test_plan_disabled(self):
        app = FakeApp()
        plan.ensure_plan(app, "/ws")
        plan.set_enabled(app, False)
        d = plan.diagnose(app, now_watching=True, only_hours=True)
        self.assertFalse(d["plan_ok"])
        self.assertIn("被禁用", d["verdict"])

    def test_all_ready(self):
        app = FakeApp()
        plan.ensure_plan(app, "/ws", interval=300)
        d = plan.diagnose(app, now_watching=True, only_hours=True)
        self.assertTrue(d["plan_ok"])
        self.assertIn("全链路就绪", d["verdict"])
        self.assertIn("300", d["verdict"])

    def test_only_hours_off_warns_token(self):
        app = FakeApp()
        plan.ensure_plan(app, "/ws")
        d = plan.diagnose(app, now_watching=True, only_hours=False)
        self.assertTrue(d["plan_ok"])
        self.assertIn("token", d["verdict"])

    def test_after_close_is_not_a_fault(self):
        """收盘后体检不能说"开关是关闭的"——那是把正常作息当成故障。

        真实误报（v2.1.4）：调用方传 `enabled and ok_now`，于是 15:00 之后
        必然报"插件盯盘开关是关闭的"并给出"点按钮打开开关"的错误动作，
        而用户明明开着；用户会以为插件坏了。
        """
        app = FakeApp()
        plan.ensure_plan(app, "/ws", interval=300)
        d = plan.diagnose(app, now_watching=True, only_hours=True,
                          in_window=False, window_note="已收盘（15:00 后价格不再变动）")
        self.assertTrue(d["plugin_on"], "开关是开着的")
        self.assertTrue(d["plan_ok"])
        self.assertNotIn("开关是关闭", d["verdict"])
        self.assertIn("就绪", d["verdict"])
        self.assertIn("已收盘", d["verdict"])
        self.assertEqual(d["actions"], [], "链路没坏就不该给修复动作")

    def test_in_window_verdict_still_says_ready(self):
        app = FakeApp()
        plan.ensure_plan(app, "/ws", interval=300)
        d = plan.diagnose(app, now_watching=True, only_hours=True, in_window=True)
        self.assertIn("全链路就绪", d["verdict"])
        self.assertNotIn("不在盯盘时段", d["verdict"])


class TestLastRun(unittest.TestCase):
    """「它真的在跑」比「计划存在」更让用户安心——所以要把最近一次探测显示出来。"""

    def _app_with_runs(self, runs):
        app = FakeApp()
        sid = plan.ensure_plan(app, "/ws", interval=300)["schedule_id"]
        app.schedule_store.get(sid).runs = runs
        return app

    def test_no_runs_is_honest(self):
        app = self._app_with_runs([])
        d = plan.diagnose(app, True, True)
        self.assertIn("还没探测过", d["last_run_text"])

    def test_shows_time_and_status(self):
        import time as _t
        app = self._app_with_runs([
            {"at": _t.time(), "status": "unchanged", "detail": "盯 3 只，无异动"}])
        d = plan.diagnose(app, True, True)
        self.assertIn("无异动", d["last_run_text"])
        self.assertIn("盯 3 只", d["last_run_text"])

    def test_changed_status_called_out(self):
        import time as _t
        app = self._app_with_runs([{"at": _t.time(), "status": "changed",
                                    "detail": "2 条信号"}])
        d = plan.diagnose(app, True, True)
        self.assertIn("发现变化", d["last_run_text"])

    def test_error_status_called_out(self):
        import time as _t
        app = self._app_with_runs([{"at": _t.time(), "status": "error",
                                    "detail": "行情获取失败"}])
        d = plan.diagnose(app, True, True)
        self.assertIn("出错", d["last_run_text"])

    def test_uses_latest_run(self):
        import time as _t
        app = self._app_with_runs([
            {"at": _t.time() - 600, "status": "error", "detail": "旧失败"},
            {"at": _t.time(), "status": "ok", "detail": "最新正常"}])
        d = plan.diagnose(app, True, True)
        self.assertIn("最新正常", d["last_run_text"])
        self.assertNotIn("旧失败", d["last_run_text"])

    def test_object_runs_supported(self):
        """宿主用 RunRecord 对象（非 dict）时也要能读。"""
        import time as _t
        from litework.watch.schedule_store import RunRecord

        class R:
            at = _t.time()
            status = "ok"
            detail = "对象形态"
            task_id = None

        app = self._app_with_runs([R()])
        d = plan.diagnose(app, True, True)
        self.assertIn("对象形态", d["last_run_text"])


class TestSetEnabled(unittest.TestCase):
    def test_disable_keeps_plan_and_hash(self):
        """暂停必须**禁用而非删除**：删了会丢指纹基线，恢复时重报全部信号。"""
        app = FakeApp()
        sid = plan.ensure_plan(app, "/ws")["schedule_id"]
        s = app.schedule_store.get(sid)
        s.last_hash = "abc123"

        res = plan.set_enabled(app, False)
        self.assertTrue(res["ok"], res)
        keep = app.schedule_store.get(sid)
        self.assertIsNotNone(keep, "暂停不该删除计划")
        self.assertFalse(keep.enabled)
        self.assertEqual(keep.last_hash, "abc123", "指纹基线必须保留")

    def test_enable_back(self):
        app = FakeApp()
        sid = plan.ensure_plan(app, "/ws")["schedule_id"]
        plan.set_enabled(app, False)
        res = plan.set_enabled(app, True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(app.schedule_store.get(sid).enabled)

    def test_no_plan(self):
        res = plan.set_enabled(FakeApp(), False)
        self.assertFalse(res["ok"])
        self.assertIn("还没有", res["message"])


class TestNotTouchingForeignPlans(unittest.TestCase):
    def test_other_sources_untouched(self):
        """别的插件的盯梢计划绝不能被本模块修改。"""
        app = FakeApp()
        from litework.watch.schedule_store import Schedule
        email_plan = Schedule(name="邮件盯梢", source="email",
                              interval_seconds=120, enabled=False)
        app.schedule_store.upsert(email_plan)

        plan.ensure_plan(app, "/ws", interval=300)
        plan.set_enabled(app, False)

        keep = app.schedule_store.get(email_plan.id)
        self.assertEqual(keep.interval_seconds, 120)
        self.assertFalse(keep.enabled, "别人的计划不该被本插件启用")
        self.assertEqual(len([s for s in app.schedule_store.all()
                              if s.source == "astock"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
