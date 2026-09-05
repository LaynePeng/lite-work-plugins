# lite-work 社区插件示例：完全自包含，仅依赖 litework 包的稳定接口。
# 格式要点：
# - 插件目录 plugins/<插件名>/plugin.py（也支持单文件 plugins/<插件名>.py）
# - 定义 ToolPlugin 子类（可多个），加载器自动发现并注册
# - 构造函数必须无参；workspace 等运行时状态在 install(kernel) 时从
#   kernel 的 "app" 服务捕获（支持项目热切换）
# - version 用 semver；同名工具自动覆盖内置版，removed_tools 可移除
#   其他插件的工具；卸载后自动回退主程序内置版
from __future__ import annotations

from typing import Any, Dict, List, Optional

from litework.core.types import ToolDefinition
from litework.tools.plugin import ToolPlugin


class ExamplePlugin(ToolPlugin):
    name = "example-greeting"
    version = "1.0.0"
    description = "示例插件：文本统计工具（演示社区插件编写格式）"

    def __init__(self) -> None:
        self._app = None

    def install(self, kernel) -> None:
        """install 时从内核服务捕获 app 引用（每次新 kernel 都会重新调用）。"""
        try:
            if kernel.has_service("app"):
                self._app = kernel.get_service("app")
        except Exception:
            self._app = None
        super().install(kernel)

    def _workspace(self) -> Optional[str]:
        return getattr(self._app, "workspace", None) if self._app else None

    def get_tools(self) -> List[ToolDefinition]:
        return [
            ToolDefinition(
                name="text_stats",
                description="统计文本的字符数、单词数与行数（示例社区插件工具）",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要统计的文本内容"},
                    },
                    "required": ["text"],
                },
            ),
        ]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name != "text_stats":
            return f"[Error]: 未知工具 {name}"
        text = str(args.get("text") or "")
        lines = text.split("\n")
        words = len(text.split())
        return (
            f"[Example OK]: 字符数 {len(text)} | 单词数 {words} | "
            f"行数 {len(lines)}（来自社区示例插件 example-greeting）"
        )
