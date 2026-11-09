"""Per-task message ownership lifecycle."""
from alembic import op
import sqlalchemy as sa
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("message_ownership",
        sa.Column("trigger_id", sa.String(36), sa.ForeignKey("triggers.id"), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("delivered_at", sa.Float(), nullable=False),
        sa.Column("read_at", sa.Float()), sa.Column("processed_at", sa.Float()),
        sa.Column("released_at", sa.Float()), sa.Column("release_reason", sa.Text()))


def downgrade():
    op.drop_table("message_ownership")
