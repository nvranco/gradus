"""Endpoints del minijuego de derivadas — primer APIRouter del repo.

main.py solo hace `app.include_router(game_router)`; todo el bounded context
vive en este paquete. La correctitud la decide el SERVER (validación numérica
contra la derivada esperada), a diferencia de las sesiones de Intervalo donde
la reporta el cliente.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import and_ as sa_and, case, func, or_ as sa_or
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

from models import GameAttempt, GameCtaEvent, GameExercise, GamePlayer, User
from universities import UNIVERSITIES as _UNIVERSIDADES, canonical_university
import handles
import xp_boost
from usernames import normalize_username, validate_username

from . import boosts
from . import chat as game_chat
from . import limits
from . import ranking
from . import elo
from . import events as game_events
from . import explain as game_explain
from . import keyboard as game_keyboard
from . import referrals
from . import simulation
from . import stats as game_stats
from . import xp as game_xp
from .aliases import alias_taken, retire_alias
from .deps import (
    create_guest_player,
    create_player_for_user,
    get_current_player,
    get_db,
    link_guest_to_user,
    lock_player,
    player_for_guest_token,
    _clerk_user,
)
from .generator import get_or_create_stat, serve_exercise, template_for
from .mathjson import MathJsonError, to_sympy
from .schemas import (
    GameNotificationSettings,
    GameNotificationSettingsRequest,
    GamePushSubscribeRequest,
    GamePushUnsubscribeRequest,
    GameAnswerRequest,
    GameAnswerResponse,
    GameBoostOut,
    GameCafecitoStatus,
    GameCtaRequest,
    GameEventOut,
    GameEventsResponse,
    GameExerciseOut,
    GameExplainOut,
    GameExplainRequest,
    GameLeaderboardEntry,
    GameLeaderboardMe,
    GameLeaderboardResponse,
    GameLeaderboardSummary,
    GameMessageIn,
    GameMessageOut,
    GamePulse,
    GamePlayerCreateRequest,
    GamePlayerCreateResponse,
    GamePlayerOut,
    GameProfilePatchRequest,
    GameRecruitEntry,
    GameRecruitsResponse,
    GameSkipRequest,
    GameStatsOut,
    GameUniversityLeaderboardResponse,
    GameUniversityRow,
)
from .templates import GENERIC_FEEDBACK, latex_es
from .validator import (
    AnswerRejected,
    expr_from_stored,
    guard_candidate,
    match_common_error,
    numerically_equivalent,
)

router = APIRouter(prefix="/game/derivemos", tags=["game"])

# Los intentos son los que la persona quiera: se responde hasta acertar o
# saltear. Este número NO es una regla de juego, es un tope de tabla — cada
# intento escribe una fila en `game_attempts` y sin ningún techo un bucle la
# llena. Nadie que esté jugando lo va a tocar: cincuenta intentos sobre la misma
# derivada ya no son alguien insistiendo.
TOPE_DE_INTENTOS = 50
AROUND_WINDOW = 15

# Mismas regex que /user/enroll (main.py): lo que no matchea se descarta para
# no crear cohortes fantasma.
_GROUP_ID_RE = re.compile(r"[a-z]{2,6}\d{1,5}")
_UTM_RE = re.compile(r"[a-z]{2,20}")

_KNOWN_CAREERS = ("E", "S", "T", "M")

# Las noventa siglas del catálogo, para reconocer lo que ya está en la lista.
_SIGLAS_CONOCIDAS = frozenset(sigla for sigla, _ in _UNIVERSIDADES)

def _chat_habilitado() -> bool:
    """El interruptor del chat, apagado por defecto.

    Opt-in y no opt-out, igual que los endpoints de desarrollo (main.py): esto
    abre la única puerta del juego por la que entra texto de una persona a la
    pantalla de todas las demás, y una feature así no puede quedar prendida
    porque nadie se acordó de apagarla. Se prende poniendo GAME_CHAT_ENABLED=1
    cuando haya alguien mirando.

    Solo frena la ESCRITURA. Leer anda siempre: con el chat apagado no entran
    mensajes nuevos, y los que ya estaban no dejan de existir por eso.
    """
    return os.getenv("GAME_CHAT_ENABLED", "").strip().lower() in {"1", "true", "yes"}


# Qué puede tener una universidad escrita a mano. El catálogo tiene noventa
# siglas, pero el campo "Otra" existe a propósito: alguien de una universidad
# chica tiene que poder anotarla, y cerrarlo al catálogo sería dejarlo afuera del
# juego. Así que se acepta texto libre, pero SANEADO.
#
# Importa más de lo que parece porque este es el único lugar del juego donde algo
# que escribe una persona se convierte en contenido compartido: la universidad
# aparece en el desplegable de filtros de TODOS los jugadores
# (/leaderboard/summary lo arma con un DISTINCT sobre esta columna), en las filas
# del ranking, en el ranking de universidades y en el feed de novedades.
#
# Letras (con acentos y ñ), dígitos, espacios y los cuatro signos que aparecen en
# nombres reales. Eso deja afuera enlaces, marcado, emojis y bloques de texto,
# que es de lo que se trata. Cuarenta caracteres: la sigla más larga del catálogo
# tiene ocho y el nombre más largo entra cómodo.
_UNIVERSIDAD_RE = re.compile(r"^[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ .&-]+$")
_MAX_UNIVERSIDAD = 40

# Hasta acá se guarda el LaTeX crudo de una respuesta. El esquema ya rechaza más
# que esto en la puerta (schemas._MAX_LATEX); esto es el recorte de la columna.
_MAX_LATEX_GUARDADO = 2000


def _universidad_aceptable(texto: str | None) -> str | None:
    """La universidad tal como se guarda, o None para vaciarla."""
    if not texto:
        return None
    # Los espacios repetidos se colapsan: "UBA        " y "U  B  A" son formas de
    # ocupar más lugar del que corresponde en una lista compartida.
    limpio = " ".join(texto.split())
    if limpio in _SIGLAS_CONOCIDAS:
        return limpio
    if len(limpio) > _MAX_UNIVERSIDAD or not _UNIVERSIDAD_RE.fullmatch(limpio):
        raise HTTPException(
            status_code=422,
            detail="Escribí el nombre o la sigla de tu universidad, sin símbolos raros.",
        )
    # Tiene que decir algo, no ser solo signos y espacios.
    if not any(c.isalpha() for c in limpio):
        raise HTTPException(status_code=422, detail="Eso no parece una universidad.")
    return limpio

# Plataformas que el cliente puede declarar en X-Game-Platform, espejo del tipo
# `Platform` de web/src/lib/platform/detect.ts. Cerrado a propósito: lo que no
# esté acá se guarda como NULL y el panel lo muestra como «sin dato», que es
# mejor que una categoría fantasma con un typo adentro.
_PLATFORMS = ("ios", "android", "desktop")


def _platform(raw: str | None) -> str | None:
    return raw if raw in _PLATFORMS else None

# Bucket de carrera, espejo de `_career_bucket_sql` en main.py: la carrera si es
# conocida, "Otra" en cualquier otro caso (incluido NULL).
_CAREER_BUCKET = case((GamePlayer.career.in_(_KNOWN_CAREERS), GamePlayer.career), else_="Otra")


def _scope_filters(university: str | None, career: str | None) -> list:
    """Filtros de scope del ranking, iguales a los del leaderboard principal."""
    filters = []
    if university is not None:
        filters.append(GamePlayer.university == university)
    if career is not None:
        filters.append(_CAREER_BUCKET == career)
    return filters


# `RESOLVIO_ACA`, `ORDEN_XP` y `ORDEN_ELO` viven en game/ranking.py: los usan
# también el feed de eventos y las estadísticas, y tienen que moverse juntos.
RESOLVIO_ACA = ranking.RESOLVIO_ACA


def _rank_of(db: Session, player: GamePlayer, scope: list | None = None) -> int:
    """Puesto 1-based en el orden canónico (xp DESC, id ASC). Los que nunca
    resolvieron una derivada no compiten (espejo del leaderboard principal)."""
    ahead = (
        db.query(GamePlayer.id)
        .filter(
            *(scope or []),
            RESOLVIO_ACA,
            sa_or(
                GamePlayer.xp > player.xp,
                sa_and(GamePlayer.xp == player.xp, GamePlayer.id < player.id),
            ),
        )
        .count()
    )
    return ahead + 1


_CALIFICADO = ranking.CALIFICADO
_ORDEN_ELO = ranking.ORDEN_ELO
_ORDEN_XP = ranking.ORDEN_XP


def _rank_of_elo(db: Session, player: GamePlayer, scope: list | None = None) -> int:
    """Puesto 1-based en el orden por Elo.

    Cuenta cuántos van DELANTE con exactamente la misma regla que `_ORDEN_ELO`,
    no con una parecida: si las dos se separan, la ventana `around_me` se centra
    en una fila que no es la propia.
    """
    mio = 1 if player.n_updates >= elo.RAMP_UPDATES else 0
    ahead = (
        db.query(GamePlayer.id)
        .filter(
            *(scope or []),
            RESOLVIO_ACA,
            sa_or(
                _CALIFICADO > mio,
                sa_and(
                    _CALIFICADO == mio,
                    sa_or(
                        GamePlayer.theta > player.theta,
                        sa_and(
                            GamePlayer.theta == player.theta,
                            GamePlayer.id < player.id,
                        ),
                    ),
                ),
            ),
        )
        .count()
    )
    return ahead + 1


def _player_out(db: Session, player: GamePlayer, with_rank: bool = True) -> GamePlayerOut:
    return GamePlayerOut(
        player_id=player.id,
        alias=player.alias,
        xp=player.xp,
        rank=_rank_of(db, player) if with_rank else None,
        combo=player.current_combo,
        best_combo=player.best_combo,
        best_rank=player.best_rank,
        exercises_correct=player.exercises_correct,
        exercises_attempted=player.exercises_attempted,
        university=player.university,
        career=player.career,
        is_guest=player.user_id is None,
        alias_is_generated=player.alias_is_generated,
        level=elo.level_of(player.theta),
        elo=elo.rating_of(player.theta),
    )


def _persist_attribution(
    player: GamePlayer,
    group_id: str | None,
    utm_source: str | None,
    platform: str | None = None,
) -> None:
    """Todo lo de PRIMER contacto, y solo si está vacío.

    La plataforma vive acá y no en un lugar propio porque tiene exactamente la
    misma regla que el grupo y la fuente: se escribe una vez y no se pisa. Quien
    empezó en el celular vino del celular, aunque después siga en la compu — lo
    que hace después se lee en `game_exercises.platform`."""
    if player.first_group_id is None and group_id and _GROUP_ID_RE.fullmatch(group_id):
        player.first_group_id = group_id
    if player.first_utm_source is None and utm_source and _UTM_RE.fullmatch(utm_source):
        player.first_utm_source = utm_source
    if player.platform is None and platform:
        player.platform = platform


def _jugador_del_usuario(
    db: Session,
    user: User,
    x_game_token: str | None,
    referrer_alias: str | None = None,
) -> GamePlayer:
    """El jugador de un usuario registrado, fusionando el invitado si hay uno.

    Esta resolución estaba escrita TRES veces —acá, en el alta y en el link
    explícito— y las tres se comportaban distinto. La diferencia que importaba:
    solo una llamaba a `on_signup`, así que registrarse por /link nunca anunciaba
    el registro en el feed y registrarse por /player sí. Con una sola función el
    resultado no depende de por qué puerta se entró.

    El orden es el que es porque el invitado tiene el progreso: si ya existe un
    jugador para este usuario se devuelve ese, y si no, se intenta rescatar lo que
    la persona venía jugando sin cuenta antes de crear uno vacío.
    """
    player = db.query(GamePlayer).filter(GamePlayer.user_id == user.id).first()
    if player is not None:
        return player
    guest = player_for_guest_token(db, x_game_token)
    if guest is not None and guest.user_id is None:
        return link_guest_to_user(db, guest, user)
    # Token ausente o de otro usuario: jugador propio nuevo.
    return create_player_for_user(db, user)


@router.post(
    "/player",
    response_model=GamePlayerCreateResponse,
    # Por IP porque todavía no hay jugador. Sesenta por minuto deja pasar un
    # aula entera detrás del mismo NAT y frena igual un bucle.
    dependencies=[Depends(limits.por_ip(60, "player"))],
)
def create_player(
    body: GamePlayerCreateRequest,
    authorization: str = Header(None),
    x_game_token: str = Header(None),
    x_game_platform: str = Header(None),
    db: Session = Depends(get_db),
):
    """Alta de jugador. Sin auth crea un guest (devuelve el token); con Clerk
    crea/devuelve el jugador del usuario. Idempotente: si ya hay jugador para
    el token/user, se devuelve ese."""
    user = _clerk_user(authorization, db)
    if user is not None:
        player = _jugador_del_usuario(db, user, x_game_token, body.referrer_alias)
        # El feed anuncia el REGISTRO, no el alta de invitado: un invitado se
        # crea en cada primera visita y anunciarlos sería anunciar el tráfico.
        # `on_signup` deduplica por jugador, así que este camino —que se recorre
        # en cada arranque de sesión— no lo repite.
        game_events.on_signup(db, player)
        _persist_attribution(player, body.group_id, body.utm_source, _platform(x_game_platform))
        db.commit()
        return GamePlayerCreateResponse(player=_player_out(db, player), guest_token=None)

    existing = player_for_guest_token(db, x_game_token)
    if existing is not None:
        _persist_attribution(existing, body.group_id, body.utm_source, _platform(x_game_platform))
        db.commit()
        return GamePlayerCreateResponse(
            player=_player_out(db, existing), guest_token=existing.guest_token
        )

    player = create_guest_player(db)
    # Recién creado: es el momento —y el único— en que se mira el `?r=`.
    referrals.anotar(db, player, body.referrer_alias)
    _persist_attribution(player, body.group_id, body.utm_source, _platform(x_game_platform))
    db.commit()
    return GamePlayerCreateResponse(player=_player_out(db, player), guest_token=player.guest_token)


@router.get("/me", response_model=GamePlayerOut)
def get_me(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    return _player_out(db, player)


@router.patch("/me", response_model=GamePlayerOut)
def patch_me(
    body: GameProfilePatchRequest,
    player: GamePlayer = Depends(get_current_player),
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    # Se declara acá y no dentro del `if body.university`: este PATCH también se
    # usa para cambiar el @ o la carrera sola, y en esos casos la variable no
    # existiría cuando la miramos después del commit.
    reviso_aforo = False
    if body.alias is not None:
        is_guest = player.user_id is None
        # La ÚNICA edición gratis: un invitado que todavía no tocó su @
        # generado puede cambiarlo una vez, sin pasar por Clerk (ver
        # models.GamePlayer.alias_is_generated). De ahí en más, elegir el @ es
        # el gancho del registro de siempre.
        free_edit = is_guest and player.alias_is_generated
        if is_guest and not free_edit:
            raise HTTPException(status_code=403, detail="Registrate para elegir tu @.")
        # El @ pide la sesión de Clerk, no alcanza el token de invitado.
        #
        # Ese token se conserva después de vincular la cuenta a propósito (ver
        # models.GamePlayer), para que un cliente que todavía lo tenga guardado
        # siga resolviendo al mismo jugador. El costo es que nunca vence y no se
        # puede revocar: quien lo tenga sigue autenticando como esa persona para
        # siempre. Para JUGAR eso es tolerable —el peor caso es que alguien te
        # sume XP—, pero el @ es la identidad pública de la cuenta, y cambiarlo
        # es lo único que no se puede deshacer desde el otro lado.
        if not is_guest and not authorization:
            raise HTTPException(
                status_code=403, detail="Iniciá sesión de nuevo para cambiar tu @."
            )
        alias = normalize_username(body.alias)
        ok, reason = validate_username(alias)
        if not ok:
            raise HTTPException(status_code=422, detail=reason)
        if alias != player.alias:
            # El @ que se deja sigue apuntando acá. Es lo que mantiene vivos los
            # links de reclutamiento ya repartidos, que si no morirían justo en
            # el momento de registrarse. Lo hace `handles.reclamar`, que además
            # verifica contra TODO el namespace —incluidos los usernames de
            # Intervalo, que este endpoint no miraba— y baja el @ nuevo a
            # `game_players.alias`, que pasa a ser caché.
            retire_alias(db, player.alias, player.id)
            try:
                handles.reclamar(
                    db, alias, user_id=player.user_id, player_id=player.id
                )
            except handles.HandleTomado:
                raise HTTPException(status_code=409, detail="Ese @ ya está tomado.")
            except IntegrityError:
                # La misma carrera un instante después: `reclamar` hace `flush()`
                # adentro, así que el UNIQUE se levanta acá y no en el commit
                # final. Es un conflicto de usuario, no un 500.
                db.rollback()
                raise HTTPException(status_code=409, detail="Ese @ ya está tomado.")
        if free_edit:
            player.alias_is_generated = False

    if body.university is not None:
        from universities import canonical_university

        # canonical_university ya devuelve la sigla o el texto sin bordes.
        nueva = _universidad_aceptable(canonical_university(body.university))
        # Solo se marca la MUDANZA, no la primera carga: cargar la universidad por
        # primera vez no puede costarte el empuje que está corriendo, pero
        # mudarte a la universidad impulsada sí (ver boosts.applies_to).
        #
        # "Primera carga" es no tener universidad Y no haberla tenido nunca. La
        # condición anterior miraba solo `player.university is not None`, y eso
        # dejaba abierto el camino de vaciar y volver a cargar: dos PATCH
        # (university="" y después la impulsada) devolvían el candado a NULL y
        # con él el empuje entero. Con empujes de un día el premio por hacerlo
        # pasó de media hora a 24 h.
        primera_carga = player.university is None and player.university_set_at is None
        if not primera_carga and nueva != player.university:
            player.university_set_at = datetime.utcnow()
        player.university = nueva
        # Solo la primera carga cuenta para el aforo del día: mudarse no es
        # entrar. `aforo.personas_nuevas_hoy` igual filtra por fecha de alta del
        # jugador, así que esto no decide quién cuenta —eso lo decide la
        # consulta— sino cuándo vale la pena preguntar.
        reviso_aforo = primera_carga and nueva is not None
    if body.career is not None:
        # Solo se persisten los códigos conocidos; "Otra" (o basura) queda NULL,
        # el mismo bucket que usa el leaderboard principal.
        career = body.career.strip()
        player.career = career if career in _KNOWN_CAREERS else None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ese @ ya está tomado.")
    # Después del commit, por lo mismo que en el alta de clásico (main.py): la
    # persona tiene que estar contada antes de preguntar si fue la décima, y un
    # problema del premio no puede voltear el PATCH que ya se guardó.
    if reviso_aforo:
        from . import aforo

        if aforo.revisar(db, player.university) is not None:
            db.commit()
    db.refresh(player)
    return _player_out(db, player)


@router.post("/reset", response_model=GamePlayerOut)
def reset_player(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Reinicia el PROGRESO del jugador desde el panel de configuración.

    Vuelve a cero XP, racha, ejercicios y el Elo — así que el juego arranca otra
    vez por la rampa inicial, con las derivadas más fáciles. Conserva identidad
    (alias, carrera, universidad, cuenta) y atribución, y no borra los intentos
    ya registrados: el historial sigue sirviendo para analítica y el Elo de las
    plantillas (game_template_stats) es global, no del jugador.
    """
    player.xp = 0
    player.current_combo = 0
    player.best_combo = 0
    player.best_rank = None
    player.exercises_correct = 0
    player.exercises_attempted = 0
    player.theta = 0.0
    player.n_updates = 0
    # El teclado también vuelve a cero: reiniciar es empezar de nuevo, y buena
    # parte de lo que se siente al empezar es ver el teclado crecer otra vez.
    player.unlocked_keys = ""
    player.numeric_cycle_json = "{}"
    player.last_seen_at = datetime.utcnow()
    # El ejercicio abierto pertenece a la partida vieja.
    db.query(GameExercise).filter(
        GameExercise.player_id == player.id, GameExercise.status == "served"
    ).update({"status": "expired"}, synchronize_session=False)
    # Volver al fondo también mueve el ranking de los demás.
    simulation.bump_version(db)
    db.commit()
    db.refresh(player)
    return _player_out(db, player)


def _ya_la_habia_visto(db: Session, player: GamePlayer, exercise: GameExercise) -> bool:
    """¿Este estudiante ya le había aportado una observación a esta plantilla?

    Es lo que mantiene `game_template_stats.n_players`, el tamaño de muestra con
    el que se decide cuánto creerle a una β aprendida (ver elo.effective_beta).
    Sin este contador, veinte respuestas de una sola persona se leerían como
    veinte datos sobre la plantilla.

    Se excluye el ejercicio actual explícitamente en vez de confiar en que
    todavía no se escribió: el orden de los `db.add` de este endpoint no tiene
    por qué quedar congelado para que esta cuenta siga siendo cierta.
    """
    return db.query(
        db.query(GameAttempt.id)
        .join(GameExercise, GameExercise.id == GameAttempt.exercise_id)
        .filter(
            GameAttempt.player_id == player.id,
            GameAttempt.attempt_number == 1,
            GameExercise.template_key == exercise.template_key,
            GameExercise.id != exercise.id,
            GameExercise.peeked.is_(False),
        )
        .exists()
    ).scalar()


def _stamp_platform(player: GamePlayer, exercise: GameExercise, platform: str | None) -> None:
    """Marca el ejercicio con el aparato que lo pidió, y rellena el del jugador
    si nunca se llenó.

    Lo segundo es la red de contención de `_persist_attribution`: un jugador
    puede existir sin haber pasado nunca por `POST /player` con el header —lo
    crea `get_current_player` cuando alguien llega ya logueado desde Intervalo—
    y sin esto quedaría para siempre sin plataforma."""
    if platform is None:
        return
    exercise.platform = platform
    if player.platform is None:
        player.platform = platform


def _exercise_out(exercise: GameExercise, player: GamePlayer) -> GameExerciseOut:
    """Arma la respuesta y, de paso, DESBLOQUEA lo que esta derivada exige.

    El desbloqueo vive acá y no en el generador porque depende de la derivada ya
    persistida, que es lo único que /next y /skip tienen en común. Muta al
    jugador: los dos endpoints commitean después de llamar a esto.
    """
    template = template_for(exercise)
    unlocked, fresh = game_keyboard.unlock(
        player.unlocked_keys, expr_from_stored(exercise.expected_derivative)
    )
    player.unlocked_keys = unlocked
    return GameExerciseOut(
        exercise_id=exercise.id,
        prompt_latex=exercise.prompt_latex,
        tier=template.tier if template else 0,
        difficulty_stars=elo.difficulty_stars(exercise.p_hat),
        p_hat=exercise.p_hat,
        combo=player.current_combo,
        keys=game_keyboard.parse_unlocked_ordered(unlocked),
        new_keys=fresh,
    )


# Cuánto vale un ejercicio servido antes de que pedir otro sea empezar de nuevo
# en vez de reintentar. Diez minutos: más que cualquier derivada de la tabla y
# menos que volver al día siguiente.
_REINTENTO_NEXT_MINUTOS = 10


@router.post(
    "/next",
    response_model=GameExerciseOut,
    dependencies=[Depends(limits.por_jugador(120, "next"))],
)
def next_exercise(
    player: GamePlayer = Depends(get_current_player),
    x_game_platform: str = Header(None),
    db: Session = Depends(get_db),
):
    # Si ya hay uno abierto y recién servido, se devuelve ESE.
    #
    # Sin esto, /next era un salteo gratis: `serve_exercise` vence en bloque lo
    # que siguiera abierto y sirve otro, sin nada del castigo de /skip —que baja
    # el θ y corta la racha justamente para que saltear lo difícil no sea la
    # forma óptima de sostener un combo—. Con la consola abierta se podía
    # re-tirar hasta que saliera una de una estrella, conservando la racha e
    # inflando resueltas, XP y puesto.
    #
    # De paso lo vuelve idempotente, que es lo que hace falta cuando la respuesta
    # se pierde en el camino y el teléfono reintenta.
    abierto = (
        db.query(GameExercise)
        .filter(
            GameExercise.player_id == player.id,
            GameExercise.status == "served",
            GameExercise.created_at
            >= datetime.utcnow() - timedelta(minutes=_REINTENTO_NEXT_MINUTOS),
        )
        .order_by(GameExercise.id.desc())
        .first()
    )
    if abierto is not None:
        out = _exercise_out(abierto, player)
        db.commit()
        return out

    exercise = serve_exercise(db, player)
    _stamp_platform(player, exercise, _platform(x_game_platform))
    # El armado va ANTES del commit: `_exercise_out` desbloquea teclas sobre el
    # jugador, y commitear primero dejaba esa escritura sin persistir — el
    # inventario volvía vacío en cada pedido y el teclado seguía comportándose
    # como el de antes.
    out = _exercise_out(exercise, player)
    db.commit()
    return out


@router.post("/cafecito-intent", status_code=204)
def cafecito_intent(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """"Me voy a Cafecito": se anota quién y de qué universidad.

    Es la única pata de la atribución que no le pide NADA a quien dona. Los tres
    campos del formulario de Cafecito son opcionales y no se pueden marcar
    obligatorios, así que exigir la sigla ahí sería poner fricción justo en el
    peor lugar del embudo. Acá, en cambio, el juego ya sabe todo.

    Devuelve 204: el cliente dispara esto y se va sin esperar nada.

    La universidad sale del jugador, salvo que no tenga: desde que la
    configuración de Intervalo clásico abre esta misma diapo (profile-content.tsx),
    acá llegan personas cuyo `GamePlayer` se creó al vuelo para servir este
    pedido (`deps.create_player_for_user`) y nunca pasó por la pantalla que
    elige universidad. Su fila de Intervalo sí la sabe.

    Sin este respaldo la intención se anota con `university=None`: alcanza para
    decirle a la persona que su cafecito llegó, pero NO para dirigir el empuje
    (`pending_intents` descarta las que no tienen universidad), así que la
    donación de alguien que nunca jugó terminaba en el reparto global en vez de
    ir a su universidad. Es la misma atribución que se pierde por donar sin
    anotar la intención, un escalón más adelante.

    Se canoniza porque `Enrollment.university` es texto libre del onboarding y
    los empujes se buscan por sigla; el jugador ya la tiene canonizada.
    """
    university = player.university
    if not university and player.user_id is not None:
        fila = xp_boost.enrollment_de_referencia(db, player.user_id)
        if fila is not None:
            university = canonical_university(fila.university) or None
    boosts.record_intent(db, player, university=university)
    db.commit()
    return Response(status_code=204)


@router.get("/cafecito-status", response_model=GameCafecitoStatus)
def cafecito_status(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Qué pasó con el cafecito de quien acaba de volver de Cafecito.

    Lo consulta la diapo cuando la pestaña vuelve a estar a la vista. Es de
    lectura y barato —una intención y, si está cumplida, los empujes de ese
    instante— así que no necesita nada especial.
    """
    e = boosts.estado_de_donacion(db, player)
    return GameCafecitoStatus(
        state=e.state,
        university=e.university,
        cafecitos=e.cafecitos,
        multiplier=e.multiplier,
        expires_in_seconds=e.expires_in_seconds,
    )


@router.post(
    "/skip",
    response_model=GameExerciseOut,
    dependencies=[Depends(limits.por_jugador(120, "skip"))],
)
def skip_exercise(
    body: GameSkipRequest,
    player: GamePlayer = Depends(get_current_player),
    x_game_platform: str = Header(None),
    db: Session = Depends(get_db),
):
    """Saltear: cierra el ejercicio sin responderlo y sirve uno más fácil.

    Saltear NO es responder, así que no mueve la beta de la plantilla ni suma a
    los ejercicios intentados: pedir algo más fácil es información sobre el
    jugador, no sobre la plantilla, y contarlo como intento inflaría el
    denominador de la tasa de acierto. Sí baja un poco el θ y corta la racha —
    si no, saltear todo lo difícil sería la forma óptima de sostener un combo.
    Tampoco da XP, y como la XP escala con la dificultad, encadenar salteos
    hasta el piso rinde cada vez menos: la mecánica se autolimita.
    """
    # Ídem /answer: saltear baja el θ y corta la racha.
    player = lock_player(db, player)

    exercise = (
        db.query(GameExercise)
        .filter(GameExercise.id == body.exercise_id, GameExercise.player_id == player.id)
        .first()
    )
    if exercise is None:
        raise HTTPException(status_code=404, detail="Ejercicio no encontrado")
    if exercise.status != "served":
        raise HTTPException(status_code=409, detail="Ese ejercicio ya se cerró")

    template = template_for(exercise)
    exercise.status = "skipped"
    exercise.answered_at = datetime.utcnow()
    # Antes de servir: serve_exercise expira en bloque lo que siga en "served",
    # y con el cambio todavía pendiente en la sesión este ejercicio entraría en
    # esa barrida y terminaría marcado "expired" en vez de "skipped".
    db.flush()

    player.theta -= elo.SKIP_THETA_PENALTY
    player.current_combo = 0

    nxt = serve_exercise(db, player, max_tier=(template.tier - 1) if template else None)
    _stamp_platform(player, nxt, _platform(x_game_platform))
    # Ídem /next: primero armar (desbloquea), después commitear.
    out = _exercise_out(nxt, player)
    db.commit()
    return out


# Zona del juego para decir "hoy". No es la del usuario —los invitados no tienen
# perfil ni zona declarada— sino la del público al que apunta: si alguien juega
# desde otro huso, su "hoy" arranca cuando arranca acá, que es cuando arranca el
# ranking con el que se compara.
_TZ_JUEGO = ZoneInfo("America/Argentina/Buenos_Aires")
_UTC = ZoneInfo("UTC")


def _inicio_del_dia() -> datetime:
    """Medianoche local de hoy, devuelta como UTC ingenuo.

    Los datetime de la base son naive UTC (datetime.utcnow), así que el corte
    hay que traerlo a esa misma escala antes de comparar; hacerlo al revés
    —convertir cada fila— impediría cualquier uso de índice.

    Hoy la consulta se sostiene por `ix_game_attempts_player_id` y porque en la
    práctica hay pocas filas por jugador, no por un índice sobre la fecha: ese
    no existe. Con los intentos ilimitados eso último es una apuesta y ya no una
    garantía —antes lo aseguraba MAX_ATTEMPTS=2, ahora solo lo hace probable el
    hecho de que nadie intenta la misma derivada diez veces—, así que si los
    intentos por jugador dejan de ser pocos, lo que hace falta es un índice
    compuesto (player_id, created_at).
    """
    ahora = datetime.now(_TZ_JUEGO)
    medianoche = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    return medianoche.astimezone(_UTC).replace(tzinfo=None)


def _correctas_de_hoy(db: Session, player_id: int) -> int:
    return (
        db.query(func.count(GameAttempt.id))
        .filter(
            GameAttempt.player_id == player_id,
            GameAttempt.is_correct.is_(True),
            GameAttempt.created_at >= _inicio_del_dia(),
        )
        .scalar()
        or 0
    )


def _registrar_fallo_de_parseo(
    db: Session,
    exercise: GameExercise,
    player: GamePlayer,
    body: GameAnswerRequest,
    prior_attempts: int,
    exc: Exception,
) -> GameAnswerResponse:
    """Lo que escribió la persona y el parser no entendió. NO consume intento.

    Se registra igual aunque no cuente. Es la única forma de medir la fricción
    del input —lo que la gente quiso escribir y el motor no entendió— y esa
    fricción se lee igual que un error de matemática desde afuera: la persona ve
    «no pudimos evaluar tu respuesta» y se va. `attempt_number` queda en el valor
    ANTERIOR (0 en la primera), que es lo que marca la fila como "no consumió
    intento".
    """
    db.add(
        GameAttempt(
            exercise_id=exercise.id,
            player_id=player.id,
            attempt_number=prior_attempts,
            answer_latex=(body.answer_latex or "")[:_MAX_LATEX_GUARDADO],
            answer_parsed=None,
            parse_ok=False,
            is_correct=False,
            response_ms=body.response_ms,
            xp_awarded=0,
            created_at=datetime.utcnow(),
        )
    )
    player.last_seen_at = datetime.utcnow()
    # Se arma ANTES del commit: después, SQLAlchemy expira los atributos del
    # jugador y cada uno vuelve a la base a releer la fila que este mismo request
    # acaba de escribir.
    respuesta = GameAnswerResponse(
        correct=False,
        parse_ok=False,
        parse_error=str(exc) or "no pudimos evaluar tu respuesta",
        attempt_number=prior_attempts,
        # None es «sin límite», que es lo que los intentos son ahora. El campo
        # queda porque el cliente viejo lo lee; el nuevo decide con `correct`.
        attempts_left=None,
        xp_awarded=0,
        xp_total=player.xp,
        combo=player.current_combo,
        combo_bonus=0,
        exercises_correct=player.exercises_correct,
        # Sin esto el campo salía en su default (0) y el contador de "ya llevás N
        # resueltas hoy" se reseteaba a cero con cualquier respuesta que el parser
        # no entendiera — justo el número que se movió al servidor para que NO se
        # resetee. Es el tipo de olvido que habilita tener dos constructores de la
        # respuesta a cien líneas de distancia, y por eso este es una función.
        correct_today=_correctas_de_hoy(db, player.id),
    )
    db.commit()
    return respuesta


def _aplicar_elo(
    db: Session,
    exercise: GameExercise,
    player: GamePlayer,
    attempt_number: int,
    correct: bool,
    *,
    peeked: bool,
) -> tuple[int, float | None, float | None]:
    """Mueve θ, β y la racha. Devuelve (nivel antes, θ antes, θ después).

    Solo el primer intento, y solo si la tabla no estuvo abierta. Con la tabla a
    la vista el resultado no dice nada sobre el jugador NI sobre la plantilla,
    así que no mueve θ ni β: meterlo al Elo ensuciaría la calibración con
    observaciones que no son de nadie. Tampoco cuenta para la rampa (n_updates),
    por lo mismo.

    El nivel de antes se lee con θ todavía sin tocar: es contra eso que el feed
    decide si hubo un salto de dificultad (ver events.on_answer).
    """
    level_before = elo.level_of(player.theta)
    # La consulta se persiste en el EJERCICIO y no en el intento: es una
    # propiedad de la derivada servida (la tabla estuvo abierta mientras esta
    # estaba en pantalla), no de cada tecleo. Se pega con OR para que un segundo
    # intento sin mirar no borre que el primero sí miró.
    if peeked and not exercise.peeked:
        exercise.peeked = True

    theta_before = theta_after = None
    if attempt_number != 1:
        return level_before, theta_before, theta_after

    if not peeked:
        stat = get_or_create_stat(db, template_for(exercise))
        theta_before = player.theta
        # `tier` y `n_players` no son decorativos: son lo que hace que θ se mueva
        # contra la β encogida —la que el motor cree de verdad— mientras la β
        # guardada se sigue corrigiendo contra la suya cruda. Ver
        # elo.effective_beta.
        theta_after, beta_after = elo.update(
            player.theta, player.n_updates, stat.beta, stat.n_observations, correct,
            tier=stat.tier, n_players=stat.n_players,
        )
        player.theta = theta_after
        player.n_updates += 1
        # El contador de personas se toca ANTES de que esta respuesta exista en
        # la tabla, así que la consulta ve solo los encuentros anteriores. Se
        # excluyen los ejercicios mirados con la tabla abierta por el mismo
        # motivo que no mueven β: no aportaron ninguna observación.
        if not _ya_la_habia_visto(db, player, exercise):
            stat.n_players += 1
        stat.beta = beta_after
        stat.n_observations += 1
        if correct:
            stat.n_correct += 1

    player.exercises_attempted += 1
    # La racha no distingue: mirar la tabla no la corta, errar sí.
    player.current_combo = player.current_combo + 1 if correct else 0
    player.best_combo = max(player.best_combo, player.current_combo)
    return level_before, theta_before, theta_after


def _otorgar_xp(
    db: Session,
    exercise: GameExercise,
    player: GamePlayer,
    attempt_number: int,
    correct: bool,
    *,
    peeked: bool,
) -> tuple[int, int, float]:
    """Suma la XP de esta respuesta. Devuelve (XP, bonus de racha, multiplicador).

    Se llama DESPUÉS de `_aplicar_elo` porque la XP escala con la racha, y la
    racha la acaba de mover esa función.

    `explained` sale del ejercicio y no del cuerpo del pedido: la explicación la
    entrega el servidor, así que no hace falta que el cliente la confiese (ver
    la columna en models.py).
    """
    xp_awarded, combo_bonus = game_xp.xp_for_answer(
        attempt_number,
        correct,
        exercise.p_hat,
        player.current_combo,
        peeked=peeked,
        explained=bool(exercise.explained),
    )

    # Empuje de la universidad. La regla es "multiplica lo que sea que haya pagado
    # esta respuesta", sin excepciones: también el XP simbólico de haber mirado la
    # tabla. Se escalan los DOS números —total y bonus— porque el bonus viaja
    # aparte en la respuesta, y un "+15 de combo" adentro de un total multiplicado
    # se lee como un error de cuentas.
    multiplier = boosts.multiplier_for_player(db, player) if correct else 1.0
    if multiplier > 1.0:
        sin_empuje = xp_awarded
        xp_awarded = round(xp_awarded * multiplier)
        combo_bonus = round(combo_bonus * multiplier)
        # Lo que el empuje agregó, anotado acá porque después no hay de dónde
        # sacarlo: lo único que queda guardado es el total de `xp`, y ahí lo
        # base y lo extra ya están sumados. Es lo que muestra el panel de
        # estadísticas. `multiplier > 1.0` implica `correct`, así que no hace
        # falta preguntarlo de nuevo.
        player.xp_from_boosts += xp_awarded - sin_empuje

    if correct:
        player.xp += xp_awarded
        player.exercises_correct += 1
        # Y su parte para quien lo trajo, si lo trajo alguien. Se ACUÑA: el
        # número de arriba ya está cerrado y no se le descuenta nada — entrar
        # por el link de alguien no puede costar XP (ver game/referrals.py).
        referrals.acreditar(db, player, xp_awarded)
    return xp_awarded, combo_bonus, multiplier


def _repetir_ultima_respuesta(
    db: Session, exercise: GameExercise, player: GamePlayer
) -> GameAnswerResponse | None:
    """La respuesta que este ejercicio ya dio, para poder repetirla en un
    reintento. None si nunca se respondió (ahí sí corresponde el 409).

    Existe por la conexión del público objetivo. El teléfono manda la respuesta,
    el servidor la procesa y la contesta, y la contestación se pierde en el
    camino; el cliente reintenta. Con el ejercicio ya cerrado, el reintento se
    llevaba un 409 pelado: sin XP, sin color, sin la derivada correcta — o sea,
    la persona ve un error por una respuesta que estuvo bien y que ya le fue
    contada.

    No se repite todo: los puestos y el récord eran de aquel instante y no se
    guardan por intento. Lo que se repite es lo que la persona necesita ver —si
    estuvo bien, cuánta XP ganó y cuál era la respuesta—, sin el festejo de la
    escalada, que ya ocurrió.
    """
    # Solo los RESPONDIDOS. Un ejercicio "expired" —el que vence un reinicio de
    # progreso, o el que quedó abierto de una partida anterior— puede tener
    # intentos viejos, y repetir aquella respuesta sería contarle a la persona
    # algo de una partida que ya no existe. Ahí corresponde el 409, que el
    # cliente convierte en "pedime otro" (ver el onError de las dos vistas).
    if exercise.status != "answered":
        return None
    ultimo = (
        db.query(GameAttempt)
        .filter(GameAttempt.exercise_id == exercise.id, GameAttempt.parse_ok.is_(True))
        .order_by(GameAttempt.attempt_number.desc(), GameAttempt.id.desc())
        .first()
    )
    if ultimo is None:
        return None
    correcto = bool(ultimo.is_correct)
    return GameAnswerResponse(
        correct=correcto,
        parse_ok=True,
        attempt_number=ultimo.attempt_number,
        attempts_left=None,
        xp_awarded=ultimo.xp_awarded,
        xp_total=player.xp,
        combo=player.current_combo,
        combo_bonus=0,
        exercises_correct=player.exercises_correct,
        correct_today=_correctas_de_hoy(db, player.id),
        correct_answer_latex=(
            latex_es(expr_from_stored(exercise.expected_derivative)) if not correcto else None
        ),
        best_rank=player.best_rank,
    )


@router.post(
    "/answer",
    response_model=GameAnswerResponse,
    # Es el endpoint que hace trabajar a sympy. Nadie escribe ciento veinte
    # derivadas por minuto a mano.
    dependencies=[Depends(limits.por_jugador(120, "answer"))],
)
def answer_exercise(
    body: GameAnswerRequest,
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Responder una derivada. El orden importa y es este:

    autorizar → contar intentos → PARSEAR Y VALIDAR (acá se decide el veredicto,
    y es lo único que el cliente necesita para pintar el color) → Elo → XP →
    cerrar → registrar → puesto y novedades → responder.

    Todo lo que va después de la validación es contabilidad: el cliente ya sabe
    si acertó porque lo calculó él mismo con la misma regla (ver
    web/src/app/derivadas/local-verdict.ts).
    """
    # Candado del jugador antes de leer nada suyo: esta respuesta va a sumarle XP,
    # ejercicios y racha, y dos en vuelo se pisan (ver deps.lock_player).
    player = lock_player(db, player)

    exercise = (
        db.query(GameExercise)
        .filter(GameExercise.id == body.exercise_id, GameExercise.player_id == player.id)
        .first()
    )
    if exercise is None:
        raise HTTPException(status_code=404, detail="Ejercicio no encontrado")
    if exercise.status != "served":
        # Reintento sobre algo ya cerrado: se repite el resultado en vez de tirar
        # un error por una respuesta que quizás estuvo bien.
        repetida = _repetir_ultima_respuesta(db, exercise, player)
        if repetida is not None:
            return repetida
        raise HTTPException(status_code=409, detail="Ese ejercicio ya se cerró")

    # Solo cuentan los intentos que PARSEARON: los que no se registran igual
    # (ver abajo) pero con parse_ok=False, y no consumen intento — escribir algo
    # que el parser no entiende nunca gastó una vida y no puede empezar a
    # hacerlo por haber agregado la fila.
    prior_attempts = (
        db.query(GameAttempt)
        .filter(GameAttempt.exercise_id == exercise.id, GameAttempt.parse_ok.is_(True))
        .count()
    )
    attempt_number = prior_attempts + 1
    if attempt_number > TOPE_DE_INTENTOS:
        raise HTTPException(status_code=409, detail="Ese ejercicio ya se cerró")

    expected = expr_from_stored(exercise.expected_derivative)

    # Parseo + guardas. Un fallo acá NO consume intento ni mueve el Elo.
    candidate = None
    try:
        if body.answer_mathjson is None:
            raise MathJsonError("falta answer_mathjson")
        candidate = to_sympy(body.answer_mathjson)
        guard_candidate(candidate)
        correct = numerically_equivalent(expected, candidate)
    except (MathJsonError, AnswerRejected) as exc:
        return _registrar_fallo_de_parseo(db, exercise, player, body, prior_attempts, exc)

    rank_before = _rank_of(db, player)

    level_before, theta_before, theta_after = _aplicar_elo(
        db, exercise, player, attempt_number, correct, peeked=body.peeked
    )
    xp_awarded, combo_bonus, multiplier = _otorgar_xp(
        db, exercise, player, attempt_number, correct, peeked=body.peeked
    )

    # Un ejercicio se cierra acertando, y nada más. Salir sin resolverlo es
    # saltear, que es un gesto propio y tiene su propio endpoint.
    closed = correct
    if closed:
        exercise.status = "answered"
        exercise.answered_at = datetime.utcnow()

    feedback = None
    if not correct:
        feedback = match_common_error(exercise.common_errors_json, candidate)
        if feedback is None:
            template = template_for(exercise)
            feedback = template.generic_feedback if template else GENERIC_FEEDBACK

    # Antes de sumar el intento de esta respuesta, así el conteo no depende de si
    # la sesión hace autoflush o no; el intento propio se suma después a mano.
    correctas_hoy_previas = _correctas_de_hoy(db, player.id)

    db.add(
        GameAttempt(
            exercise_id=exercise.id,
            player_id=player.id,
            attempt_number=attempt_number,
            answer_latex=(body.answer_latex or "")[:_MAX_LATEX_GUARDADO],
            answer_parsed=str(candidate),
            parse_ok=True,
            is_correct=correct,
            response_ms=body.response_ms,
            xp_awarded=xp_awarded,
            theta_before=theta_before,
            theta_after=theta_after,
            created_at=datetime.utcnow(),
        )
    )
    player.last_seen_at = datetime.utcnow()

    # El puesto depende solo de (xp, id), y la XP se mueve únicamente al acertar:
    # al errar, volver a contar la tabla entera devuelve por definición el mismo
    # número que `rank_before`. Era el segundo COUNT completo de la respuesta, y
    # caía justo en el camino más pesado —el de errar, que además paga la
    # comparación contra los errores predecibles.
    rank_after = _rank_of(db, player) if correct else rank_before
    if correct:
        # El ranking cambió: el pulso lo va a notar y los demás refrescan.
        simulation.bump_version(db)
    is_record = False
    if correct and (player.best_rank is None or rank_after < player.best_rank):
        is_record = player.best_rank is not None
        player.best_rank = rank_after

    if correct:
        game_events.on_answer(
            db,
            player,
            rank_before=rank_before,
            rank_after=rank_after,
            level_before=level_before,
            level_after=elo.level_of(player.theta),
        )

    # La respuesta se arma ENTERA antes del commit. Con `expire_on_commit` en su
    # valor por defecto, después de comitear cada atributo del jugador que se lea
    # acá abajo dispara un SELECT para releer la fila que este mismo request
    # acaba de escribir — eran seis lecturas y una consulta extra a la base, con
    # el cliente esperando el color.
    respuesta = GameAnswerResponse(
        correct=correct,
        parse_ok=True,
        attempt_number=attempt_number,
        attempts_left=None,
        feedback_incorrect=feedback,
        xp_awarded=xp_awarded,
        xp_total=player.xp,
        combo=player.current_combo,
        combo_bonus=combo_bonus,
        xp_multiplier=multiplier,
        exercises_correct=player.exercises_correct,
        correct_today=correctas_hoy_previas + (1 if correct else 0),
        # Ya no hay ejercicio que se cierre sin acertar, así que este campo sale
        # siempre en None desde acá. La derivada correcta llega por un solo
        # camino y es el «¿Por qué?».
        correct_answer_latex=None,
        rank_before=rank_before,
        rank_after=rank_after,
        best_rank=player.best_rank,
        is_record=is_record,
    )
    db.commit()
    return respuesta


@router.post(
    "/explain",
    response_model=GameExplainOut,
    # Hace correr sympy y lo dispara un botón que se puede tocar dos veces por
    # nervios. Sesenta por minuto es holgadísimo para una persona y corta un
    # bucle.
    dependencies=[Depends(limits.por_jugador(60, "explain"))],
)
def explain_exercise(
    body: GameExplainRequest,
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """El «¿Por qué?»: de dónde salía esta derivada.

    La explicación termina con la derivada escrita, así que este endpoint
    REGALA la respuesta. Por eso el candado del punto 2 no es una formalidad: la
    interfaz tampoco ofrece el botón antes del primer intento, pero eso lo
    decide el cliente y acá también manda el servidor.

    Leerlo con el ejercicio abierto le baja la recompensa a XP_EXPLICADO. Leerlo
    con el ejercicio ya acertado no cuesta nada — no queda nada que cobrar.
    """
    exercise = (
        db.query(GameExercise)
        .filter(GameExercise.id == body.exercise_id, GameExercise.player_id == player.id)
        .first()
    )
    if exercise is None:
        raise HTTPException(status_code=404, detail="Ejercicio no encontrado")

    # Sin un intento parseado, esto sería un endpoint que contesta ejercicios sin
    # responder. Los intentos que el parser NO entendió no cuentan, por lo mismo
    # que no consumen intento: escribir cualquier cosa no es haber intentado.
    intentos = (
        db.query(GameAttempt)
        .filter(GameAttempt.exercise_id == exercise.id, GameAttempt.parse_ok.is_(True))
        .count()
    )
    if intentos == 0:
        raise HTTPException(
            status_code=409, detail="Probá primero y después te cuento de dónde sale."
        )

    # Solo si sigue abierto, y solo la primera vez: volver a leer lo que ya se
    # leyó no puede cobrar dos veces.
    cobra = exercise.status == "served" and not exercise.explained
    if cobra:
        exercise.explained = True
        db.commit()

    explicacion = game_explain.build(exercise)
    return GameExplainOut(
        explanation=explicacion.text,
        costs_xp=cobra,
        graph_fn=explicacion.graph_fn,
        graph_fn2=explicacion.graph_fn2,
        graph_fn_latex=explicacion.graph_fn_latex,
        graph_fn2_latex=explicacion.graph_fn2_latex,
        graph_view=list(explicacion.graph_view),
    )


@router.get("/leaderboard/pulse", response_model=GamePulse)
def game_pulse(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Latido del ranking: un número que cambia cuando cambia la tabla.

    Este pedido es además lo que hace avanzar la actividad simulada. No hay
    worker ni cron: el ranking se mueve mientras haya alguien mirándolo, que es
    justo cuando importa que se mueva.
    """
    simulation.maybe_tick(db)
    return GamePulse(
        version=simulation.get_state(db).version or 0,
        boosts=[
            GameBoostOut(
                university=b.university,
                multiplier=b.multiplier,
                cafecitos=b.cafecitos,
                donor_name=b.donor_name,
                expires_in_seconds=b.expires_in_seconds,
                aforo=b.aforo,
            )
            for b in boosts.active_boosts(db)
        ],
    )


@router.get("/events", response_model=GameEventsResponse)
def game_events_feed(
    after_id: int = Query(default=0, ge=0),
    after_msg_id: int = Query(default=0, ge=0),
    before_id: int = Query(default=0, ge=0),
    before_msg_id: int = Query(default=0, ge=0),
    limit: int = Query(default=0, ge=0, le=100),
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Historial de lo que va pasando, más los mensajes del chat.

    Los `events` son SOLO del sistema —ninguna línea la escribe un usuario— y por
    eso no hay nada que moderar ahí. Los `messages` sí los escribe la gente y
    viven en otra tabla, con su propio saneado (ver chat.py); van en la misma
    respuesta porque el chat entero se apoya en eso: no hay un sondeo nuevo, hay
    un campo nuevo en el que ya corría cada ocho segundos.

    Dos cursores y dos ventanas, no una mezclada. Compartiendo las cuarenta filas,
    una racha de chat empujaría fuera de pantalla el anuncio de que alguien invitó
    cafecitos, que es el que mueve donaciones.

    Con los dos cursores devuelve únicamente lo nuevo, que es lo que hace que
    sondearlo cada pocos segundos no cueste nada.

    Y con `before_id`/`before_msg_id` mira para el otro lado: lo que hay más
    atrás de esa línea. Es lo que pide el panel al llegar arriba de todo
    scrolleando, y es el modo OPUESTO al sondeo — cada lista usa su cursor
    "before" si vino, y si no el "after". Que un mismo pedido pueda traer
    novedades viejas y mensajes nuevos no es un accidente: el panel pagina las
    dos listas juntas, pero puede tocar el fondo de una antes que el de la otra.

    Sin un `has_more` en la respuesta a propósito: una página más corta que el
    `limit` pedido YA significa "no hay más atrás", y el cliente lo sabe sin que
    se lo digan. Un campo aparte sería un segundo lugar donde puede estar mal.
    """
    n = limit or None
    eventos = game_events.recent(
        db,
        after_id=after_id,
        before_id=before_id,
        **({"limit": n} if n else {}),
    )
    mensajes = game_chat.recent(
        db,
        after_id=after_msg_id,
        before_id=before_msg_id,
        **({"limit": n} if n else {}),
    )
    return GameEventsResponse(
        events=[
            GameEventOut(
                id=e.id,
                kind=e.kind,
                text=e.text,
                emoji=e.emoji,
                actor_alias=e.actor_alias,
                actor_level=e.actor_level,
                actor_b_alias=e.actor_b_alias,
                universities=e.universities,
                is_mine=e.player_id is not None and e.player_id == player.id,
                # Las DOS universidades cuentan: en "la UNT le pasó a la UNR" te
                # toca tanto si sos de una como de la otra — sobre todo si sos de
                # la que se comió el sobrepaso.
                is_my_university=(
                    player.university is not None
                    and player.university in e.universities
                ),
                seconds_ago=e.seconds_ago,
            )
            for e in eventos
        ],
        messages=[
            GameMessageOut(
                id=m.id,
                alias=m.alias,
                level=m.level,
                university=m.university,
                text=m.text,
                is_mine=m.player_id == player.id,
                seconds_ago=m.seconds_ago,
            )
            for m in mensajes
        ],
        chat_enabled=_chat_habilitado(),
    )


@router.get("/leaderboard/summary", response_model=GameLeaderboardSummary)
def game_leaderboard_summary(
    university: str | None = Query(default=None),
    career: str | None = Query(default=None),
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Los dos números de la cabecera del ranking, más las universidades para
    poblar el filtro (esas van siempre sin scope)."""
    scope = _scope_filters(university, career)
    players, exercises, rated, theta_sum = (
        db.query(
            func.count(GamePlayer.id),
            func.coalesce(func.sum(GamePlayer.exercises_correct), 0),
            # Mismo criterio que game_university_leaderboard: solo cuenta para
            # el promedio quien ya salió de la rampa (elo.RAMP_UPDATES), o el
            # promedio mide cuántos novatos hay y no qué tan bien deriva el
            # scope.
            func.count(case((GamePlayer.n_updates >= elo.RAMP_UPDATES, 1))),
            func.coalesce(
                func.sum(case((GamePlayer.n_updates >= elo.RAMP_UPDATES, GamePlayer.theta))),
                0.0,
            ),
        )
        .filter(*scope, RESOLVIO_ACA)
        .one()
    )
    universities = [
        u
        for (u,) in db.query(GamePlayer.university)
        .filter(GamePlayer.university.isnot(None), GamePlayer.university != "")
        .distinct()
        .order_by(GamePlayer.university.asc())
        .all()
    ]
    elo_avg = (
        elo.rating_of(float(theta_sum) / rated) if rated >= boosts.MIN_PLAYERS_RANKED else None
    )
    return GameLeaderboardSummary(
        players=int(players),
        exercises=int(exercises),
        universities=universities,
        elo_avg=elo_avg,
    )


@router.get(
    "/stats",
    response_model=GameStatsOut,
    dependencies=[Depends(limits.por_jugador(30, "stats"))],
)
def game_stats_endpoint(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Estadísticas del jugador para el panel que abre la tecla `p` en
    escritorio (ver web/src/app/derivadas/elo-stats-panel.tsx): dónde está el
    Elo del jugador contra la masa de jugadores calificados, más la tabla de
    derivadas con Elo de desbloqueo y accuracy personal (game/stats.py).

    El servidor repite acá el gate de visibilidad —no confía en que el
    cliente lo haya respetado— porque acá también manda el server, como en el
    resto del juego.

    Rate limit propio y no compartido con los demás GET del ranking: esto
    agrega sobre TODOS los jugadores calificados (crece con la base entera,
    sin techo) más el historial completo del jugador — más parecido en costo
    a `/leaderboard/universities` que a `/me`, y lo dispara una tecla que
    cualquiera puede mantener apretada.
    """
    if player.exercises_correct < game_stats.UMBRAL_ESTADISTICAS:
        raise HTTPException(
            status_code=403,
            detail="Todavía te faltan derivadas para desbloquear las estadísticas.",
        )
    return game_stats.build(db, player)


@router.get("/leaderboard/universities", response_model=GameUniversityLeaderboardResponse)
def game_university_leaderboard(
    university: str | None = Query(default=None),
    career: str | None = Query(default=None),
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Ranking por universidad: Elo promedio de sus jugadores.

    Elo y no XP. La XP mide cuánto jugaste —así que premia al que le puso más
    horas— y el Elo mide qué tan difícil resolvés. Entre universidades, la
    pregunta interesante es cuál deriva mejor, no cuál tuvo más tiempo libre.
    """
    filters = [
        GamePlayer.university.isnot(None),
        GamePlayer.university != "",
        RESOLVIO_ACA,
        *_scope_filters(university, career),
    ]
    grouped = (
        db.query(
            GamePlayer.university,
            _CAREER_BUCKET.label("bucket"),
            func.count(GamePlayer.id),
            func.coalesce(func.sum(GamePlayer.xp), 0),
            # Solo los que ya salieron de la rampa cuentan para el promedio: el
            # que no respondió nada tiene θ en el valor semilla, y meterlo al
            # promedio mide cuántos novatos tenés, no qué tan bien derivan.
            func.count(case((GamePlayer.n_updates >= elo.RAMP_UPDATES, 1))),
            func.coalesce(
                func.sum(case((GamePlayer.n_updates >= elo.RAMP_UPDATES, GamePlayer.theta))),
                0.0,
            ),
        )
        .filter(*filters)
        .group_by(GamePlayer.university, _CAREER_BUCKET)
        .all()
    )

    by_uni: dict[str, dict] = {}
    total_players = 0
    for uni, bucket, players, xp, rated, theta_sum in grouped:
        total_players += players
        agg = by_uni.setdefault(
            uni,
            {"xp": 0, "players": 0, "rated": 0, "theta": 0.0,
             "careers": {c: 0 for c in (*_KNOWN_CAREERS, "Otra")}},
        )
        agg["xp"] += int(xp)
        agg["players"] += players
        agg["rated"] += int(rated or 0)
        agg["theta"] += float(theta_sum or 0.0)
        agg["careers"][bucket] += players

    # Promedio y no suma: con el total, la universidad más grande gana siempre
    # —y el empuje por cafecitos agrandaba esa brecha, porque el mismo cafecito
    # rinde por 200 jugadores de un lado y por 12 del otro—. Con el promedio, una
    # universidad chica puede ganar, que es lo único que mantiene viva la
    # rivalidad. Y como el empuje mueve XP pero NO mueve θ, ahora los cafecitos
    # tampoco pueden comprar puesto en esta tabla.
    rows = [
        GameUniversityRow(
            university=uni,
            xp=agg["xp"],
            players=agg["players"],
            rated_players=agg["rated"],
            rating_avg=(
                elo.rating_of(agg["theta"] / agg["rated"]) if agg["rated"] else 0
            ),
            ranked=agg["rated"] >= boosts.MIN_PLAYERS_RANKED,
            careers=agg["careers"],
        )
        for uni, agg in by_uni.items()
    ]
    # Las que no llegan al mínimo van al fondo en vez de desaparecer: un ranking
    # que borra tu universidad sin decir por qué es peor que uno imperfecto.
    rows.sort(key=lambda r: (r.ranked, r.rating_avg, r.rated_players), reverse=True)
    return GameUniversityLeaderboardResponse(
        rows=rows, total_players=total_players, total_universities=len(by_uni)
    )


@router.get("/leaderboard", response_model=GameLeaderboardResponse)
def game_leaderboard(
    university: str | None = Query(default=None),
    career: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    # Con tope: sin él, un ?offset=50000000 es un recorrido de la tabla entera
    # que cualquiera puede pedir escribiendo en la barra de direcciones. Cien mil
    # filas es mucho más ranking del que nadie va a scrollear.
    offset: int = Query(default=0, ge=0, le=100_000),
    around_me: bool = Query(default=False),
    # Por qué se ordena. `xp` es el orden canónico de siempre; `elo` es el que
    # pide el selector de la cabecera — ver `_ORDEN_ELO`. Va con `pattern` y no
    # como texto libre: es una clave que entra derecho a un `order_by`.
    sort: str = Query(default="xp", pattern="^(xp|elo)$"),
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Espejo del /leaderboard principal sobre game_players: solo jugadores con
    xp > 0 más el propio jugador, ordenados por XP (canónico) o por Elo.

    `university` y `career` acotan el scope igual que en el principal: el rank,
    los totales y la página se calculan todos dentro del scope elegido.

    El orden NO cambia quiénes entran, solo en qué orden salen: el total y la
    visibilidad son los mismos en los dos, así que cambiar de orden mueve a la
    gente de puesto pero no la saca de la tabla.
    """
    por_elo = sort == "elo"
    scope = _scope_filters(university, career)
    visible = sa_or(RESOLVIO_ACA, GamePlayer.id == player.id)

    total_count = db.query(GamePlayer.id).filter(*scope, visible).count()
    # Con un filtro puesto el jugador puede quedar fuera del scope (filtró por
    # otra universidad): ahí no tiene puesto y la ventana arranca del principio.
    in_scope = (
        db.query(GamePlayer.id).filter(GamePlayer.id == player.id, *scope).first() is not None
    )
    my_rank = (
        (_rank_of_elo if por_elo else _rank_of)(db, player, scope) if in_scope else None
    )
    my_index = (my_rank - 1) if my_rank is not None else None

    if around_me and my_index is not None:
        page_offset = max(0, my_index - AROUND_WINDOW)
        page_size = (my_index + AROUND_WINDOW + 1) - page_offset
    elif around_me:
        page_offset = 0
        page_size = AROUND_WINDOW * 2 + 1
    else:
        page_offset = offset
        page_size = limit

    page = (
        db.query(GamePlayer)
        # Solo las columnas que la fila del ranking necesita. Antes traía la
        # fila ENTERA de hasta doscientos jugadores, incluido `guest_token` —la
        # credencial de cada uno—, que nunca se serializa pero viajaba desde la
        # base a la memoria del proceso en cada carga del ranking.
        .options(
            load_only(
                GamePlayer.id,
                GamePlayer.alias,
                GamePlayer.xp,
                # Se lee más abajo para armar la fila, y sin declararla acá cada
                # una de las hasta 200 filas disparaba un refresh perezoso para
                # traer una columna que ya podía venir en la misma consulta.
                GamePlayer.exercises_correct,
                GamePlayer.university,
                GamePlayer.career,
                GamePlayer.theta,
                GamePlayer.user_id,
                GamePlayer.rank_recent,
                GamePlayer.rank_recent_at,
                GamePlayer.rank_snapshot,
                GamePlayer.rank_snapshot_at,
                # Cuántas respuestas ajustaron el Elo: es lo que decide si el
                # theta ya vale (elo.RAMP_UPDATES) o sigue siendo provisorio.
                GamePlayer.n_updates,
            )
        )
        .filter(*scope, visible)
        .order_by(*(_ORDEN_ELO if por_elo else _ORDEN_XP))
        .offset(page_offset)
        .limit(page_size)
        .all()
    )

    now = datetime.utcnow()
    entries = [
        GameLeaderboardEntry(
            rank=page_offset + index + 1,
            player_id=row.id,
            alias=row.alias,
            xp=row.xp,
            exercises_correct=row.exercises_correct,
            is_current_player=row.id == player.id,
            # Los sembrados cuentan como registrados: is_guest marca "todavía no
            # eligió su nombre", y eso solo aplica a gente real.
            is_guest=row.user_id is None and not row.is_bot,
            university=row.university,
            career=row.career,
            level=elo.level_of(row.theta),
            # El Elo viaja SIEMPRE, ordene la tabla por lo que ordene: es el
            # número que la fila muestra cuando el selector está en "elo", y
            # mandarlo solo en ese orden obligaría a recargar la lista entera
            # para cambiar de columna.
            elo=elo.rating_of(row.theta),
            elo_ranked=row.n_updates >= elo.RAMP_UPDATES,
            # Sin flecha en el orden por Elo. `rank_delta` se calcula contra
            # `rank_snapshot` / `rank_recent`, que son fotos del puesto por XP
            # (simulation.py): comparadas contra un puesto por Elo darían un
            # número que no es el movimiento de nadie.
            rank_delta=(
                0 if por_elo else simulation.rank_delta(row, page_offset + index + 1, now)
            ),
        )
        for index, row in enumerate(page)
    ]
    return GameLeaderboardResponse(
        entries=entries,
        total_count=total_count,
        has_more=page_offset + len(page) < total_count,
        me=GameLeaderboardMe(rank=my_rank, xp=player.xp),
    )


# Cuántos reclutas trae la lista. No es paginada: es la tabla de una persona, no
# la del juego, y con cincuenta reclutas la mecánica ya funcionó de sobra.
_MAX_RECLUTAS = 50


@router.get("/leaderboard/recruits", response_model=GameRecruitsResponse)
def game_recruits(
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """La vista "Reclutas" del ranking: quiénes entraron por tu link y cuánto te
    dieron.

    Ordenada por lo que APORTARON y no por su XP: es la tabla de quien reclutó.
    Los dos números casi siempre dan el mismo orden —el aporte es un porcentaje
    fijo del XP— pero no siempre, porque alguien pudo haber llegado con XP
    anterior a haber sido reclutado... y sobre todo porque el criterio tiene que
    ser el que la lista dice mostrar.

    Solo los que YA resolvieron algo. Un recluta que abrió el link y no jugó no
    aportó nada, y llenar la lista de renglones en cero convertiría el premio en
    una lista de gente que no vino.
    """
    filas = (
        db.query(GamePlayer)
        .options(
            load_only(
                GamePlayer.id,
                GamePlayer.alias,
                GamePlayer.university,
                GamePlayer.career,
                GamePlayer.theta,
                GamePlayer.referral_xp_given,
            )
        )
        .filter(
            GamePlayer.referred_by == player.id,
            # La lista muestra a los reclutas que ya resolvieron algo, con el
            # mismo predicado que decide quién entra al ranking: es la misma
            # pregunta ("¿jugó acá?") y no tiene por qué escribirse dos veces.
            ranking.RESOLVIO_ACA,
        )
        .order_by(GamePlayer.referral_xp_given.desc(), GamePlayer.id.asc())
        .limit(_MAX_RECLUTAS)
        .all()
    )
    # Los indicadores de arriba cuentan TODOS los reclutas, jugaron o no —a
    # diferencia de `filas`, que solo lista a los que ya aportaron algo (ver el
    # docstring). Es la misma cuenta que stats.py :: _xp_de_los_reclutas, del
    # lado del total.
    total_recruits, total_xp_given = (
        db.query(func.count(GamePlayer.id), func.sum(GamePlayer.referral_xp_given))
        .filter(GamePlayer.referred_by == player.id)
        .one()
    )
    return GameRecruitsResponse(
        entries=[
            GameRecruitEntry(
                rank=index + 1,
                player_id=fila.id,
                alias=fila.alias,
                university=fila.university,
                career=fila.career,
                level=elo.level_of(fila.theta),
                xp_given=fila.referral_xp_given,
            )
            for index, fila in enumerate(filas)
        ],
        share_percent=referrals.SHARE_PERCENT,
        total_recruits=total_recruits or 0,
        total_xp_given=total_xp_given or 0,
    )


@router.post("/link", response_model=GamePlayerOut)
def link_player(
    authorization: str = Header(None),
    x_game_token: str = Header(None),
    db: Session = Depends(get_db),
):
    """Merge explícito guest→user tras el registro. Idempotente."""
    user = _clerk_user(authorization, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    player = _jugador_del_usuario(db, user, x_game_token)
    # Igual que el alta: registrarse se anuncia en el feed, y no importa por cuál
    # de las dos puertas se haya entrado. `on_signup` deduplica por jugador.
    game_events.on_signup(db, player)
    db.commit()
    return _player_out(db, player)


def _puede_escribir(
    player: GamePlayer = Depends(get_current_player),
    authorization: str = Header(None),
) -> GamePlayer:
    """Quién tiene derecho a escribir. Devuelve el jugador o corta con el motivo.

    Es una dependencia y no un chequeo adentro del endpoint porque el ORDEN
    importa. El tope de frecuencia también es una dependencia, y FastAPI las
    resuelve en el orden en que aparecen en la firma: poniendo esta primero, un
    invitado se lleva el 403 que le explica qué hacer, y además no gasta cupo del
    limitador. Con el chequeo adentro del cuerpo pasaba lo contrario — el primer
    intento daba 403 y el segundo un 429 que no dice nada, porque el intento
    rechazado igual había consumido su turno.

    Escribir pide cuenta, por dos motivos distintos que apuntan al mismo lado:

    · Un invitado se crea con un POST sin credenciales de ningún tipo. Un mensaje
      suyo no tiene a nadie detrás a quien pedirle cuentas, y el único freno para
      fabricar invitados es un tope por IP — que a propósito es laxo, porque el
      público entra desde el wifi de una universidad (ver limits.py).
    · Es el mismo criterio con el que se elige el @: lo que muestra tu nombre a
      todos los demás pide cuenta. Y de paso el chat empuja el registro, igual que
      el @ y que los reclutas.

    Y pide la sesión de Clerk viva, no alcanza el token de invitado guardado, por
    exactamente la misma razón que el @: ese token no vence ni se puede revocar, y
    publicar bajo el nombre de alguien no se puede deshacer desde el otro lado.
    """
    if not _chat_habilitado():
        raise HTTPException(status_code=503, detail="El chat está apagado por ahora.")
    if player.user_id is None:
        raise HTTPException(status_code=403, detail="Registrate para escribir en el chat.")
    if not authorization:
        raise HTTPException(
            status_code=403, detail="Iniciá sesión de nuevo para escribir."
        )
    return player


@router.post("/message", response_model=GameMessageOut, status_code=201)
def post_message(
    body: GameMessageIn,
    player: GamePlayer = Depends(_puede_escribir),
    db: Session = Depends(get_db),
    _tope: None = Depends(limits.por_jugador(3, "message")),
):
    """Deja un mensaje en el chat.

    Hasta tres mensajes por minuto por jugador, y ese tope es parte del diseño
    y no una protección: el pedido era «dejar un mensaje cada cierto tiempo»,
    no ahogar una conversación de ida y vuelta. Lo aplica
    `limits.por_jugador(3, "message")`, que devuelve 429 con Retry-After. Quién puede
    escribir lo decide `_puede_escribir`, que corre antes (ver ahí por qué).
    """
    try:
        fila = game_chat.publicar(db, player, body.text)
    except game_chat.TextoRechazado as e:
        raise HTTPException(status_code=422, detail=e.motivo)
    db.commit()
    return GameMessageOut(
        id=fila.id,
        alias=fila.alias,
        level=fila.level,
        university=fila.university,
        text=fila.text,
        is_mine=True,
        seconds_ago=0,
    )


# Vocabulario cerrado a propósito: sin esto la tabla se llena de variantes con
# typo y las series del panel se parten en dos sin que nadie se entere.
_CTA_KINDS = ("cafecito", "share", "boost_offer", "register")
_CTA_ACTIONS = ("impression", "click")


@router.post(
    "/cta",
    status_code=204,
    # Telemetría: escribe una fila por llamada y no tiene deduplicación.
    dependencies=[Depends(limits.por_jugador(120, "cta"))],
)
def record_cta(
    body: GameCtaRequest,
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Registra que un llamado a la acción se VIO o se TOCÓ.

    Los mismos hechos ya viajan a PostHog, que sabe cosas que acá no están
    (sesión, dispositivo, referrer). Lo que PostHog no puede hacer es cerrar el
    embudo: el último escalón del cafecito es una fila en `game_boosts`, y esa
    tabla vive únicamente acá. Sin este endpoint el panel podría mostrar cuántos
    cafecitos entraron pero no sobre cuántas impresiones, que es justo el número
    que dice si el cartel funciona o si simplemente se muestra mucho.

    Devuelve 204 y nunca falla por contenido: es telemetría, y una telemetría
    que puede tirar un error en la mitad de una partida es peor que no tenerla.
    """
    if body.cta not in _CTA_KINDS or body.action not in _CTA_ACTIONS:
        return None
    db.add(
        GameCtaEvent(
            player_id=player.id,
            cta=body.cta,
            action=body.action,
            placement=(body.placement or None),
            solved=body.solved,
            # Se copia la universidad del momento en vez de joinear después: si la
            # persona la cambia mañana, el cartel de hoy se lo mostramos con la
            # que tenía hoy.
            university=player.university,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    return None


# ── Avisos push ──────────────────────────────────────────────────────────────
#
# Los gemelos de `/push/subscribe` y `/user/notification-settings` de Intervalo,
# pero autenticados con `get_current_player`, que resuelve tanto al invitado por
# su `X-Game-Token` como al registrado por Clerk. Esa es toda la diferencia y es
# la que importa: aquellos piden sesión, y entre el 50% y el 95% de cada cohorte
# del juego no tiene cuenta.

_HORA_RE = re.compile(r"^([01]\d|2[0-3]):(00|15|30|45)$")


@router.post("/push/subscribe", response_model=GameNotificationSettings)
def game_push_subscribe(
    body: GamePushSubscribeRequest,
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Guarda el navegador y devuelve las preferencias, para no pedirlas aparte."""
    from . import notifications as avisos

    avisos.upsert_subscription(
        db, player, body.endpoint, body.keys.p256dh, body.keys.auth
    )
    return avisos.get_settings(player)


@router.delete("/push/subscribe", response_model=GameNotificationSettings)
def game_push_unsubscribe(
    body: GamePushUnsubscribeRequest,
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    from . import notifications as avisos

    avisos.delete_subscription(db, player, body.endpoint)
    return avisos.get_settings(player)


@router.get("/notification-settings", response_model=GameNotificationSettings)
def game_get_notification_settings(
    player: GamePlayer = Depends(get_current_player),
):
    from . import notifications as avisos

    return avisos.get_settings(player)


@router.put("/notification-settings", response_model=GameNotificationSettings)
def game_put_notification_settings(
    body: GameNotificationSettingsRequest,
    player: GamePlayer = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Prender o apagar los avisos y elegir a qué hora.

    Las mismas validaciones que en Intervalo —y por eso el mismo formato de
    hora—: la franja de quince minutos es la que compara el resolutor, y un huso
    inválido rompería el tick de todas las noches, no el pedido de esta persona.
    """
    from . import notifications as avisos

    if body.enabled:
        if not body.time or not _HORA_RE.match(body.time):
            raise HTTPException(status_code=400, detail="time must be HH:MM in 15-min steps")
        if not body.timezone:
            raise HTTPException(status_code=400, detail="timezone is required")
        try:
            ZoneInfo(body.timezone)
        except ZoneInfoNotFoundError:
            raise HTTPException(status_code=400, detail="invalid timezone")

    return avisos.save_settings(
        db, player, enabled=body.enabled, time=body.time, timezone=body.timezone
    )
