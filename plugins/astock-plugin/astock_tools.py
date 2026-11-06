"""工具声明（19 个工具的 schema）：纯静态数据，不含任何逻辑。

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
            name="astock_calc",
            description=(
                "**技术公式计算器**：把常用公式固化成可复算的调用，"
                "不用手算也不用临时写脚本。"
                "mode=summary 全套指标（均线/偏离/RSI/年化波动率/动量/区间回撤/区间位置）；"
                "mode=ma 只算均线+偏离+位置（**盘后重算规则价用这个**）；"
                "mode=rule_check **价位规则体检**（校验 op 方向是否可达 + 均线是否漂移>1%，"
                "不传 rules 自动读当前盯盘计划）——能抓出「跌破 38.60 但现价 38.52」这类"
                "**永远不响的死规则**；"
                "mode=cost 算账（盈亏平衡价/止损止盈价/盈亏比/可买股数，注意「涨回成本价≠回本」）；"
                "mode=adjust 除权影响估算（不复权算长周期均线会虚高多少）；"
                "mode=ratio 收益/回撤比（选策略先看回撤）。"
                "口径：均线默认前复权 qfq，缺数据返回 - 不用 0 代替。只做统计，不预测涨跌。"),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["summary", "ma", "rule_check", "cost",
                                 "adjust", "ratio"],
                        "description": ("summary 全套 / ma 只算均线 / rule_check 规则体检 / "
                                        "cost 算账 / adjust 除权估算 / ratio 收益回撤比"),
                    },
                    "code": code_prop,
                    "period": {"type": "string",
                               "description": "K 线周期，默认 day（summary/ma 用）"},
                    "limit": {"type": "integer",
                              "description": "取多少根 K 线，默认 120"},
                    "adjust": {"type": "string", "enum": ["qfq", "hfq", "none"],
                               "description": "复权方式，默认 qfq（均线必须复权）"},
                    "rules": {
                        "type": "array", "items": {"type": "object"},
                        "description": ("rule_check 用：规则数组 "
                                        "[{code,price,op,label,ma_key}]。"
                                        "留空＝自动读当前盯盘计划的 rules"),
                    },
                    "use_ma": {"type": "boolean",
                               "description": "rule_check 是否做均线漂移检查，默认 true"},
                    "entry_price": {"type": "number",
                                    "description": "cost 用：成本价"},
                    "shares": {"type": "integer", "description": "cost 用：持有股数"},
                    "buy_fee": {"type": "number", "description": "cost 用：买入手续费"},
                    "sell_fee_rate": {"type": "number",
                                      "description": "cost 用：卖出费率合计（默认按主流费率）"},
                    "stop_pct": {"type": "number", "description": "cost 用：止损百分比，默认 8"},
                    "take_pct": {"type": "number", "description": "cost 用：止盈百分比，默认 15"},
                    "cash": {"type": "number", "description": "cost 用：可用现金（给了才算股数）"},
                    "equity": {"type": "number", "description": "cost 用：账户总资产"},
                    "max_position_pct": {"type": "number",
                                         "description": "cost 用：单票上限百分比"},
                    "cash_usage_pct": {"type": "number",
                                       "description": "cost 用：本轮最多动用现金百分比"},
                    "price": {"type": "number",
                              "description": "cost 用：拟买价（不给则用成本价）"},
                    "prev_close": {"type": "number",
                                   "description": "adjust 用：除权前收盘价"},
                    "dividend_per_share": {"type": "number",
                                           "description": "adjust 用：每股派现（10派20元→填2）"},
                    "ma_period": {"type": "integer",
                                  "description": "adjust 用：要估算的均线周期，默认 60"},
                    "total_return_pct": {"type": "number",
                                         "description": "ratio 用：累计收益%"},
                    "max_drawdown_pct": {"type": "number",
                                         "description": "ratio 用：最大回撤%（填负数）"},
                },
                "required": [],
            },
        ),
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
            name="astock_business",
            description=(
                "**主营构成 / 门槛②判定**：直接读东财 F10 分部收入构成，算出"
                "「相关业务收入占营收几成」，用来判定《选股方法论》第②道硬门槛"
                "（相关业务收入占营收 ≥5%）。此前这道门槛因「工具链无分部财报字段」"
                "被登记为永久缺口，导致所有标的都只能写「②无法核实→只观察」，本工具补上。"
                "结论有四档必须分清：**pass** 达标 / **below** 有该业务但占比不足 / "
                "**absent** 分部表里根本没有这块业务（分部披露有重要性门槛，"
                "没有＝远低于5%，这是**否定证据不是数据缺失**）/ "
                "**na** 事件属回购/增持等资本运作，不涉及业务占比，门槛② 本就不适用"
                "（不能据此否决）。另有 **unknown** ＝ 数据取不到（北交所/ETF），"
                "**不等于通过**。报告期按中报/年报披露，不是实时。"
                "查多只请传 codes。"),
            parameters={
                "type": "object",
                "properties": {
                    "code": code_prop,
                    "codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "批量核门槛②（持仓/候选池体检），最多 20 只",
                    },
                    "event": {
                        "type": "string",
                        "description": ("事件原文（如公告标题）。用于识别「哪块业务受益」"
                                        "以及判断是不是回购/增持这类不涉及业务占比的事件。"
                                        "不传则只给分部构成，不做门槛判定"),
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": ("手动指定业务关键词（优先级最高，可传字符串逗号分隔）。"
                                        "自动匹配不准时用它，如 [\"机器人\",\"减速器\"]"),
                    },
                },
                "required": [],
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
                "盯盘与自选股管理。action=list 看自选；add/remove 增删自选；\n"
                "**status 看盯盘状态**（是否在盯、当前时段、下次开始时间、**在盯什么**）；\n"
                "**plan 看盯盘规则**（probe_config 里的价位规则/代码范围/信号类型/级别，"
                "带原始 JSON）；\n"
                "**set_rules 改盯盘规则**（rules 整体替换 | add_rules 追加 | "
                "remove_rules 按代码删除；也可同时给 codes/kinds/min_level/once_per_day，"
                "以及阈值键 stop_loss_pct/take_profit_pct/trailing_pct/"
                "drop_alert_pct/rise_alert_pct/volume_ratio_alert——"
                "**这些阈值键写在计划里会优先生效**，压过设置页的值）；"
                "同一代码同时传 remove_rules 与 add_rules ＝ **替换**（删除旧规则、保留新规则），"
                "返回值会写「已替换」而不是「已写入」；改完建议用 action=plan 回读核对；\n"
                "**start 启动盯盘**（开启定时探测，非交易时段自动跳过不耗 token）；"
                "**stop 暂停盯盘**。\n"
                "盯盘时段：工作日「开始时间」至 15:00，午休 11:30–13:00 跳过；"
                "开始时间由设置项 watch_start_time 决定（HH:MM，留空＝09:10）。\n"
                "**为什么规则要用 set_rules 而不是设置页**：精确价位规则（rules）"
                "只存在于**计划级 probe_config**，设置页 schema 里没有这些键——"
                "这是历史上「模型看不见也改不了盯盘内容」的直接原因。"
                "通用通道是 watch-plugin 的 `schedule_upsert merge_probe_config`。\n"
                "**stop 是持久的**：暂停后插件重载、自愈、重启都不会自动恢复，"
                "只有显式 start（或设置页按钮）才恢复。若用户抱怨「停不下来」，"
                "先查 action=status 的「插件盯盘开关」而非计划 enabled——两者是不同开关。\n"
                "**邮件提醒要同时满足两个阈值**：探测级 min_level（默认 action）决定"
                "信号进不进报告，邮件级 email_min_level（默认 urgent）决定发不发邮件。"
                "severity 低于邮件阈值的信号（价格突破类通常是 action）会被静默拦掉——"
                "用户说「配了收不到邮件」时先查这里；status 会提示「⚠️ 邮件阈值会把"
                "部分信号拦掉」或「🔴 上次探测有信号被邮件阈值拦掉」。\n"
                "**邮件依赖应用在运行**：探针由宿主定时调度，应用退出/休眠期间不发信。"),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "remove", "status", "plan",
                                 "set_rules", "start", "stop"],
                        "description": "默认 list",
                    },
                    "code": code_prop,
                    "name": {"type": "string", "description": "名称（可选）"},
                    "note": {"type": "string", "description": "备注（可选）"},
                    "rules": {
                        "type": "array", "items": {"type": "object"},
                        "description": ("action=set_rules 时整体替换价位规则。每条："
                                        "{code, name?, kind, price, op, label?, "
                                        "severity?, vol_ratio_gt?, vol_ratio_lt?}；"
                                        "op: lt 跌破 / le 触及 / gt 站上 / ge 站上"),
                    },
                    "add_rules": {
                        "type": "array", "items": {"type": "object"},
                        "description": "action=set_rules 时追加（保留现有规则）",
                    },
                    "remove_rules": {
                        "type": "array", "items": {"type": "string"},
                        "description": ("action=set_rules 时按股票代码删除对应规则"
                                        "（只删原有规则；与 add_rules 同代码合用＝替换）"),
                    },
                    "codes": {
                        "type": "string",
                        "description": "action=set_rules 时顺带设「额外盯的代码」（逗号分隔）",
                    },
                    "kinds": {
                        "type": "string",
                        "description": "action=set_rules 时顺带设要盯的信号类型（逗号分隔）",
                    },
                    "min_level": {
                        "type": "string",
                        "enum": ["info", "action", "urgent"],
                        "description": "action=set_rules 时顺带设最低报告级别",
                    },
                    "once_per_day": {
                        "type": "boolean",
                        "description": "action=set_rules 时顺带设价位规则当天只报一次",
                    },
                    # 阈值键（v2.8.3）：探针**确实会读**计划里的这些键
                    # （cfg 优先、设置页兜底）。此前它们既不可写、又被 status
                    # 误报成「未识别键」——修好后必须让模型能改，否则用户想调
                    # 止损线仍只能进 GUI。
                    "stop_loss_pct": {
                        "type": "number",
                        "description": ("止损阈值（%）。浮亏达到该比例报止损。"
                                        "写入计划后**优先于设置页**；"
                                        "比设置页更严格时才推荐写（如临时调到 3 做纪律）"),
                    },
                    "take_profit_pct": {
                        "type": "number", "description": "止盈阈值（%），同上",
                    },
                    "trailing_pct": {
                        "type": "number", "description": "移动止盈回撤阈值（%）",
                    },
                    "drop_alert_pct": {
                        "type": "number", "description": "当日跌幅异动阈值（%）",
                    },
                    "rise_alert_pct": {
                        "type": "number", "description": "当日涨幅异动阈值（%）",
                    },
                    "volume_ratio_alert": {
                        "type": "number", "description": "放量提示的量比阈值",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="astock_extra",
            description=(
                "**扩展数据面**（一次调用覆盖 17 类数据，移植自 a-stock-data 的端网点位）："
                "打板情绪、题材归因、人气热榜、行业排名、板块资金流、ST 名单、"
                "两融、大宗交易、股东户数、分红送转、限售解禁、龙虎榜、板块归属、"
                "筹码分布、事件数据、研究资料、宏观利率。\n"
                "**与已有工具的分工（别选错）**：\n"
                "· 个股资金流 → `astock_flow`；**板块**资金流 → 本工具 action=board_flow；\n"
                "· 个股实时价/K线 → `astock_quote` / `astock_kline`；\n"
                "· 舆情七类证据（公告/研报/新闻/热度/龙虎榜/两融/股东户数）→ "
                "`astock_sentiment`；本工具给的是**同类数据的明细表**（含席位、金额、区间），"
                "`astock_sentiment` 给的是**带可靠度与背离提示的归集**。\n"
                "**各 action 的配套参数**：\n"
                "· limit_sentiment（打板情绪：涨跌停/炸板率/连板梯队/涨停原因）date\n"
                "· strong（同花顺题材归因）date；hot（人气热榜）period=hour/day\n"
                "· industry（行业涨幅榜）top_n；board_flow（板块资金流）"
                "board_type=industry/concept/region + period=today/5d/10d + top_n\n"
                "· st（ST 名单）market/limit；concepts（个股板块归属）code\n"
                "· margin / block_trade / holders / dividend / lockup / lhb / chips：code（必填）\n"
                "· chips 另支持 days（窗口，默认 150）/ decay（换手衰减 1.0~2.0）/ adjust\n"
                "· lhb 不给 code ＝ 全市场龙虎榜（配 date、min_net_wan）\n"
                "· events（事件数据）event=forecast/survey/holder_trade/buyback/pledge/"
                "ipo/convertible，配 code / days / limit / direction / report_date\n"
                "· docs（研究资料）doc=announce(巨潮公告)/report(研报)/finance(财报三表)，"
                "配 code / limit / months / report_type=fzb/lrb/llb\n"
                "· macro（宏观利率）macro=lpr/pmi/yield/repo/calendar，"
                "配 days / curve / kind=FR|FDR / year+month\n"
                "**口径底线**：缺数据一律显示「-」不用 0 代替；不做涨跌预测、不给概率、"
                "不合成情绪分；筹码分布是模型估算（按日 K 与换手率递推），"
                "不是交易所披露的真实持仓。"),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "limit_sentiment", "strong", "hot", "industry",
                            "board_flow", "st", "margin", "block_trade", "holders",
                            "dividend", "lockup", "lhb", "concepts", "chips",
                            "events", "docs", "macro",
                        ],
                        "description": ("要取哪一类数据。"
                                        "打板情绪=limit_sentiment / 题材归因=strong / "
                                        "热榜=hot / 行业排名=industry / 板块资金流=board_flow / "
                                        "ST名单=st / 两融=margin / 大宗=block_trade / "
                                        "股东户数=holders / 分红=dividend / 解禁=lockup / "
                                        "龙虎榜=lhb / 板块归属=concepts / 筹码分布=chips / "
                                        "事件数据=events / 研究资料=docs / 宏观利率=macro"),
                    },
                    "code": code_prop,
                    "date": {
                        "type": "string",
                        "description": ("交易日。limit_sentiment/strong/hot 用 YYYYMMDD 或 "
                                        "YYYY-MM-DD；lhb 用 YYYY-MM-DD。留空＝今天"),
                    },
                    "period": {
                        "type": "string",
                        "description": ("hot 用 hour/day；board_flow 用 "
                                        "today/5d/10d（默认 today）"),
                    },
                    "board_type": {
                        "type": "string",
                        "enum": ["industry", "concept", "region"],
                        "description": "board_flow：行业 / 概念 / 地域（默认 industry）",
                    },
                    "top_n": {"type": "integer", "description": "industry/board_flow 返回条数"},
                    "market": {
                        "type": "string", "enum": ["sh", "sz", "bj"],
                        "description": "st：只看某个市场；留空＝全部",
                    },
                    "limit": {"type": "integer", "description": "返回条数上限"},
                    "days": {
                        "type": "integer",
                        "description": ("margin/lockup/events/macro 的时间窗口（天）；"
                                        "chips 的 K 线窗口（默认 150，最大 640）"),
                    },
                    "decay": {
                        "type": "number",
                        "description": "chips：换手衰减系数，1.0＝真实换手，1.5~2.0＝加快历史筹码消散",
                    },
                    "adjust": {
                        "type": "string", "enum": ["qfq", "hfq", "none"],
                        "description": "chips：K 线复权口径（默认 qfq，筹码成本必须复权）",
                    },
                    "min_net_wan": {
                        "type": "number",
                        "description": "lhb 全市场榜：净买入下限（万元）",
                    },
                    "event": {
                        "type": "string",
                        "enum": ["forecast", "survey", "holder_trade", "buyback",
                                 "pledge", "ipo", "convertible"],
                        "description": ("events：业绩预告 / 机构调研 / 股东增减持 / "
                                        "回购 / 质押 / 新股 / 可转债"),
                    },
                    "direction": {
                        "type": "string", "enum": ["增持", "减持"],
                        "description": "events 的 holder_trade：只看增持或减持",
                    },
                    "report_date": {
                        "type": "string",
                        "description": "events 的 forecast：报告期，如 2026-09-30",
                    },
                    "include_delisted": {
                        "type": "boolean",
                        "description": "events 的 convertible：是否包含已摘牌转债",
                    },
                    "doc": {
                        "type": "string",
                        "enum": ["announce", "report", "finance"],
                        "description": "docs：巨潮公告 / 个股研报 / 财报三表",
                    },
                    "report_type": {
                        "type": "string", "enum": ["fzb", "lrb", "llb"],
                        "description": "docs 的 finance：资产负债表 / 利润表 / 现金流量表",
                    },
                    "months": {
                        "type": "integer",
                        "description": "docs 的 report：研报回溯月数（默认 12）",
                    },
                    "macro": {
                        "type": "string",
                        "enum": ["lpr", "pmi", "yield", "repo", "calendar"],
                        "description": ("macro：LPR / PMI / 中债收益率 / 回购定盘利率 / "
                                        "交易日历"),
                    },
                    "curve": {
                        "type": "string",
                        "enum": ["treasury", "bank_aaa", "mtn_aaa", "all"],
                        "description": "macro 的 yield：国债 / 商业银行 AAA / 中短票 AAA",
                    },
                    "kind": {
                        "type": "string", "enum": ["FR", "FDR"],
                        "description": "macro 的 repo：全市场 FR / 银银间 FDR",
                    },
                    "year": {"type": "integer", "description": "macro 的 calendar：年"},
                    "month": {"type": "integer", "description": "macro 的 calendar：月"},
                },
                "required": ["action"],
            },
        ),
    ]
