"""浸染拼缸出库单：建/改校验与主管整单出库。

出库动作与还原台「改状态为可染色」走同一个电位门槛
（app.services.vat_rules），任一缸不达标则整单拒绝、绝不半出库。
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import OutboundItem, OutboundOrder, Vat, Workshop
from app.services.vat_rules import (
    VatRuleError,
    dyeable_vats,
    is_dyeable,
    latest_lot_of,
)


class OutboundConflict(Exception):
    """并发冲突：单子已被另一人出库。"""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    except Exception:
        raise VatRuleError(f"{field}不是有效数字。")


def _normalise_vat_ids(raw: Iterable[str]) -> list[int]:
    ids: list[int] = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    if not ids:
        raise VatRuleError("至少挂一口染缸。")
    if len(set(ids)) != len(ids):
        raise VatRuleError("同一缸在一张出库单中不能重复。")
    return ids


def validate_and_prepare(
    db: Session,
    workshop_id: int,
    code: str,
    planned_raw: str,
    vat_id_raw: Iterable[str],
    exclude_order_id: Optional[int] = None,
) -> tuple[Workshop, str, Decimal, list[dict]]:
    """新建/更新单头共用的校验：同坊出库号唯一、计划米数、同坊可染色缸、米数上限。"""
    code = code.strip()
    if not code:
        raise VatRuleError("出库号不能为空。")

    workshop = db.get(Workshop, workshop_id)
    if workshop is None:
        raise VatRuleError("工坊不存在。")

    # 同坊出库号唯一（新建与更新同一口径）。
    dup_stmt = select(OutboundOrder).where(
        OutboundOrder.workshop_id == workshop_id, OutboundOrder.code == code
    )
    if exclude_order_id is not None:
        dup_stmt = dup_stmt.where(OutboundOrder.id != exclude_order_id)
    if db.execute(dup_stmt).scalars().first() is not None:
        raise VatRuleError(f"本工坊已存在出库号 {code}，同坊出库号必须唯一。")

    planned = _parse_decimal(planned_raw, "计划米数")
    if planned <= 0:
        raise VatRuleError("计划出库米数必须为正数。")

    vat_ids = _normalise_vat_ids(vat_id_raw)

    # 先按 id 顺序锁住候选缸行，再读可染色口径：
    # 与并发的改状态 / 登记浸染 / 其它出库单串行（出库也按同序加缸锁，避免死锁）。
    locked = (
        db.execute(
            select(Vat)
            .where(Vat.id.in_(vat_ids))
            .order_by(Vat.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if len(locked) != len(set(vat_ids)):
        raise VatRuleError("存在已不存在的染缸。")

    # 可选缸与还原台去重、出库复核同一口径：只能挂本坊当前可染色缸。
    choices = {v["id"]: v for v in dyeable_vats(db, workshop_id)}
    picked: list[dict] = []
    for vid in vat_ids:
        if vid not in choices:
            vat = db.get(Vat, vid)
            label = vat.code if vat else f"#{vid}"
            raise VatRuleError(
                f"染缸 {label} 不是本坊当前可染色缸（需状态可染色且最新电位 ≤ -500 mV），不能挂入。"
            )
        picked.append(choices[vid])

    # 上限 = 明细各缸「最新一笔浸染布米」之和。
    cap = sum((v["latestMeters"] for v in picked), Decimal("0.00"))
    if planned > cap:
        raise VatRuleError(
            f"计划出库米数 {planned} m 超过明细上限：各缸最新浸染布米合计 {cap} m。"
        )

    return workshop, code, planned, picked


def create_order(
    db: Session,
    workshop_id: int,
    code: str,
    planned_raw: str,
    vat_id_raw: Iterable[str],
    user_id: int,
) -> OutboundOrder:
    workshop, code, planned, picked = validate_and_prepare(
        db, workshop_id, code, planned_raw, vat_id_raw
    )
    order = OutboundOrder(
        workshop_id=workshop.id,
        code=code,
        plannedMeters=planned,
        status=OutboundOrder.STATUS_GROUPING,
        created_by=user_id,
    )
    order.items = [OutboundItem(vat_id=v["id"], open_flag=True) for v in picked]
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # 多半是同缸已挂在另一张未出库单（部分唯一索引兜底）。
        raise VatRuleError("其中某口缸已挂在另一张未出库单中，同缸在未出库单中不可重复。")
    db.refresh(order)
    return order


def update_order(
    db: Session,
    order: OutboundOrder,
    workshop_id: int,
    code: str,
    planned_raw: str,
    vat_id_raw: Iterable[str],
) -> OutboundOrder:
    # 锁单头行，与主管并发出库串行；锁内复核仍是组单中才可改。
    order = db.execute(
        select(OutboundOrder).where(OutboundOrder.id == order.id).with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise VatRuleError("出库单不存在。")
    if order.status != OutboundOrder.STATUS_GROUPING:
        raise VatRuleError("已出库的单据不能再修改。")

    workshop, code, planned, picked = validate_and_prepare(
        db, workshop_id, code, planned_raw, vat_id_raw, exclude_order_id=order.id
    )

    order.workshop_id = workshop.id
    order.code = code
    order.plannedMeters = planned
    # 整体替换明细：先清后挂，配合部分唯一索引拦截跨单重复。
    order.items.clear()
    db.flush()
    order.items = [OutboundItem(vat_id=v["id"], open_flag=True) for v in picked]
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise VatRuleError("其中某口缸已挂在另一张未出库单中，同缸在未出库单中不可重复。")
    db.refresh(order)
    return order


def ship_order(db: Session, order_id: int) -> OutboundOrder:
    """主管点「已出库」：逐缸复核门槛，整单原子成败。

    并发双点：先锁单头行，第一笔把状态翻成已出库并提交；
    第二笔拿到锁后看到已是「已出库」直接拒绝 —— 至多一次成功。
    """
    try:
        order = db.execute(
            select(OutboundOrder)
            .where(OutboundOrder.id == order_id)
            .with_for_update()
        ).scalar_one_or_none()

        if order is None:
            raise VatRuleError("出库单不存在。")
        if order.status == OutboundOrder.STATUS_OUT:
            raise OutboundConflict("该单已出库，请勿重复操作。")

        # 按 id 顺序加缸锁，避免两笔事务交叉等锁造成死锁。
        items = (
            db.execute(
                select(OutboundItem)
                .where(OutboundItem.order_id == order.id)
                .order_by(OutboundItem.vat_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if not items:
            raise VatRuleError("出库单没有明细缸，无法出库。")

        vats = (
            db.execute(
                select(Vat)
                .where(Vat.id.in_([it.vat_id for it in items]))
                .order_by(Vat.id)
                .with_for_update()
                .options(selectinload(Vat.lots))
            )
            .scalars()
            .all()
        )

        # 逐缸确认仍为可染色且最新读数仍过同一道电位门槛；任一失败整单拒绝。
        for vat in vats:
            latest = latest_lot_of(vat)
            if not is_dyeable(vat, latest):
                redox = "无电位" if latest is None or latest.redoxMv is None else f"{latest.redoxMv} mV"
                raise VatRuleError(
                    f"缸 {vat.code} 出库复核未过门槛（当前状态/最新电位 {redox}），整单已拒绝出库，未做任何半出库。"
                )

        order.status = OutboundOrder.STATUS_OUT
        order.outAt = datetime.now(timezone.utc)
        for it in items:
            it.open_flag = False
        db.commit()
    except (VatRuleError, OutboundConflict):
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise VatRuleError("出库数据冲突，整单已拒绝，请刷新后重试。")

    db.refresh(order)
    return order
