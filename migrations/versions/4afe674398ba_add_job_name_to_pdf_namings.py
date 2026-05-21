"""add job_name to pdf_namings

Revision ID: 4afe674398ba
Revises: 9210f15cb073
Create Date: 2026-05-20 11:55:33.966516

"""

"""add job_name to pdf_namings

Revision ID: b1c2d3e4f5a6
Revises: 9210f15cb073
Create Date: 2026-05-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "9210f15cb073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pdf_namings",
        sa.Column("job_name", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pdf_namings", "job_name")
