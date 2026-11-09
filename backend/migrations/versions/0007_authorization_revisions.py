"""Immutable authorization, specification receipts, review snapshots and source cursors."""
from alembic import op
from devflow.models import PolicyRevision, SpecRevision, SpecReceipt, ReviewRecord, SourceCursor
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None
TABLES = [PolicyRevision, SpecRevision, SpecReceipt, ReviewRecord, SourceCursor]


def upgrade():
    for model in TABLES:
        model.__table__.create(op.get_bind())


def downgrade():
    for model in reversed(TABLES):
        model.__table__.drop(op.get_bind())
