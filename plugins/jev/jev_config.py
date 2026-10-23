"""jev 插件 · 配置层（从 plugin.py 拆出，见 AGENTS.md 的多文件插件约定）。

⚠️ 模块命名与导入方式：
本文件与 `plugin.py` 同目录。宿主（`litework/tools/plugin_loader.py`）用
`spec_from_file_location` **按文件**加载 `plugin.py`，并把**插件目录插入
`sys.path`**——所以这里必须用**顶层唯一前缀**命名（`jev_*`）并走裸
`import jev_config`，不能用包相对导入（那不是包，会 ImportError）。

内容：配置常量与默认值、`JevConfig` 快照、四层配置解析（读侧）、
凭证私有镜像与可选的「写回主配置」（写侧）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

PLUGIN_NAME = "jev"

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13"            # 网关实际可用名（官方别名 jev-latest 会移动，不建议）
DEFAULT_THRESHOLD = 0.60
DEFAULT_SIZE_GATE_TOKENS = 8192
DEFAULT_TIMEOUT_MS = 1200        # 实时护栏应快速失败；真机单次约 1.3s，判不出就放行
DEFAULT_VERIFY_TIMEOUT_MS = 2500  # Verify 在收尾/轮次边界跑，可稍宽松
DEFAULT_MAX_ATTEMPTS = 2          # 1 次 + 1 次瞬时重试（原 3 次 + 长退避）
DEFAULT_CACHE_TTL_S = 600         # 判定结果缓存：同样的内容不重复问
DEFAULT_MAX_CONCURRENCY = 4       # 后台判定并发上限（避免线程/连接风暴）

logger = logging.getLogger("litework.plugins.jev")


# ---------------------------------------------------------------- Provider 契约
#
# 本模块只依赖 kernel 的这两个能力，不 import litework：
#   kernel.has_service("app") / kernel.get_service("app")
# 这样配置层可被独立导入与单测，不必打宿主桩。

def _has_service(kernel: Any, name: str) -> bool:
    fn = getattr(kernel, "has_service", None)
    return bool(fn(name)) if callable(fn) else False


def _get_service(kernel: Any, name: str) -> Any:
    return kernel.get_service(name)


# ---------------------------------------------------------------- 配置快照

class JevConfig:
    """插件配置快照。每次装配/调用都**现读**（app.config 是内存 dict 原地更新）。"""

    __slots__ = ("api_key", "model", "base_url", "headers", "threshold",
                 "size_gate_tokens", "timeout_ms", "tools_enabled",
                 "auto_gate_enabled", "has_header_credential",
                 "direct_answer", "direct_answer_confidence",
                 "skill_recommend", "screen", "verify", "verify_threshold",
                 "verify_finish_enforce",
                 "compact_decider", "compact_decider_threshold",
                 "compact_decider_batch", "compact_decider_min_keep_ratio",
                 "verify_timeout_ms", "max_attempts", "cache_ttl_s",
                 "max_concurrency", "async_screen", "screen_skip_self",
                 "screen_max_per_turn", "config_self_heal",
                 "reason")

    def __init__(self, raw: Dict[str, Any], env_key: str = "") -> None:
        self.api_key = str(raw.get("api_key") or env_key or "").strip()
        model = raw.get("model", DEFAULT_MODEL)
        self.model = str(model or "").strip()
        self.base_url = str(raw.get("base_url") or DEFAULT_BASE_URL).strip()
        self.threshold = float(raw.get("confidence_threshold") or DEFAULT_THRESHOLD)
        self.size_gate_tokens = int(raw.get("size_gate_tokens") or DEFAULT_SIZE_GATE_TOKENS)
        self.timeout_ms = int(raw.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        # 自定义请求头（网关 / 代理常用）：键值都强制为字符串
        raw_headers = raw.get("headers")
        if isinstance(raw_headers, str):        # 容错：被存成了 JSON 字符串
            try:
                raw_headers = json.loads(raw_headers)
            except Exception:  # noqa: BLE001
                raw_headers = None
        self.headers = ({str(k): str(v) for k, v in raw_headers.items()}
                        if isinstance(raw_headers, dict) else {})
        # 用户直问短路（opt-in，默认关）：命中 /jev 语法时由 Jev 直接作答、跳过 LLM
        self.direct_answer = bool(raw.get("direct_answer_enabled", False))
        self.direct_answer_confidence = float(
            raw.get("direct_answer_confidence") or 0.85)
        # 启动门通过之后才受这些开关控制（成本模型不同，见文档 §12.7）
        self.tools_enabled = bool(raw.get("tools_enabled", True))
        self.auto_gate_enabled = bool(raw.get("auto_gate_enabled", False))
        # 0.7.0 三个 opt-in 能力（默认关）：技能推荐 / Screen 护栏 / Verify 核验
        self.skill_recommend = bool(raw.get("skill_recommend_enabled", False))
        self.screen = bool(raw.get("screen_enabled", False))
        self.verify = bool(raw.get("verify_enabled", False))
        self.verify_threshold = float(raw.get("verify_threshold") or 0.5)
        # 0.8.0：Stop 钩子是否允许阻断收尾（默认 False = 只做顾问、只加信息）
        self.verify_finish_enforce = bool(raw.get("verify_finish_enforce", False))
        # 0.9.0 迟延治理（P0-P2）：不阻塞关键路径 + 内容缓存 + 超时收紧 + 并发上限
        self.async_screen = bool(raw.get("async_screen_enabled", True))
        self.screen_skip_self = bool(raw.get("screen_skip_self", True))
        try:
            self.screen_max_per_turn = max(0, int(raw.get("screen_max_per_turn") or 3))
        except (TypeError, ValueError):
            self.screen_max_per_turn = 3
        try:
            self.verify_timeout_ms = max(200, int(
                raw.get("verify_timeout_ms") or DEFAULT_VERIFY_TIMEOUT_MS))
        except (TypeError, ValueError):
            self.verify_timeout_ms = DEFAULT_VERIFY_TIMEOUT_MS
        try:
            self.max_attempts = max(1, int(raw.get("max_attempts") or DEFAULT_MAX_ATTEMPTS))
        except (TypeError, ValueError):
            self.max_attempts = DEFAULT_MAX_ATTEMPTS
        try:
            self.cache_ttl_s = max(0, int(raw.get("cache_ttl_s", DEFAULT_CACHE_TTL_S)))
        except (TypeError, ValueError):
            self.cache_ttl_s = DEFAULT_CACHE_TTL_S
        try:
            self.max_concurrency = max(1, int(
                raw.get("max_concurrency") or DEFAULT_MAX_CONCURRENCY))
        except (TypeError, ValueError):
            self.max_concurrency = DEFAULT_MAX_CONCURRENCY
        # 0.9.1：是否允许把凭证**写回用户主配置**（默认关；插件只写自己的镜像文件）
        self.config_self_heal = bool(raw.get("config_self_heal_enabled", False))
        # #1 Jev 压缩判定器（opt-in，默认关）：核心 compact_session 调用
        self.compact_decider = bool(raw.get("compact_decider_enabled", False))
        try:
            self.compact_decider_threshold = float(raw.get("compact_decider_threshold") or 0.6)
        except (TypeError, ValueError):
            self.compact_decider_threshold = 0.6
        try:
            self.compact_decider_batch = max(1, int(raw.get("compact_decider_batch") or 20))
        except (TypeError, ValueError):
            self.compact_decider_batch = 20
        try:
            self.compact_decider_min_keep_ratio = float(raw.get("compact_decider_min_keep_ratio") or 0.33)
        except (TypeError, ValueError):
            self.compact_decider_min_keep_ratio = 0.33

        # 凭证来源：API Key 字段，或自定义请求头里带的凭证（网关常用 x-api-key / token）
        _CRED_HEADS = {"authorization", "x-api-key", "api-key", "apikey", "token", "x-token"}
        self.has_header_credential = any(
            str(k).strip().lower() in _CRED_HEADS and str(v).strip()
            for k, v in self.headers.items()
        )
        if not self.api_key and not self.has_header_credential:
            self.reason = ("缺少凭证（在「API Key」填 key，或在「自定义请求头」里带 x-api-key / token 等）")
        elif not self.model:
            self.reason = "模型未设置（填固定版本，如 jev-1.13）"
        else:
            self.reason = ""

    @property
    def ready(self) -> bool:
        return not self.reason

    @property
    def timeout_s(self) -> float:
        return max(0.05, self.timeout_ms / 1000.0)


# ---------------------------------------------------------------- 私有镜像与小工具

#: 磁盘 jev 段缓存（path+mtime → 内容），避免每次 hook 都读盘
_DISK_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}

_PRIVATE_NAME = "config.local.json"


def _private_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _PRIVATE_NAME)


def _read_private() -> Dict[str, Any]:
    """读插件私有配置（首配置成功后镜像下来的凭证快照）。"""
    try:
        with open(_private_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_private(data: Dict[str, Any]) -> None:
    """写插件私有配置（0600；原子替换）。"""
    path = _private_path()
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.debug("[jev] 写私有配置失败", exc_info=True)


_CRED_KEYS = ("api_key", "base_url", "headers", "model", "direct_answer_enabled")

#: 配置来源的可读名称（面板/日志用，让"凭证到底从哪来的"可解释）
SOURCE_LABELS = {
    "memory": "设置页（内存）",
    "disk": "config.json（磁盘）",
    "env": "环境变量",
    "private": "插件私有镜像",
}

#: 「空值」判定：这些值等同于"该层没配"
_EMPTY = (None, "", {}, [])


def _is_blank(value: Any) -> bool:
    """该值是否等同于"这一层没配"。

    除 `_EMPTY` 外，**纯空白字符串**（如 `"  "`）也算空：`JevConfig` 构造时会
    `strip()` 凭证，所以 `"  "` 与 `""` 在语义上完全一样。若不在这里拦住它，
    空白值会占住"已配置"的位置、挡住下层真正的凭证，而设置页看起来又是空的
    ——用户会看到"填了却不生效（或生效的值来历不明）"。
    """
    if value in _EMPTY:
        return True
    return isinstance(value, str) and not value.strip()


def _merge_layer(raw: Dict[str, Any], sources: Dict[str, str],
                 layer: str, values: Dict[str, Any]) -> None:
    """按优先级合并一层配置：**已有非空值不覆盖**（前面的层优先）。

    两个方向都要判空，各防一类真实事故：
    - 本层值为空 → 不写、也不记来源（否则空值会覆盖高层真值，且面板虚报来源）；
    - 高层已有非空值 → 本层不覆盖（分层优先级）。
    """
    for key, value in (values or {}).items():
        if _is_blank(value):
            continue
        if not _is_blank(raw.get(key)):
            continue
        raw[key] = value
        sources[key] = layer


def _mirror_private(app: Any, raw: Dict[str, Any]) -> None:
    """把当前有效凭证镜像到**插件自己的目录**（0600）。

    注意作用域：这里只写 `plugins/jev/config.local.json`，**不碰用户主配置**。
    目的是让插件在"主 config 被别的保存动作抹掉"时仍能启动（读侧兜底见
    `read_config` 的 private 层）。

    仅在有 `config_dir` 的真 app 上启用：没有它的（测试替身 / 裸 Kernel）
    一律不写盘，避免污染。
    """
    if app is None or not getattr(app, "config_dir", None):
        return
    snapshot = {k: raw[k] for k in _CRED_KEYS if not _is_blank(raw.get(k))}
    if not snapshot:
        return
    priv = _read_private()
    if any(priv.get(k) != snapshot[k] for k in snapshot):
        _write_private({**priv, **snapshot})


#: 「已修回主配置」只报一次，避免每条消息刷日志
_HEAL_NOTIFIED = False


def _heal_main_config(app: Any, raw: Dict[str, Any], sources: Dict[str, str]) -> None:
    """**可选**地把有效凭证修回用户主配置（默认关闭，见 `config_self_heal_enabled`）。

    0.9.1 起默认**不写**用户配置：历史实现会在读配置的路径上顺手
    `app.save_config(...)`，于是"改过的键又回来了"——用户无法解释的配置残留。
    现在改为显式 opt-in，且每次写入都留 WARNING 级日志（含写回了哪些键）。
    注意：关掉它**不影响插件可用性**——私有镜像 + 读侧兜底已让凭证可恢复，
    只是设置页不会自动显示回来。
    """
    global _HEAL_NOTIFIED
    if app is None or _is_blank(raw.get("api_key")):
        return
    try:
        if not bool(raw.get("config_self_heal_enabled", False)):
            return
        current = dict(getattr(app, "config", {}).get(PLUGIN_NAME) or {})
        if not _is_blank(current.get("api_key")):
            return                                   # 主配置有值 → 不需要修
        snapshot = {k: raw[k] for k in _CRED_KEYS if not _is_blank(raw.get(k))}
        app.save_config({PLUGIN_NAME: {**current, **snapshot}})
        if not _HEAL_NOTIFIED:
            _HEAL_NOTIFIED = True
            logger.warning("[jev] 已按 config_self_heal_enabled=true 修回主配置的键：%s"
                           "（如不想让插件写你的 config.json，请关闭该开关）",
                           sorted(snapshot))
    except Exception:  # noqa: BLE001
        logger.debug("[jev] 修回主配置失败", exc_info=True)


def _read_disk_jev(config_dir: str) -> Dict[str, Any]:
    """读 `<config_dir>/config.json` 里的 `jev` 段（按 mtime 缓存）。"""
    path = os.path.join(config_dir, "config.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _DISK_CACHE["path"] == path and _DISK_CACHE["mtime"] == mtime:
        return _DISK_CACHE["data"]
    data: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict) and isinstance(cfg.get(PLUGIN_NAME), dict):
            data = dict(cfg[PLUGIN_NAME])
    except Exception:  # noqa: BLE001 - 读不到就当没有
        data = {}
    _DISK_CACHE.update({"path": path, "mtime": mtime, "data": data})
    return data


def read_config(kernel: Any, *, with_sources: bool = False) -> Any:
    """读配置，优先级链**显式且可解释**（前者优先，各层只补空缺）：

        1. `memory`  —— 设置页当前值（`app.config["jev"]`）
        2. `disk`    —— `<config_dir>/config.json` 的 `jev` 段（按 mtime 缓存）
        3. `env`     —— `JEV_BASE_URL` / `JEV_MODEL` / `JEV_HEADERS` / `TYPESAFE_API_KEY`
        4. `private` —— 插件私有镜像 `plugins/jev/config.local.json`

    为什么要有后三层：app 的配置只在启动时读一次，内存是**可能过期或被杀掉**的
    快照；实测事故是"配好的凭证被别的保存动作从内存抹掉后，插件一直报缺少凭证"。
    读侧兜底让这种抹除**不会**导致插件停摆，同时**不再需要写回用户主配置**。

    读盘只发生在内存缺键时；私有镜像带 mtime 缓存。`with_sources=True` 时额外
    返回每层命中的键（供「判断」面板解释凭证来源），签名对老调用方保持不变。
    """
    raw: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    app = None
    if _has_service(kernel, "app"):
        try:
            app = _get_service(kernel, "app")
            cfg = getattr(app, "config", None)
            if isinstance(cfg, dict):
                _merge_layer(raw, sources, "memory", dict(cfg.get(PLUGIN_NAME) or {}))
        except Exception:  # noqa: BLE001
            logger.debug("[jev] 读取 app.config 失败", exc_info=True)
    cfg_dir = getattr(app, "config_dir", None) if app is not None else None
    if cfg_dir:
        _merge_layer(raw, sources, "disk", _read_disk_jev(str(cfg_dir)))
    # 环境变量（永远有效：配置被抹也不受影响）
    env_values = {
        "base_url": os.environ.get("JEV_BASE_URL", ""),
        "model": os.environ.get("JEV_MODEL", ""),
        "headers": os.environ.get("JEV_HEADERS", ""),
        "api_key": os.environ.get("TYPESAFE_API_KEY", ""),
    }
    _merge_layer(raw, sources, "env", {k: v for k, v in env_values.items() if v})
    # 私有镜像兜底：比内存/主 config 都更能扛"被抹"
    _merge_layer(raw, sources, "private", _read_private())

    # 镜像当前有效凭证（只写插件自己的目录）；修回主配置是**显式 opt-in**
    _mirror_private(app, raw)
    _heal_main_config(app, raw, sources)

    cfg = JevConfig(raw, os.environ.get("TYPESAFE_API_KEY", ""))
    if with_sources:
        return cfg, sources
    return cfg
