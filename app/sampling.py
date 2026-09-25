"""印后抽检清单：基于完整拼版结果为每种规格抽取成品样本。

抽样规则（服务端按当前拼版参数重新计算完整拼版后执行）：

1. 名额分配：对每种规格，按页码轮流给含该规格标签的页面分配抽检名额，
   每轮每页最多一张；某页名额达到其页内候选数后跳过后续轮次，
   直到该规格名额用完（用户填写的张数不超过实际排入数，故必然分完）。
2. 页内取样：获配 q 张的页面有 N 张候选时，候选按页内从上到下、
   同行从左到右排序（与流水号同一阅读顺序），取第
   floor((j+0.5)*N/q)+1 张（j 从 0 到 q-1）——在页内均匀分布且互不重复。
3. 候选范围：只有完整拼版结果中实际排入的标签才是候选。首张纸占用区
   不是标签、未排入份数不在拼版结果中，两者都不会进入候选。

清单按页码、页内位置排序，列出页码、页内序号、规格、坐标与已有流水号；
同一请求可导出 CSV（UTF-8 带 BOM，Excel 可直接打开）。拼版参数变化后
由前端重新发起请求，服务端总是基于最新拼版结果计算，不沿用旧位置。
"""

from __future__ import annotations

import csv
import io
import math

from .layout import EPS, compute_layout
from .models import (
    LayoutRequest,
    PlacedLabel,
    SamplingItem,
    SamplingResponse,
    SamplingSpecCount,
)


def _reading_order_pairs(
    pairs: list[tuple[int, PlacedLabel]],
) -> list[tuple[int, PlacedLabel]]:
    """把 [(页内 0 基序号, PlacedLabel)] 按页内阅读顺序排序。

    与 layout._reading_order 同一规则：从上到下成行（顶边 y 相同为一行）、
    同行从左到右；这里同时保留每个标签在 pages[].labels 中的原始序号，
    供清单的「页内序号」列使用。
    """
    rows: list[list[tuple[int, PlacedLabel]]] = []
    for pair in sorted(pairs, key=lambda p: (p[1].y, p[1].x)):
        if rows and abs(rows[-1][-1][1].y - pair[1].y) <= EPS:
            rows[-1].append(pair)
        else:
            rows.append([pair])
    out: list[tuple[int, PlacedLabel]] = []
    for row in rows:
        out.extend(sorted(row, key=lambda p: p[1].x))
    return out


def build_sampling_list(
    params: LayoutRequest, samples: list[SamplingSpecCount]
) -> SamplingResponse:
    """按当前拼版参数重算完整拼版，并为每种规格抽取抽检样本。

    校验失败（规格序号越界、重复填写、张数为负或超过实际排入数）抛出
    中文 ValueError，由接口层转为 HTTP 400。
    """
    # 基本校验（不依赖拼版结果）：序号范围、重复填写、负数
    seen: set[int] = set()
    for sc in samples:
        i = sc.type_index
        if i < 0 or i >= len(params.labels):
            raise ValueError(
                f"抽检设置中的规格序号 {i + 1} 超出范围："
                f"本次共 {len(params.labels)} 种标签规格。"
            )
        name = params.labels[i].name or f"#{i + 1}"
        if i in seen:
            raise ValueError(
                f"规格「{name}」的抽检张数重复填写：每种规格只能填写一次。"
            )
        seen.add(i)
        if sc.count < 0:
            raise ValueError(
                f"规格「{name}」的抽检张数不能为负数（可填 0 表示不抽检）。"
            )

    # 服务端基于完整拼版结果抽样，不信任前端传来的位置 / 数量
    plan = compute_layout(params)

    items: list[SamplingItem] = []
    for sc in samples:
        i = sc.type_index
        c = sc.count
        if c == 0:
            continue
        name = params.labels[i].name or f"#{i + 1}"
        placed = plan.summaries[i].placed
        if c > placed:
            raise ValueError(
                f"规格「{name}」抽检张数 {c} 超过该规格实际排入数 {placed}："
                f"抽检张数可填 0，但不得超过实际排入的成品张数（0–{placed}）。"
            )

        # 每页候选：该规格在本页实际排入的标签（页内阅读顺序），
        # 记录其在 pages[].labels 中的 0 基序号作为页内序号来源
        page_candidates: list[tuple[int, list[tuple[int, PlacedLabel]]]] = []
        for pg in plan.pages:
            pairs = [
                (idx, lab)
                for idx, lab in enumerate(pg.labels)
                if lab.type_index == i
            ]
            if pairs:
                page_candidates.append((pg.page, _reading_order_pairs(pairs)))

        # 按页码轮流分配名额：每轮每页最多一张；页内候选取满的页跳过
        quotas = [0] * len(page_candidates)
        remaining = c
        while remaining > 0:
            for k, (_, cand) in enumerate(page_candidates):
                if remaining <= 0:
                    break
                if quotas[k] < len(cand):
                    quotas[k] += 1
                    remaining -= 1

        # 页内均匀取样：N 张候选取 q 张，第 floor((j+0.5)*N/q)+1 张
        for (page_no, cand), q in zip(page_candidates, quotas):
            n = len(cand)
            for j in range(q):
                pick = math.floor((j + 0.5) * n / q)  # 0 基序号，即第 …+1 张
                idx, lab = cand[pick]
                items.append(
                    SamplingItem(
                        page=page_no,
                        index=idx + 1,
                        type_index=i,
                        name=lab.name,
                        x=lab.x,
                        y=lab.y,
                        width=lab.width,
                        height=lab.height,
                        serial=lab.serial,
                    )
                )

    # 清单按页码、页内从上到下再从左到右排序，便于按页抽检
    items.sort(key=lambda it: (it.page, it.y, it.x))
    return SamplingResponse(items=items, total=len(items))


def _fmt_num(v: float) -> str:
    """CSV 中的毫米数：整数不带小数点，其余保留 2 位（与 SVG 输出一致）。"""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def build_sampling_csv(result: SamplingResponse) -> str:
    """把抽检清单导出为 CSV 文本（UTF-8 带 BOM，Excel 直接打开不乱码）。"""
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM：让 Excel 按 UTF-8 识别中文表头
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(
        ["序号", "页码", "页内序号", "规格", "X (mm)", "Y (mm)",
         "宽 (mm)", "高 (mm)", "流水号"]
    )
    for k, it in enumerate(result.items, 1):
        writer.writerow(
            [
                k,
                it.page,
                it.index,
                it.name or f"#{it.type_index + 1}",
                _fmt_num(it.x),
                _fmt_num(it.y),
                _fmt_num(it.width),
                _fmt_num(it.height),
                it.serial or "",
            ]
        )
    return buf.getvalue()
