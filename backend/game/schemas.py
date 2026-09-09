"""Modelos Pydantic del minijuego (requests y responses)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# Topes de tamaño de todo lo que entra por el cuerpo de un pedido.
#
# Hasta ahora varios campos no tenían ninguno y el recorte pasaba recién al
# guardar, o sea después de haber recibido y parseado el cuerpo entero: un envío
# de varios megabytes se procesaba completo para terminar guardando dos mil
# caracteres. Declararlos acá los rechaza en la puerta, con un 422 que además
# explica cuál campo se pasó.
_MAX_LATEX = 2000
_MAX_ALIAS = 40
_MAX_UNIVERSIDAD = 120
_MAX_ATRIBUCION = 32


class GamePlayerCreateRequest(BaseModel):
    # Atribución de primer contacto (?g=), mismas regex que /user/enroll.
    group_id: Optional[str] = Field(default=None, max_length=_MAX_ATRIBUCION)
    utm_source: Optional[str] = Field(default=None, max_length=_MAX_ATRIBUCION)
    # El @ de quien compartió el link (?r=). A diferencia de los dos de arriba
    # este no es solo analítica: deja al jugador anotado como recluta de ese @ y
    # a partir de ahí una parte de su XP se le paga (ver game/referrals.py).
    # Solo se mira al CREAR la fila; en un jugador que ya existe se ignora.
    referrer_alias: Optional[str] = Field(default=None, max_length=_MAX_ALIAS)


class GamePlayerOut(BaseModel):
    player_id: int
    alias: str
    xp: int
    rank: Optional[int] = None
    combo: int
    best_combo: int
    best_rank: Optional[int] = None
    exercises_correct: int
    exercises_attempted: int
    university: Optional[str] = None
    career: Optional[str] = None
    is_guest: bool
    # Si sigue en True, este @ todavía no pasó por la edición gratis de la
    # slide de "elegí tu @" (ver GamePlayer.alias_is_generated).
    alias_is_generated: bool = True
    # Nivel 0-3 derivado del θ (elo.level_of): el front lo pinta con los colores
    # de cinturón, como el ranking de Intervalo pinta el cinturón máximo.
    level: int = 0
    # El mismo θ en escala de ajedrez (elo.rating_of). Es el tercer marcador de
    # la card del ejercicio: a diferencia de la XP —que solo sube— este baja
    # cuando se erra, que es lo que lo vuelve una medida de qué tan difícil se
    # está resolviendo y no de cuánto se jugó.
    elo: int = 1000


class GamePlayerCreateResponse(BaseModel):
    player: GamePlayerOut
    # Solo en jugadores guest; el cliente lo guarda en localStorage.
    guest_token: Optional[str] = None


class GameProfilePatchRequest(BaseModel):
    alias: Optional[str] = Field(default=None, max_length=_MAX_ALIAS)
    university: Optional[str] = Field(default=None, max_length=_MAX_UNIVERSIDAD)
    career: Optional[str] = Field(default=None, max_length=8)


class GameExerciseOut(BaseModel):
    exercise_id: int
    prompt_latex: str
    tier: int
    difficulty_stars: int
    # Crudo, sin redondear a estrellas: lo necesita el festejo optimista del
    # front para estimar la XP de un acierto ANTES de que /answer conteste
    # (ver web/src/app/derivadas/xp-estimate.ts, espejo de game/xp.py).
    p_hat: float
    combo: int
    # Inventario COMPLETO de teclas desbloqueadas del jugador, en orden canónico
    # (ver game/keyboard.py). No es lo que este ejercicio necesita: es todo lo
    # que la persona ya se ganó, y crece.
    keys: list[str] = []
    # Las que se desbloquean con ESTE ejercicio, subconjunto de `keys`. El front
    # las usa para festejar solo lo nuevo en vez de animar la fila entera.
    new_keys: list[str] = []


class GameSkipRequest(BaseModel):
    exercise_id: int


class GameAnswerRequest(BaseModel):
    exercise_id: int
    answer_latex: str = Field(max_length=_MAX_LATEX)
    # Árbol MathJSON de @cortex-js/compute-engine (ce.parse(latex).json). El
    # tamaño lo acota mathjson.to_sympy durante el recorrido: acá todavía es un
    # objeto cualquiera y no hay forma de medirlo sin recorrerlo.
    answer_mathjson: Any = None
    # Cuánto tardó la persona, medido por el cliente. Acotado a un día: la
    # columna es un Integer de 32 bits y un valor cualquiera —que se puede
    # mandar a mano— rompía el insert con un 500 en el momento de responder.
    response_ms: Optional[int] = Field(default=None, ge=0, le=86_400_000)
    # La tabla de derivadas estuvo abierta en este ejercicio. Lo reporta el
    # cliente porque es el único que lo sabe; no hay nada que validar del lado
    # del server. Mentir acá solo sirve para PERDER (θ y XP), así que no hace
    # falta defenderlo.
    peeked: bool = False


class GameAnswerResponse(BaseModel):
    correct: bool
    parse_ok: bool
    # Mensaje cuando parse_ok=False ("no pudimos evaluar tu respuesta").
    parse_error: Optional[str] = None
    attempt_number: int
    # None es «sin límite», que es lo que los intentos son: se responde hasta
    # acertar o saltear. El campo sobrevive a la mecánica de dos intentos porque
    # lo lee cualquier cliente viejo que todavía esté abierto en un teléfono; el
    # nuevo decide si el ejercicio se cerró mirando `correct`.
    attempts_left: Optional[int] = None
    feedback_incorrect: Optional[str] = None
    xp_awarded: int
    xp_total: int
    combo: int
    combo_bonus: int
    # Empuje de la universidad ya aplicado a `xp_awarded` y `combo_bonus`. 1.0 = sin
    # empuje. Viaja para que el festejo pueda decir por qué el número es más
    # grande que de costumbre.
    xp_multiplier: float = 1.0
    # Correctas acumuladas por el jugador DESPUÉS de esta respuesta. Va acá y no
    # se deduce en el cliente porque los hitos del juego —la pausa para el
    # cafecito, cada N resueltas— se cuentan sobre la partida entera y no sobre
    # la pestaña: contándolas del lado del front, cada recarga volvía el
    # contador a cero y el hito no llegaba nunca.
    exercises_correct: int = 0
    # Correctas de HOY, en hora argentina. Es lo que la pausa del cafecito le
    # dice a la persona ("ya llevás 23 derivadas resueltas hoy"): el total
    # histórico no sirve ahí, porque el mérito del que se está hablando es el de
    # esta sentada.
    correct_today: int = 0
    # Siempre None desde que los intentos son ilimitados: ya no existe el
    # ejercicio que se cierra sin acierto. La derivada correcta ahora llega por
    # un solo camino, el «¿Por qué?» (POST /explain).
    correct_answer_latex: Optional[str] = None
    rank_before: Optional[int] = None
    rank_after: Optional[int] = None
    best_rank: Optional[int] = None
    is_record: bool = False


class GameExplainRequest(BaseModel):
    exercise_id: int


class GameExplainOut(BaseModel):
    """La explicación del «¿Por qué?», en el mismo MathText que el banco de
    Intervalo: prosa con `$…$` y bloques `$$…$$`."""

    explanation: str
    # Este pedido dejó marcado el ejercicio, o sea que acertarlo va a pagar
    # XP_EXPLICADO. False cuando ya estaba cerrado (se acertó y no queda nada
    # que cobrar) o cuando ya se había abierto antes.
    costs_xp: bool = False
    # El gráfico de cierre: f y f' en los mismos ejes. Lo pintan las dos
    # vistas (web/src/app/derivadas/mobile-flow.tsx y desktop-layout.tsx), y
    # viaja siempre: a diferencia de `graph_fn` en el banco de Intervalo, acá
    # NINGÚN ejercicio se queda sin curva que mostrar (ver game/explain.py ::
    # Explanation). `graph_fn`/`graph_fn2` son fórmulas de mathjs, no LaTeX —
    # mismo formato que usa el banco de Intervalo para sus propios `GRAF`
    # (ver `web/components/math-graph.tsx`).
    graph_fn: str
    graph_fn2: str
    # La MISMA f y f' de arriba, en LaTeX: solo para la leyenda del gráfico,
    # que si no diría un genérico "f(x)"/"f'(x)" en vez de la fórmula real.
    graph_fn_latex: str
    graph_fn2_latex: str
    graph_view: list[float]


class GameLeaderboardEntry(BaseModel):
    rank: int
    player_id: int
    alias: str
    xp: int
    exercises_correct: int
    is_current_player: bool
    is_guest: bool
    university: Optional[str] = None
    career: Optional[str] = None
    level: int = 0
    # El Elo de la persona (game/elo.py :: rating_of). Viaja siempre, ordene el
    # ranking por XP o por Elo: es la otra columna que la fila puede mostrar, y
    # el selector la cambia sin recargar nada.
    elo: int = 0
    # ¿Ya salió de la rampa? Con menos de elo.RAMP_UPDATES respuestas el Elo
    # todavía es provisorio: la fila muestra "—" en vez de un número, y en el
    # orden por Elo va detrás de todos los calificados. Mismo criterio que
    # `GameUniversityRow.ranked`, una persona en vez de una universidad.
    elo_ranked: bool = False
    # Puestos ganados (+) o perdidos (−) en los últimos minutos. 0 = sin
    # movimiento reciente, y el front no dibuja flecha. Siempre 0 en el orden
    # por Elo: las fotos contra las que se compara son del puesto por XP.
    rank_delta: int = 0


class GameRecruitEntry(BaseModel):
    """Un renglón de la vista "Reclutas" del ranking.

    NO lleva la XP propia del recluta, a propósito. La única columna que importa
    acá es `xp_given`: es la tabla de quien reclutó, no la del juego, y con los
    dos números al lado el ojo compara y el que importa pierde.
    """

    rank: int
    player_id: int
    alias: str
    university: Optional[str] = None
    career: Optional[str] = None
    level: int = 0
    # Cuánta XP le dio este recluta a quien lo trajo, desde que llegó.
    xp_given: int


class GameRecruitsResponse(BaseModel):
    entries: list[GameRecruitEntry]
    # El porcentaje vigente, para que la diapo y la lista no lo tengan escrito a
    # mano en el cliente: si algún día cambia, cambia en un solo lado.
    share_percent: int
    # Los indicadores de arriba cuando el ranking está en esta vista. TODOS los
    # reclutas y su aporte total, sin el filtro de actividad que sí tiene
    # `entries` (ver game/stats.py :: _xp_de_los_reclutas, es la misma cuenta):
    # un recluta que abrió el link y no jugó no gana un renglón en la lista,
    # pero sigue siendo alguien que trajiste, y el número de arriba no puede
    # decir menos reclutas de los que en verdad hay.
    total_recruits: int = 0
    total_xp_given: int = 0


class GameLeaderboardMe(BaseModel):
    rank: Optional[int] = None
    xp: int


class GameLeaderboardResponse(BaseModel):
    entries: list[GameLeaderboardEntry]
    total_count: int
    has_more: bool
    me: GameLeaderboardMe


class GameLeaderboardSummary(BaseModel):
    """Los dos números de la cabecera + las universidades para poblar el filtro.

    Cuenta la misma población que muestra la lista de abajo (sembrados
    incluidos): un contador que dijera otra cosa contradiría al ranking.
    """

    players: int
    exercises: int
    universities: list[str]
    # Elo promedio del mismo scope, mismo cálculo que GameUniversityRow.rating_avg
    # (promedio en θ, solo jugadores ya salidos de la rampa) pero sin agrupar por
    # universidad. None con menos de boosts.MIN_PLAYERS_RANKED calificados — un
    # promedio de tres personas no es un promedio, es quiénes son esas tres.
    elo_avg: Optional[int] = None


class GameStatsHistogramBucket(BaseModel):
    """Un escalón del histograma de Elo (ver game/stats.py :: _histograma)."""

    from_rating: int
    to_rating: int
    count: int


class GameStatsRow(BaseModel):
    """Una fila de la tabla de derivadas, con las dos columnas que agrega el
    panel de estadísticas (ver game/stats.py :: ROW_TEMPLATES)."""

    slug: str
    # None en dos casos DISTINTOS que el front tiene que poder diferenciar: la
    # fila no tiene ninguna plantilla en el generador (inv_x/sqrt_x/tan_x —
    # siempre None) o la tiene pero nadie la vio nunca (ahí cae en la semilla
    # del tier vía elo.effective_beta y nunca da None).
    unlock_elo: Optional[int] = None
    # None si hay menos de stats.MIN_MUESTRA_FILA intentos limpios en la
    # ventana de los últimos 10.
    accuracy: Optional[int] = None
    # Cuántos de los últimos 10 hay de verdad (0-10). El front lo usa para el
    # tooltip del placeholder ("resolviste 2 de este tipo, todavía poco").
    sample: int = 0
    # Milisegundos, promedio de la MISMA ventana que `accuracy`. None con el
    # mismo criterio que `accuracy` (menos de MIN_MUESTRA_FILA intentos con
    # tiempo registrado) — sin comparación contra el resto de los jugadores,
    # a propósito: es un dato personal, no un ranking de velocidad.
    avg_response_ms: Optional[int] = None


class GameStatsGeneral(BaseModel):
    exercises_correct: int
    exercises_attempted: int
    accuracy_overall: Optional[int] = None
    best_combo: int
    best_rank: Optional[int] = None
    days_playing: int
    xp: int
    # Los dos son de ESTA persona y de los dos lados que le dan XP sin
    # resolver una derivada más: lo que le generaron sus reclutas, y lo que le
    # agregó el empuje de su universidad.
    xp_from_referrals: int = 0
    xp_from_boosts: int = 0


class GameStatsOut(BaseModel):
    """Payload de GET /stats: el Elo del jugador contra la masa de jugadores
    calificados, más la tabla de derivadas enriquecida con Elo de desbloqueo
    y accuracy personal (ver game/stats.py)."""

    n_rated_players: int
    # Si es False, `histogram`/`player_bucket_index`/`percentile` vienen
    # vacíos: con pocos jugadores calificados una campana no es un gráfico,
    # es señalar quiénes son (ver stats.MIN_HISTOGRAM_PLAYERS).
    enough_for_histogram: bool
    histogram: list[GameStatsHistogramBucket] = []
    player_rating: int
    player_bucket_index: Optional[int] = None
    percentile: Optional[int] = None
    general: GameStatsGeneral
    rows: list[GameStatsRow]


class GameBoostOut(BaseModel):
    """Un empuje de XP vigente, agregado por universidad (ver game/boosts.py)."""

    # NULL = empuje GLOBAL, para todo el juego. Es a dónde va la donación que no
    # se pudo atribuir a ninguna universidad.
    university: Optional[str] = None
    multiplier: float
    cafecitos: int
    donor_name: Optional[str] = None
    # Segundos que le quedan, NO un instante. Los datetime del proyecto son
    # naive UTC, y mandar un instante sin zona a un cliente que lo va a comparar
    # contra su reloj local es pedir un bug de zonas horarias. Un entero de
    # segundos no tiene ambigüedad.
    expires_in_seconds: int


class GameCafecitoStatus(BaseModel):
    """Qué pasó con la donación de quien acaba de volver de Cafecito.

    Existe por un agujero del embudo: la persona tocaba «invitar», se iba a
    Cafecito en otra pestaña, pagaba, volvía — y encontraba la misma pantalla que
    había dejado, como si no hubiera hecho nada. Es el peor momento posible para
    no decir nada, porque acaba de pagar.

    El estado sale de `game_boost_intents.consumed_at`, que es exacto y no una
    adivinanza: la donación que llega marca como cumplidas las intenciones
    abiertas (ver boosts.resolve_donation), así que si la de esta persona está
    marcada, su cafecito llegó.
    """

    # "none"     — no tocó el botón (o fue hace mucho)
    # "pending"  — lo tocó y todavía no llegó nada
    # "credited" — llegó y ya está aplicado
    state: str
    # A dónde fue el empuje. None con state="credited" es el empuje GLOBAL, que
    # es donde cae la donación de quien todavía no eligió universidad.
    university: Optional[str] = None
    cafecitos: int = 0
    # El multiplicador que le toca a ESTA persona ahora mismo, con todo lo
    # vigente ya sumado; es el número que la pantalla muestra grande.
    multiplier: float = 1.0
    expires_in_seconds: int = 0


class GamePulse(BaseModel):
    """Latido del ranking. El cliente lo consulta cada 10 s y solo refresca la
    lista si `version` cambió — y de paso ese mismo pedido es lo que hace
    avanzar la actividad simulada (ver game/simulation.py).

    Los empujes vigentes viajan acá y no en un endpoint propio: este pedido ya
    late cada 10 s desde los dos layouts, así que el cartel se entera sin sumar
    ni una request."""

    version: int
    boosts: list[GameBoostOut] = []


class GameEventOut(BaseModel):
    """Una línea del historial. El emoji viaja aparte del texto para que el
    cliente lo pueda poner siempre al final, sin depender del copy."""

    id: int
    kind: str
    # Con marcadores: `{a}` es el protagonista, `{b}` un segundo protagonista
    # (si lo hay) y `{u0}`/`{u1}` las siglas. El cliente los reemplaza por la tag
    # de cada universidad y por el nombre pintado con el color de su nivel. Las
    # filas viejas no traen marcadores y salen tal cual, que es exactamente lo
    # que corresponde.
    text: str
    emoji: str
    actor_alias: Optional[str] = None
    # Nivel del protagonista, para pintarlo igual que en el ranking. NULL cuando
    # el nombre no es de un jugador (quien invita un cafecito).
    actor_level: Optional[int] = None
    # Sin nivel propio: se pinta semibold sin color, como las siglas de
    # universidad. Ver GameEvent.actor_b_alias (models.py).
    actor_b_alias: Optional[str] = None
    # En el mismo orden en que aparecen {u0} y {u1}.
    universities: list[str] = []
    # Los dos resaltados del feed: "esto sos vos" y "esto es tu universidad".
    is_mine: bool = False
    is_my_university: bool = False
    # Segundos, no un instante: mismo motivo que en GameBoostOut.
    seconds_ago: int


class GameMessageOut(BaseModel):
    """Un mensaje del chat. Trae el @ y la universidad DE CUANDO SE ESCRIBIÓ, no
    los de hoy: ver el docstring de GameMessage en models.py."""

    id: int
    alias: str
    # Para pintar el @ con el color de su nivel, igual que en el ranking.
    level: int
    university: Optional[str] = None
    text: str
    is_mine: bool = False
    # Segundos, no un instante: mismo motivo que en GameEventOut.
    seconds_ago: int


class GameMessageIn(BaseModel):
    # Tope laxo a propósito, como el de la universidad: el de verdad son los 140
    # de chat.MAX_TEXTO. Este solo está para que un cuerpo absurdo muera en la
    # puerta sin llegar al saneado.
    text: str = Field(min_length=1, max_length=400)


class GameEventsResponse(BaseModel):
    events: list[GameEventOut]
    # Si el chat acepta mensajes ahora mismo (GAME_CHAT_ENABLED). Viaja con el
    # feed y no en /me porque es una propiedad del SERVICIO y no de la persona:
    # así el cliente puede apagar el campo con el motivo de verdad en vez de
    # dejar escribir para después contestar 503. Un interruptor que solo conoce
    # el servidor es medio interruptor.
    chat_enabled: bool = False
    # Los mensajes viajan en la MISMA respuesta que las novedades, y esa es toda
    # la idea del chat: no hay un sondeo nuevo, hay un campo nuevo en el que ya
    # existía. Dos listas y no una mezclada, porque cada una tiene su cursor y su
    # ventana — si compartieran las cuarenta filas, una racha de chat taparía el
    # anuncio de cafecitos.
    messages: list[GameMessageOut] = []


class GameUniversityRow(BaseModel):
    university: str
    xp: int
    players: int
    # Elo promedio de la universidad, en la escala de ajedrez (elo.rating_of).
    # Es el número por el que ordena el ranking Y el que se muestra: ordenar por
    # uno y mostrar otro se lee como un bug.
    #
    # Elo y no XP: la XP mide cuánto jugaste y el Elo qué tan difícil resolvés.
    # Con XP promedio gana la universidad que más horas le puso; con Elo, la que
    # mejor deriva — que es la pelea que el juego quiere tener.
    rating_avg: int
    # Jugadores con Elo ya establecido (los que cuentan para `rating_avg`).
    rated_players: int = 0
    # False = tiene menos de MIN_PLAYERS_RANKED jugadores con Elo. No se la esconde: se
    # devuelve igual, y la UI la muestra apagada al pie. Un ranking que borra tu
    # universidad sin explicación es peor que uno imperfecto.
    ranked: bool = True
    careers: dict[str, int]


class GameUniversityLeaderboardResponse(BaseModel):
    rows: list[GameUniversityRow]
    total_players: int
    total_universities: int


class GameCtaRequest(BaseModel):
    """Telemetría de un llamado a la acción. Todo opcional salvo qué y qué pasó:
    el cliente manda lo que sabe y el server no discute."""

    cta: str = Field(max_length=32)
    action: str = Field(max_length=32)
    placement: Optional[str] = Field(default=None, max_length=24)
    # Mismo caso que response_ms: es un Integer en la base, y este endpoint está
    # documentado como "nunca falla por contenido". Sin el tope, un valor grande
    # lo hacía fallar con un 500 en el commit.
    solved: Optional[int] = Field(default=None, ge=0, le=1_000_000)


# ── Avisos push ──────────────────────────────────────────────────────────────
# Espejo de los de Intervalo (main.py), y no importados de allá porque el router
# del juego no puede importar main sin cerrar un ciclo. La forma es la misma
# porque el cliente es el mismo: `PushSubscription.toJSON()` del navegador.


class GamePushKeys(BaseModel):
    p256dh: str
    auth: str


class GamePushSubscribeRequest(BaseModel):
    endpoint: str
    keys: GamePushKeys


class GamePushUnsubscribeRequest(BaseModel):
    endpoint: str


class GameNotificationSettings(BaseModel):
    enabled: bool
    time: Optional[str] = None      # "HH:MM", pasos de 15 minutos
    timezone: Optional[str] = None  # nombre IANA


class GameNotificationSettingsRequest(BaseModel):
    enabled: bool
    time: Optional[str] = None
    timezone: Optional[str] = None
