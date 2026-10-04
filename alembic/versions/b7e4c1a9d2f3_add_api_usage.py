"""add api_usage

Revision ID: b7e4c1a9d2f3
Revises: 3cda580f0bc4
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4c1a9d2f3'
down_revision: Union[str, Sequence[str], None] = '3cda580f0bc4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'api_usage',
        sa.Column('api', sa.String(), nullable=False),
        sa.Column('period', sa.String(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('api', 'period'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('api_usage')
