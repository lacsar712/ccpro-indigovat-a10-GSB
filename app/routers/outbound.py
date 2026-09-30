"""浸染拼缸出库专页。

- 染缸工（普通用户）：维护单头与明细（新建/更新）。
- 主管（is_superuser）：点「已出库」，逐缸复核电位门槛，整单原子成败。
"""

from decimal import Decimal
from typing import Optional

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import get_current_user
from app.db import get_db
from app.models import OutboundItem, OutboundOrder, User, Vat, Workshop
from app.services import outbound as svc
from app.services.vat_rules import VatRuleError, dyeable_counts_by_workshop, dyeable_vats

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["tojson"] = lambda v: markupsafe.Markup(
    json.dumps(v, ensure_ascii=False)
)

STATUS_LABELS = {
    OutboundOrder.STATUS_GROUPING: "组单中",
    OutboundOrder.STATUS_OUT: "已出库",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _login_redirect():
    return RedirectResponse("/login", status_code=303)


def _open_occupied_vats(db: Session, exclude_order_id: Optional[int] = None) -> set[int]:
    """挂在「其它」未出库单里的缸：本单编辑时这些缸互斥不可选。"""
    stmt = select(OutboundItem.vat_id).where(OutboundItem.open_flag.is_(True))
    if exclude_order_id is not None:
        stmt = stmt.where(OutboundItem.order_id != exclude_order_id)
    return {row[0] for row in db.execute(stmt).all()}


def _order_payload(db: Session, order: OutboundOrder) -> dict:
    items = []
    for it in sorted(order.items, key=lambda x: x.vat.code if x.vat else ""):
        vat = it.vat
        lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id)) if vat and vat.lots else []
        latest = lots[-1] if lots else None
        items.append(
            {
                "vatId": it.vat_id,
                "code": vat.code if vat else f"#{it.vat_id}",
                "dyeType": vat.dyeType if vat else "",
                "latestMeters": float(latest.clothMeters) if latest else None,
                "latestRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
            }
        )
    cap = sum(
        (Decimal(str(i["latestMeters"] or 0)) for i in items), Decimal("0.00")
    )
    return {
        "id": order.id,
        "code": order.code,
        "workshopId": order.workshop_id,
        "workshopName": order.workshop.name if order.workshop else "",
        "plannedMeters": float(order.plannedMeters),
        "status": order.status,
        "statusLabel": STATUS_LABELS.get(order.status, order.status),
        "creator": order.creator.username if order.creator else "",
        "createdAt": order.createdAt.strftime("%Y-%m-%d %H:%M") if order.createdAt else "",
        "outAt": order.outAt.strftime("%Y-%m-%d %H:%M") if order.outAt else None,
        "lines": items,
        "capMeters": float(cap),
    }


def _base_ctx(request: Request, user: User, active: str) -> dict:
    return {"request": request, "user": user, "active": active}


@router.get("/outbound", response_class=HTMLResponse)
async def outbound_list(
    request: Request,
    workshop: Optional[int] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return _login_redirect()

    workshops = db.query(Workshop).order_by(Workshop.name).all()
    stmt = (
        select(OutboundOrder)
        .options(
            selectinload(OutboundOrder.items).selectinload(OutboundItem.vat),
            selectinload(OutboundOrder.workshop),
            selectinload(OutboundOrder.creator),
        )
        .order_by(OutboundOrder.createdAt.desc(), OutboundOrder.id.desc())
    )
    if workshop is not None:
        stmt = stmt.where(OutboundOrder.workshop_id == workshop)
    orders = db.execute(stmt).scalars().all()

    ctx = _base_ctx(request, user, "outbound")
    ctx.update(
        {
            "workshops": [{"id": w.id, "name": w.name} for w in workshops],
            "filter_workshop": workshop,
            "orders": [_order_payload(db, o) for o in orders],
            "dyeable_counts": dyeable_counts_by_workshop(db),
            "error": error,
        }
    )
    return render(request, "outbound_list.html", ctx)


def _form_context(
    request: Request,
    user: User,
    db: Session,
    *,
    workshop_id: Optional[int],
    selected_vat_ids: set[int],
    form_code: str = "",
    form_planned: str = "",
    error: Optional[str] = None,
    order: Optional[OutboundOrder] = None,
):
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    exclude = order.id if order else None
    occupied = _open_occupied_vats(db, exclude)
    # 一次性下发所有工坊的可染色缸（同一口径），前端按所选工坊分组过滤。
    choices = dyeable_vats(db)
    choice_ids = {c["id"] for c in choices}
    workshop_names = {w.id: w.name for w in workshops}
    for c in choices:
        c["occupiedElsewhere"] = c["id"] in occupied
        c["selected"] = c["id"] in selected_vat_ids
        c["notDyeable"] = False
    # 已挂在本单、但此刻已掉出可染色口径的缸：保留并明确标红，保存/出库都会被拦。
    missing = selected_vat_ids - choice_ids
    if missing:
        for vat in db.query(Vat).filter(Vat.id.in_(missing)).all():
            lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
            latest = lots[-1] if lots else None
            choices.append(
                {
                    "id": vat.id,
                    "code": vat.code,
                    "workshopId": vat.workshop_id,
                    "workshopName": workshop_names.get(vat.workshop_id, ""),
                    "dyeType": vat.dyeType,
                    "latestLotId": latest.id if latest else None,
                    "latestMeters": latest.clothMeters if latest else Decimal("0.00"),
                    "latestRedox": latest.redoxMv if latest else None,
                    "occupiedElsewhere": vat.id in occupied,
                    "selected": True,
                    "notDyeable": True,
                }
            )
    choices.sort(key=lambda c: (c["workshopId"], c["code"]))
    cap = sum(
        (c["latestMeters"] for c in choices if c["selected"]), Decimal("0.00")
    )
    choices_json = [
        {
            "id": c["id"],
            "code": c["code"],
            "workshopId": c["workshopId"],
            "workshopName": c["workshopName"],
            "dyeType": c["dyeType"],
            "latestMeters": float(c["latestMeters"]),
            "latestRedox": float(c["latestRedox"]) if c["latestRedox"] is not None else None,
            "occupiedElsewhere": c["occupiedElsewhere"],
            "selected": c["selected"],
            "notDyeable": c["notDyeable"],
        }
        for c in choices
    ]
    ctx = _base_ctx(request, user, "outbound")
    ctx.update(
        {
            "workshops": [{"id": w.id, "name": w.name} for w in workshops],
            "filter_workshop": workshop_id,
            "choices_json": choices_json,
            "selected_count": len(selected_vat_ids),
            "cap_meters": float(cap),
            "form_code": form_code,
            "form_planned": form_planned,
            "error": error,
            "order": _order_payload(db, order) if order else None,
            "is_worker": not user.is_superuser,
            "is_supervisor": bool(user.is_superuser),
        }
    )
    return ctx


@router.get("/outbound/new", response_class=HTMLResponse)
async def outbound_new(
    request: Request,
    workshop: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return _login_redirect()
    if user.is_superuser:
        # 主管不组单：回到列表。
        return RedirectResponse("/outbound", status_code=303)
    ctx = _form_context(
        request, user, db, workshop_id=workshop, selected_vat_ids=set()
    )
    return render(request, "outbound_form.html", ctx)


@router.post("/outbound", response_class=HTMLResponse)
async def outbound_create(
    request: Request,
    workshop_id: int = Form(...),
    code: str = Form(...),
    plannedMeters: str = Form(...),
    vat_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return _login_redirect()
    if user.is_superuser:
        return RedirectResponse("/outbound", status_code=303)
    try:
        order = svc.create_order(db, workshop_id, code, plannedMeters, vat_ids, user.id)
        return RedirectResponse(f"/outbound/{order.id}", status_code=303)
    except (VatRuleError, ValueError) as exc:
        message = exc.message if isinstance(exc, VatRuleError) else f"输入无效：{exc}"
        db.rollback()
        selected = set()
        for raw in vat_ids:
            raw = raw.strip()
            if raw.isdigit():
                selected.add(int(raw))
        ctx = _form_context(
            request,
            user,
            db,
            workshop_id=workshop_id,
            selected_vat_ids=selected,
            form_code=code,
            form_planned=plannedMeters,
            error=message,
        )
        return render(request, "outbound_form.html", ctx, status_code=400)


@router.get("/outbound/{pk}", response_class=HTMLResponse)
async def outbound_detail(pk: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _login_redirect()
    order = (
        db.query(OutboundOrder)
        .options(
            selectinload(OutboundOrder.items).selectinload(OutboundItem.vat).selectinload(Vat.lots),
            selectinload(OutboundOrder.workshop),
            selectinload(OutboundOrder.creator),
        )
        .filter(OutboundOrder.id == pk)
        .first()
    )
    if not order:
        return RedirectResponse("/outbound", status_code=303)

    # 组单中且是染缸工 → 直接给编辑表单；主管或已出库 → 只读明细（主管可出库）。
    if order.status == OutboundOrder.STATUS_GROUPING and not user.is_superuser:
        ctx = _form_context(
            request,
            user,
            db,
            workshop_id=order.workshop_id,
            selected_vat_ids={it.vat_id for it in order.items},
            form_code=order.code,
            form_planned=str(order.plannedMeters),
            order=order,
        )
        return render(request, "outbound_form.html", ctx)

    ctx = _base_ctx(request, user, "outbound")
    ctx.update(
        {
            "order": _order_payload(db, order),
            "is_supervisor": bool(user.is_superuser),
            "error": None,
        }
    )
    return render(request, "outbound_detail.html", ctx)


@router.post("/outbound/{pk}/edit", response_class=HTMLResponse)
async def outbound_update(
    pk: int,
    request: Request,
    workshop_id: int = Form(...),
    code: str = Form(...),
    plannedMeters: str = Form(...),
    vat_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return _login_redirect()
    if user.is_superuser:
        return RedirectResponse("/outbound", status_code=303)
    order = db.get(OutboundOrder, pk)
    if not order:
        return RedirectResponse("/outbound", status_code=303)
    try:
        svc.update_order(db, order, workshop_id, code, plannedMeters, vat_ids)
        return RedirectResponse(f"/outbound/{pk}", status_code=303)
    except VatRuleError as exc:
        db.rollback()
        order = (
            db.query(OutboundOrder)
            .options(
                selectinload(OutboundOrder.items).selectinload(OutboundItem.vat).selectinload(Vat.lots),
                selectinload(OutboundOrder.workshop),
                selectinload(OutboundOrder.creator),
            )
            .filter(OutboundOrder.id == pk)
            .first()
        )
        selected = set()
        for raw in vat_ids:
            raw = raw.strip()
            if raw.isdigit():
                selected.add(int(raw))
        ctx = _form_context(
            request,
            user,
            db,
            workshop_id=workshop_id,
            selected_vat_ids=selected,
            form_code=code,
            form_planned=plannedMeters,
            error=exc.message,
            order=order,
        )
        return render(request, "outbound_form.html", ctx, status_code=400)


@router.post("/outbound/{pk}/ship", response_class=HTMLResponse)
async def outbound_ship(pk: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return _login_redirect()
    if not user.is_superuser:
        # 只有主管能出库。
        return RedirectResponse(f"/outbound/{pk}", status_code=303)
    try:
        svc.ship_order(db, pk)
        return RedirectResponse(f"/outbound/{pk}", status_code=303)
    except (VatRuleError, svc.OutboundConflict) as exc:
        db.rollback()
        order = (
            db.query(OutboundOrder)
            .options(
                selectinload(OutboundOrder.items).selectinload(OutboundItem.vat).selectinload(Vat.lots),
                selectinload(OutboundOrder.workshop),
                selectinload(OutboundOrder.creator),
            )
            .filter(OutboundOrder.id == pk)
            .first()
        )
        # 失败也必须能正常打开专页：重渲染明细，不做任何写入。
        ctx = _base_ctx(request, user, "outbound")
        ctx.update(
            {
                "order": _order_payload(db, order) if order else None,
                "is_supervisor": True,
                "error": exc.message,
            }
        )
        status_code = 409 if isinstance(exc, svc.OutboundConflict) else 400
        return render(request, "outbound_detail.html", ctx, status_code=status_code)
