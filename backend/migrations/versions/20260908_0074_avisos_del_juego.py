"""Push para jugadores del minijuego, con o sin cuenta

El canal de avisos de Intervalo no llega al juego, y hay tres cortes que lo
impiden. Este es el primero: `push_subscriptions.user_id` es NOT NULL, así que
un invitado no puede suscribirse — y el invitado es entre el 50% y el 95% de
cada cohorte del juego, según la semana.

Se agregan dos tablas propias en vez de aflojar las de Intervalo. Aquella tabla
además borra todas las demás filas del usuario en cada alta (un dispositivo por
persona, a propósito), así que tocarla para que acepte una clave nula arriesga
el canal que hoy sí funciona.

`game_push_subscriptions.endpoint` es único a secas y no por jugador: es un
navegador, y si alguien juega de invitado, se registra y vuelve a activar, la
fila tiene que mudarse de jugador en vez de duplicarse.

`game_notification_sends` es la gemela de `notification_sends`. Append-only, con
categoría y variante, y es además la idempotencia de los avisos reactivos: la
pregunta «¿ya se lo dije hoy?» se contesta consultándola.

Las columnas de `game_players` son las preferencias y el cupo DEL INVITADO.
Quien tiene cuenta usa las de `users`, que es lo que hace que el tope de tres
avisos por día sea de la persona y no de cada producto: el juego reclama contra
los contadores del usuario y Intervalo se queda sin cupo ese día, que es el
orden buscado.

Revision ID: 20260908_0074
Revises: 20260903_0073
Create Date: 2026-09-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260908_0074"
down_revision: Union[str, Sequence[str], None] = "20260903_0073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMNAS = (
    ("notify_enabled", sa.Boolean(), False, sa.text("false")),
    ("notify_time", sa.String(5), True, None),
    ("notify_timezone", sa.String(64), True, None),
    ("notify_last_sent_on", sa.Date(), True, None),
    ("notify_last_category", sa.String(30), True, None),
    ("notify_last_variant_key", sa.String(50), True, None),
    ("notify_events_on", sa.Date(), True, None),
    ("notify_events_count", sa.Integer(), False, sa.text("0")),
    ("notify_last_rank", sa.Integer(), True, None),
    ("referral_xp_push_seen", sa.Integer(), False, sa.text("0")),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tablas = set(inspector.get_table_names())

    existentes = {c["name"] for c in inspector.get_columns("game_players")}
    for nombre, tipo, nullable, default in COLUMNAS:
        if nombre not in existentes:
            op.add_column(
                "game_players",
                sa.Column(nombre, tipo, nullable=nullable, server_default=default),
            )

    if "game_push_subscriptions" not in tablas:
        op.create_table(
            "game_push_subscriptions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("player_id", sa.Integer(), nullable=False),
            sa.Column("endpoint", sa.String(1000), nullable=False),
            sa.Column("p256dh", sa.String(1000), nullable=False),
            sa.Column("auth", sa.String(1000), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["player_id"], ["game_players.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("endpoint", name="unique_game_endpoint"),
        )
        op.create_index(
            "ix_game_push_subscriptions_id", "game_push_subscriptions", ["id"]
        )
        op.create_index(
            "ix_game_push_subscriptions_player_id",
            "game_push_subscriptions",
            ["player_id"],
        )

    if "game_notification_sends" not in tablas:
        op.create_table(
            "game_notification_sends",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("player_id", sa.Integer(), nullable=False),
            sa.Column("category", sa.String(30), nullable=False),
            sa.Column("variant_key", sa.String(50), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("body", sa.String(500), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=False),
            sa.Column("delivery_status", sa.String(20), nullable=True),
            sa.Column("delivered_at", sa.DateTime(), nullable=True),
            sa.Column("opened_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["player_id"], ["game_players.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_game_notification_sends_id", "game_notification_sends", ["id"]
        )
        op.create_index(
            "idx_game_notification_sends_player",
            "game_notification_sends",
            ["player_id"],
        )
        op.create_index(
            "idx_game_notification_sends_sent_at",
            "game_notification_sends",
            ["sent_at"],
        )


def downgrade() -> None:
    op.drop_table("game_notification_sends")
    op.drop_table("game_push_subscriptions")
    for nombre, _tipo, _nullable, _default in reversed(COLUMNAS):
        op.drop_column("game_players", nombre)
