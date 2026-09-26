"""persist scheduled message run state"""
revision='0003_scheduled_message_runs'; down_revision='0002_scheduler_media'; branch_labels=None; depends_on=None
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table(
        'scheduled_message_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('schedule_id', sa.Integer(), sa.ForeignKey('scheduled_messages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(24), nullable=False, server_default='running'),
        sa.Column('sent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_scheduled_message_runs_schedule_id', 'scheduled_message_runs', ['schedule_id'])
    op.create_index('ix_scheduled_message_runs_scheduled_for', 'scheduled_message_runs', ['scheduled_for'])
    op.create_index('ix_scheduled_message_runs_status', 'scheduled_message_runs', ['status'])
    op.create_index('uq_schedule_scheduled_for', 'scheduled_message_runs', ['schedule_id', 'scheduled_for'], unique=True)

def downgrade():
    op.drop_index('uq_schedule_scheduled_for', table_name='scheduled_message_runs')
    op.drop_index('ix_scheduled_message_runs_status', table_name='scheduled_message_runs')
    op.drop_index('ix_scheduled_message_runs_scheduled_for', table_name='scheduled_message_runs')
    op.drop_index('ix_scheduled_message_runs_schedule_id', table_name='scheduled_message_runs')
    op.drop_table('scheduled_message_runs')
