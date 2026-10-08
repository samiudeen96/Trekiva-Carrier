"""review checks: location risk and duplicate orders

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shopify_fulfillment_orders",
        sa.Column("review_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "shopify_fulfillment_orders",
        sa.Column(
            "review_flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_shopify_orders_shop_id_shopify_created_at",
        "shopify_orders",
        ["shop_id", "shopify_created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_shopify_orders_shop_id_shopify_created_at", table_name="shopify_orders")
    op.drop_column("shopify_fulfillment_orders", "review_flags")
    op.drop_column("shopify_fulfillment_orders", "review_checked_at")
