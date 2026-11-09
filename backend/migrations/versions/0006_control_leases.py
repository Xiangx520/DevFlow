"""Independent routing and control leases."""
from alembic import op
import sqlalchemy as sa
from devflow.models import Control
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("inbox", sa.Column("owner", sa.String(36)))
    op.add_column("inbox", sa.Column("lease_until", sa.Float(), server_default="0", nullable=False))
    Control.__table__.create(op.get_bind())


def downgrade():
    Control.__table__.drop(op.get_bind())
    op.drop_column("inbox", "lease_until")
    op.drop_column("inbox", "owner")
