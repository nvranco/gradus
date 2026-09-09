"""Métricas del minijuego de derivadas, en un payload de dicts planos.

Mismo criterio que `metrics/queries.py`: un `SELECT` por tabla y el resto es
Python. Los motivos son los mismos (dev en SQLite, producción en Postgres, datos
chicos), y arriba de eso hay uno propio del juego: acá casi ninguna métrica es
una agregación. La curva de supervivencia por número de ejercicio y la
sesionización por huecos son recorridos sobre series ordenadas, y escribirlos en
SQL portable sería peor código para el mismo resultado.

**Definiciones que no se negocian.** Son las que, si se aflojan, convierten el
panel en un generador de números lindos:

  - **Estudiante** = fila de `game_players` con `is_bot = false`. Los sembrados
    pueblan el ranking para que el primero en llegar tenga a quién escalar
    (scripts/seed_game_bots.py); contarlos como gente inflaría todo.
  - **Respuesta** = intento con `parse_ok = true`. Lo que no parsea se registra
    igual pero vive en su propia sección: es fricción del input, no matemática.
  - **Derivada resuelta** = acierto. Un ejercicio se cierra al acertar o al
    gastar el segundo intento, así que no hay doble conteo.
  - **Sesión de juego** = tanda de respuestas separadas por menos de
    `SESSION_GAP_MINUTES`. El juego no tiene un objeto «sesión» —se entra por un
    link y se juega hasta que uno se cansa— así que la sesión se reconstruye por
    huecos, que es la única definición disponible y hay que decirlo en voz alta.
  - **Partida** = la PRIMERA sesión de un estudiante, y nada más. Es lo que
    mide la curva de profundidad. Antes era su vida entera, y eso tenía dos
    costos: había que esperar 24 h de silencio para leerla —la cohorte de la
    semana en curso quedaba vacía todo el día— y la curva de una cohorte vieja
    seguía moviéndose para siempre, porque alguien de agosto que vuelve en
    octubre cambia la mediana de agosto. Una cohorte cerrada tiene que ser un
    hecho, no un número móvil.
  - **Partida cerrada** = su primera sesión ya no puede crecer, o sea que pasó
    más de `SESSION_GAP_MINUTES` desde la última respuesta de esa tanda. Es la
    única que entra en la curva: quien está jugando ahora todavía puede sumar
    derivadas, y contarlo hunde la cola por reloj y no por comportamiento.

**Zona horaria.** Igual que el panel de Intervalo: columnas naive en UTC, el día
del negocio es el de Argentina, todo pasa por `local_date()`. Semanas de lunes a
domingo.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session as DBSession

from .queries import _pct, _rows, local_date, week_start

# Hueco que corta una sesión de juego. Media hora es lo que dura un empuje de
# cafecito y lo que la industria usa como default de sesión; lo importante no es
# el número exacto sino que sea UNO y esté escrito en un solo lugar.
SESSION_GAP_MINUTES = 30

# Los hitos donde el producto INTERRUMPE la partida para pedir algo. Los números
# no se eligen acá: son los del front, y hay que venir a cambiarlos cuando allá
# cambien.
#
#   - carrera y universidad a las 3   (web/src/app/derivadas/hitos-del-juego.ts :: HITO_PERFIL)
#   - registro a las 12               (idem :: HITO_REGISTRO)
#   - cafecito cada 20                (web/src/app/derivadas/cafecito-cta.tsx :: CAFECITO_EVERY)
#
# El panel los marca para poder ver si el escalón de abandono cae JUSTO ahí, que
# sería el producto pinchando su propia partida. Por eso importa que estén al
# día: con la universidad marcada en la 5 cuando en realidad se pide en la 3, el
# escalón que se estaba buscando quedaba dos derivadas corrido.
PEDIDO_PERFIL = 3
PEDIDO_REGISTRO = 12
PEDIDO_CAFECITO = 20

# Hasta dónde se dibuja la curva de supervivencia por ejercicio.
DEPTH_MAX = 40

# El teléfono y la compu son dos juegos distintos: en uno hay un flujo infinito
# de slides y un teclado matemático apoyado sobre uno táctil; en el otro está
# todo en una vista y la persona escribe con las dos manos. Agrupar iOS y
# Android bajo «teléfono» es el corte que decide dónde invertir; separarlos sale
# gratis, y en un juego que se difunde por WhatsApp en Argentina la mezcla dice
# a quién le está llegando el link.
PLATFORM_ORDER: tuple[str, ...] = ("android", "ios", "desktop")
PLATFORM_LABEL = {"android": "Android", "ios": "iOS", "desktop": "Escritorio",
                  None: "Sin dato"}

# El origen de un empuje que cuenta como ingreso: `cafecito` es el que entró por
# el oyente del stream (game/cafecito_stream.py). Los otros dos —`manual`, que
# insertamos nosotros para probar, y `aforo`, que regala el propio juego— no son
# plata y no pueden sumar al titular. Pasó: el primer día de producción 20 de 35
# cafecitos eran grants a mano.
DONADO = "cafecito"

# Primera semana que el panel del juego muestra: la de la difusión.
#
# Antes de esto el juego existía pero no lo había abierto nadie, así que todas
# las cohortes anteriores son ceros estructurales. Ceros que igual se dibujan:
# el sparkline arrancaba con tres semanas planas, cada titular decía «+72 vs.
# semana anterior» comparando contra una semana en la que el producto no estaba
# difundido, y las métricas de tasa quedaban en «sin base». Nada de eso es
# información — es la ausencia de producto con formato de tendencia.
#
# El piso es del PANEL, no de los datos: si alguna vez hay filas anteriores, las
# consultas las cuentan igual. Lo único que se corta es ofrecer esas semanas
# como si fueran comparables. Mismo criterio que FIRST_WEEK en metrics/queries.py.
FIRST_WEEK = date(2026, 8, 24)


def clamp_week(w: date) -> date:
    """No dejar salir del rango que el panel sabe mostrar."""
    return min(max(w, FIRST_WEEK), week_start(local_date(datetime.utcnow())))


def _week_of(dt: datetime | None) -> date | None:
    d = local_date(dt)
    return None if d is None else week_start(d)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _p(values: list[float], q: float) -> float | None:
    """Percentil por interpolación, sin numpy. `q` en 0..1."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(float(s[0]), 1)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 1)


# ── Carga ────────────────────────────────────────────────────────────────────

def load(db: DBSession) -> dict:
    """Un SELECT por tabla, solo las columnas que se usan.

    Solo tablas del juego: las de Intervalo (`users`, `sessions`) se cargaban
    enteras y no las leía ninguna sección, así que eran dos recorridos completos
    por armado del panel a cambio de nada.
    """
    data = {
        "players": _rows(db, """
            SELECT id, user_id, university, referred_by, platform, is_bot,
                   created_at, last_seen_at
            FROM game_players"""),
        "exercises": _rows(db, "SELECT id, player_id, created_at FROM game_exercises"),
        "attempts": _rows(db, """
            SELECT player_id, attempt_number, parse_ok, is_correct, created_at
            FROM game_attempts"""),
        "boosts": _rows(db, "SELECT cafecitos, source, created_at FROM game_boosts"),
        "cta": _rows(db, "SELECT player_id, created_at FROM game_cta_events"),
    }

    # Los bots se sacan UNA vez, acá, y no en cada bloque: filtrar en diez
    # lugares es la forma segura de olvidarse en el undécimo.
    bots = {p["id"] for p in data["players"] if p["is_bot"]}
    data["players"] = [p for p in data["players"] if not p["is_bot"]]
    data["exercises"] = [e for e in data["exercises"] if e["player_id"] not in bots]
    data["attempts"] = [a for a in data["attempts"] if a["player_id"] not in bots]
    data["cta"] = [c for c in data["cta"] if c["player_id"] not in bots]
    data["_bots"] = len(bots)

    # Respuestas de verdad: las que el parser entendió. Se ordenan una sola vez
    # porque la supervivencia, las sesiones y la escalera de θ recorren la misma
    # serie, y reordenarla tres veces es puro gasto.
    data["_answers"] = sorted(
        (a for a in data["attempts"] if a["parse_ok"]),
        key=lambda a: (a["player_id"], a["created_at"] or datetime.min),
    )
    data["_firsts"] = [a for a in data["_answers"] if a["attempt_number"] == 1]
    return data


def _weeks_back(week: date, n: int) -> list[date]:
    """La semana elegida y las n-1 anteriores, de más vieja a más nueva.

    Corta en `FIRST_WEEK`: antes de la difusión el juego no tenía a nadie, y esas
    semanas vacías no son una caída sino la ausencia de producto."""
    ws = [week - timedelta(weeks=i) for i in range(n - 1, -1, -1)]
    return [w for w in ws if w >= FIRST_WEEK] or [week]


def _in_week(dt: datetime | None, week: date) -> bool:
    d = local_date(dt)
    return d is not None and week <= d <= week + timedelta(days=6)


# ── 0 · Titulares ──────────────────────────────────────────────────────────

def _primera_sesion(lista: list[dict]) -> list[dict]:
    """La PRIMERA tanda de un jugador: corta en el primer hueco largo.

    `lista` son las respuestas de un solo jugador en orden —`_answers` y
    `_firsts` ya vienen ordenadas por (jugador, fecha), así que agruparlas
    alcanza—. Es el único lugar donde se decide dónde termina una sesión, y lo
    usan el titular de la primera sesión y la curva de profundidad: si se
    partiera en dos, el panel tendría dos definiciones de «sesión» y una de las
    dos envejecería mal.
    """
    fin = None
    tanda: list[dict] = []
    for a in lista:
        t = a["created_at"]
        if t is None:
            continue
        if fin is not None and (t - fin) > timedelta(minutes=SESSION_GAP_MINUTES):
            break
        fin = t
        tanda.append(a)
    return tanda


def _correctas_de_la_primera_sesion(lista: list[dict]) -> int:
    """Cuántas acertó alguien en su primera tanda."""
    return sum(1 for a in _primera_sesion(lista) if a["is_correct"])


def headline(data: dict, weeks: list[date]) -> list[dict]:
    """Los ocho números de arriba: entra gente, se queda, y trae más gente.

    La fila de arriba es el embudo de entrada —quién llegó, quién se quedó— y la
    de abajo el motor de crecimiento —qué deja y a quién trae—. Es el orden en
    que se toman las decisiones, no el orden en que salieron las features.
    """
    players = data["players"]
    answers = data["_answers"]
    alta_de = {p["id"]: local_date(p["created_at"]) for p in players}

    # ── Quién estuvo cada semana ────────────────────────────────────────
    # No hay tabla de visitas: el juego no registra un pageview, registra lo que
    # la persona HACE. Así que "ingresó" se arma con toda huella fechada que deja
    # una visita —el alta, un ejercicio servido, una respuesta, un cartel visto—
    # más `last_seen_at`, que es lo único que deja quien volvió y no tocó nada.
    #
    # Lo que esto NO ve: alguien que ya existía, vuelve a abrir la página, no hace
    # nada, y otra semana vuelve y sí juega. Su primera vuelta se pierde, porque
    # `last_seen_at` es un solo instante y se lo lleva la segunda. Los pageviews
    # de verdad los tiene PostHog; acá el número es un piso, nunca un techo.
    visto: dict[date, set[int]] = defaultdict(set)

    def marcar(pid: int, cuando) -> None:
        w = _week_of(cuando)
        if w is not None:
            visto[w].add(pid)

    for p in players:
        marcar(p["id"], p["created_at"])
        marcar(p["id"], p["last_seen_at"])
    for e in data["exercises"]:
        marcar(e["player_id"], e["created_at"])
    for a in data["attempts"]:
        marcar(a["player_id"], a["created_at"])
    for c in data["cta"]:
        marcar(c["player_id"], c["created_at"])

    por_jugador: dict[int, list[dict]] = defaultdict(list)
    for a in answers:
        por_jugador[a["player_id"]].append(a)

    def nuevos(w: date) -> list[dict]:
        return [p for p in players if _in_week(p["created_at"], w)]

    def per_week(fn) -> list:
        return [fn(w) for w in weeks]

    def ingresos(w: date) -> int:
        return len(visto.get(w, ()))

    def altas(w: date) -> int:
        return len(nuevos(w))

    def registrados(w: date) -> float | None:
        ns = nuevos(w)
        return _pct(sum(1 for p in ns if p["user_id"]), len(ns))

    def retenidos(w: date) -> int:
        """Gente de OTRA semana que volvió a jugar en esta.

        Se mide por respuesta y no por visita: volver a abrir la página sin hacer
        nada no es retención, es un rebote con más pasos.
        """
        return len({
            a["player_id"] for a in answers
            if _in_week(a["created_at"], w)
            and (alta_de.get(a["player_id"]) or date.max) < w
        })

    def cafecitos(w: date) -> int:
        return sum(b["cafecitos"] for b in data["boosts"]
                   if _in_week(b["created_at"], w) and b["source"] == DONADO)

    def reclutas(w: date) -> int:
        return sum(1 for p in nuevos(w) if p["referred_by"])

    def viralidad(w: date) -> float | None:
        """Cuánta gente nueva trajo, en promedio, cada uno de los que ya estaban.

        El denominador son los que EXISTÍAN al empezar la semana, que son los
        únicos que podían repartir su `?r=`. Uno significa que el juego se sostiene
        solo; abajo de uno, cada camada trae menos que la anterior y el
        crecimiento sigue dependiendo de que difundamos.
        """
        base = sum(1 for p in players if (alta_de.get(p["id"]) or date.max) < w)
        return round(reclutas(w) / base, 2) if base else None

    def primera_sesion(w: date) -> float | None:
        """Mediana de derivadas resueltas en la primera tanda de cada uno.

        Sobre los nuevos de la semana —para quienes esa tanda ES la primera— y
        solo sobre los que llegaron a responder algo: quien abrió y se fue no
        tiene primera sesión que medir, y meterlo como cero convierte esto en otra
        medición de rebote, que ya hace el embudo.
        """
        valores = [
            float(_correctas_de_la_primera_sesion(por_jugador[p["id"]]))
            for p in nuevos(w) if por_jugador.get(p["id"])
        ]
        return _median(valores)

    def card(label: str, series: list, suffix: str, hint: str, dec: int = 1) -> dict:
        value = series[-1]
        prev = series[-2] if len(series) > 1 else None
        delta = round(value - prev, dec) if (value is not None and prev is not None) else None
        return {"label": label, "value": value, "suffix": suffix, "series": series,
                "delta": delta, "hint": hint, "dec": dec}

    return [
        # Fila 1 · quién entró y quién se quedó.
        card("Ingresos", per_week(ingresos), "",
             "Personas distintas que abrieron el juego esa semana. Es un piso: una "
             "visita sin actividad de alguien que ya existía no deja rastro."),
        card("Usuarios nuevos", per_week(altas), "",
             "Los que abrieron el juego por primera vez esa semana."),
        card("Se registran", per_week(registrados), "%",
             "De los nuevos de la semana, cuántos dejaron de ser invitados."),
        card("Usuarios retenidos", per_week(retenidos), "",
             "Gente de otra semana que volvió a jugar en esta."),
        # Fila 2 · qué deja y a quién trae.
        card("Cafecitos", per_week(cafecitos), "",
             "Solo los donados de verdad: los grants a mano y los de aforo no cuentan."),
        card("Reclutas", per_week(reclutas), "",
             "Nuevos que entraron por el link de otro jugador."),
        card("Coeficiente de viralidad", per_week(viralidad), "",
             "Cuánta gente trajo cada uno de los que ya estaban. Uno es el juego "
             "creciendo solo.", dec=2),
        card("Primera sesión", per_week(primera_sesion), "",
             "Mediana de derivadas resueltas en la primera tanda, entre los que "
             "llegaron a responder."),
    ]


# ── 1 · Embudo ───────────────────────────────────────────────────────────────

def funnel(data: dict, week: date) -> dict:
    """Embudo de la cohorte que abrió el juego en `week`, seguida hasta hoy.

    Arranca en «abrió el juego» y no en «vio el link»: la fila de
    `game_players` se crea en la primera carga, así que todo lo anterior
    (impresiones de WhatsApp, clicks que no llegaron a cargar) solo lo sabe
    PostHog. El embudo lo dice en vez de fingir que empieza antes.
    """
    cohort = [p for p in data["players"] if _in_week(p["created_at"], week)]
    ids = {p["id"] for p in cohort}
    answers = [a for a in data["_answers"] if a["player_id"] in ids]

    served: Counter = Counter(e["player_id"] for e in data["exercises"] if e["player_id"] in ids)
    correct_by = Counter(a["player_id"] for a in answers if a["is_correct"])
    respondieron = {a["player_id"] for a in answers}
    dias_by: dict[int, set] = defaultdict(set)
    for a in answers:
        dias_by[a["player_id"]].add(local_date(a["created_at"]))

    base = len(cohort)
    # `cadena` marca los pasos que SÍ están anidados: cada uno es un subconjunto
    # del anterior, así que «% del paso anterior» significa algo. Los otros tres
    # no lo están —se puede cargar la universidad sin haber llegado a 25, y quien
    # viene de Intervalo llega registrado desde el minuto cero— y para ellos el
    # único denominador honesto es la cohorte. Sin esta distinción salían cosas
    # como «600% del paso anterior», que no quiere decir nada.
    steps = [
        ("Abrió el juego", base, True),
        ("Vio una derivada", sum(1 for pid in ids if served.get(pid)), True),
        ("Respondió", len(respondieron), True),
        ("Acertó una", sum(1 for pid in ids if correct_by.get(pid, 0) >= 1), True),
        # Tres y no cinco: es la derivada donde el juego frena y pide carrera y
        # universidad, así que el paso de al lado —«cargó universidad»— se lee
        # contra la gente que efectivamente llegó a que se lo preguntaran.
        (f"Llegó a {PEDIDO_PERFIL}",
         sum(1 for pid in ids if correct_by.get(pid, 0) >= PEDIDO_PERFIL), True),
        # Cada pedido va pegado al hito que lo dispara y no todos juntos al
        # final: la universidad se pide en la 3 y el registro en la 12, así que
        # leídos en ese lugar dicen cuánta de la gente que llegó a que se lo
        # preguntaran contestó. Al fondo de la lista no decían nada — parecían
        # dos pasos más de una cadena a la que no pertenecen.
        ("Cargó universidad", sum(1 for p in cohort if p["university"]), False),
        ("Llegó a 10", sum(1 for pid in ids if correct_by.get(pid, 0) >= 10), True),
        ("Se registró", sum(1 for p in cohort if p["user_id"]), False),
        ("Llegó a 25", sum(1 for pid in ids if correct_by.get(pid, 0) >= 25), True),
        ("Volvió otro día", sum(1 for pid in ids if len(dias_by.get(pid, ())) >= 2), False),
    ]

    out, prev = [], None
    for label, n, cadena in steps:
        out.append({"label": label, "n": n, "cadena": cadena, "pct_base": _pct(n, base),
                    "pct_prev": _pct(n, prev) if (cadena and prev) else None})
        if cadena:
            prev = n
    return {"base": base, "steps": out}


# ── 2 · Profundidad de partida ─────────────────────────────────────────────

# Los cortes con los que se puede partir la curva. `total` es una sola línea con
# todo el mundo; los otros tres la parten para poder comparar.
#
# Son los tres ejes por los que el juego puede ser distinto para dos personas:
# CUÁNDO llegaron (la difusión de esa semana no es la de la anterior), DE DÓNDE
# (cada universidad llega por su propio grupo y con su propia carrera) y CON QUÉ
# (el teclado matemático sobre una pantalla táctil es otro producto). Cualquier
# otra cosa —carrera, origen del link— se puede mirar en las secciones que ya
# están; estas tres cambian la forma de la curva, que es lo que se compara acá.
CORTES = ("total", "cohorte", "universidad", "aparato")

# Cuántas series como máximo. Tres cohortes porque es lo que pidió el uso —dos
# no es tendencia y cuatro ya no se distinguen— y cinco universidades porque a
# partir de ahí las líneas de abajo son de tres personas y ensucian el dibujo
# sin decir nada. Los aparatos son tres y no hace falta recortarlos.
MAX_COHORTES = 3
MAX_UNIVERSIDADES = 5

# Piso para que un grupo merezca su propia línea. Con menos de cinco partidas
# cerradas la curva es una escalera de a 20 puntos: dibuja el ruido de cuatro
# personas con la misma tinta que la tendencia de cuarenta.
MIN_BASE_SERIE = 5


def _curva_de(largos: list[int]) -> list[dict]:
    """La curva de supervivencia de una lista de largos de partida."""
    base = len(largos)
    out = []
    for k in range(1, DEPTH_MAX + 1):
        vivos = sum(1 for n in largos if n >= k)
        siguen = sum(1 for n in largos if n >= k + 1)
        out.append({
            "k": k,
            "vivos": vivos,
            "pct": _pct(vivos, base),
            # Riesgo: de los que llegaron a k, qué fracción NO llegó a k+1. Es
            # lo que localiza el escalón; la curva acumulada lo suaviza y lo
            # esconde.
            "abandono": _pct(vivos - siguen, vivos),
        })
    return out


def profundidad(data: dict, weeks: list[date], now: datetime | None = None,
                corte: str = "total") -> dict:
    """Cuántas derivadas aguanta la gente, y dónde exactamente se va.

    Es LA métrica del juego. El Elo, el ranking y el cafecito existen para mover
    esta curva, así que conviene mirarla antes que a ellos.

    **La cohorte es la de la semana elegida**, la misma que mide el embudo. Que
    las dos secciones hablen de la misma gente es lo que permite leerlas
    seguidas: el embudo dice cuántos de esa camada llegaron a la décima derivada
    y la curva dice dónde se fueron los que no.

    Antes esto tomaba a todos los que habían entrado desde el principio de la
    ventana visible y no cortaba por arriba, así que mirar una semana vieja
    devolvía una curva con gente que todavía no existía esa semana —una semana
    de agosto traía 29 partidas cuando la cohorte real eran 13—. Eso no es una
    cohorte: es «todo el mundo, ordenado por otra cosa».

    **La partida es la PRIMERA sesión**, no la vida entera del jugador, y está
    cerrada cuando esa tanda ya no puede crecer: pasaron más de
    `SESSION_GAP_MINUTES` desde su última respuesta.

    Antes se medía la vida entera y se esperaban 24 h de silencio para leerla.
    Los datos de producción dicen por qué eso no se arreglaba bajando la espera:
    los huecos entre derivadas son bimodales —el 97% dura menos de media hora y
    después no hay nada hasta el día siguiente—, así que entre 3 h y 12 h de
    espera se gana 0,2 puntos de cobertura. La única mejora real estaba en las
    24 h, que son justo las que dejaban la semana en curso vacía: 2 partidas
    legibles contra 62 abiertas.

    Medir la primera sesión cuesta poco y paga dos veces. Cuesta poco porque
    para el 85% de los jugadores esa tanda ES toda su vida en el juego, y cubre
    el 79% de las derivadas. Y paga dos veces: la cohorte de hoy se lee en media
    hora en vez de en un día, y la curva de una cohorte vieja **se congela** —con
    la vida entera, alguien de agosto que vuelve en octubre movía la mediana de
    agosto para siempre—.

    `corte` agrega líneas, y de dos maneras distintas:

      - `universidad` y `aparato` PARTEN la cohorte de la semana en montones,
        así que sus líneas suman exactamente la del total;
      - `cohorte` TRAE OTRAS cohortes —la elegida y las dos anteriores— para
        comparar camadas entre sí. Ahí las líneas no suman nada: son tres
        poblaciones distintas, y esa es justamente la comparación.
    """
    now = now or datetime.utcnow()
    corte = corte if corte in CORTES else "total"
    corte_reloj = now - timedelta(minutes=SESSION_GAP_MINUTES)
    semana = weeks[-1]

    # La primera tanda de cada uno: cuántas derivadas tiene y cuándo terminó.
    # Se arma una sola vez para todos los jugadores porque `cohorte` vuelve a
    # recorrer tres semanas y `universidad`/`aparato` reparten la misma.
    por_jugador: dict[int, list[dict]] = defaultdict(list)
    for a in data["_firsts"]:
        por_jugador[a["player_id"]].append(a)
    primera_de: dict[int, tuple[int, datetime]] = {}
    for pid, lista in por_jugador.items():
        tanda = _primera_sesion(lista)
        if tanda:
            primera_de[pid] = (len(tanda), tanda[-1]["created_at"])

    def largos_de(w: date) -> tuple[list[dict], list[int]]:
        """Las partidas cerradas de la cohorte de `w`, y su largo."""
        cohorte = [p for p in data["players"]
                   if _in_week(p["created_at"], w) and p["id"] in primera_de]
        cerrados = [p for p in cohorte if primera_de[p["id"]][1] < corte_reloj]
        return cerrados, [primera_de[p["id"]][0] for p in cerrados]

    cerrados, largos = largos_de(semana)
    abiertos = sum(1 for p in data["players"]
                   if _in_week(p["created_at"], semana) and p["id"] in primera_de
                   and primera_de[p["id"]][1] >= corte_reloj)
    base = len(largos)
    curva = _curva_de(largos)

    # El escalón más grande de los primeros 20, que es el tramo donde el
    # producto interviene. Se pide una base mínima: un abandono del 100% sobre 2
    # personas no es un escalón. Se calcula siempre sobre la cohorte de la
    # semana —con corte o sin corte— porque es el titular de la sección y no
    # puede moverse al cambiar de desglose.
    tramo = [c for c in curva if c["k"] <= 20 and c["vivos"] >= 5]
    peor = max(tramo, key=lambda c: c["abandono"] or 0) if tramo else None

    def serie(label: str, clave: str | None, valores: list[int]) -> dict:
        return {"label": label, "clave": clave, "base": len(valores),
                "curva": _curva_de(valores),
                "mediana": _median([float(n) for n in valores])}

    if corte == "total":
        series = [serie("Siguen jugando", None, largos)]
    elif corte == "cohorte":
        # La elegida y las dos anteriores, de la más vieja a la más nueva: el
        # orden del tiempo es el orden en que se lee la comparación.
        series = []
        for i in range(MAX_COHORTES - 1, -1, -1):
            w = semana - timedelta(weeks=i)
            if w < FIRST_WEEK:
                continue
            _, v = largos_de(w)
            if len(v) >= MIN_BASE_SERIE:
                series.append(serie(w.strftime("%d/%m"), w.isoformat(), v))
    else:
        grupos: dict = defaultdict(list)
        for p in cerrados:
            clave = p["university"] if corte == "universidad" else p["platform"]
            if clave:
                grupos[clave].append(primera_de[p["id"]][0])
        vivos = [(k, v) for k, v in grupos.items() if len(v) >= MIN_BASE_SERIE]
        if corte == "universidad":
            vivos.sort(key=lambda kv: -len(kv[1]))
            vivos = vivos[:MAX_UNIVERSIDADES]
        else:
            orden = {k: i for i, k in enumerate(PLATFORM_ORDER)}
            vivos.sort(key=lambda kv: orden.get(kv[0], 99))
        series = [
            serie(str(k) if corte == "universidad" else PLATFORM_LABEL[k], str(k), v)
            for k, v in vivos
        ]

    return {
        "corte": corte,
        "semana": semana,
        "base": base,
        "abiertos": abiertos,
        "curva": curva,
        "series": series,
        # Cuánta de la cohorte cubre el desglose. Solo tiene sentido para los
        # cortes que PARTEN la cohorte: en «por cohorte» las líneas son otras
        # camadas y no hay nada que cubrir.
        "cubiertos": (sum(x["base"] for x in series)
                      if corte in ("universidad", "aparato") else base),
        "mediana": _median([float(n) for n in largos]),
        "p90": _p([float(n) for n in largos], 0.90),
        "peor_escalon": peor,
    }


# ── Entrada ──────────────────────────────────────────────────────────────────

def build(db: DBSession, week: date, weeks_shown: int = 4,
          corte: str = "total") -> dict:
    """Payload completo del panel del juego para la semana `week` (su lunes)."""
    data = load(db)
    weeks = _weeks_back(week, weeks_shown)
    return {
        "meta": {
            "week": week.isoformat(),
            "weeks": [w.isoformat() for w in weeks],
            "labels": [f"{w.strftime('%d/%m')}–{(w + timedelta(days=6)).strftime('%d/%m')}"
                       for w in weeks],
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "tz": "-03:00",
            "estudiantes": len(data["players"]),
            "respuestas": len(data["_answers"]),
            "bots_excluidos": data["_bots"],
        },
        "headline": headline(data, weeks),
        "funnel": funnel(data, week),
        "profundidad": profundidad(data, weeks, corte=corte),
    }
