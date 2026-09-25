"""印后抽检清单：基于完整拼版按规格均匀抽取已排入成品。

服务端收到抽验名额后先用与预览 / 导出完全相同的 :func:`compute_layout`
重算完整拼版（拼版参数变化即按新结果重算，**不沿用旧位置**），再按
如下规则在**已排入**的标签中取样——首张纸占用区本就不是排入的标签、
未排入份数本就不在拼版结果中，二者天然不进入候选：

1. **页间轮流分配名额**：对每种规格，按页码 1、2、…… 循环遍历含该规格
   标签的页面，每轮每页最多分到 1 个名额，直到该规格名额用完；
2. **页内均匀取样**：某页该规格最终获配 q 张、页内共有 N 个该规格候选
   （按页内从上到下、同行从左到右的阅读顺序排列）时，取第
   ``floor((j + 0.5) N / q) + 1`` 张（j = 0 … q-1，结果为 1 基序号）。

抽样数可填 0；超过该规格实际排入数时抛出明确中文错误。
"""

from __future__ import annotations

import math

from .layout import _reading_order, compute_layout
from .models import (
    InspectionItem,
    InspectionRequest,
    InspectionResponse,
    InspectionSpecSummary,
    LayoutResponse,
)


def _spec_candidates(plan: LayoutResponse) -> list[list[tuple[int, int, object]]]:
    """按规格汇总每页的阅读顺序候选。

    返回 ``by_type[i][page_index] = [(页内阅读序号0基, 标签对象), ...]``，
    只包含实际排入拼版的标签；页按页码升序、页内按阅读顺序排列。
    """
    by_type: list[list[list[tuple[int, int, object]]]] = [
        [[] for _ in plan.pages] for _ in plan.summaries
    ]
    for pg in plan.pages:
        for read_idx, lab in enumerate(_reading_order(pg.labels)):
            by_type[lab.type_index][pg.page - 1].append((read_idx, lab))
    return by_type


def _allocate_pages(
    quota: int, per_page: list[int]
) -> list[int]:
    """页间轮流分配：每轮按页码顺序给仍有候选的页面各分 1 个名额。

    per_page[p] 为第 p 页该规格候选数；返回每页获配名额（每轮每页至多 1）。
    quota 必须不超过候选总数（调用前已校验）。
    """
    allocated = [0] * len(per_page)
    remaining = quota
    while remaining > 0:
        progressed = False
        for p, n in enumerate(per_page):
            if remaining <= 0:
                break
            # 已分到的名额不能超过该页候选数；每页每轮最多 +1
            if allocated[p] < n:
                allocated[p] += 1
                remaining -= 1
                progressed = True
        if not progressed:  # 理论上不会发生（quota ≤ 候选总数），防御性保护
            break
    return allocated


def build_inspection(
    plan: LayoutResponse, samples: list[int]
) -> InspectionResponse:
    """在已计算的完整拼版上生成印后抽检清单。

    samples 与拼版规格一一对应；长度不符、含负数或超过实际排入数时
    抛出 ValueError（接口层转为 HTTP 400 中文错误）。
    """
    if len(samples) != len(plan.summaries):
        raise ValueError(
            f"抽检名额数量（{len(samples)}）与标签规格数量"
            f"（{len(plan.summaries)}）不一致，请重新生成拼版后再填写。"
        )

    by_type = _spec_candidates(plan)
    # (页码, 页内阅读序号0基, 标签) 按规格暂存，最后统一排序输出
    picked: list[tuple[int, int, object]] = []
    summaries: list[InspectionSpecSummary] = []

    for i, summary in enumerate(plan.summaries):
        quota = samples[i]
        if not isinstance(quota, int) or quota < 0:
            raise ValueError(
                f"规格「{summary.name or i + 1}」的抽验张数必须是不小于 0 的整数。"
            )
        pages = by_type[i]
        per_page = [len(cands) for cands in pages]
        placed = sum(per_page)
        if quota > placed:
            raise ValueError(
                f"规格「{summary.name or i + 1}」抽验 {quota} 张，"
                f"超过该规格实际排入数 {placed} 张（需求 {summary.requested} 张，"
                f"未排入 {summary.requested - placed} 张不进入候选）；"
                "请减小抽验张数或调整拼版参数后重新生成。"
            )

        if quota > 0:
            alloc = _allocate_pages(quota, per_page)
            for p, q in enumerate(alloc):
                if q <= 0:
                    continue
                cands = pages[p]
                n = len(cands)
                # 页内均匀取样：第 floor((j+0.5)N/q)+1 张（1 基）
                chosen = {
                    min(n - 1, math.floor((j + 0.5) * n / q + 1e-9))
                    for j in range(q)
                }
                for k in sorted(chosen):
                    read_idx, lab = cands[k]
                    picked.append((p + 1, read_idx, lab))

        summaries.append(
            InspectionSpecSummary(
                type_index=i,
                name=summary.name,
                requested=summary.requested,
                placed=placed,
                sampled=quota,
                pages_used=sum(1 for n in per_page if n > 0),
            )
        )

    # 清单按页码升序、页内阅读顺序排列（与预览高亮、印刷走纸顺序一致）
    picked.sort(key=lambda t: (t[0], t[1]))
    items = [
        InspectionItem(
            page=page_no,
            page_index=read_idx + 1,
            type_index=lab.type_index,
            name=lab.name,
            width=lab.width,
            height=lab.height,
            rotated=lab.rotated,
            x=lab.x,
            y=lab.y,
            serial=lab.serial,
        )
        for page_no, read_idx, lab in picked
    ]
    return InspectionResponse(
        items=items,
        summaries=summaries,
        total_samples=len(items),
        total_pages=len(plan.pages),
        page_width=plan.page_width,
        page_height=plan.page_height,
    )


def build_inspection_from_request(req: InspectionRequest) -> InspectionResponse:
    """重算完整拼版后生成抽检清单（服务端唯一入口，杜绝沿用旧位置）。"""
    plan = compute_layout(req.params)
    return build_inspection(plan, req.samples)


# CSV 列头（中英文均保留，便于印后工序直接使用）
CSV_HEADER = [
    "页码",
    "页内序号",
    "规格名称",
    "规格宽(mm)",
    "规格高(mm)",
    "旋转90°",
    "左上角X(mm)",
    "左上角Y(mm)",
    "已有流水号",
]


def build_inspection_csv(resp: InspectionResponse) -> str:
    """把抽检清单渲染为带 UTF-8 BOM 的 CSV（Excel 直接打开不乱码）。"""
    def mm(v: float) -> str:
        """毫米数输出：整数不带小数点，其余保留 2 位（与 SVG / 界面一致）。"""
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        return f"{v:.2f}".rstrip("0").rstrip(".")

    def cell(v) -> str:
        s = "" if v is None else str(v)
        if any(ch in s for ch in (",", '"', "\n", "\r")):
            s = '"' + s.replace('"', '""') + '"'
        return s

    lines = [",".join(CSV_HEADER)]
    for it in resp.items:
        lines.append(
            ",".join(
                cell(v)
                for v in (
                    it.page,
                    it.page_index,
                    it.name or f"#{it.type_index + 1}",
                    mm(it.width),
                    mm(it.height),
                    "是" if it.rotated else "否",
                    mm(it.x),
                    mm(it.y),
                    it.serial if it.serial is not None else "",
                )
            )
        )
    # BOM 让 Excel 识别 UTF-8；换行使用 CRLF 以兼容表格软件
    return "\ufeff" + "\r\n".join(lines) + "\r\n"