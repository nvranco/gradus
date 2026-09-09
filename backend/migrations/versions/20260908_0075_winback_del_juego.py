"""Marcador del mail de vuelta del minijuego

`users.winback_email_sent_at` no sirve para esto: son dos productos distintos y
quien dejó de derivar puede seguir estudiando en Intervalo, donde no hay nada que
recuperar. Con un solo marcador, mandar uno apagaba el otro.

Revision ID: 20260908_0075
Revises: 20260908_0074
Create Date: 2026-09-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_0075"
down_revision: Union[str, Sequence[str], None] = "20260908_0074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("game_players")}
    if "winback_email_sent_at" not in existing:
        op.add_column(
            "game_players", sa.Column("winback_email_sent_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("game_players", "winback_email_sent_at")
