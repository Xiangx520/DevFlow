"""Durable Feishu messages before project binding."""
from alembic import op
import sqlalchemy as sa
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("feishu_messages",
        sa.Column("message_id", sa.String(200), primary_key=True),
        sa.Column("chat_id", sa.String(200), nullable=False),
        sa.Column("sender_id", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False))
    op.create_index("ix_feishu_messages_chat_id", "feishu_messages", ["chat_id"])

def downgrade():
    op.drop_table("feishu_messages")
