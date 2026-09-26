"""${message}"""
revision = '${up_revision}'
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}
from alembic import op
import sqlalchemy as sa
${upgrades if upgrades else 'def upgrade():\n    pass'}
${downgrades if downgrades else 'def downgrade():\n    pass'}
