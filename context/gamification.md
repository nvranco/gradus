# Gamification

Fuente de verdad: `algorithm/xp.py`. Todas las constantes de esta página vienen de ahí.

## Jerarquía: esto es lo primero que hay que entender

El sistema de gamificación tiene **dos capas con roles distintos, no un conjunto plano de mecánicas**:

- **Corto plazo (feedback loop diario)**: XP por ejercicio, multiplicador de dificultad personal, bonus por racha de aciertos dentro de una sesión, multiplicador de racha diaria. Esto está para que cada sesión se sienta bien y para incentivar volver mañana.
- **Largo plazo (motor de retención real)**: **la competencia en el ranking — especialmente la rivalidad entre universidades**. `notification_copy.py` es la evidencia más clara de esto: de 7 categorías de notificación, `university` (20%), `social` universitario (15%), `ranking` (15%) y `podium` (15%) — es decir, **65% del peso de las notificaciones push está atado directa o indirectamente al ranking/competencia**, contra 15% de recordatorio genérico de práctica.

**Al diseñar o evaluar cualquier feature de gamificación nueva, la pregunta correcta no es "¿cómo le doy más XP a esto?" sino "¿cómo esto alimenta o refuerza la competencia en el ranking, en particular entre universidades?"**. XP es un medio, el ranking es el fin.

## XP

**No hay niveles.** La feature de curva de niveles se descartó (2026-08): el XP es el puntaje crudo que ordena el ranking, sin capa de nivel encima. El código (`XP_TABLE`, `level_progress`, `level_info` en los payloads) se eliminó por completo — si aparece una referencia a "nivel" en algún lado, es resto viejo.

### XP por ejercicio — modo Repaso (`"main"`)

Base según intento (`XP_BY_ATTEMPT`): 1er intento = 8, 2do = 1, 3ro = 0, 4to (por descarte) = 0. Del 2do intento en adelante la elección entre las opciones restantes es mayormente azar, así que casi no paga: XP obtenible por suerte devalúa todo el XP (Octalysis CD2).

**Fase de aprendizaje** (`XP_LEARNING_CORRECT`): mientras la unit está en `learning` (primer contacto + drills a 1-2 días), el 1er intento paga 5 plano, sin multiplicador de dificultad. El logro que el XP certifica es recordar tras un intervalo real; esto es lo que evita la lluvia de XP de las primeras sesiones, donde todo es aprendizaje.

El XP del primer intento **en fase de review** se pondera además por un **multiplicador de dificultad personal** (`difficulty_multiplier`): entre ×0.5 (ítem que el estudiante domina) y ×1.25 (ítem que le cuesta), lineal sobre su precisión rodante de primer intento en las últimas 10 respuestas (`DIFFICULTY_WINDOW`), neutro (×1.0) con menos de 3 muestras. El techo del premio (+25%) es menor que el piso del descuento (−50%) a propósito: la ventana arrastra fallos viejos y sin tope pagaba más justo después de haber fallado.

Bonus fijo por racha de aciertos limpios dentro de la sesión: cada 5 correctas seguidas (`XP_STREAK_INTERVAL`) suma +5 XP (`XP_STREAK_BONUS`), sin multiplicadores.

### XP por ejercicio — modo Práctica

Plano, sin ajuste de dificultad: 3 XP si acierta al primer intento (`XP_PRACTICE_CORRECT`), 0 si no. Sí escala con el multiplicador de racha diaria. Base deliberadamente baja para que no sea farmeable — práctica es volumen ilimitado a elección del usuario.

### Multiplicador de racha diaria (`STREAK_TIERS`)

La racha cuenta **días distintos con ≥1 sesión completada**, no necesariamente consecutivos:

| Días acumulados | Multiplicador |
|---|---|
| 0 | ×1.0 |
| 3 | ×1.2 |
| 9 | ×1.4 |
| 18 | ×1.6 |
| 30 | ×1.8 |
| 45 | ×2.0 (máximo de la racha) |

Se resetea a 0 tras 30 días consecutivos sin actividad (`STREAK_RESET_AFTER_DAYS`).

### Multiplicador de cafecito, y el tope del producto (`MAX_TOTAL_MULTIPLIER`)

La racha ya no es el único multiplicador. Un cafecito invitado en el minijuego
multiplica el XP de **toda una universidad** durante 24 h (48 h al tope de una
donación), y desde el cruce eso vale también en Intervalo clásico:
`backend/xp_boost.py` traduce «de qué universidad es esta persona» al vocabulario
que `game/boosts.py` ya entiende y le pregunta a él. No hay una segunda mecánica
ni una segunda tabla.

Los dos multiplicadores se aplican **juntos, redondeando una sola vez sobre el
producto** (`effective_multiplier`), con un tope propio de **×4,0**
(`MAX_TOTAL_MULTIPLIER`), no el ×3,0 del juego. El tope es 4,0 y no 3,0 porque la
racha sola llega a ×2,0 y **una** donación sola llega a ×2,0: con tope 3,0 lo
alcanzaría una sola persona con racha alta y su propia donación, y
`game/boosts.py` promete con todas las letras lo contrario — «el ×3 no se compra,
se junta».

**El candado antimudanza** (`enrollments.university_set_at`,
`game_players.university_set_at`) es lo que sostiene la rivalidad: mudarse a la
universidad impulsada después de que arrancó el empuje no lo cobra. Sin él, cada
empuje se llenaría de gente que se muda por un día.

### Empuje por aforo: 10 personas nuevas en un día (`game/aforo.py`)

El segundo modo de encender un empuje, y el único que no cuesta plata. Cuando
entra la **persona número 10** de una universidad en el mismo día, esa
universidad se lleva **×1,5 durante 2 horas**, en el acto y sin que nadie done.

**Qué cuenta como una persona nueva**: la suma de los dos productos —un alta de
Intervalo clásico que se inscribe en esa universidad, y un jugador nuevo del
minijuego que carga la suya—. Se cuentan **personas y no filas**: quien juega de
invitado y después se registra el mismo día deja dos filas y es una sola
persona, y la identidad ya está unificada por `game_players.user_id`.

**Una sola vez por universidad y por día**, con el día en huso argentino. No hay
columna de estado ni candado: `external_ref` vale `aforo:<sigla>:<día>` y es
UNIQUE, así que la persona 11, dos altas simultáneas y el reintento de un POST
rebotan todos contra el mismo índice.

**Vive en la misma tabla que el cafecito** (`game_boosts`, `source="aforo"`) y
por lo tanto **suma** con él: una universidad con una donación de 3 y el aforo
del día está en ×1,8. Lo que NO hace es hacerse pasar por una donación — el
cartel cuenta en `cafecitos` solo lo que se donó de verdad y marca el aforo con
su propia bandera, el feed tiene su propia frase, y el mail de agradecimiento
del vencimiento lo saltea porque no hay a quién agradecerle.

**Por qué dura 2 h y no 24**: el de aforo se junta esa misma tarde y tiene que
gastarse esa misma tarde. Con la duración del pago le comería el lugar, que es
lo que financia el proyecto.

### Cómo se reparte el extra

`Answer.xp_base` es antes de los multiplicadores y `Answer.xp_earned` después. La
diferencia **no** es "el bonus por tu racha": es la suma de los dos
multiplicadores. Lo que puso el cafecito se guarda aparte, en
`Answer.xp_from_boost`, porque no se puede reconstruir después — solo sobrevive el
total. El resumen de sesión devuelve las dos partes por separado
(`streak.xp_bonus` y `xp_from_boost`), y las push de universidad descuentan el
empuje de sus ventanas semanales: el ranking acumulado sí lo incluye, pero
meterlo en una ventana temporal lo hace competir contra semanas que no lo tenían.

## Belts y graduación

Ver [domain-model.md](domain-model.md#maestría-y-graduación-algorithmgraduationpy) para la definición exacta. Belts activos: blanco → azul → violeta → marrón.

## Ranking (leaderboard) — el objetivo de largo plazo

Ranking global y **por universidad** (`web/src/lib/nav`/`leaderboard/UseUniversityLeaderboard.ts`), ordenado por `User.total_xp`. `User.notify_last_rank` guarda el último rank global conocido del usuario para detectar "te pasaron en el ranking" y disparar el push correspondiente (categoría `ranking`, `notification_copy.py::_ranking_named`/`_ranking_generic`).

**Quién aparece**: `main.VISIBLE_EN_RANKING` es `total_xp > referral_xp_earned`, o
sea «resolvió algo acá». No alcanza con tener XP: desde los reclutas cruzados, la
XP la puede subir un recluta sin que el reclutador haya respondido nunca un
ejercicio. El gemelo del minijuego es `game/ranking.py :: RESOLVIO_ACA`
(`exercises_correct > 0`), y los dos existen por el mismo motivo. Los lugares que
filtran tienen que moverse juntos: si el que cuenta el total no filtra igual que
el que arma la lista, los números de la cabecera dejan de cuadrar con las filas.

**Una tercera vista: Reclutas.** Quien entra por el link de alguien (`?r=<@>`) le
paga un **10%** (`referrals.SHARE_PERCENT`) de lo que gane, en la moneda que gane
— XP de clásico a `users.total_xp`, XP de juego a `game_players.xp`, sin
cruzarse—. La XP se **acuña**: al recluta no se le descuenta nada. Un solo nivel:
los reclutas de tus reclutas no pagan. La contabilidad va en centésimas
(`referral_pending`) porque el 10% de 25 XP son 2,5 y redondear cada pago hacia
abajo convertiría el 10% en 8%.

**El @ es uno solo en todo Intervalo** (`backend/handles.py`): una tabla
`handles` que es la autoridad, con `users.username` y `game_players.alias` como
caché desnormalizado para que el ranking no tenga que joinear en cada request. Un
@ soltado **no se libera**: queda `retired` y sigue resolviendo los links `?r=`
que esa persona repartió.

Categorías de notificación atadas al ranking/universidad (`backend/notification_copy.py`, pesos sobre 1.0):
- `university` (0.20): XP semanal aportado a la universidad, top contribuyente, brecha contra universidad rival.
- `social` (0.15): cuántos compañeros de la misma universidad ya practicaron hoy.
- `ranking` (0.15): alguien te superó en el ranking (nombrado si se conoce el nombre).
- `podium` (0.15): a cuánto XP estás del top N general o de tu universidad.
- `practice` (0.15): recordatorio genérico, sin componente social.
- `reactivation` (0.10) / `personal_best` (0.10): reengagement puro.

## Badges de emoji

Track de desbloqueo append-only por carrera (`emoji_tree.py`, `User.emoji_path`/`emoji_worn`), mostrado/vestido en el leaderboard — capa de ownership (Core Drive 4 de Octalysis) distinta de XP/belts, pensada como otro insumo de estatus social visible en el ranking, no como sistema de recompensa aislado.

Última verificación: 2026-09-03
