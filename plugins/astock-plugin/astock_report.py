"""组合舆情报告（盘后自动 + 对话手动共用一份实现）及其两个联动。

────────────────────────────────────────────────────────────────────────
为什么单独成文件
────────────────────────────────────────────────────────────────────────
这是一段**有明确输入输出的流程**（台账 → 七类证据 → 报告文本），与插件
类本身无关。原先它和面板渲染、30 多个工具处理函数挤在 `plugin.py` 里。

**拆出来才发现的一个循环依赖**：原实现里有 `_pl = AstockPlugin()` 直接
new 一个插件实例来复用 `_company_in_text()`——这在单文件里能跑，但拿到
独立模块就变成 `astock_report → plugin → astock_report` 的环。

修法不是"把 import 延后"（那只是把环藏起来），而是**让调用方注入**：
`build_report(ledger, company_resolver=...)`。谁有解析能力谁传进来，
本模块不关心它怎么实现。

────────────────────────────────────────────────────────────────────────
两条产品规矩（改之前先读）
────────────────────────────────────────────────────────────────────────
① **只推荐、不自动改池**：事件推荐说「加自选」才加，健康检查说「移除」
   才删。自动改会把"用户的观察池"变成"机器的池"。
② **不合成情绪分**：只列证据与背离，不给 0-100 的分数——那会掩盖这些
   信号可靠度差几个数量级的事实。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re as _re
from typing import Any, Awaitable, Callable, Dict, List, Optional

import astock_data as data
import astock_market as mkt
import astock_sentiment as senti
import astock_sentiment_data as senti_data

logger = logging.getLogger("litework.astock.report")

#: 事件类型 → 关键词（公司硬事件；宏观/行业消息不关联个股、不进推荐位）
EVENT_KINDS: Dict[str, tuple] = {
    "回购": ("回购",),
    "增持": ("增持",),
    "中标": ("中标", "签约", "订单", "合同"),
    "预增": ("预增", "业绩增长", "净利润增长", "扭亏"),
    "重组": ("重组", "并购", "收购", "资产注入"),
    "获批": ("获批", "批准", "通过审批", "取得批文"),
    "分红": ("分红", "派息", "权益分派"),
    "利空": ("减持", "亏损", "处罚", "立案", "问询", "退市", "商誉减值"),
}

#: 报告最多覆盖多少只（更多无意义且请求太重）
MAX_COVER = 10


async def build_report(ledger: Any, manual: bool = False, *,
                       company_resolver: Optional[
                           Callable[[str], Awaitable[Optional[Dict[str, str]]]]
                       ] = None,
                       enable_event_reco: bool = True,
                       enable_health_check: bool = True) -> str:
    """持仓+自选的全量舆情报告。

    - 每只标的：七类证据收集 → tone 判定 → 首要背离；
    - 组合层：背离清单 + 同向清单 + 操作纪律提示；
    - 名称以台账为准（舆情接口按代码查询常不给名称）；
    - 覆盖 ≤MAX_COVER 只。

    `company_resolver` 用于事件推荐里"从新闻文本认出公司"。不传则跳过
    事件推荐（而不是像原实现那样 new 一个插件实例制造循环依赖）。
    """
    name_by_code: Dict[str, str] = {}
    codes: List[str] = []
    for p in ledger.list_positions():
        codes.append(p["code"])
        name_by_code[p["code"]] = p.get("name") or ""
    for w in ledger.list_watch():
        if w["code"] not in codes:
            codes.append(w["code"])
        name_by_code.setdefault(w["code"], w.get("name") or "")
    if not codes:
        return "台账里没有持仓或自选，先录入（astock_record / astock_watch）。"

    today = dt.date.today().isoformat()
    parts: List[str] = [
        f"## 📊 {'组合舆情报告' if manual else '盘后舆情报告'}（{today}）",
        "",
        f"覆盖 {len(codes)} 只标的（持仓+自选）。数据来源七类证据，"
        f"**只列事实与背离，不预测涨跌**。",
        "",
    ]
    aligned: List[str] = []
    conflicted: List[str] = []
    for code in codes[:MAX_COVER]:
        try:
            collected = await asyncio.to_thread(
                senti_data.collect, code, "")   # scope 留空=默认全量七类
            rep = await asyncio.to_thread(
                senti.summarize, collected["items"], code, "",
                collected["errors"], collected["extras"])
        except Exception:
            continue
        name = name_by_code.get(code) or next(
            (getattr(x, "name", "") for x in collected.get("items", [])
             if getattr(x, "name", "")), code)
        tone_mark = {"aligned_positive": "🟢", "conflicted": "🟡",
                     "aligned_negative": "🔴", "thin": "⚪"}.get(rep.tone, "⚪")
        parts.append(f"- {tone_mark} **{name}（{code}）**：{rep.tone_text or rep.tone}")
        if rep.conflicts:
            conflicted.append(f"{name}（{code}）")
            for c in rep.conflicts[:1]:
                parts.append(f"  - ⚠️ 背离：{c.title}")
        elif rep.tone == "aligned_positive":
            aligned.append(f"{name}（{code}）")
    parts.append("")
    if conflicted:
        parts.append(f"**⚠️ 背离标的（{len(conflicted)} 只）**：{'、'.join(conflicted)}"
                     f" —— 按「以客观数据为准」原则，融资余额止跌回升前不宜据研报加仓。")
    if aligned:
        parts.append(f"**✅ 同向偏正（{len(aligned)} 只）**：{'、'.join(aligned)}")
    if not conflicted and not aligned:
        parts.append("今日证据量普遍偏薄（thin），无明确方向。")

    # ── 联动①：事件推荐区（快讯硬事件 × 资金同向，且不在池）——只推荐不自动加：
    #    自动改自选会让「观察池」变成机器的池，用户说「加」才加。
    if enable_event_reco and company_resolver is not None:
        try:
            ev_recos = await event_recommendations(
                set(codes), company_resolver=company_resolver)
        except Exception:
            ev_recos = []
        if ev_recos:
            parts += ["", f"**📌 事件推荐（{len(ev_recos)} 只，未在池）**"
                          f"——说「加自选」即入池："]
            for r in ev_recos:
                parts.append(f"- {r['mark']} **{r['name']}（{r['code']}）**"
                             f"｜{r['kind']}：{r['text']}（{r['flow']}）")

    # ── 联动②：自选健康检查——按池内 note 的既定约定标「建议移除」，只建议不删。
    if enable_health_check:
        try:
            drops = await watch_health_check(ledger)
        except Exception:
            drops = []
        if drops:
            parts += ["", f"**🧹 建议移除（{len(drops)} 只）**——说「移除」即删："]
            for d in drops:
                parts.append(f"- {d}")
    return "\n".join(parts)


async def event_recommendations(existing_codes: set, *,
                                company_resolver: Callable[
                                    [str], Awaitable[Optional[Dict[str, str]]]],
                                max_items: int = 3) -> List[Dict[str, Any]]:
    """盘后报告联动①：快讯硬事件 → 推荐候选（排除在池/持仓；资金须同向）。

    **资金必须印证**：只有事件没有资金流入的，不进推荐位——消息面最容易
    "看起来很好但没人买"。这不是预测涨跌，只是要求两条独立证据同向。
    """
    feed = await asyncio.to_thread(senti_data.fetch_feed, 40, "")
    recos: List[Dict[str, Any]] = []
    seen: set = set()
    for ev in feed:
        text = (ev.summary or ev.title or "")
        for k_name, k_words in EVENT_KINDS.items():
            if k_name == "利空":
                continue
            if not any(w in text for w in k_words):
                continue
            company = await company_resolver(text)
            if not company or company["code"] in existing_codes | seen:
                break
            seen.add(company["code"])
            try:
                flows = await asyncio.to_thread(
                    data.fetch_money_flow, company["code"], 5)
                net5 = (sum(float(f.get("main_net") or 0) for f in flows)
                        if flows else None)
                await asyncio.sleep(0.4)
            except Exception:
                net5 = None
            if net5 is None or net5 <= 0:
                break          # 资金未印证/背离 → 不进推荐位
            recos.append({
                "kind": k_name, "code": company["code"], "name": company["name"],
                "text": ev.title or text[:60],
                "flow": f"5日主力净流入 {net5 / 1e8:+.2f} 亿", "mark": "✅"})
            break
        if len(recos) >= max_items:
            break
    return recos


async def watch_health_check(ledger: Any) -> List[str]:
    """盘后报告联动②：自选淘汰建议（只建议，不删）。

    规则全部基于入池时写进 note 的约定——机器只执行**用户自己定的纪律**：
    1. 买点失效：note 有「回踩XX.X」而现价高于买点 8% 以上（追高成本
       过高，原观察逻辑不成立）；
    2. 背离持续：note 注明「背离」的候选，舆情仍 conflicted → 提醒
       （不判死刑：背离消除日可能正是买点，交给用户）。
    """
    drops: List[str] = []
    held = {p["code"] for p in ledger.list_positions()}
    # 现价：批量拉一次自选行情（不逐只打接口）
    watched = ledger.list_watch()
    codes = [w["code"] for w in watched if w["code"] not in held]
    quotes: Dict[str, Dict[str, Any]] = {}
    if codes:
        try:
            items = []
            for c in codes:
                try:
                    cc, vv = mkt.normalize_code(c)
                    items.append((cc, vv, "stock"))
                except ValueError:
                    continue
            for q in await asyncio.to_thread(data.fetch_quotes, items):
                quotes[q.code] = q.as_dict()
        except Exception:
            pass
    for w in watched:
        code = w["code"]
        if code in held:
            continue
        note = w.get("note") or ""
        q = quotes.get(code) or {}
        price = q.get("price")
        # 规则1：买点失效
        m = _re.search(r"回踩(\d+(?:\.\d+)?)", note)
        if m and price and price > float(m.group(1)) * 1.08:
            drops.append(f"{w.get('name') or code}（{code}）：现价 {price} 已偏离"
                         f"买点 {m.group(1)} 超过 8%，观察前提失效")
            continue
        # 规则2：背离持续（只对 note 提到背离的查，控制请求量）
        if "背离" in note:
            try:
                collected = await asyncio.to_thread(senti_data.collect, code, "")
                rep = await asyncio.to_thread(
                    senti.summarize, collected["items"], code, "",
                    collected["errors"], collected["extras"])
                if rep.tone == "conflicted":
                    drops.append(f"{w.get('name') or code}（{code}）：舆情背离持续"
                                 f"（等「背离消除」条件触发，或移除换标的）")
            except Exception:
                pass
    return drops[:5]
