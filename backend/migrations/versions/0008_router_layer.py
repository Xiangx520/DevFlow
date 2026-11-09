"""Replace retired manager columns with project pause control."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.drop_column("projects", "manager_summary")
    op.drop_column("projects", "manager_owner")
    op.drop_column("projects", "manager_until")


def downgrade():
    op.add_column("projects", sa.Column("manager_summary", sa.Text(), server_default="", nullable=False))
    op.add_column("projects", sa.Column("manager_owner", sa.String(36)))
    op.add_column("projects", sa.Column("manager_until", sa.Float(), server_default="0", nullable=False))
    op.drop_column("projects", "paused")
