"""freeze payment details on deposit requests"""
from alembic import op
import sqlalchemy as sa

revision = "0004_deposit_payment_snapshot"
down_revision = "0003_scheduled_message_runs"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    cols = {c['name'] for c in sa.inspect(bind).get_columns('deposits')}
    additions = [
        ('payment_method_name_snapshot', sa.String(120), ''),
        ('payment_country_snapshot', sa.String(120), ''),
        ('payment_currency_snapshot', sa.String(16), ''),
        ('payment_details_snapshot', sa.Text(), ''),
        ('payment_instructions_snapshot', sa.Text(), ''),
    ]
    for name, typ, default in additions:
        if name not in cols:
            op.add_column('deposits', sa.Column(name, typ, nullable=False, server_default=default))


def downgrade():
    bind = op.get_bind()
    cols = {c['name'] for c in sa.inspect(bind).get_columns('deposits')}
    for name in ['payment_instructions_snapshot','payment_details_snapshot','payment_currency_snapshot','payment_country_snapshot','payment_method_name_snapshot']:
        if name in cols:
            op.drop_column('deposits', name)
