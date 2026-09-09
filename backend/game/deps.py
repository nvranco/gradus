"""Auth dual del minijuego: Clerk JWT o guest token (X-Game-Token).

Prioridad: Clerk gana. Si el JWT resuelve a un user sin jugador y además viene
un guest token válido, el jugador guest se linkea en el acto (es el caso
"volvió del OAuth de Google": conserva xp/alias/theta sin paso extra).
"""

from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_or_create_user_from_clerk, verify_clerk_token
from database import SessionLocal
from models import GamePlayer, User

from . import keyboard
import handles
import referrals as referrals_top

from .aliases import alias_for_user, generate_guest_alias, retire_alias

_CREATE_ATTEMPTS = 3


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_guest_token() -> str:
    return secrets.token_urlsafe(32)


def _clerk_user(authorization: str | None, db: Session) -> User | None:
    """Resuelve el user de Clerk o None. 401 solo si el header vino y es inválido."""
    if not authorization:
        return None
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    try:
        claims = verify_clerk_token(token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    try:
        return get_or_create_user_from_clerk(db, claims)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=503, detail="No pudimos crear tu cuenta. Probá de nuevo."
        )


def player_for_guest_token(db: Session, x_game_token: str | None) -> GamePlayer | None:
    if not x_game_token:
        return None
    return db.query(GamePlayer).filter(GamePlayer.guest_token == x_game_token).first()


def create_guest_player(db: Session) -> GamePlayer:
    """Crea un jugador guest con token y alias nuevos. Commitea."""
    for _ in range(_CREATE_ATTEMPTS):
        try:
            player = GamePlayer(
                guest_token=new_guest_token(),
                alias=generate_guest_alias(db),
                created_at=datetime.utcnow(),
                last_seen_at=datetime.utcnow(),
            )
            db.add(player)
            db.flush()
            # El @ del invitado se anota en el registro en la MISMA transacción
            # que la fila. Si se hiciera después, entre las dos otro alta podría
            # llevarse el nombre y quedarían dos personas con el mismo @, que es
            # justo lo que el registro existe para impedir. El IntegrityError del
            # índice único lo agarra el `except` de abajo y reintenta con otro.
            handles.reclamar(db, player.alias, player_id=player.id)
            db.commit()
            db.refresh(player)
            return player
        except IntegrityError:
            db.rollback()
    raise HTTPException(status_code=503, detail="No pudimos crear tu jugador. Probá de nuevo.")


def create_player_for_user(db: Session, user: User) -> GamePlayer:
    """Jugador para un user registrado sin jugador previo. Commitea."""
    for _ in range(_CREATE_ATTEMPTS):
        try:
            player = GamePlayer(
                user_id=user.id,
                alias=alias_for_user(db, user.username, user.name),
                created_at=datetime.utcnow(),
                last_seen_at=datetime.utcnow(),
            )
            db.add(player)
            db.flush()
            # Estrena jugador pero la persona ya existe: `reclamar` reconoce que
            # el @ es suyo si ya lo tenía como username y le suma la cara de
            # jugador a la MISMA fila, en vez de darle un segundo nombre.
            handles.reclamar(db, player.alias, user_id=user.id, player_id=player.id)
            db.commit()
            db.refresh(player)
            return player
        except IntegrityError:
            db.rollback()
            # Carrera consigo mismo (dos pestañas): la fila del ganador sirve.
            existing = db.query(GamePlayer).filter(GamePlayer.user_id == user.id).first()
            if existing:
                return existing
    raise HTTPException(status_code=503, detail="No pudimos crear tu jugador. Probá de nuevo.")


def _descontar_lo_autogenerado(reclutador: GamePlayer, user: User) -> None:
    """Saca de la deuda lo que esta persona se generó a sí misma. No commitea.

    Las tres guardas contra autoreclutarse cubren el ESTADO, no la historia. Hay
    un camino que las esquiva a las tres: juego de invitado → comparto mi link →
    lo abro yo → me anoto en clásico (la guarda de `anotar_usuario` pasa porque
    el jugador todavía no tiene `user_id`) → estudio, y cada respuesta acumula el
    10% en `classic_xp_owed` (la guarda de runtime tampoco dispara: sigue sin
    `user_id`). Al volver al juego logueado, la arista se limpia... y acto seguido
    se paga toda esa deuda.

    Lo autogenerado es exactamente `user.referral_xp_given`: esa columna cuenta
    lo que ESTA persona le dio a quien la trajo, y quien la trajo era ella misma.
    Se descuenta solo eso y no la deuda entera, porque el mismo invitado puede
    haber traído gente de verdad, y eso sí lo ganó.
    """
    propio = user.referral_xp_given or 0
    if propio <= 0:
        return
    reclutador.classic_xp_owed = max(0, (reclutador.classic_xp_owed or 0) - propio)
    # La arista ya no existe, así que la contabilidad de ese lado tampoco: dejarla
    # haría que la vista de Reclutas siga mostrando un aporte sin reclutador.
    user.referral_xp_given = 0
    user.referral_pending = 0


def link_guest_to_user(db: Session, guest: GamePlayer, user: User) -> GamePlayer:
    """Merge guest→user. Commitea. Devuelve el jugador vigente.

    Idempotente solo en el caso fácil: si el invitado YA es de este usuario se
    devuelve tal cual. Si hay que fusionar, no lo es —la fila del invitado se
    borra— así que llamar dos veces con el mismo invitado da 401 la segunda,
    porque el token ya no resuelve a nadie.
    """
    if guest.user_id == user.id:
        return guest
    if guest.user_id is not None:
        # El token pertenece a otro usuario registrado: no se transfiere.
        raise HTTPException(status_code=409, detail="Ese progreso ya pertenece a otra cuenta.")

    # Los avisos que la persona configuró de invitada se mudan a su cuenta antes
    # de que el jugador deje de ser el titular del cupo: a partir de acá el
    # resolutor lee las preferencias de `users` (ver
    # game/notifications.py :: titular_del_cupo), así que sin esto registrarse
    # apagaría en silencio los recordatorios que acababa de prender.
    from . import notifications as avisos

    avisos.copiar_preferencias_al_usuario(guest, user)

    existing = db.query(GamePlayer).filter(GamePlayer.user_id == user.id).first()
    if existing is None:
        guest.user_id = user.id
        db.flush()
        # Si esta persona se había anotado a sí misma como recluta —jugó de
        # invitada, compartió su link y lo abrió ella— la arista queda apuntando
        # al jugador que ACABA de volverse suyo. Limpiarla acá es la primera de
        # las tres guardas; la de runtime en `acreditar_clasico` es la que cierra
        # el caso incluso para aristas creadas antes de que esto existiera.
        if user.referred_by_player_id == guest.id:
            user.referred_by_player_id = None
            _descontar_lo_autogenerado(guest, user)
        # Y cobra lo que ya había generado sin tener dónde: es el mejor argumento
        # para registrarse, y por eso se paga en el mismo momento.
        referrals_top.saldar_deuda_de_clasico(db, guest, user.id)
        # Las dos caras de la persona pasan a ser UNA en el registro, y gana el @
        # del JUEGO: es el que vio en pantalla, el que compartió y bajo el que la
        # conocen en el ranking. El username con el que Clerk dio de alta la
        # cuenta se retira, pero no se pierde — sigue siendo suyo y sus links
        # siguen resolviendo (ver backend/handles.py :: vincular).
        handles.vincular(db, user_id=user.id, player_id=guest.id)
        db.commit()
        db.refresh(guest)
        return guest

    # El user ya tenía jugador (jugó registrado en otro dispositivo): sobrevive
    # esa fila; se suman contadores y gana el Elo con más evidencia.
    from models import (  # import local, evita ciclo
        GameAliasHistory,
        Handle,
        GameAttempt,
        GameBoostIntent,
        GameCtaEvent,
        GameEvent,
        GameExercise,
        GameMessage,
    )

    existing.xp += guest.xp
    existing.exercises_correct += guest.exercises_correct
    existing.exercises_attempted += guest.exercises_attempted
    existing.best_combo = max(existing.best_combo, guest.best_combo)
    if guest.best_rank is not None:
        existing.best_rank = (
            guest.best_rank
            if existing.best_rank is None
            else min(existing.best_rank, guest.best_rank)
        )
    if guest.n_updates > existing.n_updates:
        existing.theta = guest.theta
        existing.n_updates = guest.n_updates
    if existing.university is None:
        existing.university = guest.university
    if existing.career is None:
        existing.career = guest.career
    if existing.first_group_id is None:
        existing.first_group_id = guest.first_group_id
    if existing.first_utm_source is None:
        existing.first_utm_source = guest.first_utm_source
    # Quién trajo a cada uno, y lo que cada uno ya pagó. Si la cuenta todavía no
    # tenía reclutador se queda con el del invitado —es el que efectivamente
    # entró por el link de alguien— y lo aportado se suma, porque son dos tramos
    # de la misma deuda con la misma persona.
    if existing.referred_by is None and guest.referred_by != existing.id:
        existing.referred_by = guest.referred_by
        existing.referral_pending = guest.referral_pending
    existing.referral_xp_given += guest.referral_xp_given
    # El teclado se UNE, no se elige uno de los dos. Es progresión ganada
    # resolviendo derivadas —cada tecla apareció porque una la exigía— y perderla
    # justo al registrarse castiga exactamente el paso que se quiere fomentar.
    existing.unlocked_keys = keyboard.serialize(
        keyboard.parse_unlocked(existing.unlocked_keys)
        | keyboard.parse_unlocked(guest.unlocked_keys)
    )

    # TODAS las tablas que apuntan al invitado, no solo las dos del progreso.
    #
    # El feed, las métricas de CTA y las intenciones de donación también lo
    # referencian, y quedaban colgadas apuntando a una fila borrada. En la base
    # de producción eso pasaba en silencio —las migraciones que crearon esas tres
    # tablas se olvidaron la clave foránea que models.py sí declara— pero en una
    # base armada con create_all, que es la que usan los scripts de chequeo, el
    # borrado levanta IntegrityError desde adentro de get_current_player, o sea
    # en CUALQUIER endpoint y en bucle.
    # `GameMessage` va en la lista aunque hoy no pueda tener filas de un
    # invitado —escribir pide cuenta— porque el día que esa regla se afloje, el
    # síntoma de haberlo olvidado no es un chat roto: es IntegrityError adentro de
    # get_current_player, o sea el juego entero caído.
    for tabla in (
        GameExercise,
        GameAttempt,
        GameEvent,
        GameCtaEvent,
        GameBoostIntent,
        GameMessage,
    ):
        db.query(tabla).filter(tabla.player_id == guest.id).update(
            {"player_id": existing.id}, synchronize_session=False
        )
    # Los RECLUTAS del invitado pasan a ser los de la cuenta. Sin esto,
    # registrarse borraría de un plumazo a toda la gente que trajiste —quedaban
    # apuntando a una fila que se está por borrar— y con ella la única razón por
    # la que alguien compartió el link.
    db.query(GamePlayer).filter(GamePlayer.referred_by == guest.id).update(
        {"referred_by": existing.id}, synchronize_session=False
    )
    # Lo mismo del lado de clásico: los usuarios que entraron por el link del
    # invitado pasan a apuntar a la fila que sobrevive.
    #
    # El `!= user.id` no es paranoia: sin él, alguien que se anotó a sí mismo con
    # su propio link queda apuntándose, y desde ahí cobra 10% de su propia XP
    # para siempre. Es la misma guarda que la rama simple, y la única razón por
    # la que hay tres: cada una tapa un camino distinto al mismo agujero.
    db.query(User).filter(
        User.referred_by_player_id == guest.id, User.id != user.id
    ).update({"referred_by_player_id": existing.id}, synchronize_session=False)
    if user.referred_by_player_id == guest.id:
        db.query(User).filter(User.id == user.id).update(
            {"referred_by_player_id": None}, synchronize_session=False
        )
        # Misma resta que en la rama simple, y ANTES del traspaso: si la deuda
        # autogenerada pasa a `existing`, se paga igual un renglón más abajo.
        _descontar_lo_autogenerado(guest, user)
    # La deuda de clásico del invitado se traspasa ANTES del delete, o se pierde
    # con la fila. Se suma a la de la cuenta que sobrevive, que puede tener la
    # suya.
    if (guest.classic_xp_owed or 0) > 0:
        db.query(GamePlayer).filter(GamePlayer.id == existing.id).update(
            {GamePlayer.classic_xp_owed: GamePlayer.classic_xp_owed + guest.classic_xp_owed},
            synchronize_session=False,
        )
        guest.classic_xp_owed = 0
    db.flush()
    # El UPDATE de arriba es `synchronize_session=False`, así que NO tocó el
    # objeto en memoria: `existing.classic_xp_owed` todavía tiene el valor previo
    # al traspaso. Sin expirarlo, `saldar_deuda_de_clasico` lee ese valor viejo
    # —casi siempre 0— y no paga nada; y como solo se la llama al fusionar, esa
    # XP no se vuelve a mirar nunca. Se pierde sin dejar rastro.
    db.expire(existing, ["classic_xp_owed"])
    referrals_top.saldar_deuda_de_clasico(db, existing, user.id)
    # Y el @ del invitado, que en un segundo deja de existir, queda apuntando a
    # la cuenta: los links que se mandaron con él siguen trayendo gente para la
    # misma persona (ver models.GameAliasHistory). Junto con los @ que el
    # invitado ya hubiera soltado antes.
    db.query(GameAliasHistory).filter(GameAliasHistory.player_id == guest.id).update(
        {"player_id": existing.id}, synchronize_session=False
    )
    # Lo mismo en el registro, y en TRES pasos con este orden exacto.
    #
    # El @ que sobrevive es el de `existing`, no el del invitado: la fila del
    # invitado se borra en un segundo. Así que primero hay que RETIRAR el @
    # activo del invitado y recién después reapuntar sus filas, o la cuenta
    # quedaría un instante con dos @ activos y el índice parcial lo rebota.
    activo_del_invitado = handles.activo_de_jugador(db, guest.id)
    if activo_del_invitado is not None:
        activo_del_invitado.status = "retired"
        activo_del_invitado.released_at = datetime.utcnow()
        db.flush()
    # Ahora sí: los @ del invitado —el que usaba y los que ya había soltado—
    # pasan a la cuenta que sobrevive. Va ANTES del delete: reapuntarlos después
    # los dejaría colgando de un jugador que ya no existe, y con ellos morirían
    # los links `?r=` de esa persona.
    db.query(Handle).filter(Handle.player_id == guest.id).update(
        {"player_id": existing.id}, synchronize_session=False
    )
    retire_alias(db, guest.alias, existing.id)
    db.flush()
    handles.reclamar(db, existing.alias, user_id=user.id, player_id=existing.id)
    db.delete(guest)
    db.commit()
    db.refresh(existing)
    return existing


def lock_player(db: Session, player: GamePlayer) -> GamePlayer:
    """Vuelve a leer la fila del jugador tomando su candado, para los endpoints
    que le suman cosas.

    Sin esto, `/answer` y `/skip` leen los contadores en Python y los escriben de
    vuelta con el valor ya calculado (`SET xp = 125`, no `SET xp = xp + 25`). Con
    dos respuestas en vuelo —un doble toque, o el reintento que dispara el
    teléfono cuando la primera tardó demasiado— las dos leen 100, las dos
    escriben 125, y una recompensa entera desaparece. Lo mismo con los ejercicios
    resueltos, los intentos y la racha, que además puede ir para atrás.

    Es un candado de FILA: solo se serializan las respuestas de un mismo jugador,
    que es algo que igual pasa de a una. En SQLite el dialecto lo ignora, así que
    los scripts de chequeo siguen andando igual.

    La simulación ya lo hacía bien para los bots (simulation.py, con incrementos
    del lado de SQL); el camino humano no.
    """
    return (
        db.query(GamePlayer)
        .filter(GamePlayer.id == player.id)
        .with_for_update()
        .one()
    )


def get_current_player(
    authorization: str = Header(None),
    x_game_token: str = Header(None),
    db: Session = Depends(get_db),
) -> GamePlayer:
    user = _clerk_user(authorization, db)
    if user is not None:
        # Mismo criterio que `router._jugador_del_usuario`, repetido acá porque
        # el router importa de este módulo y no al revés. Si cambia uno tiene que
        # cambiar el otro: lo único que los diferencia a propósito es que el del
        # router además anuncia el registro en el feed, y este no puede hacerlo
        # porque corre en TODOS los endpoints, no solo al entrar.
        player = db.query(GamePlayer).filter(GamePlayer.user_id == user.id).first()
        if player is not None:
            return player
        guest = player_for_guest_token(db, x_game_token)
        if guest is not None and guest.user_id is None:
            return link_guest_to_user(db, guest, user)
        # Token ausente o de otro usuario: jugador propio nuevo.
        return create_player_for_user(db, user)

    guest = player_for_guest_token(db, x_game_token)
    if guest is not None:
        return guest
    raise HTTPException(status_code=401, detail="Jugador no encontrado")
