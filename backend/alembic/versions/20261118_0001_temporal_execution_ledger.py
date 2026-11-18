"""create PostgreSQL save schema and Temporal execution ledger

Revision ID: 0001_temporal
Revises:
Create Date: 2026-11-18 00:00:00
"""
from alembic import op

from db import metadata


revision = "0001_temporal"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind(), checkfirst=True)
