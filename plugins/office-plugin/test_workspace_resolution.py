# office-plugin / ocr-plugin 工作区解析回归
#
# 共同的真实故障：宿主 `app.workspace` 在桌面多端模式下常常是 **None**
# （`POST /api/workspace` 只登记该端的 client-view，不写全局），旧实现
# `getattr(self._app, "workspace", None)` 因此拿到 None →
#   - office：产物静默写进 `~`，项目内文件被判「路径越界」；
#   - ocr：项目里的图片一律被判「路径越界」。
# 两个插件的 `_current_workspace()` 必须按
# `header_context` → 会话 metadata → 全局 app.workspace 的顺序回退。
#
# 运行：<主仓库 .venv python> -m unittest plugins... 不便时直接用
#   PYTHONPATH=<主仓库> python plugins/office-plugin/test_workspace_resolution.py
import importlib.util
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "office_plugin_under_test", os.path.join(_HERE, "plugin.py"))
officeplugin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(officeplugin)  # type: ignore[union-attr]


class WorkspaceResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="lw-office-ws-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _mkws(self, name):
        path = os.path.join(self._tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_none_app_and_none_workspace(self):
        """没有 app / app.workspace 为 None 时返回 None（让 OfficeTools 自己兜底）。"""
        self.assertIsNone(officeplugin._current_workspace(None))
        self.assertIsNone(officeplugin._current_workspace(SimpleNamespace(workspace=None)))

    def test_relative_or_missing_workspace_rejected(self):
        """相对路径 / 不存在目录不能被当成工作区（否则产物落到进程 CWD）。"""
        for bad in ("", ".", "..", "relative/dir",
                    os.path.join(self._tmp, "does-not-exist")):
            self.assertIsNone(officeplugin._current_workspace(
                SimpleNamespace(workspace=bad)), f"{bad!r} 不应被采纳")

    def test_header_context_wins(self):
        """任务钉死的工作区（worktree 隔离时是工作树）优先于全局 app.workspace。"""
        proj = self._mkws("proj")
        wt = self._mkws("wt")
        from litework.core.types import header_context

        token = header_context.set({"workspace": wt})
        try:
            self.assertEqual(
                officeplugin._current_workspace(SimpleNamespace(workspace=proj)), wt)
        finally:
            header_context.reset(token)

    def test_session_metadata_fallback(self):
        """无任务上下文时，用会话所属项目（桌面多端下的真实工作区）。"""
        proj = self._mkws("proj")
        other = self._mkws("other")

        class _Store:
            def load(self, sid):
                return SimpleNamespace(metadata={"workspace": proj})

        app = SimpleNamespace(workspace=other, session_store=_Store())
        from litework.tools import todos as _todos

        token = _todos.current_session_id.set("session_x")
        try:
            self.assertEqual(officeplugin._current_workspace(app), proj)
        finally:
            _todos.current_session_id.reset(token)

    def test_global_workspace_last_resort(self):
        """单端旧语义（无 client-id）仍支持：全局 app.workspace 可作兜底。"""
        proj = self._mkws("proj")
        self.assertEqual(
            officeplugin._current_workspace(SimpleNamespace(workspace=proj)), proj)


if __name__ == "__main__":
    unittest.main()
