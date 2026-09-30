from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)


class Workshop(Base):
    __tablename__ = "workshops"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, default="")

    vats: Mapped[list["Vat"]] = relationship(back_populates="workshop")


class Vat(Base):
    __tablename__ = "vats"
    __table_args__ = (
        UniqueConstraint("workshop_id", "code", name="uniq_vat_code_per_workshop"),
    )

    STATUS_IDLE = "idle"
    STATUS_REDUCING = "reducing"
    STATUS_READY = "ready"

    id: Mapped[int] = mapped_column(primary_key=True)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(40))
    dyeType: Mapped[str] = mapped_column(String(80))
    volumeL: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_IDLE)

    workshop: Mapped["Workshop"] = relationship(back_populates="vats")
    lots: Mapped[list["DipLot"]] = relationship(back_populates="vat")


class DipLot(Base):
    __tablename__ = "dip_lots"

    id: Mapped[int] = mapped_column(primary_key=True)
    vat_id: Mapped[int] = mapped_column(ForeignKey("vats.id", ondelete="CASCADE"))
    dippedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clothMeters: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    redoxMv: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)

    vat: Mapped["Vat"] = relationship(back_populates="lots")


class OutboundOrder(Base):
    """浸染拼缸出库单：一单挑多口同坊可染色缸。"""

    __tablename__ = "outbound_orders"
    __table_args__ = (
        UniqueConstraint(
            "workshop_id", "code", name="uniq_outbound_code_per_workshop"
        ),
    )

    STATUS_GROUPING = "grouping"  # 组单中
    STATUS_OUT = "out"            # 已出库

    id: Mapped[int] = mapped_column(primary_key=True)
    workshop_id: Mapped[int] = mapped_column(ForeignKey("workshops.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(40))
    plannedMeters: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), default=STATUS_GROUPING)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    outAt: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    workshop: Mapped["Workshop"] = relationship()
    creator: Mapped["User"] = relationship()
    items: Mapped[list["OutboundItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OutboundItem(Base):
    __tablename__ = "outbound_items"
    __table_args__ = (
        UniqueConstraint("order_id", "vat_id", name="uniq_vat_per_outbound_order"),
        # 同一缸在任何「组单中」的出库单里至多出现一次（跨单也不行）。
        # 谓词不能跨表子查询，故冗余 open_flag，由服务层与单头状态在同一事务翻转。
        Index(
            "uniq_vat_in_open_outbound",
            "vat_id",
            unique=True,
            postgresql_where=text("open_flag"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("outbound_orders.id", ondelete="CASCADE")
    )
    vat_id: Mapped[int] = mapped_column(ForeignKey("vats.id", ondelete="RESTRICT"))
    # 组单中恒为 true；整单出库成功的同一事务里随单头翻成 false。
    open_flag: Mapped[bool] = mapped_column(Boolean, default=True)

    order: Mapped["OutboundOrder"] = relationship(back_populates="items")
    vat: Mapped["Vat"] = relationship()
