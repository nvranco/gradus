"""Empuje automático por aforo: 10 personas nuevas de una universidad en un día.

El empuje de cafecito premia a quien pone plata; este premia a quien trae gente.
Cuando entra la persona número 10 de una universidad en el mismo día, esa
universidad se lleva un ×1,5 de dos horas, sin que nadie done nada.

**Qué cuenta como una persona nueva.** La suma de los dos productos, que es lo
que hace que "entrar a Intervalo" signifique una sola cosa:

  - un jugador nuevo del minijuego que carga su universidad, y
  - un alta de Intervalo clásico que se inscribe en esa universidad.

Se cuentan PERSONAS y no filas: quien juega de invitado y después se registra
deja una fila en `game_players` y otra en `users`, y es una sola persona. La
identidad ya está unificada por `game_players.user_id`, así que el que tiene
cuenta se cuenta por su usuario y el invitado por su jugador. Sin esto, alguien
que hace las dos cosas el mismo día valdría por dos y el umbral sería de seis
personas y media.

**Por qué el disparo es al entrar y no un job.** "Cuando entra la persona 10"
tiene que sentirse en el momento: el empuje dura dos horas y lo que lo hace
valer es que la gente que acaba de llegar lo aproveche. Un cron cada quince
minutos convertiría el premio en algo que aparece solo.

**Por qué no se puede disparar dos veces.** `external_ref` es UNIQUE y acá vale
`aforo:<sigla>:<día>`, así que la segunda llamada del día devuelve None sin
insertar nada. Eso cubre las tres formas de repetirlo: la persona 11, dos altas
simultáneas, y el reintento de un POST que en realidad había llegado. No hace
falta candado ni columna de estado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Enrollment, GameBoost, GamePlayer, User
from universities import canonical_university

from . import boosts, events

# Cuántas personas nuevas hacen falta. Fijo para todas las universidades: es el
# número que se puede decir en una frase ("diez y se prende"), y un umbral
# proporcional al padrón obliga a explicar por qué la de al lado necesita otro.
UMBRAL_PERSONAS = 10

# Dos horas. Corto a propósito, y es la diferencia con el cafecito: el de
# aforo no se compra, se junta esa misma tarde, así que tiene que gastarse esa
# misma tarde. Un empuje de aforo de 24 h le comería el lugar al pago.
BOOST_MINUTOS = 120

# ×1,5 escrito en la moneda del motor. Todo el cálculo del multiplicador está
# denominado en cafecitos (`boosts.CAFECITO_STEP`), así que un empuje que no se
# pagó con cafecitos igual tiene que decir cuántos "vale" para sumar con los que
# sí. Cinco décimos = ×1,5. Si algún día cambia CAFECITO_STEP, esto se recalcula
# solo y el ×1,5 se mantiene.
MULTIPLICADOR = 1.5
CAFECITOS_EQUIVALENTES = round((MULTIPLICADOR - 1.0) / boosts.CAFECITO_STEP)

# El sello de la fila. Separa este empuje de una donación en TODAS las lecturas
# que preguntan "¿alguien donó?" — el mail de agradecimiento, el conteo de
# cafecitos del cartel — sin que ninguna tenga que adivinarlo por el
# `donor_name` vacío.
SOURCE = "aforo"

# El día es el de acá, no el del servidor. Mismo huso que usa el resto del
# juego para decir "hoy" (game/router.py :: _TZ_JUEGO): quien juega desde otro
# huso comparte el día con el público al que apunta el ranking.
_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def dia_de(now: datetime) -> date:
    """El día calendario argentino de un instante naive-UTC."""
    return now.replace(tzinfo=ZoneInfo("UTC")).astimezone(_TZ).date()


def _ventana(now: datetime) -> tuple[datetime, datetime]:
    """El [desde, hasta) en UTC naive que cubre el día argentino de `now`."""
    hoy = dia_de(now)
    desde = datetime.combine(hoy, datetime.min.time(), tzinfo=_TZ)
    return (
        desde.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        (desde + timedelta(days=1)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
    )


def referencia_de(university: str, now: datetime) -> str:
    """La `external_ref` del empuje de esa universidad ese día."""
    return f"{SOURCE}:{university}:{dia_de(now).isoformat()}"


def personas_nuevas_hoy(db: Session, university: str, now: datetime | None = None) -> int:
    """Personas NUEVAS de esa universidad hoy, sumando los dos productos.

    Dos consultas y no un `UNION` a mano porque las dos identidades viven en
    tablas distintas y hay que restar la intersección: quien se registró hoy Y
    tiene jugador es una persona sola. Se cuenta desde el lado del usuario —que
    es la identidad fuerte— y del lado del juego solo los jugadores que TODAVÍA
    no tienen cuenta.
    """
    now = now or datetime.utcnow()
    desde, hasta = _ventana(now)

    altas = (
        db.query(func.count(func.distinct(User.id)))
        .join(Enrollment, Enrollment.user_id == User.id)
        .filter(
            User.created_at >= desde,
            User.created_at < hasta,
            Enrollment.university == university,
        )
        .scalar()
        or 0
    )
    # `user_id IS NULL` es la resta de la intersección: si ese jugador ya tiene
    # cuenta, o bien la cuenta es de hoy y ya lo contó `altas`, o es de antes y
    # entonces la persona no es nueva. En los dos casos no suma acá.
    invitados = (
        db.query(func.count(GamePlayer.id))
        .filter(
            GamePlayer.created_at >= desde,
            GamePlayer.created_at < hasta,
            GamePlayer.university == university,
            GamePlayer.user_id.is_(None),
            GamePlayer.is_bot.is_(False),
        )
        .scalar()
        or 0
    )
    return int(altas) + int(invitados)


def ya_se_dio_hoy(db: Session, university: str, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return (
        db.query(GameBoost.id)
        .filter(GameBoost.external_ref == referencia_de(university, now))
        .first()
        is not None
    )


def revisar(
    db: Session, university: str | None, now: datetime | None = None
) -> GameBoost | None:
    """¿Esta persona fue la número 10? Si sí, prende el empuje.

    Devuelve el empuje recién creado, o None — que es lo normal, porque son
    nueve de cada diez llamadas. No commitea: el endpoint que llama es dueño de
    su transacción, igual que `boosts.grant`.

    Nunca levanta: esto cuelga del camino del alta y del de cargar la
    universidad, y ninguno de los dos puede fallar porque el premio no salió.
    Un empuje que no se prende es una lástima; un alta que devuelve 500 es un
    usuario perdido.
    """
    try:
        if not university:
            return None
        uni = (canonical_university(university) or "").strip()
        if not uni:
            return None
        now = now or datetime.utcnow()
        # El barato primero: una sola consulta por índice, y corta el 100% de
        # las llamadas del resto del día una vez que ya se dio.
        if ya_se_dio_hoy(db, uni, now):
            return None
        personas = personas_nuevas_hoy(db, uni, now)
        if personas < UMBRAL_PERSONAS:
            return None
        boost = boosts.grant(
            db,
            university=uni,
            cafecitos=CAFECITOS_EQUIVALENTES,
            donor_name=None,
            source=SOURCE,
            external_ref=referencia_de(uni, now),
            minutes=BOOST_MINUTOS,
            now=now,
            anunciar=False,
        )
        if boost is None:
            # Otra request ganó la carrera en el mismo instante. No es un error:
            # el UNIQUE hizo exactamente su trabajo.
            return None
        events.on_aforo(
            db,
            university=uni,
            personas=personas,
            multiplier=boosts.multiplier_for(db, uni, now=now),
            horas=BOOST_MINUTOS // 60,
        )
        return boost
    except Exception:  # noqa: BLE001 - ver el docstring
        import logging

        logging.getLogger(__name__).exception("aforo: no se pudo revisar %s", university)
        return None
