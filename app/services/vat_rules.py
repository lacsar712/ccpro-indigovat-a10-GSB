"""染缸状态业务规则。

可染色口径在本模块内只有一处：改缸状态、拼缸出库复核、专页可选缸、
还原台可染色去重计数全部走同一组函数，不允许各自另写判定。
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.models import DipLot, Vat

# 最新浸染批次氧化还原电位门槛：必须已填且 <= -500 mV
REDOX_GATE_MV = Decimal("-500")


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def latest_reading_passes(latest: Optional[DipLot]) -> bool:
    """最新浸染批次是否跨过电位门槛（已填 redoxMv 且 <= -500）。"""
    return (
        latest is not None
        and latest.redoxMv is not None
        and Decimal(latest.redoxMv) <= REDOX_GATE_MV
    )


def vat_is_dyeable(vat: Vat, latest: Optional[DipLot] = None) -> bool:
    """可染色：缸状态为 ready，且最新读数仍过电位门槛。

    出库复核、专页可选缸、还原台去重计数统一调用本函数。
    """
    if vat.status != Vat.STATUS_READY:
        return False
    if latest is None:
        latest = vat.latest_lot()
    return latest_reading_passes(latest)


def assert_vat_dyeable(vat: Vat, latest: Optional[DipLot] = None) -> None:
    """逐缸复核：状态须仍为可染色，且最新读数仍过门槛，否则整单拒绝。"""
    if latest is None:
        latest = vat.latest_lot()
    if vat.status != Vat.STATUS_READY:
        raise VatRuleError(f"染缸 {vat.code} 当前不是可染色状态，整单拒绝出库。")
    if latest is None:
        raise VatRuleError(f"染缸 {vat.code} 没有浸染记录，整单拒绝出库。")
    if latest.redoxMv is None:
        raise VatRuleError(f"染缸 {vat.code} 最新浸染批次未填电位，整单拒绝出库。")
    if Decimal(latest.redoxMv) > REDOX_GATE_MV:
        raise VatRuleError(
            f"染缸 {vat.code} 最新电位 {latest.redoxMv} mV 高于 "
            f"{REDOX_GATE_MV} mV 门槛，整单拒绝出库。"
        )


def query_dyeable_vats(db: Session, workshop_id: Optional[int] = None) -> list[Vat]:
    """与还原台同口径的可染色缸列表（已按缸去重）。

    可选缸、还原台计数、出库前展示都以此为准。
    """
    q = db.query(Vat).options(joinedload(Vat.workshop), joinedload(Vat.lots))
    if workshop_id is not None:
        q = q.filter(Vat.workshop_id == workshop_id)
    return [v for v in q.order_by(Vat.code).all() if vat_is_dyeable(v)]


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if not latest_reading_passes(latest):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_vat_status_change(vat: Vat, new_status: str, latest: Optional[DipLot]) -> None:
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
