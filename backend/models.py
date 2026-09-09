from datetime import datetime
from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    String,
    Text,
    Float,
    Boolean,
    DateTime,
    Date,
    ForeignKey,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    """User model — identity is owned by Clerk; `clerk_user_id` is the link."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(200), unique=True, index=True, nullable=True)
    email = Column(String(200), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=True)
    username = Column(String(30), unique=True, index=True, nullable=True)
    total_xp = Column(Integer, nullable=False, default=0)

    # Daily push-notification preferences. `notify_time` is "HH:MM" (15-min
    # steps) interpreted in `notify_timezone` (IANA). `notify_last_sent_on` is
    # the per-user idempotency guard (one send per local day).
    notify_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    notify_time = Column(String(5), nullable=True)
    notify_timezone = Column(String(64), nullable=True)
    notify_last_sent_on = Column(Date, nullable=True)

    # Rotación de copy: última categoría/variante enviada, para no repetirla en
    # el próximo envío (ver notification_copy.py). Se setea junto con
    # notify_last_sent_on en la misma transacción de claim (due_notifications).
    notify_last_category = Column(String(32), nullable=True)
    notify_last_variant_key = Column(String(64), nullable=True)

    # Tope de las notificaciones DE EVENTO (reclutas y cafecito), aparte del de
    # la normal. La regla es 3 por día: una normal, hasta dos de evento.
    #
    # Contador propio y no un `notify_last_sent_on` compartido: si compartieran
    # cupo, un cafecito de la mañana le comería el recordatorio de estudio del
    # mediodía, que es la que sostiene el hábito. `notify_events_on` guarda el
    # día local al que corresponde el conteo, así se reinicia solo al cambiar de
    # día sin necesitar un job que lo limpie.
    # Última semana en que se mandó el resumen de reclutas. Guarda la FECHA y no
    # un booleano para que el guard sea "ya se le mandó esta semana" y no "ya se
    # le mandó alguna vez".
    reclutas_email_sent_on = Column(Date, nullable=True)

    notify_events_on = Column(Date, nullable=True)
    notify_events_count = Column(Integer, nullable=False, default=0, server_default="0")

    # Detección de "te pasaron en el ranking": rank global (por total_xp) tal
    # como estaba la última vez que se chequeó a este usuario en
    # due_notifications — no un valor live. Solo se refresca para candidatos
    # efectivamente due, nunca en un full scan.
    notify_last_rank = Column(Integer, nullable=True)

    # IANA timezone del usuario (p.ej. "America/Argentina/Buenos_Aires"); define el
    # "día" de la repetición espaciada. Se autocompleta desde el navegador en cada
    # carga del home. NULL → fallback a Argentina (ver session_store.user_today).
    timezone = Column(String(64), nullable=True)

    # Racha global de días de actividad: días distintos (no necesariamente
    # consecutivos) con al menos una sesión completada. Define el multiplicador
    # de XP del modo Repaso. 30 días seguidos sin actividad resetean a 0.
    streak_days = Column(Integer, nullable=False, default=0, server_default="0")
    streak_last_date = Column(Date, nullable=True)

    # Desbloqueo de emojis (badges) por carrera. `emoji_worn` es el id del nodo
    # que el usuario muestra en el ranking; NULL = raíz del bucket (default).
    # Ver emoji_tree.py.
    #
    # `emoji_path` está MUERTA: describía una cadena append-only de nodos
    # desbloqueados, pero el desbloqueo no tiene estado propio — se deriva de
    # total_xp vía unlocked_depth() (ver la cabecera de emoji_tree.py). No la lee
    # ni la escribe nadie; queda la columna porque sacarla necesita migración.
    emoji_path = Column(Text, nullable=True)
    emoji_worn = Column(String(64), nullable=True)

    # Emails automáticos de retención (bounce + win-back). `email_unsubscribed`
    # es un opt-out global; los `*_sent_at` son la idempotencia de cada tipo de
    # mail (ver lifecycle_emails.py).
    email_unsubscribed = Column(Boolean, nullable=False, default=False, server_default="false")
    bounce_email_sent_at = Column(DateTime, nullable=True)
    winback_email_sent_at = Column(DateTime, nullable=True)
    # Último hito de multiplicador ya felicitado por email (3/9/18/30/45 días).
    # Se compara contra el tier derivado de streak_days: mayor ⇒ hay mail
    # pendiente. Se limpia al resetear la racha, así quien la pierde y vuelve a
    # llegar recibe la felicitación de nuevo. `sent_at` es observabilidad.
    streak_email_sent_tier = Column(Integer, nullable=True)
    streak_email_sent_at = Column(DateTime, nullable=True)

    # ¿Llegó alguna vez al home? Lo marca `/user/progress`, que es el endpoint
    # que el home llama en cada carga. Es el escalón del embudo entre "completó
    # el onboarding" y "arrancó una sesión": separa a quien se quedó trabado en
    # la autenticación de quien llegó a la app y no tocó «empezar» — dos
    # problemas con soluciones distintas.
    #
    # Booleano y no timestamp a propósito. La columna se creó el 24/08 y hubo
    # que rellenar el pasado; un `first_home_at` con fechas inventadas para los
    # usuarios viejos sería una bomba para cualquier análisis temporal futuro.
    # El hecho se puede reconstruir, el momento no.
    reached_home = Column(Boolean, nullable=False, default=False, server_default="false")

    # Instaló y abrió la PWA (display-mode: standalone) al menos una vez. Lo
    # manda el cliente en /user/progress?pwa=1 (ver
    # web/src/lib/platform/detect.ts :: isStandalone()) y se escribe una sola
    # vez, igual que reached_home.
    #
    # DateTime y no bool, a diferencia de reached_home: acá no hay pasado que
    # reconstruir (la columna nace vacía para todo el mundo, no hubo que
    # rellenar nada), así que el momento exacto es dato real desde el día uno
    # y alimenta la curva de retención re-basada (ver retention() en
    # metrics/queries.py).
    pwa_first_seen_at = Column(DateTime, nullable=True)

    # Nº de sesión en la que el resumen le pidió algo por última vez (un cafecito
    # o que recluta), sin distinguir cuál de los dos. Es lo único que hace falta
    # para que los dos pedidos nunca salgan pegados: ver summary_asks.py.
    #
    # El número de sesión y no un timestamp porque la cadencia se cuenta en
    # sesiones, no en días — quien hace tres seguidas una tarde tiene que ver lo
    # mismo que quien las reparte en tres días.
    summary_ask_last_session = Column(Integer, nullable=True)

    # Atribución de primer contacto: por qué grupo de WhatsApp llegó la persona
    # ("uba042") y su prefijo de universidad ("uba"). Lo manda el cliente al
    # completar el onboarding, desde lo que capturó al aterrizar (ver
    # web/src/lib/analytics/attribution.ts).
    #
    # Se escriben UNA sola vez, solo si están en NULL (ver enroll_user): gana el
    # primer contacto, igual que el register_once del cliente. Si alguien vuelve
    # a entrar por otro link, no se le pisa el origen real.
    #
    # NULL es esperable en dos casos: usuarios anteriores a la columna, y quien
    # llegó sin `?g=` (link directo, boca a boca).
    first_group_id = Column(String(20), nullable=True, index=True)
    first_utm_source = Column(String(20), nullable=True)

    # Quién trajo a esta persona a Intervalo. Apunta a `game_players` y no a
    # `users` porque el reclutador puede ser un INVITADO del minijuego, que no
    # tiene fila en `users` — y ese es justo el caso que hace viral al juego.
    # Como `game_players.user_id` es UNIQUE, el jugador ES el join hacia el
    # usuario: el pagador de clásico se deriva con un SELECT por PK.
    #
    # Se escribe UNA vez, en el alta, con la misma guarda write-once que
    # `first_group_id`. Ver backend/referrals.py.
    referred_by_player_id = Column(
        Integer, ForeignKey("game_players.id"), nullable=True, index=True
    )
    # Cuánta XP de CLÁSICO le dio esta persona a quien la trajo, y el resto de la
    # división en centésimas (0-99). Espejo exacto de las columnas del juego, y
    # por el mismo motivo: el 10% de una respuesta de 12 XP son 1,2, y
    # redondeando cada pago hacia abajo el 10% prometido se vuelve 8%.
    referral_xp_given = Column(Integer, nullable=False, default=0, server_default="0")
    referral_pending = Column(Integer, nullable=False, default=0, server_default="0")

    # Cuánto de `referral_xp_given` YA se le contó a quien trajo a esta persona,
    # por cada canal. La diferencia contra `referral_xp_given` es lo NUEVO, que es
    # lo único que un aviso puede llamar "hoy" y un mail "esta semana".
    #
    # Van acá, en la fila del RECLUTA, y no en la del reclutador: con una sola
    # marca por reclutador se sabría cuánta XP nueva entró, pero no de cuántas
    # personas — y el copy se elige justamente por eso ("uno de tus reclutas" vs
    # "tres de tus reclutas"). Con la marca por recluta, "cuántos se movieron" es
    # contar filas con diferencia.
    #
    # Dos columnas y no una porque los dos canales tienen cadencias distintas: el
    # aviso sale el día que pasó algo y el mail una vez por semana, así que cada
    # uno tiene que recordar su propio corte.
    referral_xp_push_seen = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    referral_xp_email_seen = Column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Cuánta de `total_xp` la pusieron los reclutas y no el estudio propio.
    #
    # Existe para poder preguntar "¿esta persona resolvió algo ACÁ?" sin mirar
    # `answers`, que es la tabla más grande: `total_xp > referral_xp_earned` es
    # una comparación entre dos columnas de la misma fila. Es el gemelo de
    # `game_players.exercises_correct`, que el minijuego usa para lo mismo y por
    # el mismo motivo — un reclutador puede subir de XP sin haber resuelto nunca,
    # y sin este corte aparece en el ranking igual.
    referral_xp_earned = Column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Habilidad estimada del modelo Elo jerárquico (theta). 0.0 = neutro
    # (arranca ahí, sin cold start raro: la primera predicción es 0.5 y se
    # ajusta solo). `ability_n` es el conteo de respuestas usadas para el
    # learning rate decreciente. Ver algorithm/elo.py y
    # 2026-08-26-motor-de-sesiones.md §5/§9.
    ability = Column(Float, nullable=False, default=0.0, server_default="0.0")
    ability_n = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enrollments = relationship("Enrollment", back_populates="user")
    unit_states = relationship("UnitState", back_populates="user")
    sessions = relationship("Session", back_populates="user")
    answers = relationship("Answer", back_populates="user")
    push_subscriptions = relationship("PushSubscription", back_populates="user")


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enrollments = relationship("Enrollment", back_populates="course")
    unit_states = relationship("UnitState", back_populates="course")
    sessions = relationship("Session", back_populates="course")
    answers = relationship("Answer", back_populates="course")
    push_subscriptions = relationship("PushSubscription", back_populates="course")
    exercises = relationship("Exercise", back_populates="course")
    belt_infos = relationship("BeltInfo", back_populates="course")


class Enrollment(Base):
    __tablename__ = "enrollments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    university = Column(String(100), nullable=True)
    # Cuándo se CAMBIÓ la universidad por última vez — NULL si nunca cambió
    # (incluido el caso normal: se cargó en el onboarding y quedó). Gemelo de
    # `game_players.university_set_at`, y por el mismo motivo: desde que el
    # empuje de cafecito vale también acá (ver backend/xp_boost.py), sin este
    # sello cualquiera podría rehacer el alta con la universidad impulsada y
    # cobrar el empuje. Hoy no hay UI para cambiarla —/onboarding redirige si ya
    # estás inscripto y solo POST /user/enroll la escribe— así que la exposición
    # es de API, pero la columna cuesta dos líneas y el agujero dura 24 h.
    university_set_at = Column(DateTime, nullable=True)
    career = Column(String(200), nullable=True)
    # Retirada del onboarding (la respuesta no predecía comportamiento). Se
    # conserva por los valores históricos; en altas nuevas queda NULL.
    motivation = Column(String(50), nullable=True)
    # Unidades que la persona declaró conocer en el onboarding, claves del
    # catálogo separadas por coma ("functions,limits"). Declarativo: no altera
    # el plan de estudio ni el estado inicial de SM-2.
    known_units = Column(String(100), nullable=True)
    enrolled_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "course_id", name="unique_user_course"),)

    user = relationship("User", back_populates="enrollments")
    course = relationship("Course", back_populates="enrollments")


class CourseProgress(Base):
    """Configuración y estado de progreso del usuario para un curso: cuántos
    ítems puede tener en aprendizaje a la vez (`active_cap`) y en qué iteración
    de progreso está (se incrementa al reiniciar el curso). Fila creada de forma
    lazy la primera vez que se necesita (default cap = ACTIVE_CAP_DEFAULT)."""
    __tablename__ = "course_progress"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    iteration = Column(Integer, nullable=False, default=1, server_default="1")
    active_cap = Column(Integer, nullable=False, default=18, server_default="18")
    # Máximo de ejercicios por sesión de repaso (config del editor).
    session_size = Column(Integer, nullable=False, default=3, server_default="3")
    # True mientras nadie tocó el selector manual: create_session_db recalcula
    # session_size antes de cada sesión (rampa 3→4→4→5.., ver
    # session_store._adaptive_session_size). Se apaga solo la primera vez que
    # el usuario fija un valor a mano (set_session_size) y se reactiva al
    # reiniciar el curso. 2026-08-26-motor-de-sesiones.md §8/§9.
    session_size_auto = Column(Boolean, nullable=False, default=True, server_default="true")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="unique_user_course_progress"),
    )


class UnitState(Base):
    """SM-2 state for each (belt, topic, exercise_type) unit per user per course."""
    __tablename__ = "unit_states"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    belt = Column(String(20), nullable=False)
    topic = Column(String(50), nullable=False)
    exercise_type = Column(String(20), nullable=False)

    phase = Column(String(20), nullable=False, default="learning")
    step_index = Column(Integer, default=0)
    ease_factor = Column(Float, default=2.5)
    interval_days = Column(Integer, default=1)
    repetitions = Column(Integer, default=0)
    next_due = Column(Date, nullable=True)
    attempted = Column(Boolean, default=False)
    # Historial de resultados recientes en fase de aprendizaje ("1"/"0" por
    # intento, más reciente al final, hasta learning_window caracteres). Portón
    # de graduación "N de los últimos M" en vez de racha estricta — ver
    # algorithm/sm2.py::_update_learning.
    recent_results = Column(String(8), nullable=False, default="", server_default="")
    # Unit creada como "catch-up": un exercise_type/tema que quedó detrás del
    # frontier ya desbloqueado (p.ej. al agregar un ítem nuevo al catálogo). Se
    # excluye del cálculo de maestría/cinturón para no despromocionar un tema ya
    # dominado; se aprende como repaso extra.
    is_catchup = Column(Boolean, nullable=False, default=False, server_default="false")
    # Tema suspendido por el usuario desde el editor: se oculta del home y se
    # excluye de sesiones y del cálculo de maestría/cinturón. Reversible (Adelantar).
    suspended = Column(Boolean, nullable=False, default=False, server_default="false")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_reviewed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "course_id", "belt", "topic", "exercise_type",
            name="unique_user_course_unit",
        ),
        Index("idx_unit_states_next_due", "next_due"),
        Index("idx_unit_states_user_course", "user_id", "course_id"),
    )

    user = relationship("User", back_populates="unit_states")
    course = relationship("Course", back_populates="unit_states")


class UnitStateArchive(Base):
    """Snapshot de las UnitState al reiniciar un curso. Preserva el progreso de
    iteraciones anteriores (no se pierde el dato) mientras `unit_states` queda
    solo con la iteración vigente, así las queries activas y el cálculo de
    cinturón no necesitan filtrar por iteración."""
    __tablename__ = "unit_state_archive"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    iteration = Column(Integer, nullable=False, default=1)

    belt = Column(String(20), nullable=False)
    topic = Column(String(50), nullable=False)
    exercise_type = Column(String(20), nullable=False)

    phase = Column(String(20), nullable=False)
    step_index = Column(Integer, default=0)
    ease_factor = Column(Float, default=2.5)
    interval_days = Column(Integer, default=1)
    repetitions = Column(Integer, default=0)
    next_due = Column(Date, nullable=True)
    attempted = Column(Boolean, default=False)
    is_catchup = Column(Boolean, nullable=False, default=False)
    suspended = Column(Boolean, nullable=False, default=False)

    archived_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_unit_state_archive_user_course", "user_id", "course_id"),
    )


class ItemExerciseCycle(Base):
    """Ejercicios ya servidos en el ciclo actual de un ítem (belt+topic+
    exercise_type) para un usuario. Garantiza que no se repita un ejercicio
    hasta haber completado todos los del ítem: se resetea (vacía) cuando el
    ciclo se agota, o cuando el usuario reinicia el curso (ver reset_course)."""
    __tablename__ = "item_exercise_cycles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    belt = Column(String(20), nullable=False)
    topic = Column(String(50), nullable=False)
    exercise_type = Column(String(20), nullable=False)

    # JSON-encoded list de Exercise.external_id ya servidos en el ciclo vigente.
    served_external_ids = Column(Text, nullable=False, default="[]", server_default="[]")

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "course_id", "belt", "topic", "exercise_type",
            name="unique_user_course_item_cycle",
        ),
        Index("idx_item_exercise_cycles_user_course", "user_id", "course_id"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    abandoned = Column(Boolean, default=False)

    exercises_total = Column(Integer, nullable=False)
    exercises_correct = Column(Integer, default=0)
    xp_earned = Column(Integer, default=0)

    # Iteración de progreso del curso al momento de la sesión (ver CourseProgress).
    # Reiniciar el curso incrementa la iteración; el histórico queda etiquetado.
    iteration = Column(Integer, nullable=False, default=1, server_default="1")

    # "main" for the daily spaced-repetition session, "practice" for free practice.
    # "main" sessions are gated to one per day, except the user can keep starting
    # new ones while pending (due) items remain — see create_session_db.
    mode = Column(String(16), nullable=False, default="main", server_default="main")

    # Identidad y orden de los ejercicios servidos, escrita en el mismo commit
    # que crea la fila (JSON list de external_id). Sin esto una sesión
    # abandonada sin ninguna respuesta no dejaba rastro de qué vio el usuario,
    # y una caché fría re-sorteaba en vez de reconstruir (ver
    # session_store._reconstruct_session_state). "[]" en sesiones viejas.
    # 2026-08-26-motor-de-sesiones.md §4-bis.
    served_external_ids = Column(Text, nullable=False, default="[]", server_default="[]")

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_sessions_user_course", "user_id", "course_id"),
        Index("idx_sessions_started_at", "started_at"),
        Index("idx_sessions_finished_at", "finished_at"),
    )

    user = relationship("User", back_populates="sessions")
    course = relationship("Course", back_populates="sessions")
    answers = relationship("Answer", back_populates="session")


class Answer(Base):
    __tablename__ = "answers"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    exercise_id = Column(String(20), nullable=True)
    # Identificador estable del ejercicio real que vio el usuario (p. ej.
    # "white_definition_clsf_01"). exercise_id es solo el slot posicional de la
    # sesión ("ex_000"); este campo permite saber con certeza qué ejercicio se
    # sirvió, y es lo que alimenta el ciclo de no-repetición por ítem
    # (ver ItemExerciseCycle). Lo reporta el cliente al responder.
    exercise_external_id = Column(String(100), nullable=True)
    belt = Column(String(20), nullable=False)
    topic = Column(String(50), nullable=False)
    exercise_type = Column(String(20), nullable=False)

    is_correct = Column(Boolean, nullable=False)
    response_time_ms = Column(Integer, nullable=True)
    quality_score = Column(Integer, nullable=True)
    xp_earned = Column(Integer, default=0)
    # XP de esta respuesta antes de los multiplicadores (por intento y dificultad
    # personal del ítem). xp_earned - xp_base = XP extra ganado gracias a ellos,
    # mostrado en el resumen de sesión.
    xp_base = Column(Integer, nullable=False, default=0, server_default="0")
    # De ese extra, cuánto lo puso el empuje de cafecito de la universidad y no
    # la racha diaria (ver algorithm/xp.py :: xp_from_boost).
    #
    # Se guarda por respuesta porque después NO se puede reconstruir: lo único
    # que sobrevive es el total, y ni el multiplicador que corría en ese momento
    # ni su reparto entre racha y cafecito quedan en ninguna fila. Es el mismo
    # motivo por el que existe `game_players.xp_from_boosts`.
    #
    # Sirve para dos cosas concretas: separar racha de cafecito en el resumen, y
    # poder DESCONTAR el empuje de los agregados por ventana de tiempo de las
    # push de universidad (push_store), que si no anuncian saltos que nadie
    # resolvió. Lo que NO permite es atribuir por donante: `multiplier_for`
    # colapsa el empuje global y el dirigido en un solo número.
    xp_from_boost = Column(Integer, nullable=False, default=0, server_default="0")

    # Iteración de progreso del curso (ver CourseProgress). Reiniciar el curso
    # incrementa la iteración; las respuestas viejas quedan etiquetadas.
    iteration = Column(Integer, nullable=False, default=1, server_default="1")

    answered_at = Column(DateTime, default=datetime.utcnow)
    intra_session_position = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_answers_session_id", "session_id"),
        Index("idx_answers_user_course", "user_id", "course_id"),
        Index("idx_answers_answered_at", "answered_at"),
        Index("idx_answers_belt_topic", "belt", "topic"),
        Index("idx_answers_exercise_external_id", "exercise_external_id"),
        # Un slot de sesión se responde una sola vez. La clave es el slot
        # (ex_000) y NO exercise_external_id: una sesión más larga que el pool de
        # la unidad repite externals legítimamente en slots distintos. Los NULL
        # (Answer sintético del onboarding) quedan exentos. El guard "amable"
        # vive en record_answer_db; esto es la red de contención en el esquema.
        Index("uq_answers_session_slot", "session_id", "exercise_id", unique=True),
    )

    session = relationship("Session", back_populates="answers")
    user = relationship("User", back_populates="answers")
    course = relationship("Course", back_populates="answers")


class Exercise(Base):
    """Question bank scoped by course, belt, topic."""
    __tablename__ = "exercises"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    external_id = Column(String(100), nullable=True)
    belt = Column(String(20), nullable=False)
    topic = Column(String(50), nullable=False)
    exercise_type = Column(String(20), nullable=False)
    question = Column(Text, nullable=False)
    option_a = Column(Text, nullable=False)
    option_b = Column(Text, nullable=False)
    option_c = Column(Text, nullable=False)
    option_d = Column(Text, nullable=False)
    correct_index = Column(Integer, nullable=False)
    has_math = Column(Boolean, default=False)
    feedback_correct = Column(Text, nullable=False)
    feedback_incorrect = Column(Text, nullable=False)
    graph_fn = Column(String(500), nullable=True)
    graph_view = Column(String(100), nullable=True)
    graph_shade = Column(String(100), nullable=True)
    # true desactiva el aspecto 1:1 forzado de Mafs (solo probabilidad, ver
    # authoring-context.md sección Gráficos); ausente/false = comportamiento
    # actual sin cambios.
    graph_free_aspect = Column(Boolean, nullable=True)
    # Tabla de datos embebida en el enunciado, serializada como JSON (columnas,
    # filas y la columna que pinta cada opción). Ver authoring-context.md,
    # sección Tablas. Se llama table_data y no table para no confundirse con el
    # __table__ de SQLAlchemy; en el JSON de autoría y en la API es "table".
    table_data = Column(Text, nullable=True)
    explanation = Column(Text, nullable=True)
    tags = Column(Text, nullable=True)
    # Estado editorial del contenido (viene del JSON de autoría, ver
    # seed_content.py). Usado por feedback_survey.py para priorizar ítems no
    # revisados a la hora de elegir qué ejercicio lleva la micro-encuesta.
    reviewed = Column(Boolean, nullable=True)

    # Dificultad estimada del ejercicio individual (beta) por el Elo
    # jerárquico. 0.0 = neutro; se mezcla con la dificultad del ítem
    # (item_difficulty) con peso n/(n+4) hasta tener evidencia propia. Ver
    # algorithm/elo.py y 2026-08-26-motor-de-sesiones.md §5/§6/§9.
    difficulty = Column(Float, nullable=False, default=0.0, server_default="0.0")
    difficulty_n = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_exercises_lookup", "course_id", "belt", "topic"),
        UniqueConstraint("course_id", "external_id", name="uq_exercises_course_external_id"),
        Index("idx_exercises_external_id", "course_id", "external_id"),
    )

    course = relationship("Course", back_populates="exercises")


class ItemDifficulty(Base):
    """Dificultad Elo del ítem (belt, topic, exercise_type) — el backoff del
    Elo por ejercicio (`Exercise.difficulty`) cuando un ejercicio individual
    todavía no acumuló evidencia propia. Chica a propósito: una fila por ítem
    (209 en producción al 2026-08-26), no por ejercicio. Ver algorithm/elo.py
    y 2026-08-26-motor-de-sesiones.md §5/§9."""
    __tablename__ = "item_difficulty"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    belt = Column(String(20), nullable=False)
    topic = Column(String(50), nullable=False)
    exercise_type = Column(String(20), nullable=False)
    difficulty = Column(Float, nullable=False, default=0.0, server_default="0.0")
    difficulty_n = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "course_id", "belt", "topic", "exercise_type",
            name="uq_item_difficulty_unit",
        ),
    )


class BeltInfo(Base):
    __tablename__ = "belt_info"

    id          = Column(Integer, primary_key=True, index=True)
    course_id   = Column(Integer, ForeignKey("courses.id"), nullable=False)
    belt        = Column(String(20), nullable=False)
    headline    = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("course_id", "belt", name="uq_belt_info_course_belt"),
    )

    course = relationship("Course", back_populates="belt_infos")


class Feedback(Base):
    """User-submitted feedback (error report, idea, or comment) from settings."""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    categoria = Column(String(20), nullable=False)  # error | idea | comentario
    mensaje = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ExerciseFeedback(Base):
    """Micro-encuesta post-ejercicio (dificultad/explicación/interés) y reporte
    de problemas de contenido. `exercise_external_id` es la clave real del
    ejercicio (Exercise.external_id), no el slot de sesión (Answer.exercise_id),
    para poder agregar respuestas del mismo ítem entre sesiones/usuarios.
    `answered_at` NULL = impression mostrada pero no respondida (skip),
    necesario para el kill-switch de feedback_survey.py."""
    __tablename__ = "exercise_feedback"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    exercise_external_id = Column(String(100), nullable=False)

    question_type = Column(String(1), nullable=False)  # "A" | "B" | "C" | "D"
    # A: muy_facil|justo|muy_dificil — B: util|no_util — C: categoría de reporte
    # D: aburrido|justo|interesante
    # OJO: "justo" existe en A (la dificultad estuvo bien) y en D (ni aburrido ni
    # interesante). Son cosas distintas: agrupar por `value` sin filtrar
    # `question_type` da un número plausible y falso.
    value = Column(String(30), nullable=True)
    free_text = Column(Text, nullable=True)
    # Canal D: chip de razón, solo en los extremos. Lista cerrada en
    # feedback_survey.D_REASONS, validada en el endpoint.
    reason = Column(String(30), nullable=True)

    shown_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    answered_at = Column(DateTime, nullable=True)

    # Mail de agradecimiento por reportar (question_type="C"), ver
    # lifecycle_emails.due_report_thanks_emails. NULL = todavía no se mandó.
    thanks_sent_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_exfb_user_course", "user_id", "course_id"),
        Index("idx_exfb_session", "session_id"),
        Index("idx_exfb_user_item", "user_id", "exercise_external_id"),
        # Targeting por canal en feedback_survey: cuenta impresiones por
        # (ítem, canal). idx_exfb_user_item no sirve porque arranca por user_id.
        Index("idx_exfb_item_type", "exercise_external_id", "question_type"),
    )

    user = relationship("User")
    session = relationship("Session")
    course = relationship("Course")


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    endpoint = Column(String(1000), nullable=False)
    p256dh = Column(String(1000), nullable=False)
    auth = Column(String(1000), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "endpoint", name="unique_user_endpoint"),
    )

    user = relationship("User", back_populates="push_subscriptions")
    course = relationship("Course", back_populates="push_subscriptions")


class NotificationSend(Base):
    """Historial de notificaciones push enviadas: una fila por usuario por
    envío (no por dispositivo), con la categoría/variante de copy elegida
    (ver notification_copy.py) y si se llegó a clickear. A diferencia de
    User.notify_last_* (que solo guardan el último estado, para el guard de
    idempotencia diario), esta tabla es append-only y permite analizar
    efectividad por categoría/variante a lo largo del tiempo."""
    __tablename__ = "notification_sends"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)

    category = Column(String(30), nullable=False)
    variant_key = Column(String(50), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(String(500), nullable=False)

    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Resultado que devolvió el push service (FCM/APNs), reportado por el
    # notifier después de intentar el envío. La fila se crea al elegir el copy,
    # o sea antes de intentar mandar, así que sin esto un envío que nunca salió
    # queda idéntico a uno exitoso y no se puede distinguir "la ignoraron" de
    # "nunca llegó". "ok" | "error_<status>" | "error" (fallo sin status).
    # NULL = todavía sin reportar (o envío anterior a esta columna).
    delivery_status = Column(String(20), nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    # Se completa desde notificationclick en el service worker (ver sw.js);
    # None mientras no se haya clickeado. Idempotente: el primer click gana.
    opened_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_notification_sends_user_id", "user_id"),
        Index("idx_notification_sends_sent_at", "sent_at"),
    )


# ── Minijuego de derivadas (backend/game/) ───────────────────────────────────
# Bounded context aparte del motor SM-2: jugadores (guests o linkeados a users),
# Elo por plantilla generadora, ejercicios servidos con la derivada esperada
# del lado del server, e intentos. El XP del juego vive en game_players.xp y
# NUNCA suma a users.total_xp (eso desbloquearía emojis y dispararía
# notificaciones del ranking de Intervalo).


class GamePlayer(Base):
    __tablename__ = "game_players"

    id = Column(Integer, primary_key=True, index=True)
    # NULL en jugadores registrados creados directo con Clerk; se conserva tras
    # el link para que un cliente viejo con el token guardado siga resolviendo
    # al mismo jugador.
    guest_token = Column(String(64), unique=True, index=True, nullable=True)
    # NULL = guest. UNIQUE: un usuario tiene a lo sumo un jugador.
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True, nullable=True)
    # Namespace propio, separado de users.username. Mismas reglas de formato
    # (usernames.validate_username). El guest recibe uno autogenerado y NO lo
    # edita: elegir el @ es el gancho del registro.
    alias = Column(String(30), unique=True, index=True, nullable=False)
    # Si sigue en True, este @ es el que le tocó al azar y todavía no lo tocó:
    # habilita la ÚNICA edición gratis de la slide de "elegí tu @" (ver
    # patch_me en game/router.py). Se apaga apenas se usa una vez —con
    # éxito—; de ahí en más, cambiar el @ vuelve a ser el gancho de registro
    # de siempre.
    alias_is_generated = Column(Boolean, nullable=False, default=True, server_default="true")
    # Indexada: se filtra, se agrupa y se ordena por acá en los tres endpoints de
    # ranking, y en el GROUP BY del feed que corre en cada tick de simulación.
    university = Column(String(120), nullable=True, index=True)
    # Cuándo se CAMBIÓ la universidad por última vez — NULL si nunca cambió
    # (incluido el caso normal: se cargó una vez y quedó). Es el candado de los
    # empujes por universidad: sin él, un empuje activo se llenaría de gente que se
    # muda a la universidad impulsada por un día y la rivalidad se muere. Ver
    # game/boosts.py. El gemelo de clásico es `enrollments.university_set_at`.
    university_set_at = Column(DateTime, nullable=True)
    career = Column(String(1), nullable=True)

    # Elo del jugador (ver game/elo.py). n_updates = respuestas de primer
    # intento que ya lo ajustaron; también gobierna la rampa inicial.
    theta = Column(Float, nullable=False, default=0.0, server_default="0")
    n_updates = Column(Integer, nullable=False, default=0, server_default="0")

    # Orden del ranking del juego: (xp DESC, id ASC).
    xp = Column(Integer, nullable=False, default=0, index=True, server_default="0")
    current_combo = Column(Integer, nullable=False, default=0, server_default="0")
    best_combo = Column(Integer, nullable=False, default=0, server_default="0")
    # Mejor puesto histórico (1 = primero). Detecta récords del lado del server.
    best_rank = Column(Integer, nullable=True)
    # Indexada por el mismo motivo que `xp`: es el filtro de TODAS las consultas
    # de ranking. Quién entra a la tabla se decide por acá y no por la XP, que
    # la puede subir un recluta sin que el reclutador haya derivado nunca (ver
    # game/router.py :: RESOLVIO_ACA).
    exercises_correct = Column(
        Integer, nullable=False, default=0, server_default="0", index=True
    )
    exercises_attempted = Column(Integer, nullable=False, default=0, server_default="0")

    # Teclas del teclado que este jugador ya desbloqueó, separadas por coma y en
    # orden canónico (ver game/keyboard.py). Son acumulativas: una vez que una
    # derivada las pidió, quedan para siempre. El teclado no es una pista del
    # ejercicio de turno sino el inventario de lo que la persona ya sabe
    # escribir, y verlo crecer es parte del juego.
    unlocked_keys = Column(Text, nullable=False, default="", server_default="")
    # Ciclado de números por plantilla (game/cycler.py): qué valores le quedan
    # por servir a cada ranura de cada plantilla antes de repetir ninguno.
    # {"t1_pow:n": [3, 5]} = a t1_pow, en su ranura "n", le faltan 3 y 5 antes
    # de volver a barajar su rango. Se completa lazy, solo con las ranuras que
    # el jugador ya vio — nace vacío para todo el mundo, igual que
    # unlocked_keys cuando se agregó.
    numeric_cycle_json = Column(Text, nullable=False, default="{}", server_default="{}")

    # Con qué dispositivo apareció por primera vez: "ios" | "android" |
    # "desktop". Lo manda el cliente (X-Game-Platform) y NO se deduce del
    # User-Agent, porque el layout lo elige `getPlatform()` mirando también
    # maxTouchPoints — un iPad se reporta como Macintosh y juega el flujo de
    # teléfono. Es de primer contacto y no se pisa: quien empieza en el celular
    # y sigue en la compu vino del celular, y de dónde vino cada uno es lo que
    # explica cómo se distribuye el link. Lo que hace después se lee en
    # game_exercises.platform.
    platform = Column(String(8), nullable=True, index=True)

    # Atribución de primer contacto, espejo de users.first_group_id (mismas
    # regex al persistir, solo si están en NULL).
    first_group_id = Column(String(20), nullable=True, index=True)
    first_utm_source = Column(String(20), nullable=True)

    # Quién lo trajo: el jugador cuyo @ venía en el `?r=` del link (ver
    # game/referrals.py). Se escribe UNA vez, al crear la fila, y no se toca más
    # — poder reasignarlo después sería poder elegirse un reclutador cuando ya
    # se sabe quién conviene.
    #
    # Indexada porque es la consulta entera de la vista "Reclutas" del ranking:
    # todos los jugadores cuyo `referred_by` soy yo.
    referred_by = Column(Integer, ForeignKey("game_players.id"), nullable=True, index=True)
    # Cuánta XP le dio ESTE jugador a quien lo trajo. Vive en la fila del
    # recluta y no en la del reclutador porque es lo que muestra cada renglón de
    # esa vista: no el total, sino cuánto puso cada uno.
    referral_xp_given = Column(Integer, nullable=False, default=0, server_default="0")
    # Cuánto de esa XP ya se le contó a quien trajo a esta persona en un aviso
    # push. La diferencia contra `referral_xp_given` es lo NUEVO, que es lo único
    # que se puede anunciar sin repetir. Gemela de `users.referral_xp_push_seen`,
    # y existe porque el reclutador puede ser un INVITADO: ese es justamente el
    # caso que Intervalo no cubre (game/notifications.py :: _contexto_recluta).
    referral_xp_push_seen = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # El resto de la división, en centésimas de XP (0-99).
    #
    # El 10% de una derivada de 25 XP son 2,5. Redondeando cada pago hacia abajo
    # se pagan 2, y el 10% escrito en la diapo pasa a ser un 8% — una quinta
    # parte de lo prometido evaporada en el redondeo. Guardando el resto, lo que
    # sobra de una respuesta se cobra en la siguiente y la cuenta cierra exacta.
    referral_pending = Column(Integer, nullable=False, default=0, server_default="0")
    # La XP EXTRA que le ganó a esta persona el empuje de su universidad: la
    # diferencia entre lo que pagó cada respuesta con el multiplicador puesto y
    # lo que habría pagado sin él.
    #
    # Se acumula al otorgar (game/router.py :: _otorgar_xp) y no se calcula al
    # leer porque no hay de dónde reconstruirla: ni la XP de cada respuesta ni
    # el multiplicador que corría en ese momento quedan guardados en ninguna
    # fila. Lo único que sobrevive es el total de `xp`, y ahí las dos cosas ya
    # están sumadas.
    #
    # Arranca en cero para todo el mundo, también para quien venía jugando con
    # empujes puestos: lo de antes no se puede recuperar, y un número inventado
    # sería peor que un cero honesto.
    xp_from_boosts = Column(Integer, nullable=False, default=0, server_default="0")

    # XP de CLÁSICO que este jugador ya se ganó como reclutador pero que todavía
    # no se le pudo pagar, porque no tiene cuenta de Intervalo donde acreditarla.
    #
    # Pasa con el reclutador INVITADO: comparte su link, alguien entra por él y
    # estudia en clásico, y ese 10% no tiene a dónde ir. Perderlo mataría
    # justamente el caso viral, así que se acumula acá y se salda en el momento
    # exacto en que la fila adquiere `user_id` (ver game/deps.py ::
    # link_guest_to_user). Es, además, el mejor argumento para registrarse: al
    # hacerlo cobrás lo que ya generaste.
    classic_xp_owed = Column(Integer, nullable=False, default=0, server_default="0")

    # Jugador sembrado (ver scripts/seed_game_bots.py): puebla el ranking para
    # que el primero en llegar tenga a quién escalar. No lo controla nadie —
    # tiene user_id y guest_token en NULL, así que ninguna request lo resuelve.
    # La columna existe para poder EXCLUIRLOS de cualquier métrica de uso: sin
    # ella, 100 filas sembradas serían indistinguibles de usuarios reales al
    # analizar el juego.
    is_bot = Column(Boolean, nullable=False, default=False, server_default="false")

    # Dos fotos del puesto, en registro de desplazamiento (ver game/simulation.py).
    # La diferencia entre `rank_snapshot` y el puesto actual es la flechita de
    # "se movió recién" de cada fila. Son DOS y no una porque con una sola, al
    # refrescarla, todas las flechas del ranking se apagarían de golpe a la vez;
    # con dos, la referencia se desliza y siempre queda entre media ventana y
    # una ventana de antigüedad.
    rank_snapshot = Column(Integer, nullable=True)
    rank_snapshot_at = Column(DateTime, nullable=True)
    rank_recent = Column(Integer, nullable=True)
    rank_recent_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=True)

    # ── Avisos ──────────────────────────────────────────────────────────────
    # Las preferencias de notificación DEL INVITADO. Quien tiene cuenta usa las
    # de `users`: son la misma persona y no puede tener dos horarios ni dos
    # cupos, y además es lo que hace que el tope de 3 por día sea de la persona
    # y no de cada producto (ver game/notifications.py :: titular_del_cupo).
    #
    # Están acá y no solo en `users` porque el 50-95% del juego, según la
    # semana, es gente sin cuenta: el push es la única forma de traerla de
    # vuelta, y `push_subscriptions.user_id` es NOT NULL. Al registrarse, lo que
    # haya elegido de invitado se copia al usuario (deps.link_guest_to_user).
    notify_enabled = Column(Boolean, default=False, nullable=False,
                            server_default=text("false"))
    notify_time = Column(String(5), nullable=True)  # "HH:MM" local
    notify_timezone = Column(String(64), nullable=True)
    # Cupo: uno programado por día (`notify_last_sent_on`) y hasta dos
    # reactivos (`notify_events_*`), exactamente como en `users`. Los nombres
    # coinciden a propósito — el resolutor los lee por duck typing sobre el
    # titular del cupo, que puede ser un `User` o este mismo jugador.
    notify_last_sent_on = Column(Date, nullable=True)
    notify_last_category = Column(String(30), nullable=True)
    notify_last_variant_key = Column(String(50), nullable=True)
    notify_events_on = Column(Date, nullable=True)
    notify_events_count = Column(Integer, default=0, nullable=False,
                                 server_default="0")
    # Último puesto del ranking del juego que esta persona YA VIO en un aviso.
    # Sin esto, "te pasaron" saldría todos los días mientras siga abajo, que es
    # la misma noticia repetida. Es el gemelo de `users.notify_last_rank`.
    notify_last_rank = Column(Integer, nullable=True)

    # `foreign_keys` explícito: desde que existe `users.referred_by_player_id`
    # hay DOS caminos de clave foránea entre estas dos tablas, y sin decir cuál
    # es este SQLAlchemy no puede armar el join.
    user = relationship("User", foreign_keys=[user_id])


class GamePushSubscription(Base):
    """La suscripción de push de un JUGADOR, con o sin cuenta.

    Tabla aparte de `push_subscriptions` y no una columna nueva allá. Dos
    motivos, y el primero es el que decide: aquella tiene `user_id` NOT NULL y
    su `upsert` borra todas las demás filas del usuario, así que aflojarla para
    que acepte invitados toca el canal que hoy funciona. El segundo es que
    `game/` ya es un bounded context declarado y esto es suyo.

    Una fila por navegador. `endpoint` es único a secas —y no por jugador— para
    que el mismo aparato no quede suscripto dos veces: si alguien juega de
    invitado, se registra y vuelve a activar, el endpoint es el mismo y la fila
    tiene que mudarse de jugador, no duplicarse.
    """
    __tablename__ = "game_push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False,
                       index=True)

    endpoint = Column(String(1000), nullable=False)
    p256dh = Column(String(1000), nullable=False)
    auth = Column(String(1000), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("endpoint", name="unique_game_endpoint"),
    )


class GameNotificationSend(Base):
    """Historial de avisos del juego: una fila por jugador por envío.

    Gemela de `notification_sends` y por los mismos motivos —append-only, con la
    categoría y la variante elegidas, para poder mirar efectividad—, más uno
    propio: es la idempotencia de los avisos reactivos. La pregunta «¿ya se lo
    dije hoy?» se contesta con una consulta acá y no estrenando una columna de
    estado por cada cosa que se pueda avisar.

    Separada de `notification_sends` por lo mismo que la tabla de suscripciones:
    aquella tiene `user_id` NOT NULL y un invitado no tiene fila en `users`.
    """
    __tablename__ = "game_notification_sends"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False)

    category = Column(String(30), nullable=False)
    variant_key = Column(String(50), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(String(500), nullable=False)

    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Lo que contestó el push service, reportado por el notifier después de
    # intentar mandar. La fila se crea al elegir el copy —o sea antes del
    # intento— así que sin esto un envío que nunca salió queda idéntico a uno
    # exitoso. "ok" | "error_<status>" | "error". NULL = todavía sin reportar.
    delivery_status = Column(String(20), nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_game_notification_sends_player", "player_id"),
        Index("idx_game_notification_sends_sent_at", "sent_at"),
    )


class Handle(Base):
    """El registro de nombres de usuario: quién es dueño de cada @, en TODO Intervalo.

    Hasta acá había dos namespaces que se ignoraban entre sí al validar:
    `users.username` (que solo miraba `users`) y `game_players.alias` (que miraba
    `game_players` ∪ `game_alias_history` y nunca `users`). El mismo string podía
    ser de dos personas distintas, una en cada producto.

    Esta tabla es la ÚNICA autoridad sobre qué nombre está tomado y de quién es.
    `users.username` y `game_players.alias` sobreviven como caché desnormalizado
    —para que el ranking siga siendo una consulta de una sola tabla— pero dejan
    de decidir: lo único que decide es una fila de acá. Nada escribe esas dos
    columnas fuera de `backend/handles.py`.

    Generaliza `game_alias_history`, que hacía esto mismo pero solo para el juego
    y solo para los @ ya soltados. Dos cosas que aquella tabla resolvía y que hay
    que conservar, porque son la razón de que exista:

      · Un @ soltado NO queda libre. Sigue resolviendo links de reclutamiento
        (`?r=`), así que dárselo a otra persona sería darle también la gente que
        trajo la primera. Por eso las filas se RETIRAN y no se borran.
      · Al retirarse, la fila sigue apuntando a su dueño. El link viejo no muere:
        sigue llevando a quien lo repartió.

    Lo nuevo es que ese blindaje ahora cubre `users.username`, que no tenía
    historia: cambiarte el username liberaba el string, y con `?r=` unificado eso
    le regalaba tus reclutas a un desconocido.

    Un dueño y solo uno, pero el dueño puede tener dos caras: una persona
    registrada que además juega es UNA fila con `user_id` y `player_id` puestos.
    Un invitado del juego es una fila con solo `player_id` — y ahí vive su @, sin
    inventarle una fila fantasma en `users`.
    """

    __tablename__ = "handles"

    handle = Column(String(30), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=True, index=True)
    # "active" = es el @ que esta persona usa hoy. "retired" = lo soltó, pero
    # sigue siendo suyo y sigue resolviendo sus links.
    status = Column(String(10), nullable=False, default="active", server_default="active")
    claimed_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    released_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # Un handle sin dueño no significa nada: sería un nombre reservado que
        # nadie puede reclamar y que nada libera.
        CheckConstraint(
            "user_id IS NOT NULL OR player_id IS NOT NULL", name="ck_handles_owner"
        ),
        CheckConstraint("status IN ('active','retired')", name="ck_handles_status"),
        # Un solo handle ACTIVO por dueño, que es lo que hace de esto un registro
        # y no una lista. Parciales porque los retirados sí se acumulan: una
        # persona puede haber soltado cinco @ y todos siguen siendo suyos.
        Index(
            "uq_handles_active_user",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'active' AND user_id IS NOT NULL"),
            postgresql_where=text("status = 'active' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_handles_active_player",
            "player_id",
            unique=True,
            sqlite_where=text("status = 'active' AND player_id IS NOT NULL"),
            postgresql_where=text("status = 'active' AND player_id IS NOT NULL"),
        ),
    )


class GameAliasHistory(Base):
    """Los @ que un jugador tuvo antes, y que siguen apuntando a él.

    Existe por los reclutas. El link que reparte el botón de WhatsApp lleva el @
    de quien comparte (`?r=cociente3196`) y el servidor lo resuelve mirando quién
    se llama así. Cambiar de @ rompía eso, y no en un caso raro: el juego ofrece
    reclutar a las diez resueltas y pide el registro a las doce, y registrarse es
    exactamente el momento en que se elige el @ definitivo. O sea que el camino
    normal era mandar un link y dejarlo muerto dos ejercicios después.

    Y hay un segundo agujero, más silencioso: al soltar un @ este quedaba libre.
    Quien lo tomara después heredaba todos los links viejos y cobraría por gente
    que trajo otra persona. Con esta tabla el @ queda RESERVADO para siempre
    —`alias_taken` la consulta— así que soltarlo no se lo regala a nadie.

    La clave primaria es el alias: un @ apunta a una sola persona, y esa
    unicidad es justamente lo que hay que garantizar.
    """

    __tablename__ = "game_alias_history"

    alias = Column(String(30), primary_key=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False, index=True)
    released_at = Column(DateTime, default=datetime.utcnow)


class GameSimState(Base):
    """Estado de la simulación de actividad del ranking — una sola fila.

    Los jugadores sembrados no juegan solos: los adelanta un tick perezoso que
    dispara el propio tráfico (game/simulation.py). Acá vive cuándo fue el
    último avance, para no adelantarlos dos veces, y un `version` que se
    incrementa con cada cambio del ranking: el cliente lo consulta cada 10 s y
    solo refresca la lista si cambió.
    """
    __tablename__ = "game_sim_state"

    id = Column(Integer, primary_key=True)
    last_tick_at = Column(DateTime, nullable=True)
    # Última vez que se refrescaron las fotos de puesto de todos los jugadores.
    last_snapshot_at = Column(DateTime, nullable=True)
    # Último orden visto del ranking de universidades, como JSON de siglas. Es la
    # referencia contra la que se detecta un sobrepaso: sin una foto anterior,
    # "la UNT le pasó a la UNR" no se puede afirmar, solo el orden de ahora.
    uni_order_json = Column(Text, nullable=True)
    version = Column(Integer, nullable=False, default=0, server_default="0")


class GameTemplateStat(Base):
    """Dificultad Elo por plantilla generadora. Filas creadas lazy al servir
    por primera vez, con beta seed por tier (game/elo.py) — sin migración de
    datos al agregar plantillas nuevas."""
    __tablename__ = "game_template_stats"

    id = Column(Integer, primary_key=True, index=True)
    template_key = Column(String(80), unique=True, index=True, nullable=False)
    tier = Column(Integer, nullable=False)
    beta = Column(Float, nullable=False, default=0.0)
    n_observations = Column(Integer, nullable=False, default=0, server_default="0")
    n_correct = Column(Integer, nullable=False, default=0, server_default="0")
    # Estudiantes DISTINTOS que aportaron una observación a esta plantilla. Es el
    # tamaño de muestra que cuenta para el ancla de β (game/elo.py): veinte
    # respuestas de una sola persona no son veinte datos sobre la plantilla, son
    # veinte datos sobre esa persona. Cuánto se le cree a una β aprendida se
    # decide con este número y no con `n_observations`.
    n_players = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GameExercise(Base):
    """Ejercicio servido a un jugador. Server-authoritative: la derivada
    esperada y los errores predecibles se guardan acá y la validación es
    numérica contra esto — el cliente nunca ve la respuesta."""
    __tablename__ = "game_exercises"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False, index=True)
    template_key = Column(String(80), nullable=False, index=True)
    params_json = Column(Text, nullable=True)
    prompt_latex = Column(Text, nullable=False)
    # str(sympy_expr), ej. "3*x**2 + cos(x)". Se re-parsea con sympify (string
    # propio del server, no input del cliente).
    expected_derivative = Column(Text, nullable=False)
    # JSON [{expr, feedback}] con las derivadas erróneas predecibles del template.
    common_errors_json = Column(Text, nullable=True)

    theta_at_serve = Column(Float, nullable=False)
    beta_at_serve = Column(Float, nullable=False)
    p_hat = Column(Float, nullable=False)

    status = Column(String(10), nullable=False, default="served", server_default="served")
    # La tabla de derivadas estuvo abierta durante ESTE ejercicio. Lo manda el
    # cliente al responder y ya gobernaba la mecánica (sin Elo, XP simbólica);
    # se persiste además porque sin la columna el panel no puede separar
    # «resolvió» de «copió», y esas dos cosas mezcladas arruinan tanto la tasa
    # de acierto como la lectura de qué plantillas cuestan de verdad.
    peeked = Column(Boolean, nullable=False, default=False, server_default="false")
    # El «¿Por qué?» se abrió mientras ESTE ejercicio estaba abierto, o sea que
    # la persona leyó de dónde salía la derivada antes de acertarla. Gobierna la
    # recompensa (game/xp.py :: XP_EXPLICADO) igual que `peeked`, pero con una
    # diferencia que importa: `peeked` depende de que el cliente confiese que
    # tenía la tabla abierta, y esto no. La explicación solo existe si el
    # servidor la entregó, así que la marca la pone el propio endpoint.
    explained = Column(Boolean, nullable=False, default=False, server_default="false")
    # Desde qué dispositivo se pidió ESTE ejercicio. Va por ejercicio y no solo
    # en el jugador porque la pregunta que importa —¿se comportan distinto?—
    # necesita poder atribuir cada respuesta al aparato en el que se dio, y
    # porque quien arranca en el colectivo y sigue en la compu existe.
    platform = Column(String(8), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    answered_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_game_exercises_player_status", "player_id", "status"),
    )


class GameAttempt(Base):
    __tablename__ = "game_attempts"

    id = Column(Integer, primary_key=True, index=True)
    exercise_id = Column(Integer, ForeignKey("game_exercises.id"), nullable=False, index=True)
    # Denormalizado para contar respuestas por jugador sin join.
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False, index=True)

    attempt_number = Column(Integer, nullable=False)
    answer_latex = Column(Text, nullable=True)
    # str(expr) de lo que se logró parsear del MathJSON; NULL si no parseó.
    answer_parsed = Column(Text, nullable=True)
    parse_ok = Column(Boolean, nullable=False, default=True)
    is_correct = Column(Boolean, nullable=False)
    response_ms = Column(Integer, nullable=True)
    xp_awarded = Column(Integer, nullable=False, default=0)
    # Solo en el intento 1 (el único que mueve el Elo).
    theta_before = Column(Float, nullable=True)
    theta_after = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        # Un solo intento por número y por ejercicio. El tope de intentos se
        # chequea con un COUNT sin candado, o sea que solo es consultivo: dos
        # respuestas en vuelo —un doble toque, o el reintento del teléfono
        # cuando la primera tardó— podían pasar las dos. Esto lo cierra en la
        # base, que es el único lugar donde se puede cerrar de verdad.
        #
        # PARCIAL, solo sobre lo que parseó: los intentos que el parser no
        # entendió se guardan a propósito con el número ANTERIOR —no consumen
        # intento, ver router.answer_exercise— así que se repiten de manera
        # legítima y no pueden entrar en la restricción.
        Index(
            "uq_game_attempts_slot",
            "exercise_id",
            "attempt_number",
            unique=True,
            sqlite_where=text("parse_ok"),
            postgresql_where=text("parse_ok"),
        ),
    )


class GameBoost(Base):
    """Un empuje de XP para una universidad, pagado con cafecitos.

    Cada donación inserta una fila y ninguna se muta nunca: el multiplicador
    activo de una universidad es una SUMA sobre las filas no vencidas
    (game/boosts.py). Resolver el solapamiento al leer y no al escribir es lo
    que hace que dos donaciones simultáneas no puedan pisarse.
    """

    __tablename__ = "game_boosts"

    id = Column(Integer, primary_key=True, index=True)
    # Sigla canónica (canonical_university), la misma que game_players.university.
    # NULL = empuje GLOBAL, para todo el mundo. Es a dónde va a parar la donación
    # que no se puede atribuir a ninguna universidad: en vez de perderse —lo peor
    # que puede pasar, porque la persona pagó y no vio nada— levanta el juego
    # entero. Ser generoso sale más barato que acertar.
    university = Column(String(120), nullable=True, index=True)
    cafecitos = Column(Integer, nullable=False)
    # Lo que escribió quien donó; se muestra en el cartel. Puede faltar.
    donor_name = Column(String(80), nullable=True)
    source = Column(String(20), nullable=False, default="manual", server_default="manual")
    # Identificador de la donación en el origen. UNIQUE desde el día uno aunque
    # hoy el disparo sea manual: es lo que va a impedir que un mail de Cafecito
    # reenviado dos veces regale el empuje dos veces.
    external_ref = Column(String(64), unique=True, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    # Cuándo se le contó al donante qué hizo su empuje. El mail sale al
    # VENCER, que es cuando el número está cerrado, así que esta columna es el
    # guard de "ya se lo contamos": sin ella, cada corrida del worker le
    # mandaría el mismo mail otra vez.
    email_sent_at = Column(DateTime, nullable=True)


class GameEvent(Base):
    """Una línea del historial del juego: lo que pasó y cuándo.

    El feed es SOLO del sistema —no hay texto escrito por usuarios— así que no
    hay nada que moderar. Cada fila trae la oración ya armada: el texto es parte
    del evento, y guardarlo hecho significa que cambiar el copy mañana no
    reescribe lo que la gente ya leyó ayer.

    `dedupe_key` es lo que evita que el mismo hecho se cuente dos veces: el hito
    de racha 25 de un jugador, su registro, o el aviso de que una universidad está
    por pasar a otra. UNIQUE no sirve —algunos eventos se repiten pasada una
    ventana de tiempo— así que la unicidad la decide `events.emit`.
    """

    __tablename__ = "game_events"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(16), nullable=False, index=True)
    # La oración, sin el emoji: el emoji va aparte para que el cliente lo pueda
    # poner siempre al final, por más que el texto cambie.
    text = Column(Text, nullable=False)
    emoji = Column(String(8), nullable=False)
    # El texto viene con marcadores —{a} para el nombre, {u0}/{u1} para las
    # siglas— y estas columnas traen con qué reemplazarlos. Guardar la oración
    # con agujeros y no ya resuelta es lo que deja pintar el nombre con el color
    # de su nivel y las siglas con la tag de cada universidad, en vez de escupir
    # texto plano.
    actor_alias = Column(String(30), nullable=True)
    # Nivel del jugador al momento del evento (elo.level_of). Es lo que le da el
    # color al nombre, igual que en el ranking. NULL cuando el nombre no es de un
    # jugador — quien donó un cafecito escribió lo que quiso en Cafecito.
    actor_level = Column(Integer, nullable=True)
    # El @ de un segundo protagonista ("{a} reclutó a {b}"). Sin nivel propio a
    # propósito: se pinta semibold sin color, como las siglas de universidad
    # ({u0}/{u1}) y no como {a} — acá lo que importa es decir quién es, no
    # destacar su rango.
    actor_b_alias = Column(String(30), nullable=True)
    # "Esto sos vos" / "esto es tu universidad": los dos resaltados del feed.
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=True, index=True)
    university = Column(String(120), nullable=True, index=True)
    # La segunda universidad, cuando el evento es entre dos ("le pasó a", "está a
    # nada de pasar a"). El resaltado de "esto es tu universidad" mira las dos.
    university_b = Column(String(120), nullable=True)
    dedupe_key = Column(String(80), nullable=True, index=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class GameCtaEvent(Base):
    """Una impresión o un click de un llamado a la acción del minijuego.

    Existe porque el panel del juego tiene que poder contestar «¿cuántos de los
    que VIERON el cartel de cafecito lo tocaron?» sin salir de esta base. Los
    mismos hechos viajan a PostHog —y ahí se pueden cruzar con la sesión, el
    dispositivo y el referrer, cosas que acá no están— pero PostHog no conoce
    `game_boosts`, así que el último escalón del embudo (el cafecito que
    efectivamente llegó) solo se puede cerrar del lado del server.

    Es deliberadamente pobre: quién, qué, dónde y cuándo. Nada de payloads
    libres — una tabla de eventos con un JSON adentro termina siendo un log que
    nadie consulta.
    """

    __tablename__ = "game_cta_events"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=True, index=True)
    # "cafecito" | "share" | "boost_offer" | "register" — qué llamado es.
    cta = Column(String(20), nullable=False, index=True)
    # "impression" | "click".
    action = Column(String(12), nullable=False, index=True)
    # Dónde salió (header, card, settings…) o qué lo disparó (record,
    # big_climb, milestone). Un solo campo: en la práctica cada CTA usa uno u
    # otro, y dos columnas casi siempre nulas se leen peor que una.
    placement = Column(String(24), nullable=True)
    # Cuántas derivadas llevaba resueltas cuando pasó. Es la variable que
    # decide si el cartel salió temprano o tarde, y sin ella el embudo no se
    # puede cortar por momento de la partida.
    solved = Column(Integer, nullable=True)
    university = Column(String(120), nullable=True, index=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class GameBoostIntent(Base):
    """"Voy a donar": lo que el juego sabe justo antes de mandarte a Cafecito.

    Es la pata principal para saber a qué universidad va un cafecito, y la única
    que no le pide NADA al donante: cuando toca el botón, el juego ya sabe quién
    es y de qué universidad. Cafecito no puede devolver ese dato —sus campos son
    todos opcionales y no se pueden marcar obligatorios— así que la donación se
    empareja después, por cercanía en el tiempo.

    `consumed_at` evita que una intención vieja se coma todas las donaciones que
    lleguen dentro de su ventana.
    """

    __tablename__ = "game_boost_intents"

    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False, index=True)
    # La que tenía EN ESE MOMENTO: si después se cambia, la intención no cambia.
    university = Column(String(120), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    consumed_at = Column(DateTime, nullable=True)


class GameMessage(Base):
    """Un mensaje que alguien escribió en el chat del minijuego.

    Vive aparte de `game_events` y no como un `kind` más, por dos razones que no
    son de estilo:

    · La ventana del feed son cuarenta filas. Compartiendo tabla, una racha de
      chat empuja fuera de esa ventana el anuncio de que alguien invitó
      cafecitos, que es justamente el que mueve donaciones.
    · El feed del sistema no tiene nada que moderar porque no hay texto de
      usuarios, y esa propiedad está escrita en cuatro lugares como el motivo por
      el que no hay filtros. Mezclando acá el texto de la gente, esa frase pasa a
      ser mentira en los cuatro. Con tabla propia sigue siendo cierta, y todo lo
      que hay que mirar con lupa queda de este lado.

    `alias`, `university` y `level` van DESNORMALIZADOS a propósito, igual que en
    `game_events`: el chat tiene que poder mostrar quién habló aunque esa persona
    después se cambie de universidad, suba de nivel o borre su cuenta. Un mensaje
    es lo que se dijo en un momento, no una vista de quien lo dijo hoy.
    """

    __tablename__ = "game_messages"

    id = Column(Integer, primary_key=True, index=True)
    # Quién escribió. NOT NULL porque escribir pide cuenta: los invitados leen.
    player_id = Column(Integer, ForeignKey("game_players.id"), nullable=False, index=True)
    # Su @ y su universidad al momento de escribir (ver arriba).
    alias = Column(String(30), nullable=False)
    university = Column(String(120), nullable=True)
    # Nivel del jugador entonces (elo.level_of). Es lo que le da color al @, igual
    # que en el ranking y en el feed.
    level = Column(Integer, nullable=False, default=0, server_default="0")
    # Ya saneado: lo que entra pasó por chat.limpiar (allowlist, tope de largo,
    # espacios colapsados). Lo que se guarda es lo que se muestra.
    text = Column(Text, nullable=False)
    # Bajar un mensaje sin perder la fila. No hay interfaz para esto y es a
    # propósito: se hace con un UPDATE a mano el día que haga falta, y mientras
    # tanto queda el rastro de qué se bajó y cuándo se había escrito.
    hidden = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
