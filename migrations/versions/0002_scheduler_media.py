"""add media fields for broadcasts and schedules"""
revision='0002_scheduler_media'; down_revision='0001_initial'; branch_labels=None; depends_on=None
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

def upgrade():
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if 'broadcasts' in tables:
        cols = {c['name'] for c in inspector.get_columns('broadcasts')}
        if 'media_file_id' not in cols: op.add_column('broadcasts', sa.Column('media_file_id', sa.Text(), nullable=True))
        if 'caption' not in cols: op.add_column('broadcasts', sa.Column('caption', sa.Text(), nullable=True))
    if 'scheduled_messages' in tables:
        cols = {c['name'] for c in inspector.get_columns('scheduled_messages')}
        if 'media_file_id' not in cols: op.add_column('scheduled_messages', sa.Column('media_file_id', sa.Text(), nullable=True))
        if 'caption' not in cols: op.add_column('scheduled_messages', sa.Column('caption', sa.Text(), nullable=True))

def downgrade():
    inspector = inspect(op.get_bind())
    if 'scheduled_messages' in inspector.get_table_names():
        cols={c['name'] for c in inspector.get_columns('scheduled_messages')}
        if 'caption' in cols: op.drop_column('scheduled_messages','caption')
        if 'media_file_id' in cols: op.drop_column('scheduled_messages','media_file_id')
    if 'broadcasts' in inspector.get_table_names():
        cols={c['name'] for c in inspector.get_columns('broadcasts')}
        if 'caption' in cols: op.drop_column('broadcasts','caption')
        if 'media_file_id' in cols: op.drop_column('broadcasts','media_file_id')
