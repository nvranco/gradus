# Features catalog

Inventario pantalla por pantalla. Rutas relativas a `web/src/app/`. Grupo `(app)` = shell autenticado con tab bar.

## Dashboard / Home (`dashboard-entry.tsx`, ruta `/`)

Pantalla principal. Selector de curso (`CourseSwitcher`, entre `analisis`/`probabilidad`/`algebra`), grilla de belts mostrando progreso por unidad (`BeltGrid`), CTAs "Repasar"/"Practicar" que arrancan una sesión (`useStartSession`). XP y nivel del usuario, indicador de racha.

**Editor de curso inline**: desde el dashboard se puede entrar en modo edición para suspender/reactivar topics, ajustar `active_cap`/`session_size` (stepper con debounce), y reiniciar el curso (con confirmación — botón de reset en rojo, guardar en verde; reiniciar incrementa `iteration` y archiva el `UnitState` actual en `UnitStateArchive`).

Transición deliberada antes de entrar a una sesión: fade-out de 200ms + delay forzado de 500ms, para que el prefetch de la sesión siempre resuelva antes de mostrar cualquier loading state.

## Sesión — runner (`session/[sessionId]/session-runner.tsx`)

El loop central de ejercicios. Tarjeta swipeable (Framer Motion, `drag="x"`, elástico, con snap-back) para pasar entre ejercicios. Grilla de opciones 2×2 solo si hay exactamente 4 opciones y todas ≤35 caracteres; si no, lista apilada (regla espejada en `backend/content/authoring-context.md` del lado de contenido). Trackea intentos por ítem (`wrongOptions`) para alimentar `quality_from_attempts`.

Micro-encuesta post-ejercicio (canales A/B/C/D — dificultad, utilidad de la explicación "¿Por qué?", reporte de contenido, e interés del problema): se dispara como una slide intermedia con la misma mecánica de interacción que un ejercicio (seleccionar → Continuar → banner verde de agradecimiento + sonido), gateada por `feedback_survey.py` (caps anti-fatiga: máx 1 por sesión, nunca 1er/último ejercicio, alternancia entre sesiones, pausa tras 3 skips seguidos). Deliberadamente **sin XP** — el motor es el reconocimiento ("esto ayuda a elegir mejor qué mostrarte"), no la recompensa.

El canal **D** (aburrido / justo / interesante, con un chip de razón opcional en los extremos) es el norte para análisis de contenido y retención, y por eso se lleva la mayoría del cupo muestreado: 60% D, 25% A, 15% B. A diferencia de los otros tres canales, su targeting cuenta impresiones **por canal** — un ítem con votos de dificultad no está "cubierto" para interés, son preguntas distintas. Las reglas anti-fatiga, en cambio, cuentan a D igual que a los demás. Al leer estos datos hay que controlar por acierto al primer intento (`answers.quality_score = 5`): el interés reportado correlaciona fuerte con "me salió".

## Resumen de sesión (`session/[sessionId]/summary/`)

XP ganada (con el bonus por racha separado), confetti con los colores de belt (`BELT_VIVID_COLORS`). No hay nivel: la feature se descartó y el código de curva de niveles se eliminó de `algorithm/xp.py` y de los payloads (2026-08) — no asumir que existe UI ni API de nivel.

## Práctica (`practice/`)

Volumen libre e ilimitado a elección del usuario, sin gate diario, XP plano y bajo (ver [gamification.md](gamification.md)) para que no sea farmeable, pero sí escala con el multiplicador de racha diaria.

## Test (`test/`)

Pantalla de configuración estilo examen, flujo separado de Repaso/Práctica.

## Leaderboard (`leaderboard/`)

Ranking global y **por universidad** — ver [gamification.md](gamification.md), es el objetivo de retención de largo plazo del producto, no una pantalla secundaria.

## Perfil (`profile/`)

Edición de nombre/username, configuración de notificaciones (hora local en pasos de 15 min + timezone), árbol de badges de emoji (desbloqueables, se pueden "vestir" para mostrar en el ranking), flujo de feedback libre.

## Onboarding (`onboarding/`)

Intake de curso/carrera/universidad/motivación (los datos de universidad son los que alimentan directamente el ranking universitario y la segmentación de notificaciones), secuencia animada de colores de belt, termina en `onboarding/complete` con prompt de instalación PWA.

## Minijuego de derivadas (`derivadas/`, backend `game/`)

Producto aparte, con identidad y economía propias pero la misma tabla de
cafecitos. Lo único documentado acá es **cómo elige qué ejercicio servir**, que
es la mecánica que gobierna la experiencia entera:

- **El @ se asigna y después se elige.** Quien entra sin cuenta arranca con un
  @ autogenerado (`game/aliases.py`): `casifinal`, `triplechoripan`,
  `goldenmedialuna` — comida rioplatense y vida de cursada, sin números. El
  formato viejo era palabra-del-temario + cuatro dígitos (`modulo4124`) y hacía
  dos cosas mal: el número delataba que el nombre no lo eligió nadie, y la
  palabra venía de la materia. Un invitado puede cambiarlo **una vez gratis**;
  de ahí en más elegir el @ es el gancho del registro.
- **Elo online, no niveles fijos.** Cada jugador tiene un θ y cada plantilla una
  β; el motor sirve lo que cae en la banda p̂ ∈ [0.70, 0.80], o sea lo que
  estima que va a acertar 3 de cada 4 veces. El θ se muestra en escala de
  ajedrez (`rating = 1000 + 200·θ`) porque 1166 se lee y 0.83 no.
- **Rampa de arranque.** Los tres primeros ejercicios son fijos (x, x², 2x²) y
  hasta la quinta respuesta el tier disponible crece de a uno, para que el juego
  no abra con una exponencial por cómo haya caído el Elo.
- **La β se ancla a la semilla de su tier** (`elo.effective_beta`), pesada en
  PERSONAS distintas y no en respuestas. Sin ese ancla un motor adaptativo se
  autoengaña: lo difícil solo se le sirve a quien va bien, así que lo difícil
  solo recibe evidencia de quien va bien y termina pareciendo fácil.
- **Piso de Elo por plantilla** (`templates.PISO_TRIGONOMETRICAS`, hoy 1200).
  Es el único criterio de la lista que NO es adaptativo, y por eso existe: el
  ancla frena que una β se desboque pero no la revierte, y las trigonométricas
  se habían desplomado hasta servirse desde 760 de rating. Un piso dice «esto
  no antes de acá» aunque el motor crea lo contrario; pasada la barrera vuelven
  a competir por la banda como cualquier otra. El panel de la tecla `p` muestra
  el piso como Elo de desbloqueo de la fila, así que la promesa de la pantalla y
  lo que el generador hace son el mismo número.

### Avisos del minijuego (`game/notifications.py`, `game/notification_copy.py`)

Canal propio de push, porque el de Intervalo no le llega: `push_subscriptions`
exige un usuario, `due_notifications` corta en repasos SM-2 pendientes y todo su
copy sale de tablas que el juego no toca.

- **Le llega al invitado**, que es la mitad del punto: `game_push_subscriptions`
  cuelga del jugador y los endpoints van con el token de invitado.
- **Tres por día como máximo, y el cupo es de la PERSONA**: uno programado en el
  horario elegido y hasta dos reactivos. Un jugador registrado reclama contra los
  contadores de su `User`, así que un día en que dx tenga tres cosas que decir
  Intervalo no manda nada. El orden lo fija el cron del notifier, que corre las
  tandas del juego tres y seis minutos antes que las de Intervalo.
- **Programadas**: `social` (compañeros de tu universidad que jugaron hoy, XP de
  la semana, tu aporte), `reactivacion` y `record`. Si no hay ningún hecho que
  contar, no se manda nada: no existe un «vení a jugar» genérico.
- **Reactivas**, por orden de prioridad: `empuje` (cafecito), `recluta`,
  `ranking` y `universidad`.
- **La reactivación termina.** Sale a los días 1, 3, 7 y 14 de silencio y nunca
  más; al mes se apaga el canal solo para esa persona.
- **Qué se mide con qué.** El ranking de personas va por XP y el de universidades
  por Elo promedio (que es lo que impide que un cafecito compre puesto), así que
  ningún aviso puede prometer XP para escalar la tabla de universidades.

## Misceláneo

- Splash animado con colores de belt al cargar (`splash-context.tsx`/`splash-gate.tsx`).
- Tab bar / shell (`app-chrome.tsx`).
- PWA: manifest, splash screens iOS generados por script.

Última verificación: 2026-08-01
