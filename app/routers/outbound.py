from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import OutboundItem, OutboundOrder, User, Vat, Workshop
from app.services import outbound as svc
from app.services.vat_rules import (
    VatRuleError,
    query_dyeable_vats,
    vat_is_dyeable,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ORDER_STATUS_LABELS = {
    OutboundOrder.STATUS_DRAFT: "组单中",
    OutboundOrder.STATUS_SHIPPED: "已出库",
}


def render(request: Request, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, "outbound.html", ctx, status_code=status_code)


def _held_vat_ids(db: Session) -> set[int]:
    """当前挂在未出库（组单中）单里的缸。"""
    rows = (
        db.query(OutboundItem.vat_id)
        .join(OutboundOrder, OutboundItem.order_id == OutboundOrder.id)
        .filter(
            OutboundItem.released.is_(False),
            OutboundOrder.status == OutboundOrder.STATUS_DRAFT,
        )
        .all()
    )
    return {r[0] for r in rows}


def _vat_summary(vat: Vat) -> dict:
    latest = vat.latest_lot()
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "status": vat.status,
        "lastMeters": float(latest.clothMeters) if latest else 0.0,
        "lastRedox": (
            float(latest.redoxMv)
            if latest and latest.redoxMv is not None
            else None
        ),
        "dyeable": vat_is_dyeable(vat, latest),
    }


def _page_context(
    request: Request,
    db: Session,
    user: User,
    error: Optional[str] = None,
    selected_order: Optional[int] = None,
):
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    held = _held_vat_ids(db)

    # 专页可选缸：与还原台去重计数、出库复核同一口径（vat_is_dyeable）
    dyeable = query_dyeable_vats(db)
    dyeable_ids = [v.id for v in dyeable]
    vat_summaries = {v.id: _vat_summary(v) for v in dyeable}

    vats_by_workshop: dict[int, list[dict]] = {}
    counts_by_workshop: dict[int, int] = {}
    for v in dyeable:
        s = vat_summaries[v.id]
        vats_by_workshop.setdefault(v.workshop_id, []).append(s)
        counts_by_workshop[v.workshop_id] = counts_by_workshop.get(v.workshop_id, 0) + 1

    users = {u.id: u.username for u in db.query(User).all()}

    orders = (
        db.query(OutboundOrder)
        .options(
            joinedload(OutboundOrder.workshop),
            joinedload(OutboundOrder.items)
            .joinedload(OutboundItem.vat)
            .joinedload(Vat.lots),
        )
        .order_by(
            OutboundOrder.status.asc(),  # draft 在 shipped 前
            OutboundOrder.createdAt.desc(),
        )
        .all()
    )

    def order_payload(o: OutboundOrder) -> dict:
        items = []
        own_vat_ids = set()
        for it in o.items:
            vat = it.vat
            latest = vat.latest_lot() if vat is not None else None
            own_vat_ids.add(it.vat_id)
            items.append(
                {
                    "itemId": it.id,
                    "vatId": it.vat_id,
                    "code": vat.code if vat else f"#{it.vat_id}",
                    "dyeType": vat.dyeType if vat else "",
                    "released": it.released,
                    "lastMeters": float(latest.clothMeters) if latest else 0.0,
                    "lastRedox": (
                        float(latest.redoxMv)
                        if latest and latest.redoxMv is not None
                        else None
                    ),
                    "dyeable": vat_is_dyeable(vat, latest) if vat else False,
                }
            )
        capacity = svc.latest_capacity_meters(
            [it.vat for it in o.items if it.vat is not None]
        )
        blocked = held - own_vat_ids
        addable = [
            s for s in vats_by_workshop.get(o.workshop_id, [])
            if s["id"] not in own_vat_ids and s["id"] not in blocked
        ]
        return {
            "id": o.id,
            "code": o.code,
            "workshopId": o.workshop_id,
            "workshopName": o.workshop.name if o.workshop else "",
            "plannedMeters": float(o.plannedMeters),
            "status": o.status,
            "statusLabel": ORDER_STATUS_LABELS.get(o.status, o.status),
            "draft": o.status == OutboundOrder.STATUS_DRAFT,
            "createdByName": users.get(o.created_by, str(o.created_by)),
            "createdAt": o.createdAt.strftime("%Y-%m-%d %H:%M") if o.createdAt else "",
            "shippedAt": o.shippedAt.strftime("%Y-%m-%d %H:%M") if o.shippedAt else "",
            "capacityMeters": float(capacity),
            "items": items,
            "addableVats": addable,
        }

    return {
        "request": request,
        "user": user,
        "is_supervisor": bool(user.is_superuser),
        "workshops": [{"id": w.id, "name": w.name} for w in workshops],
        "dyeable_ids": dyeable_ids,
        "dyeable_total": len(dyeable_ids),
        "counts_by_workshop": counts_by_workshop,
        "vats_by_workshop": vats_by_workshop,
        "held_vat_ids": sorted(held),
        "orders": [order_payload(o) for o in orders],
        "error": error,
        "selected_order": selected_order,
        "active": "outbound",
    }


@router.get("/outbound", response_class=HTMLResponse)
async def outbound_page(
    request: Request,
    order: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, _page_context(request, db, user, selected_order=order))


def _fail(request, db, user, message, selected_order=None, code=400):
    return render(
        request,
        _page_context(request, db, user, error=message, selected_order=selected_order),
        status_code=code,
    )


@router.post("/outbound/orders", response_class=HTMLResponse)
async def create_order(
    request: Request,
    workshop_id: int = Form(...),
    code: str = Form(...),
    plannedMeters: str = Form(...),
    vat_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        order = svc.create_order(db, workshop_id, code, plannedMeters, vat_ids, user)
        return RedirectResponse(f"/outbound?order={order.id}", status_code=303)
    except (svc.OutboundError, VatRuleError) as exc:
        return _fail(request, db, user, exc.message)


@router.post("/outbound/orders/{pk}/header", response_class=HTMLResponse)
async def update_header(
    pk: int,
    request: Request,
    code: str = Form(...),
    plannedMeters: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        svc.update_header(db, pk, code, plannedMeters)
        return RedirectResponse(f"/outbound?order={pk}", status_code=303)
    except (svc.OutboundError, VatRuleError) as exc:
        return _fail(request, db, user, exc.message, selected_order=pk)


@router.post("/outbound/orders/{pk}/items", response_class=HTMLResponse)
async def add_item(
    pk: int,
    request: Request,
    vat_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        svc.add_item(db, pk, vat_id)
        return RedirectResponse(f"/outbound?order={pk}", status_code=303)
    except (svc.OutboundError, VatRuleError) as exc:
        return _fail(request, db, user, exc.message, selected_order=pk)


@router.post("/outbound/orders/{pk}/items/{item_pk}/delete", response_class=HTMLResponse)
async def remove_item(
    pk: int,
    item_pk: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        svc.remove_item(db, pk, item_pk)
    except svc.OutboundError as exc:
        return _fail(request, db, user, exc.message, selected_order=pk)
    return RedirectResponse(f"/outbound?order={pk}", status_code=303)


@router.post("/outbound/orders/{pk}/delete", response_class=HTMLResponse)
async def delete_order(pk: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        svc.delete_order(db, pk)
    except svc.OutboundError as exc:
        return _fail(request, db, user, exc.message, selected_order=pk)
    return RedirectResponse("/outbound", status_code=303)


@router.post("/outbound/orders/{pk}/ship", response_class=HTMLResponse)
async def ship_order(pk: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user.is_superuser:
        return _fail(
            request, db, user,
            "只有主管可以点已出库；染缸工负责组单与核对米数。",
            selected_order=pk,
            code=403,
        )
    try:
        svc.ship_order(db, pk)
        return RedirectResponse(f"/outbound?order={pk}", status_code=303)
    except (svc.OutboundError, VatRuleError) as exc:
        # 任一缸失败整单拒绝：回滚后专页照常打开，不会出现半出库
        return _fail(request, db, user, exc.message, selected_order=pk, code=409)
