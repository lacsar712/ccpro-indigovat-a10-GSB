"""浸染拼缸出库单：组单校验与整单原子出库。

口径约定（与 app.services.vat_rules 同源，不得另写）：
- 可染色 = vat_is_dyeable：状态 ready 且最新浸染批次 redoxMv <= -500；
- 专页可选缸、还原台可染色去重计数、出库逐缸复核全部走这一处。
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload

from app.models import OutboundItem, OutboundOrder, User, Vat, Workshop
from app.services.vat_rules import VatRuleError, assert_vat_dyeable, vat_is_dyeable


class OutboundError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def parse_planned_meters(raw: str) -> Decimal:
    try:
        return Decimal((raw or "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise OutboundError("计划米数不是有效数字。")


def latest_capacity_meters(vats: Iterable[Vat]) -> Decimal:
    """明细各缸最新一笔浸染布米之和（可染色缸必有最新批次）。"""
    total = Decimal("0.00")
    for vat in vats:
        latest = vat.latest_lot()
        if latest is not None:
            total += Decimal(latest.clothMeters)
    return total


def validate_order_header(
    code: str, planned_meters: Decimal, vats: Iterable[Vat]
) -> tuple[str, Decimal, Decimal]:
    """新建与更新单头共用：出库号非空、计划米数为正数且不超过明细上限。

    返回 (规范化出库号, 计划米数, 明细米数上限)。
    """
    code = (code or "").strip()
    if not code:
        raise OutboundError("出库号不能为空。")
    if planned_meters <= 0:
        raise OutboundError("计划出库米数须为正数。")
    capacity = latest_capacity_meters(vats)
    if planned_meters > capacity:
        raise OutboundError(
            f"计划出库米数 {planned_meters} m 超过明细各缸最新浸染布米之和 "
            f"{capacity} m（米数合计上限）。"
        )
    return code, planned_meters, capacity


def _eligible_vat_or_raise(
    db: Session, vat_id: int, workshop_id: int, exclude_order_id: Optional[int]
) -> Vat:
    vat = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == vat_id)
        .first()
    )
    if vat is None:
        raise OutboundError(f"染缸 {vat_id} 不存在。")
    if vat.workshop_id != workshop_id:
        raise OutboundError(
            f"染缸 {vat.code} 不属于本单所属工坊，禁止跨坊挂缸。"
        )
    if not vat_is_dyeable(vat):
        raise VatRuleError(
            f"染缸 {vat.code} 当前不可染色（状态非可染色或最新电位未过 -500 mV 门槛）。"
        )
    held = (
        db.query(OutboundItem)
        .join(OutboundOrder, OutboundItem.order_id == OutboundOrder.id)
        .filter(
            OutboundItem.vat_id == vat_id,
            OutboundItem.released.is_(False),
            OutboundOrder.status == OutboundOrder.STATUS_DRAFT,
        )
    )
    if exclude_order_id is not None:
        held = held.filter(OutboundItem.order_id != exclude_order_id)
    other = held.first()
    if other is not None:
        raise OutboundError(
            f"染缸 {vat.code} 已挂在未出库单中，同缸不可重复挂单。"
        )
    return vat


def assert_editable(order: OutboundOrder) -> None:
    if order.status != OutboundOrder.STATUS_DRAFT:
        raise OutboundError("已出库单不可修改。")


def assert_code_unique(
    db: Session, workshop_id: int, code: str, exclude_order_id: Optional[int] = None
) -> None:
    """同坊出库号唯一：新建与更新单头共用（DB 唯一约束继续兜底并发）。"""
    q = db.query(OutboundOrder).filter(
        OutboundOrder.workshop_id == workshop_id, OutboundOrder.code == code
    )
    if exclude_order_id is not None:
        q = q.filter(OutboundOrder.id != exclude_order_id)
    if q.first() is not None:
        raise OutboundError(f"出库号 {code} 在本工坊已存在。")


def create_order(
    db: Session,
    workshop_id: int,
    code: str,
    planned_raw: str,
    vat_ids: list[int],
    user: User,
) -> OutboundOrder:
    workshop = db.get(Workshop, workshop_id)
    if workshop is None:
        raise OutboundError("请选择工坊。")
    planned = parse_planned_meters(planned_raw)

    if not vat_ids:
        raise OutboundError("拼缸出库至少要挂一口同坊可染色缸。")
    if len(set(vat_ids)) != len(vat_ids):
        raise OutboundError("同一缸不能在明细中重复。")

    vats = [
        _eligible_vat_or_raise(db, vid, workshop_id, None) for vid in vat_ids
    ]
    code, planned, _capacity = validate_order_header(code, planned, vats)
    assert_code_unique(db, workshop_id, code)

    order = OutboundOrder(
        workshop_id=workshop_id,
        code=code,
        plannedMeters=planned,
        status=OutboundOrder.STATUS_DRAFT,
        created_by=user.id,
    )
    db.add(order)
    db.flush()
    for vat in vats:
        db.add(OutboundItem(order_id=order.id, vat_id=vat.id, released=False))
    try:
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OutboundError(f"出库号 {code} 在本工坊已存在，或缸位已被其他未出库单占用。")
    return order


def update_header(
    db: Session, order_id: int, code: str, planned_raw: str
) -> OutboundOrder:
    order = (
        db.query(OutboundOrder)
        .options(joinedload(OutboundOrder.items).joinedload(OutboundItem.vat).joinedload(Vat.lots))
        .filter(OutboundOrder.id == order_id)
        .first()
    )
    if order is None:
        raise OutboundError("出库单不存在。")
    assert_editable(order)
    planned = parse_planned_meters(planned_raw)
    vats = [item.vat for item in order.items]
    code, planned, _capacity = validate_order_header(code, planned, vats)
    assert_code_unique(db, order.workshop_id, code, exclude_order_id=order.id)
    order.code = code
    order.plannedMeters = planned
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OutboundError(f"出库号 {code} 在本工坊已存在。")
    return order


def add_item(db: Session, order_id: int, vat_id: int) -> None:
    order = db.get(OutboundOrder, order_id)
    if order is None:
        raise OutboundError("出库单不存在。")
    assert_editable(order)
    if any(item.vat_id == vat_id for item in order.items):
        raise OutboundError("该缸已在本单明细中。")
    vat = _eligible_vat_or_raise(db, vat_id, order.workshop_id, order.id)
    db.add(OutboundItem(order_id=order.id, vat_id=vat.id, released=False))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OutboundError("该缸已挂在其他未出库单中，同缸不可重复挂单。")


def remove_item(db: Session, order_id: int, item_id: int) -> None:
    order = db.get(OutboundOrder, order_id)
    if order is None:
        raise OutboundError("出库单不存在。")
    assert_editable(order)
    item = db.get(OutboundItem, item_id)
    if item is None or item.order_id != order_id:
        raise OutboundError("明细不存在。")
    db.delete(item)
    db.commit()


def delete_order(db: Session, order_id: int) -> None:
    order = db.get(OutboundOrder, order_id)
    if order is None:
        raise OutboundError("出库单不存在。")
    assert_editable(order)
    db.delete(order)
    db.commit()


def ship_order(db: Session, order_id: int) -> OutboundOrder:
    """主管出库：行锁串行化 + 逐缸复核可染色口径，任一缸失败整单拒绝。

    不允许半出库：只在全部缸复核通过后一次性提交；
    两人同时点同一组单中单子时，单据行锁 + 条件翻转（WHERE status='draft'）
    使至多一笔成功，第二笔要么看到已出库、要么翻转 0 行被拒。
    """
    # 先锁单头行：同单并发出库在此排队，至多一笔能读到 draft
    order = (
        db.query(OutboundOrder)
        .filter(OutboundOrder.id == order_id)
        .with_for_update()
        .first()
    )
    if order is None:
        raise OutboundError("出库单不存在。")
    if order.status == OutboundOrder.STATUS_SHIPPED:
        raise OutboundError("该单已出库，不能重复出库。")

    items = (
        db.query(OutboundItem)
        .filter(OutboundItem.order_id == order_id, OutboundItem.released.is_(False))
        .order_by(OutboundItem.vat_id)
        .all()
    )
    if not items:
        raise OutboundError("单据没有明细缸，无法出库。")

    # 锁定每口缸后逐缸复核（与改状态同源门槛）；任一失败抛错，整单回滚
    locked_vats: dict[int, Vat] = {}
    for item in items:
        vat = (
            db.query(Vat)
            .filter(Vat.id == item.vat_id)
            .with_for_update()
            .first()
        )
        if vat is None:
            raise OutboundError("明细中的染缸已不存在，整单拒绝出库。")
        locked_vats[vat.id] = vat

    for item in items:
        vat = locked_vats[item.vat_id]
        try:
            assert_vat_dyeable(vat, vat.latest_lot())
        except (VatRuleError, OutboundError):
            # 任一缸不过门槛：显式回滚释放行锁，整单保持组单中、绝无半出库
            db.rollback()
            raise

    now = datetime.now(timezone.utc)
    # 条件翻转：只有仍是组单中才置为已出库（并发的第二笔影响 0 行）
    flipped = db.execute(
        update(OutboundOrder)
        .where(
            OutboundOrder.id == order_id,
            OutboundOrder.status == OutboundOrder.STATUS_DRAFT,
        )
        .values(status=OutboundOrder.STATUS_SHIPPED, shippedAt=now)
    ).rowcount
    if flipped != 1:
        db.rollback()
        raise OutboundError("该单刚被另一笔操作出库，本次出库拒绝（至多一次成功）。")

    for item in items:
        item.released = True
    try:
        db.commit()
    except (IntegrityError, OperationalError):
        # 锁冲突/死锁/唯一约束：整单回滚，绝不留下半出库
        db.rollback()
        raise OutboundError("出库与其他操作冲突，已整单拒绝，请刷新后重试。")
    db.refresh(order)
    return order
