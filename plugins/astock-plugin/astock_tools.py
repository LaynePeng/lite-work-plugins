"""工具声明（16 个工具的 schema）：纯静态数据，不含任何逻辑。

────────────────────────────────────────────────────────────────────────
为什么单独成文件
────────────────────────────────────────────────────────────────────────
`get_tools()` 有 450 行，全是 `ToolDefinition(...)` 字面量——**没有一行逻辑**，
却占了 `plugin.py` 近七分之一。它挤在业务代码里有两个实际坏处：

① 改业务逻辑时要在 900 行声明里翻找，容易误改 description；
② 声明与实现的 diff 混在一起，review 时噪音很大。

拆出来后：`plugin.get_tools()` 只留一行 `return build_tools()`，
声明想改就改 `astock_tools.py`。

⚠️ **`manifest.json` 的 `tools` 必须与本文件一致**（CI 会检查）：
`tests` 里 `test_all_tools_declared_in_manifest` 会比对两者集合。
"""
from __future__ import annotations

from typing import List

from litework.core.types import ToolDefinition


def build_tools() -> List[ToolDefinition]:
    code_prop = {
        "type": "string",
        "description": "股票代码：600000 / sh600000 / 000001.sz 均可",
    }
    return [
        ToolDefinition(
            name="astock_quote",
            description=(
                "A 股实时行情快照：现价、涨跌幅、涨跌停价、量比、换手、市值、"
                "五档外的关键字段与交易时段说明。不给 codes 时返回台账里的持仓+自选股。"
                "批量最多 50 只。数据来源腾讯行情（失败自动换新浪）。"),
            parameters={
                "type": "object",
                "properties": {
                    "codes": {
                        "type": "array", "items": {"type": "string"},
                        "description": "代码列表，如 [\"600000\",\"000001\"]；留空＝持仓+自选股",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["stock", "index", "etf"],
                        "description": "标的类型；查指数必须传 index（000001 既是个股也是上证指数）",
                    },
                    "no_cache": {"type": "boolean", "description": "跳过本地缓存强制刷新"},
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_kline",
            description=(
                "K 线 + 技术摘要：日/周/月/分钟线，默认前复权。返回均线(MA5/10/20/60)、"
                "区间高低、累计涨跌幅、最近 N 根量能对比。用于判断位置与趋势，但**不做预测**。"),
            parameters={
                "type": "object",
                "properties": {
                    "code": code_prop,
                    "period": {
                        "type": "string",
                        "enum": ["day", "week", "month", "5m", "15m", "30m", "60m"],
                        "description": "周期，默认 day",
                    },
                    "limit": {"type": "number", "description": "取多少根，默认 120，上限 640"},
                    "adjust": {
                        "type": "string", "enum": ["qfq", "hfq", "none"],
                        "description": "复权方式，默认 qfq（前复权）；分钟线不支持复权",
                    },
                    "with_bars": {
                        "type": "boolean",
                        "description": "是否附带最近 K 线的原始数据（默认 false，只给摘要）",
                    },
                },
                "required": ["code"],
            },
        ),
        ToolDefinition(
            name="astock_search",
            description="按代码/名称/拼音检索 A 股标的（返回代码、类型、板块）。买入前先确认代码。",
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "代码或名称或拼音首字母，如 浦发 / PFYH / 600000"},
                    "limit": {"type": "number", "description": "最多几条，默认 10"},
                },
                "required": ["keyword"],
            },
        ),
        ToolDefinition(
            name="astock_screen",
            description=(
                "条件选股（全市场快照排序+过滤）：按涨跌幅/成交额/换手/市值/主力净流入排序，"
                "可过滤涨跌幅区间、成交额、换手率、市值、是否排除 ST。金额单位：元。"
                "只做筛选，不构成推荐。"),
            parameters={
                "type": "object",
                "properties": {
                    "universe": {"type": "string", "enum": ["all", "main", "gem", "star", "bj", "etf"],
                                 "description": "市场范围，默认 all"},
                    "sort_by": {"type": "string",
                                "enum": ["change_pct", "price", "amount", "volume", "turnover", "mcap", "pe", "main_net"],
                                "description": "排序字段，默认 change_pct"},
                    "ascending": {"type": "boolean", "description": "升序（默认降序）"},
                    "limit": {"type": "number", "description": "返回条数，默认 20"},
                    "min_change": {"type": "number", "description": "涨跌幅下限 %"},
                    "max_change": {"type": "number", "description": "涨跌幅上限 %"},
                    "min_amount": {"type": "number", "description": "成交额下限（元）"},
                    "max_amount": {"type": "number", "description": "成交额上限（元）"},
                    "min_turnover": {"type": "number", "description": "换手率下限 %"},
                    "max_turnover": {"type": "number", "description": "换手率上限 %"},
                    "min_mcap": {"type": "number", "description": "总市值下限（元）"},
                    "max_mcap": {"type": "number", "description": "总市值上限（元）"},
                    "exclude_st": {"type": "boolean", "description": "排除 ST，默认 true"},
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_flow",
            description="个股资金流：近 N 日主力/超大单/大单/中单/小单净流入（元）。用来看资金是否持续流入。",
            parameters={
                "type": "object",
                "properties": {
                    "code": code_prop,
                    "days": {"type": "number", "description": "取最近几天，默认 10"},
                },
                "required": ["code"],
            },
        ),
        ToolDefinition(
            name="astock_record",
            description=(
                "录入一笔**已在自己券商成交**的买卖（本插件不下单、不连接券商）。"
                "buy/sell + 成交价 + 数量 + 成交日期；自动算费用、更新移动加权成本、"
                "校验 T+1 可卖与整手规则。dry_run=true 先预览校验结果不落账。"),
            parameters={
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["buy", "sell"], "description": "买入或卖出"},
                    "code": code_prop,
                    "price": {"type": "number", "description": "实际成交价（元）"},
                    "shares": {"type": "number", "description": "成交数量（股）"},
                    "trade_date": {"type": "string", "description": "成交日期 YYYY-MM-DD，默认今天"},
                    "name": {"type": "string", "description": "名称（可选，首次录入时便于展示）"},
                    "note": {"type": "string", "description": "备注（可选）"},
                    "fee": {"type": "number", "description": "实际费用合计（可选；不给则按设置费率自动算）"},
                    "dry_run": {"type": "boolean", "description": "true＝只预览校验，不写台账"},
                },
                "required": ["side", "code", "price", "shares"],
            },
        ),
        ToolDefinition(
            name="astock_portfolio",
            description=(
                "台账总览：持仓（实时浮盈/占比/今日盈亏）+ 可用资金 + 已实现盈亏 + 累计费用。"
                "action=trades 查流水；action=set_cash 设资金；action=delete_trade 删错录的流水"
                "（会自动按剩余流水重算成本）；action=summary 只要汇总。"),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "summary", "trades", "set_cash", "delete_trade"],
                               "description": "默认 list"},
                    "cash": {"type": "number", "description": "action=set_cash 时的可用资金余额"},
                    "trade_id": {"type": "number", "description": "action=delete_trade 时的流水 id"},
                    "code": {"type": "string", "description": "action=trades 时按代码过滤"},
                    "limit": {"type": "number", "description": "action=trades 返回条数，默认 30"},
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_hint",
            description=(
                "**下单提示**（本插件的核心）：结合实时行情与台账，给出"
                "① 该不该动手（止损/止盈/回撤/涨跌停/异动是否触发）；"
                "② 动多少（买入股数按资金与单票上限估算、卖出股数/清仓或减半）；"
                "③ 参考价格区间与费用；④ T+1 今日可卖多少；⑤ 附舆情背离提醒（研报vs两融、热度vs资金等）。"
                "指定 code 只算该标的，留空则扫全部持仓。"),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "只算这一只（可选）"},
                    "buy": {"type": "boolean", "description": "同时给出买入数量估算（需配合 code）"},
                    "sell_ratio": {"type": "number", "description": "卖出比例，如 0.5 表示减半、1 清仓"},
                    "no_sentiment": {"type": "boolean",
                                     "description": "true＝跳过舆情背离速查（默认会查，慢约 1-3 秒）"},
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_sync",
            description=(
                "**成交回单同步**（你在券商下单后用它）：把券商 App 的成交明细"
                "粘贴进来，或传导出的 CSV/TSV 内容，一次解析多笔 → 自动匹配"
                "之前的建议计划 → 校价（成交价与计划价偏差）→ 落账更新成本与 T+1。"
                "默认 dry_run 只预览不落账。识别不出的行会明确列出，不猜。"),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": ("回单原文。支持：带表头的 CSV/TSV（列名自动识别）；"
                                        "或每行一笔 `600000 买入 9.53 2000 2026-10-24`"),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "默认 true＝只预览校验，确认后再用 false 落账",
                    },
                },
                "required": ["text"],
            },
        ),
        ToolDefinition(
            name="astock_sentiment",
            description=(
                "**投资舆情分析**（重要参考）：按七类证据归集——公司公告、机构研报、"
                "相关新闻、市场热度、龙虎榜、融资融券、股东户数。每条标可靠度"
                "（事实/观点/媒体/情绪）与影响方向，并**优先给出信号背离提示**"
                "（如「研报偏多但融资余额下降」）。不合成情绪分、不预测涨跌。"
                "可选返回行业背景与全市场快讯。"
                "code 传 **portfolio** ＝跑持仓+自选的全量组合报告"
                "（与盘后自动报告同款输出）。"),
            parameters={
                "type": "object",
                "properties": {
                    "code": code_prop,
                    "scope": {
                        "type": "array",
                        "items": {"type": "string",
                                  "enum": ["announce", "report", "news", "hot",
                                           "lhb", "margin", "holder", "feed"]},
                        "description": "只取指定类别（默认除 feed 外全部）；查行业政策时加 feed",
                    },
                    "days": {"type": "number",
                             "description": "证据回溯天数，默认 7（研报固定至少看 6 个月）"},
                    "brief": {"type": "boolean",
                              "description": "true＝只要一句话结论 + 背离，不要完整证据清单"},
                },
                "required": ["code"],
            },
        ),
        ToolDefinition(
            name="astock_events",
            description=(
                "**事件驱动筛股**：全市场快讯流 → 个股硬事件（回购/增持/中标/"
                "预增/重组/获批/分红，可选利空）→ 5日主力资金印证 → 背离标记。"
                "只列事实与资金行为，**不给涨跌概率、不预测、不喊单**——"
                "概率无数学基础，编造数字比不给更危险。"),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "事件类型过滤：回购/增持/中标/预增/重组/"
                                       "获批/分红/利空；留空=利好类全部",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多列几条（默认 8）",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_pick",
            description=(
                "**量化选股 + 买入数量**（本插件新增的核心能力）："
                "全市场扫描 → 硬门槛过滤 → 多因子池内打分 → 按现金/单票上限"
                "换算成**具体股数**，输出「买哪几只、每只买多少股、挂什么止损止盈价」。"
                "三套策略（因子方向相反，按市况选）："
                "momentum 趋势动量（站上均线+放量+接近高点，趋势市用）/ "
                "reversal 均值回归（跌到区间低位+RSI超卖，震荡市用）/ "
                "quality 质量价值（低PE/PB+大市值+低波动，长期持有）。"
                "得分是**候选池内相对排名**（0-100），不是上涨概率、不是预期收益；"
                "每只都给出得分来源（各因子贡献）以便核对。"
                "rank_only=true 只出排行不给股数（想自己决定买哪只时用）。"
                "**compare=true 一次采集同时算出三套并做横向对比**"
                "（三套共用同一份行情，成本≈单策略，且价格同源可比）——"
                "用户问「三套策略对比一下」「哪个策略更合适」时用它。"
                "整个扫描约 6 秒（30 只 K 线，受限速）。"),
            parameters={
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["momentum", "reversal", "quality"],
                        "description": ("策略。momentum 追强势（趋势市）/ "
                                        "reversal 抄超跌（震荡市）/ "
                                        "quality 低估值大盘（长期）。"
                                        "留空＝用设置页的默认策略"),
                    },
                    "rank_only": {
                        "type": "boolean",
                        "description": "true＝只出得分排行，不给买入股数",
                    },
                    "compare": {
                        "type": "boolean",
                        "description": ("true＝一次采集同时跑三套策略并横向对比"
                                        "（成本≈单策略，价格同源）。"
                                        "用户想比较三套策略时用；默认只跑一套"),
                    },
                    "max_positions": {
                        "type": "number",
                        "description": "本轮最多买几只（默认取设置页，通常 5）",
                    },
                    "cash_usage_pct": {
                        "type": "number",
                        "description": ("本轮最多动用可用现金的百分比"
                                        "（默认取设置页，通常 80）"),
                    },
                    "universe": {
                        "type": "string",
                        "enum": ["main", "all", "gem", "star"],
                        "description": "选股范围，默认 main（沪深主板+创业板）",
                    },
                    "fresh": {
                        "type": "boolean",
                        "description": "true＝跳过缓存强制重新扫描（默认用 3 分钟内缓存）",
                    },
                    "top_n": {
                        "type": "number",
                        "description": "rank_only 时显示前几名，默认 10",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_backtest",
            description=(
                "**策略回测 + 推荐率**：把选股策略放回历史逐期重放，统计"
                "「按这套策略选股，历史上多少期跑赢沪深300」——这个比例就是"
                "**推荐率**，它来自历史数据，不是主观打分。"
                "输出：推荐率、每期平均收益、平均超额、累计收益（策略 vs 基准）、"
                "最大回撤、最好/最差单期、逐期明细（可选）。"
                "**能力边界（重要）**：历史只有约 2.6 年（641 根日 K，接口上限），"
                "不含退市股（幸存者偏差，真实表现会略差），quality 策略的 PE/PB "
                "无历史序列故回测不完整。结论衡量的是**策略**，不是单只股票"
                "上涨的概率。历史不代表未来。约 6-14 秒（需拉 25 只历史 K 线）。"),
            parameters={
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["momentum", "reversal", "quality"],
                        "description": "要回测的策略；留空＝设置页默认策略",
                    },
                    "rebalance_days": {
                        "type": "number",
                        "description": "多少交易日调仓一次，默认 10（约两周）",
                    },
                    "top_k": {
                        "type": "number",
                        "description": "每期买入几只，默认 3",
                    },
                    "hold_days": {
                        "type": "number",
                        "description": "持有多少交易日，默认 10",
                    },
                    "pool_size": {
                        "type": "number",
                        "description": "回测股票池大小，默认 25（每只 1 次 K 线请求）",
                    },
                    "detail_periods": {
                        "type": "number",
                        "description": "附带最近 N 期逐期明细（默认 0＝不看明细）",
                    },
                    "fresh": {
                        "type": "boolean",
                        "description": "true＝跳过 30 分钟缓存重新回测",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_archive",
            description=(
                "**本地历史行情存档**：把日 K 落到本地 SQLite，让回测能突破"
                "在线接口「只有约 2.6 年」的硬上限。"
                "action=update 增量更新（默认标的：台账持仓+自选，可指定 codes）；"
                "status 看存档覆盖情况（多少只/多少根/起止日期/占用空间）；"
                "prune 清理（必须指定 before 或 codes，否则拒绝执行）。"
                "**重要**：存档只存「不复权」原始价（前复权会随分红重算整段历史，"
                "存了会自相矛盾）；**它不会立刻变长**——今天开始存，一年后多一年，"
                "所以建议每天跑一次。回测会自动优先使用存档数据。"),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["update", "status", "prune"],
                        "description": "默认 status",
                    },
                    "codes": {
                        "type": "array", "items": {"type": "string"},
                        "description": ("update：要更新的代码（留空＝台账持仓+自选）；"
                                        "prune：要清空的代码"),
                    },
                    "limit": {
                        "type": "number",
                        "description": "update 时每只取最近多少根，默认 640",
                    },
                    "before": {
                        "type": "string",
                        "description": "prune：删除该日期（YYYY-MM-DD）之前的数据",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_act",
            description=(
                "**操作指示**（傻瓜化的最后一步）：把「选股结果」和「当前持仓」"
                "合成为一份**照着做就行**的清单——先卖什么（卖多少股、什么价、为什么）、"
                "再买什么（买多少股、止损止盈价）、操作后现金剩多少。"
                "执行顺序固定「先卖后买」，因为 A 股卖出资金当日可用于买入，"
                "反过来先买会因资金不足失败。卖出仅基于明确规则：触发止损/止盈、"
                "不在本轮选股名单且浮亏（换仓）、单票超仓；"
                "数量已按 T+1 可卖约束裁剪，不会给出卖不掉的废单。"
                "**本插件不连接券商、不下单**，只产出给人看的指示。"),
            parameters={
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["momentum", "reversal", "quality"],
                        "description": "用哪套策略选股；留空＝设置页默认",
                    },
                    "max_positions": {
                        "type": "number",
                        "description": "本轮最多买几只（默认取设置页）",
                    },
                    "fresh": {
                        "type": "boolean",
                        "description": "true＝跳过缓存重新选股",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_watch",
            description=(
                "盯盘与自选股管理。action=list 看自选；add/remove 增删自选；"
                "**status 看盯盘状态**（是否在盯、当前时段、下次开始时间）；"
                "**start 启动盯盘**（开启定时探测，非交易时段自动跳过不耗 token）；"
                "**stop 暂停盯盘**。盯盘按 A 股交易时段自律：工作日 9:10–15:00、"
                "午休跳过、收盘自动停。"),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove", "status", "start", "stop"],
                        "description": "默认 list",
                    },
                    "code": code_prop,
                    "name": {"type": "string", "description": "名称（可选）"},
                    "note": {"type": "string", "description": "备注（可选）"},
                },
                "required": [],
            },
        ),
    ]
