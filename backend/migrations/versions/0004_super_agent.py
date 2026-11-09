"""Super-agent core: triggers, threads, mandates, typed inputs and eval decisions."""
from alembic import op
import sqlalchemy as sa
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("policy_version", sa.String(64), nullable=True))
    op.add_column("projects", sa.Column("spec_version", sa.Integer(), nullable=True))

    op.add_column("tasks", sa.Column("mandate", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("follows", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("spec_version", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("report_state", sa.String(30), nullable=True))
    op.add_column("tasks", sa.Column("ended_by", sa.String(30), nullable=True))
    op.add_column("tasks", sa.Column("outcome", sa.String(30), nullable=True))
    op.add_column("tasks", sa.Column("ended_at", sa.Float(), nullable=True))

    op.add_column("attempts", sa.Column("report_state", sa.String(30), nullable=True))

    op.add_column("task_inputs", sa.Column("kind", sa.String(30), nullable=True))
    op.add_column("task_inputs", sa.Column("trigger_id", sa.String(36), nullable=True))
    op.add_column("task_inputs", sa.Column("read_at", sa.Float(), nullable=True))

    op.add_column("decisions", sa.Column("kind", sa.String(30), nullable=True))
    op.add_column("decisions", sa.Column("thread_key", sa.String(300), nullable=True))
    op.add_column("decisions", sa.Column("trigger_ids", sa.JSON(), nullable=True))
    op.add_column("decisions", sa.Column("strength", sa.String(30), nullable=True))
    op.add_column("decisions", sa.Column("mandate", sa.JSON(), nullable=True))
    op.add_column("decisions", sa.Column("audit", sa.JSON(), nullable=True))
    op.create_index("ix_decisions_kind", "decisions", ["kind"])
    op.create_index("ix_decisions_thread_key", "decisions", ["thread_key"])

    op.create_table("triggers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("dedupe_key", sa.String(500), nullable=False, unique=True),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("human", sa.Boolean(), nullable=False),
        sa.Column("addressed", sa.String(30), nullable=True),
        sa.Column("thread_keys", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("channel", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("at", sa.Float(), nullable=False),
        sa.Column("disposition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_triggers_project_id", "triggers", ["project_id"])
    op.create_index("ix_triggers_source", "triggers", ["source"])

    op.create_table("threads",
        sa.Column("key", sa.String(300), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=False),
        sa.Column("evaluating", sa.JSON(), nullable=False),
        sa.Column("subscriptions", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("expired", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_index("ix_threads_project_id", "threads", ["project_id"])

    # Backfill rows written before these columns existed; new inserts use Python defaults.
    op.execute("UPDATE projects SET policy_version = COALESCE(policy_version, '')")
    op.execute("UPDATE projects SET spec_version = COALESCE(spec_version, 0)")
    op.execute("UPDATE tasks SET mandate = COALESCE(mandate, '{}')")
    op.execute("UPDATE tasks SET follows = COALESCE(follows, '[]')")
    op.execute("UPDATE tasks SET spec_version = COALESCE(spec_version, 0)")
    op.execute("UPDATE tasks SET report_state = COALESCE(report_state, '')")
    op.execute("UPDATE attempts SET report_state = COALESCE(report_state, '')")
    op.execute("UPDATE task_inputs SET kind = COALESCE(kind, 'text')")
    op.execute("UPDATE decisions SET kind = COALESCE(kind, 'manager')")
    op.execute("UPDATE decisions SET trigger_ids = COALESCE(trigger_ids, '[]')")
    op.execute("UPDATE decisions SET mandate = COALESCE(mandate, '{}')")
    op.execute("UPDATE decisions SET audit = COALESCE(audit, '{}')")


def downgrade():
    op.drop_table("threads")
    op.drop_table("triggers")
    op.drop_index("ix_decisions_thread_key", table_name="decisions")
    op.drop_index("ix_decisions_kind", table_name="decisions")
    for name in ("audit", "mandate", "strength", "trigger_ids", "thread_key", "kind"):
        op.drop_column("decisions", name)
    for name in ("read_at", "trigger_id", "kind"):
        op.drop_column("task_inputs", name)
    op.drop_column("attempts", "report_state")
    for name in ("ended_at", "outcome", "ended_by", "report_state", "spec_version", "follows", "mandate"):
        op.drop_column("tasks", name)
    op.drop_column("projects", "spec_version")
    op.drop_column("projects", "policy_version")
