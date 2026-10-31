# ocr-plugin 工作区解析回归
#
# 真实故障：宿主 `app.workspace` 在桌面多端模式下常常是 **None**
# （`POST /api/workspace` 只登记该端的 client-view，不写全局），旧实现
# `getattr(self._app, "workspace", None)` 拿到 None → OCRTools 回落到家目录
# → 项目里的图片一律被判「路径越界」。
#
# 运行：PYTHONPATH=<主仓库> python plugins/ocr-plugin/test_workspace_resolution.py
import importlib.util
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ocr_plugin_under_test", os.path.join(_HERE, "plugin.py"))
ocrplugin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ocrplugin)  # type: ignore[union-attr]


class WorkspaceResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="lw-ocr-ws-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _mkws(self, name):
        path = os.path.join(self._tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_none_app_and_none_workspace(self):
        self.assertIsNone(ocrplugin._current_workspace(None))
        self.assertIsNone(ocrplugin._current_workspace(SimpleNamespace(workspace=None)))

    def test_relative_or_missing_workspace_rejected(self):
        for bad in ("", ".", "..", "relative/dir",
                    os.path.join(self._tmp, "does-not-exist")):
            self.assertIsNone(ocrplugin._current_workspace(
                SimpleNamespace(workspace=bad)), f"{bad!r} 不应被采纳")

    def test_header_context_wins(self):
        proj = self._mkws("proj")
        wt = self._mkws("wt")
        from litework.core.types import header_context

        token = header_context.set({"workspace": wt})
        try:
            self.assertEqual(
                ocrplugin._current_workspace(SimpleNamespace(workspace=proj)), wt)
        finally:
            header_context.reset(token)

    def test_session_metadata_fallback(self):
        proj = self._mkws("proj")
        other = self._mkws("other")

        class _Store:
            def load(self, sid):
                return SimpleNamespace(metadata={"workspace": proj})

        app = SimpleNamespace(workspace=other, session_store=_Store())
        from litework.tools import todos as _todos

        token = _todos.current_session_id.set("session_x")
        try:
            self.assertEqual(ocrplugin._current_workspace(app), proj)
        finally:
            _todos.current_session_id.reset(token)

    def test_project_files_no_longer_out_of_bounds(self):
        """核心回归：项目内的图片必须是「工作区内的文件」，不能判越界。"""
        proj = self._mkws("proj")
        img = os.path.join(proj, "scan.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG")

        class _Store:
            def load(self, sid):
                return SimpleNamespace(metadata={"workspace": proj})

        app = SimpleNamespace(workspace=None, session_store=_Store())
        from litework.tools import todos as _todos

        token = _todos.current_session_id.set("session_x")
        try:
            tools = ocrplugin.OCRTools(ocrplugin._current_workspace(app))
            self.assertEqual(tools.workspace, proj)
            self.assertEqual(tools._resolve_path("scan.png"), img)
        finally:
            _todos.current_session_id.reset(token)


if __name__ == "__main__":
    unittest.main()
