"""m6: per-project motion continuity

How one shot's clip is joined to the next. Defaults to 'none', which is
exactly what every existing project already does, so this migration changes
no film that has already been rendered.

Revision ID: b1c4e9d20f31
Revises: 5c272053b67a
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c4e9d20f31'
down_revision: Union[str, Sequence[str], None] = '5c272053b67a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('projects', sa.Column(
        'motion_continuity', sa.String(length=16),
        nullable=False, server_default='none'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('projects', 'motion_continuity')
