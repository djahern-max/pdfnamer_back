from datetime import datetime
import hashlib
import secrets

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Boolean, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─── Tenant ───────────────────────────────────────────────────────────────────


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    pdf_namings: Mapped[list["PdfNaming"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


# ─── ApiKey ───────────────────────────────────────────────────────────────────


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(
        String(128), nullable=False, server_default="default"
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="api_keys")

    @staticmethod
    def generate() -> tuple[str, str]:
        """
        raw_key  → show to the user ONCE, never store it.
        key_hash → store in DB.
        """
        raw = "pdfn_" + secrets.token_urlsafe(32)
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        return raw, hashed

    @staticmethod
    def hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()


# ─── PDF Naming ───────────────────────────────────────────────────────────────

# Valid doc_type values — enforced in the prompt, stored as plain strings for flexibility.
#
#   invoice      Traditional A/P invoice with a due date
#   statement    Periodic account summary
#   cc_receipt   Paid at point of sale via credit/debit card
#   check_receipt  Paid at POS via check or cash
#   contract     Agreement / rental contract
#   estimate     Quote or proposal
#   other        Anything that doesn't fit the above


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

    # e.g. "Visa_1762", "Mastercard_4433", "Check", "Cash" — for reconciliation
    payment_method: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # e.g. "INV-1042", "2026-0391" — extracted from invoice/statement headers
    invoice_number: Mapped[str | None] = mapped_column(String(128), nullable=True)

    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pattern_used: Mapped[str | None] = mapped_column(String(512), nullable=True)

    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    pattern_used: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Claude API token tracking
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="pdf_namings")


# ─── Indexes ──────────────────────────────────────────────────────────────────
Index("ix_api_keys_key_hash", ApiKey.key_hash)
Index("ix_pdf_namings_tenant", PdfNaming.tenant_id, PdfNaming.created_at)
