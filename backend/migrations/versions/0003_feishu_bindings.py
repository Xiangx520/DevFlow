"""Bind application-scoped Feishu groups to projects and authorized identities."""
from alembic import op
import sqlalchemy as sa
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("feishu_bindings",
        sa.Column("app_id", sa.String(200), primary_key=True),
        sa.Column("chat_id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("bot_open_id", sa.String(200), nullable=False),
        sa.Column("allowed_open_ids", sa.JSON(), nullable=False))
    op.create_index("ix_feishu_bindings_project_id", "feishu_bindings", ["project_id"])

def downgrade():
    op.drop_table("feishu_bindings")
