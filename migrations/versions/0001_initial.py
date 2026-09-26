"""initial schema

The initial migration creates the base schema except for tables introduced by
later migrations.  In particular, scheduled_message_runs is created by 0003.
"""
revision="0001_initial"
down_revision=None
branch_labels=None
depends_on=None

from alembic import op
import app.db.models  # register models
from app.db.base import Base


def upgrade():
    bind = op.get_bind()
    # Do not create later-migration tables here.  Using checkfirst keeps this
    # safe for databases that already contain some objects.
    for table in Base.metadata.sorted_tables:
        if table.name == "scheduled_message_runs":
            continue
        table.create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "scheduled_message_runs":
            continue
        table.drop(bind=bind, checkfirst=True)
