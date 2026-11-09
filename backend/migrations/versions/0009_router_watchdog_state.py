"""Router memory and maintainer-facing Watchdog instructions."""
from alembic import op
from devflow.models import RouterState, WatchdogInstruction

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    RouterState.__table__.create(op.get_bind())
    WatchdogInstruction.__table__.create(op.get_bind())


def downgrade():
    WatchdogInstruction.__table__.drop(op.get_bind())
    RouterState.__table__.drop(op.get_bind())
