"""alerts outbox

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("shop_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "WARNING", "ERROR", "CRITICAL", name="alertseverity", native_enum=False, length=40
            ),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("fulfillment_order_id", sa.BigInteger(), nullable=True),
        sa.Column("shipment_id", sa.BigInteger(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "SENDING",
                "SENT",
                "FAILED",
                "SKIPPED",
                name="alertstatus",
                native_enum=False,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "delivered_channels", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["fulfillment_order_id"],
            ["shopify_fulfillment_orders.id"],
            name=op.f("fk_alerts_fulfillment_order_id_shopify_fulfillment_orders"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["shopify_orders.id"],
            name=op.f("fk_alerts_order_id_shopify_orders"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shipment_id"],
            ["shipments.id"],
            name=op.f("fk_alerts_shipment_id_shipments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["shop_id"],
            ["shops.id"],
            name=op.f("fk_alerts_shop_id_shops"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    op.create_index(
        op.f("ix_alerts_status_next_attempt_at"),
        "alerts",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_alerts_fingerprint_sent_at"), "alerts", ["fingerprint", "sent_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_alerts_fingerprint_sent_at"), table_name="alerts")
    op.drop_index(op.f("ix_alerts_status_next_attempt_at"), table_name="alerts")
    op.drop_table("alerts")
