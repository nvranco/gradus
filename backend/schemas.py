"""
schemas.py — Pydantic response models for the HTTP API.

Kept in a single module so the OpenAPI spec exposes clean, named schemas that
`openapi-typescript` can turn into useful TypeScript types. Request bodies for
POSTs still live next to the endpoints that consume them in `main.py`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


# ── Generic ───────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str


# ── Enrollment ────────────────────────────────────────────────────────────────

class EnrollmentResponse(BaseModel):
    success: bool
    message: str


# ── Status ────────────────────────────────────────────────────────────────────

class UserStatusResponse(BaseModel):
    # Authoritative "new vs returning" signal, read from the DB rather than from
    # client-writable Clerk metadata (which drifts). `enrolled` means the user
    # finished onboarding; `has_progress` means they have any learning state.
    enrolled: bool
    has_progress: bool


# ── Progress ──────────────────────────────────────────────────────────────────

class SkillProgress(BaseModel):
    exercise_type: str
    state: str          # "sin_empezar" | "aprendiendo" | "dominado"
    next_review: str | None = None  # ISO date del próximo repaso de este skill


class TopicProgress(BaseModel):
    phase: str          # "learning" | "review"
    step_index: int
    status: str         # "nuevo" | "aprendiendo" | "dominado"
    progress: str       # "0/3" | "1/3" | "2/3" | "3/3"
    is_pending: bool
    attempted: bool
    next_review: str | None = None
    failed: bool
    suspended: bool = False
    skills: list[SkillProgress] = []


class StreakInfo(BaseModel):
    days: int                   # días de actividad acumulados (racha global)
    multiplier: float           # multiplicador de XP de Repaso vigente
    next_threshold: int | None  # días del próximo tramo; None en el máximo
    next_multiplier: float | None
    days_to_next: int           # 0 en el tramo máximo
    is_max: bool
    tier_reached: bool = False  # el total cae justo en un piso: hoy se desbloqueó el multiplicador
    prev_multiplier: float | None = None  # multiplicador del tramo anterior; None en el base
    counted_today: bool         # esta sesión fue la primera completada del día
    xp_bonus: int = 0           # XP extra ganado en esta sesión gracias al multiplicador (solo summary)


class BoostTramo(BaseModel):
    """Un empuje de cafecito vigente, ya agregado. `university=None` = global.

    Proyección fiel de `game.boosts.BoostView`: los mismos campos con los
    mismos significados. Lo usan dos pantallas distintas —la tile de Practicar,
    que muestra los tramos que le tocan a UNA persona, y el cartel del ranking,
    que muestra los de TODAS las universidades— porque en las dos el objeto es
    el mismo: un cafecito vigente.
    """

    university: str | None
    # Lo que ESTE tramo solo multiplica. Ojo: `BoostInfo.multiplier` NO es la
    # suma de los multiplicadores de sus tramos. Los cafecitos se suman primero
    # y recién después se convierten en factor (`multiplier_from_cafecitos`), así
    # que dos tramos de ×1,2 no dan ×2,4 sino ×1,4.
    multiplier: float
    # El total CRUDO de cafecitos, sin el tope por donación que sí tiene
    # `multiplier` (ver BoostView): con una donación de 30 valen ×2,0 y 30.
    cafecitos: int
    donor_name: str | None
    expires_in_seconds: int
    # Parte del multiplicador la puso el aforo del día y no una donación (ver
    # game/aforo.py). Va aparte de `cafecitos` —que cuenta solo lo donado— para
    # que el cartel pueda decir la verdad en los tres casos: solo donaciones,
    # solo aforo, o las dos cosas sumadas.
    aforo: bool = False


class BoostInfo(BaseModel):
    """El empuje de cafecito que le toca a esta persona, si hay alguno.

    Es una LISTA de tramos más dos escalares derivados, y no un empuje solo,
    porque se pueden estar cobrando dos a la vez —el global y el de su
    universidad— con dos donantes y dos vencimientos distintos. El multiplicador
    es la suma de los dos, así que no le pertenece a ninguno de los tramos.
    """

    multiplier: float            # el factor del empuje solo
    effective_multiplier: float  # racha × empuje, topeado: el que de verdad se cobra
    # El MÍNIMO de los tramos: el instante en que el número deja de ser cierto.
    expires_in_seconds: int
    tramos: list[BoostTramo]


class UserProgressResponse(BaseModel):
    topic_states: dict[str, TopicProgress]
    # None cuando no hay ningún empuje corriendo, que es casi siempre.
    boost: BoostInfo | None = None
    main_session_done_today: bool
    last_course: str | None = None
    active_cap: int = 18          # ítems en aprendizaje permitidos a la vez
    total_items: int = 0          # total de ítems del curso (máx del cap)
    iteration: int = 1            # iteración de progreso vigente
    session_size: int = 8         # máx de ejercicios por sesión de repaso
    session_size_max: int = 30    # tope superior del selector de session_size
    streak: StreakInfo


class PracticeStatsResponse(BaseModel):
    # Stats del usuario para un curso (iteración vigente), solo modo práctica.
    sessions_completed: int   # sesiones de práctica terminadas
    exercises_answered: int   # ejercicios respondidos en sesiones de práctica
    exercises_correct: int    # ejercicios acertados en sesiones de práctica


# ── Editor de curso ─────────────────────────────────────────────────────────

class TopicActionRequest(BaseModel):
    belt: str
    topic: str


class ActiveCapRequest(BaseModel):
    value: int


class CapPreviewResponse(BaseModel):
    value: int                 # valor clampeado (1..total)
    unlock: list[str] = []     # claves "belt/topic" que se desbloquean
    lock: list[str] = []       # claves "belt/topic" que se re-bloquean


class CourseResetResponse(BaseModel):
    iteration: int             # nueva iteración de progreso


# ── Push notifications ──────────────────────────────────────────────────────────

class NotificationSettings(BaseModel):
    enabled: bool
    time: str | None = None       # "HH:MM", 15-min steps
    timezone: str | None = None   # IANA name


class SimpleResponse(BaseModel):
    success: bool


# ── Lifecycle emails ─────────────────────────────────────────────────────────────

class EmailRunResponse(BaseModel):
    bounce_sent: int
    winback_sent: int
    streak_tier_sent: int
    report_thanks_sent: int
    # Los dos mails del cruce. Sin declararlos acá, FastAPI los descartaba del
    # response y el log del worker decía que no se mandó ninguno.
    cafecito_efecto_sent: int = 0
    reclutas_sent: int = 0


class SweepAbandonedResponse(BaseModel):
    marked: int


# ── Emoji unlock tree (badges) ──────────────────────────────────────────────────

class EmojiStateResponse(BaseModel):
    # Estado dinámico del árbol de desbloqueo del usuario. La estructura estática
    # del árbol vive en el front (emoji-tree.generated.ts); acá solo va el estado.
    # El conjunto desbloqueado no se persiste: se deriva de total_xp (todo nodo
    # con depth <= profundidad alcanzada está desbloqueado, en cualquier rama).
    bucket: str | None = None      # E/S/T/M/Otra (de la enrollment); None si no hay
    total_xp: int = 0
    worn: str | None = None        # id vestido; None → raíz del bucket (default)


# ── Feedback ──────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    categoria: Literal["error", "idea", "comentario"]
    mensaje: str


class PushSubscriptionOut(BaseModel):
    id: int
    endpoint: str
    p256dh: str
    auth: str


class DueNotification(BaseModel):
    user_id: int
    pending_count: int
    title: str
    body: str
    notification_id: int
    subscriptions: list[PushSubscriptionOut]


class DueGameNotification(BaseModel):
    """Un aviso del minijuego listo para mandar.

    Misma forma que `DueNotification` salvo dos campos, y las dos diferencias
    tienen motivo. `player_id` en vez de `user_id` porque el destinatario puede
    ser un invitado, que no tiene fila en `users` —que es justamente el caso que
    este canal existe para cubrir—. Y `url` porque son dos apps instaladas: sin
    decirlo, el service worker abre la home de Intervalo (tiene `"/"`
    hardcodeado) y el tap lleva al producto equivocado.

    No lleva `pending_count`: ese número son los repasos SM-2 que la persona
    tiene vencidos, y el juego no tiene repasos.
    """

    player_id: int
    title: str
    body: str
    notification_id: int
    url: str
    subscriptions: list[PushSubscriptionOut]


# ── Leaderboard ───────────────────────────────────────────────────────────────

class LeaderboardEntry(BaseModel):
    rank: int
    user_id: int
    name: str
    username: str | None = None
    total_xp: int
    exercises: int
    is_current_user: bool
    career: str | None = None
    university: str | None = None
    emoji: str | None = None  # emoji vestido; None → el front cae al de bucket
    belt: str = "white"  # máximo cinturón desbloqueado (en cualquier curso)


class LeaderboardMe(BaseModel):
    # Datos del usuario actual dentro del scope (filtro) pedido. Se calculan
    # sobre el set completo, no sobre la página, así "posición actual" no depende
    # de cuántas filas se hayan cargado.
    rank: int | None = None       # posición en el scope, None si no aparece
    total_xp: int = 0


class LeaderboardResponse(BaseModel):
    entries: list[LeaderboardEntry]  # solo la página pedida (offset..offset+limit)
    total_xp: int                    # total del scope (global o por universidad)
    total_exercises: int             # total del scope
    total_count: int                 # cantidad de usuarios en el scope
    has_more: bool                   # quedan más páginas después de esta
    me: LeaderboardMe
    universities: list[str]          # universidades presentes (para el filtro)


class RecruitEntry(BaseModel):
    """Una persona que entró por tu link. Deliberadamente NO lleva su XP total:
    lo que se muestra es cuánto te aportó, no cuánto estudia."""

    rank: int
    username: str | None = None
    university: str | None = None
    career: str | None = None
    xp_given: int  # XP que ESTA persona te generó
    # El mismo cinturón máximo que lleva su fila del ranking individual, y por el
    # mismo motivo: es lo que pinta el nombre. Es la misma persona en las dos
    # tablas y tiene que verse igual en las dos.
    belt: str = "white"


class RecruitsResponse(BaseModel):
    entries: list[RecruitEntry]
    # Los contadores cuentan a TODOS los reclutas, también a los que todavía no
    # resolvieron nada; la lista muestra solo a los que ya aportaron. Es la misma
    # asimetría que el ranking del minijuego, y es a propósito: "trajiste a 8
    # personas" es la noticia, aunque 3 no hayan arrancado.
    total_recruits: int
    total_xp_given: int
    # El porcentaje viaja para que el cliente nunca lo hardcodee: está escrito
    # con todas las letras en la diapo, así que cambiarlo es cambiar una promesa.
    share_percent: int
    # El @ propio, que es lo que arma el link para compartir.
    handle: str | None = None


class UniversityRankRow(BaseModel):
    university: str
    total_xp: int                 # XP acumulada por sus estudiantes
    students: int                 # estudiantes con esta universidad
    careers: dict[str, int]       # conteo por carrera; llaves: E, S, T, M, Otra


class UniversityLeaderboardResponse(BaseModel):
    rows: list[UniversityRankRow]  # ordenadas por total_xp desc
    total_students: int            # estudiantes con universidad (suma de rows)
    total_universities: int        # universidades distintas
    career_totals: dict[str, int]  # agregado global por carrera (llaves E,S,T,M,Otra)


class PublicUniversityStat(BaseModel):
    university: str
    students: int
    total_xp: int


class PublicUniversityLeaderboardResponse(BaseModel):
    # Snapshot agregado sin auth para la landing (marketing-home.tsx) — un
    # visitante sin cuenta no tiene sesión para pegarle a /leaderboard/universities.
    # Sin PII: solo universidad + conteos, top 8 por XP (mismo orden que
    # /leaderboard/universities).
    rows: list[PublicUniversityStat]


class LeaderboardSummaryResponse(BaseModel):
    # Números generales de la cabecera del leaderboard, SIEMPRE globales (sin
    # filtros de carrera/universidad).
    total_students: int            # usuarios con universidad registrada
    total_exercises: int           # ejercicios resueltos (todos los usuarios)
    universities: list[str]        # universidades presentes (para el filtro)
    # Los empujes de cafecito vigentes, de TODAS las universidades y no solo de
    # la de quien mira. Esa es la mecánica, no un descuido: ver que la UTN está
    # en ×2,0 mientras la propia está en nada es lo que hace mirar cuánto sale un
    # cafecito (context/gamification.md: la pregunta es cómo esto alimenta la
    # competencia entre universidades).
    #
    # Tampoco los toca el filtro de carrera/universidad de la cabecera: filtrar
    # el ranking a la UBA no apaga el empuje de la UTN, solo deja de mostrar sus
    # filas. Viajan acá y no por un endpoint nuevo porque son cabecera igual que
    # los dos números de arriba, y así el ranking sigue haciendo dos pedidos.
    boosts: list[BoostTramo] = []
    # La universidad de quien MIRA. Dos pantallas la necesitan y ninguna la tenía:
    # el cartel de empujes, que pone la propia primero, y el estado vacío de
    # Reclutas, que pinta sus renglones de ejemplo con la propia porque la promesa
    # es "así se va a ver TU universidad creciendo".
    #
    # Viaja acá y en ningún otro lado a propósito: es un hecho solo, y tenerlo
    # también colgado de la respuesta de reclutas serían dos campos que pueden
    # empezar a decir cosas distintas.
    university: str | None = None


# ── Session ───────────────────────────────────────────────────────────────────

class SessionExercise(BaseModel):
    id: str
    external_id: str = ""
    exercise_type: str = ""
    question: str
    options: list[str]
    correct_index: int
    has_math: bool
    topic: str
    belt: str
    graph_fn: str
    graph_view: list[Any] | None = None
    graph_shade: list[Any] | None = None
    graph_free_aspect: bool | None = None
    table: dict[str, Any] | None = None
    feedback_correct: str
    feedback_incorrect: str | list[str | None]
    explanation: str | None = None


class SessionSurvey(BaseModel):
    """Marca qué ejercicio de la sesión (si alguno) lleva micro-encuesta de
    feedback y de qué tipo. Ver feedback_survey.py. `exercise_id` es el slot
    de sesión (ej. "ex_003"), no la clave real del ejercicio."""
    exercise_id: str
    type: str  # "A" (dificultad) | "B" (explicación)


class SessionStartResponse(BaseModel):
    session_id: str
    user_name: str
    total: int
    mode: str = "main"
    exercises: list[SessionExercise]
    survey: SessionSurvey | None = None


class AnswerResponse(BaseModel):
    correct: bool
    quality: int       # SM-2 quality score
    feedback: str
    xp_earned: int


class SummaryItem(BaseModel):
    topic: str
    belt: str
    correct: bool


class SessionSummaryResponse(BaseModel):
    session_id: str
    user_name: str
    mode: str
    course: str
    total: int
    correct: int
    first_try_correct: int
    incorrect: int
    items: list[SummaryItem]
    topic_states: dict[str, TopicProgress]
    xp_earned: int
    streak: StreakInfo
    # Cuánta de la XP de arriba la puso el empuje de cafecito. Va SEPARADO de
    # `streak.xp_bonus`, que es solo el de la racha: los dos multiplicadores se
    # aplican sobre la misma base, así que sumarlos en un número deja al resumen
    # mostrando una cuenta que no cierra con ninguno de los dos.
    xp_from_boost: int = 0
    session_number: int  # nº de orden de esta sesión entre todas las terminadas por el usuario
    # ¿Le pedimos algo en esta pantalla, y cuál de las dos cosas?
    #
    # Lo decide el SERVIDOR y no el cliente por dos motivos: una de las señales
    # —haber instalado la PWA— vive en la base y no en el dispositivo (quien la
    # instaló en el teléfono y abre en la compu tiene que contar igual), y la
    # separación entre los dos pedidos necesita recordar en qué sesión salió el
    # anterior. Ver backend/summary_asks.py.
    pedido: Literal["cafecito", "reclutas"] | None = None
    # El @ de quien mira y el porcentaje vigente, para armar el pedido de
    # reclutas sin un segundo request desde el resumen. `handle` en None mientras
    # no tenga: el CTA se deshabilita solo, igual que en el ranking.
    handle: str | None = None
    share_percent: int = 10
