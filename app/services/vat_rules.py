"""染缸状态与拼缸出库共用的业务规则（全系统唯一口径）。

- 电位门槛：最新一笔浸染批次 redoxMv 已填且 <= READY_REDOX_MAX（-500 mV）。
- 可染色：缸状态为 ready 且最新读数过门槛。
  还原台改状态复核、拼缸专页可选缸、还原台可染色去重计数、出库逐缸确认，
  全部走本模块的同一组函数，不得各写一份。
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DipLot, Vat

# 可染色电位上限（mV）：最新读数须 <= 该值。
READY_REDOX_MAX = Decimal("-500")


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def latest_lot_of(vat: Vat) -> Optional[DipLot]:
    """某缸最新一笔浸染（与入库排序口径一致：时间倒序，平局取 id 大者）。"""
    if not vat.lots:
        return None
    return sorted(vat.lots, key=lambda x: (x.dippedAt, x.id), reverse=True)[0]


def passes_redox_gate(latest: Optional[DipLot]) -> bool:
    """最新读数是否过电位门槛：已填且 <= -500 mV。"""
    return (
        latest is not None
        and latest.redoxMv is not None
        and Decimal(latest.redoxMv) <= READY_REDOX_MAX
    )


def is_dyeable(vat: Vat, latest: Optional[DipLot] = None) -> bool:
    """可染色判定的唯一 Python 口径：ready + 最新读数过门槛。"""
    if latest is None:
        latest = latest_lot_of(vat)
    return vat.status == Vat.STATUS_READY and passes_redox_gate(latest)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if not passes_redox_gate(latest):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_vat_status_change(vat: Vat, new_status: str, latest: Optional[DipLot]) -> None:
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)


# ---------------------------------------------------------------------------
# SQL 口径：专页可选缸、还原台可染色去重计数、出库复核共用。
# DISTINCT ON 取每缸最新一笔，口径与 latest_lot_of 完全一致。
# ---------------------------------------------------------------------------

def latest_lot_sq():
    return (
        select(
            DipLot.vat_id.label("vat_id"),
            DipLot.id.label("lot_id"),
            DipLot.clothMeters.label("cloth_meters"),
            DipLot.redoxMv.label("redox_mv"),
        )
        .distinct(DipLot.vat_id)
        .order_by(DipLot.vat_id, DipLot.dippedAt.desc(), DipLot.id.desc())
        .subquery()
    )


def dyeable_vat_stmt(workshop_id: Optional[int] = None):
    """可染色缸查询：状态 ready 且最新一笔浸染电位 <= -500（每缸只出现一次）。"""
    sq = latest_lot_sq()
    stmt = (
        select(Vat, sq.c.cloth_meters, sq.c.redox_mv, sq.c.lot_id)
        .join(sq, sq.c.vat_id == Vat.id)
        .where(
            Vat.status == Vat.STATUS_READY,
            sq.c.redox_mv.isnot(None),
            sq.c.redox_mv <= READY_REDOX_MAX,
        )
    )
    if workshop_id is not None:
        stmt = stmt.where(Vat.workshop_id == workshop_id)
    return stmt.order_by(Vat.code)


def dyeable_vats(db: Session, workshop_id: Optional[int] = None) -> list[dict]:
    """专页挂缸选择与还原台去重计数共用的明细结果。"""
    rows = db.execute(dyeable_vat_stmt(workshop_id)).all()
    out = []
    for vat, meters, redox, lot_id in rows:
        out.append(
            {
                "id": vat.id,
                "code": vat.code,
                "workshopId": vat.workshop_id,
                "workshopName": vat.workshop.name,
                "dyeType": vat.dyeType,
                "latestLotId": lot_id,
                "latestMeters": Decimal(meters),
                "latestRedox": Decimal(redox),
            }
        )
    return out


def dyeable_counts_by_workshop(db: Session) -> dict[int, int]:
    """还原台「可染色缸去重数」：每个工坊几口可染色缸，口径同专页可选缸。"""
    counts: dict[int, int] = {}
    for row in dyeable_vats(db):
        counts[row["workshopId"]] = counts.get(row["workshopId"], 0) + 1
    return counts
