"""add invoice_number to pdf_namings

Revision ID: a1b2c3d4e5f6
Revises: 623ec38b9974
Create Date: 2026-04-12
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "623ec38b9974"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pdf_namings", sa.Column("invoice_number", sa.String(length=128), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pdf_namings", "invoice_number")
