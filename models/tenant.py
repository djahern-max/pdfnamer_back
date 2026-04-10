"""
models/tenant.py  –  PDF Auto-Namer (standalone, multi-tenant)

Tables
------
tenants        – one row per customer org
api_keys       – hashed API keys belonging to a tenant
pdf_namings    – per-tenant naming history (pattern learning store)
"""

import hashlib
import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, func, Index
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─── Tenant ──────────────────────────────────────────────────────────────────

class Tenant(Base):
    """One row = one customer (company / individual)."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    pdf_namings: Mapped[list["PdfNaming"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


# ─── API Key ─────────────────────────────────────────────────────────────────

class ApiKey(Base):
    """
    Hashed API keys.  The raw key is only shown once at creation time.
    We store SHA-256(key) so a DB leak doesn't expose usable secrets.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)   # SHA-256 hex
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def generate() -> tuple[str, str]:
        """
        Returns (raw_key, key_hash).
        raw_key  → show to the user ONCE, never store it.
        key_hash → store in DB.
        """
        raw = "pdfn_" + secrets.token_urlsafe(32)   # e.g. pdfn_abc123…
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        return raw, hashed

    @staticmethod
    def hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()


# ─── PDF Naming (updated with tenant FK) ─────────────────────────────────────

class PdfNaming(Base):
    """
    Per-tenant naming history.
    Each confirmed row is a few-shot example for that tenant only.
    """

    __tablename__ = "pdf_namings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    suggested_name: Mapped[str] = mapped_column(String(512), nullable=False)
    confirmed_name: Mapped[str | None] = mapped_column(String(512), nullable=True)

    vendor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    doc_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    doc_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pattern_used: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="pdf_namings")


# ─── Indexes ──────────────────────────────────────────────────────────────────
Index("ix_api_keys_key_hash",   ApiKey.key_hash)
Index("ix_pdf_namings_tenant",  PdfNaming.tenant_id, PdfNaming.created_at)
