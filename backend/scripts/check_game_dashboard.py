"""Verifica las métricas del panel del minijuego contra un escenario a mano.

Cada bloque de `metrics/game_queries.py` tiene una respuesta conocida sobre datos
sembrados acá, así que el resultado es determinístico. Nada de comparar contra
producción, que cambia sola.

Lo que más importa que quede clavado son las definiciones, porque son las que se
pueden aflojar sin que nadie se entere y convierten el panel en un generador de
números lindos:

  - los estudiantes sembrados (`is_bot`) NO cuentan en ninguna métrica;
  - una respuesta que no parsea no es una respuesta: no cuenta como intento ni
    baja el acierto;
  - la curva de profundidad se calcula solo sobre partidas CERRADAS;
  - el desglose reparte a la misma gente en montones: no cambia la base ni el
    largo de ninguna partida.

Uso:
    python backend/scripts/check_game_dashboard.py

Sale con código 1 si algo falla.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# La consola de Windows abre en cp1252 y este script imprime acentos; sin esto un
# check que falla muere con UnicodeEncodeError y tapa el error real.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BACKEND = Path(__file__).resolve().parent.parent
# NullPool abre una conexión por statement y ":memory:" perdería el esquema
# entre inserts (mismo motivo que en check_dashboard.py).
os.environ["DATABASE_URL"] = "sqlite:///" + str(
    Path(tempfile.mkdtemp()) / "gamepanel.db"
).replace("\\", "/")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND.parent))

import database  # noqa: E402
from models import (  # noqa: E402
    Base, Course, GameAttempt, GameBoost, GameCtaEvent, GameEvent, GameExercise,
    GamePlayer, User,
)

Base.metadata.create_all(database.engine)
S = database.SessionLocal

fallos: list[str] = []


def check(nombre: str, cond: bool, detalle: str = "") -> None:
    print(f"{'ok  ' if cond else 'FALLA'}  {nombre} {detalle}".rstrip())
    if not cond:
        fallos.append(nombre)


from metrics import game_queries as q  # noqa: E402
from metrics import game_render  # noqa: E402

# ── Escenario ────────────────────────────────────────────────────────────────
# La semana de referencia arranca el lunes 2026-08-17 (hora Argentina). Todo se
# escribe en UTC (las columnas son naive UTC) así que las horas van +3 respecto
# de la hora local que se quiere representar: 15:00 UTC = 12:00 en Argentina.
WEEK = datetime(2026, 8, 17).date()

# El escenario vive en una semana anterior al piso real del panel
# (game_queries.FIRST_WEEK = la semana de la difusión). Se baja el piso para
# este check: lo que se prueba acá son las definiciones de las métricas, no
# desde cuándo el panel decide mostrar semanas.
q.FIRST_WEEK = WEEK - timedelta(weeks=8)


def T(dia: int, hora: int = 15, minuto: int = 0) -> datetime:
    """Lunes de la semana + `dia`, a las `hora`:`minuto` UTC."""
    return datetime(2026, 8, 17, hora, minuto) + timedelta(days=dia)


# `now` fijo: la profundidad depende de qué partidas están cerradas, y con
# `utcnow()` el resultado cambiaría según cuándo se corra el check.
NOW = datetime(2026, 8, 24, 12, 0)

s = S()

s.add(Course(id=1, slug="analisis-1", name="Análisis 1"))

# Un usuario de Intervalo que se creó DESPUÉS de su estudiante (o sea: lo trajo el
# juego) y otro que ya existía antes.
s.add(User(id=1, clerk_user_id="u1", email="uno@x.com", name="Uno", created_at=T(0, 16)))
s.add(User(id=2, clerk_user_id="u2", email="dos@x.com", name="Dos",
           created_at=datetime(2026, 7, 1)))
s.flush()

# ── Estudiantes ──────────────────────────────────────────────────────────────
# p1  registrado, UBA, estudiante profundo (12 respuestas), partida CERRADA
# p2  invitado, UBA, 3 respuestas, partida CERRADA
# p3  invitado, UTN, 1 respuesta, partida ABIERTA (respondió hace 1 h)
# p4  registrado con cuenta vieja, UTN, 5 respuestas, cerrada
# p5  de CUATRO semanas antes y vuelve a jugar en esta: el único retenido, y el
#     único que ya existía cuando la semana empezó —o sea, el denominador de la
#     viralidad—. Cuatro semanas y no una para que quede afuera de la ventana
#     visible: acá se lo quiere solo como "el de antes", no como parte de las
#     cohortes que miden profundidad, difusión y aparato.
# p9  BOT: no tiene que aparecer en ninguna métrica
PLAYERS = [
    dict(id=1, user_id=1, alias="uno", university="UBA", career="E", is_bot=False,
         platform="desktop",
         created_at=T(0, 14), last_seen_at=T(0, 16)),
    dict(id=2, user_id=None, alias="dos", university="UBA", career="E", is_bot=False,
         platform="android", referred_by=5,
         created_at=T(1, 14), last_seen_at=T(1, 15)),
    dict(id=3, user_id=None, alias="tres", university="UTN", career="S", is_bot=False,
         platform="ios",
         created_at=T(6, 14), last_seen_at=NOW - timedelta(hours=1)),
    dict(id=4, user_id=2, alias="cuatro", university="UTN", career="E", is_bot=False,
         platform="android",
         created_at=T(2, 14), last_seen_at=T(2, 15)),
    dict(id=5, user_id=None, alias="cero", university=None, career=None,
         is_bot=False, platform=None,
         created_at=T(-28, 14), last_seen_at=T(0, 15)),
    dict(id=9, user_id=None, alias="bot", university="UBA", career="E", is_bot=True,
         platform="desktop",
         created_at=T(0, 10), last_seen_at=T(0, 11)),
]
for p in PLAYERS:
    s.add(GamePlayer(theta=0.5, n_updates=5, xp=100, unlocked_keys="pow,sq", **p))

s.flush()

# ── Ejercicios y respuestas ──────────────────────────────────────────────────
# Un helper que sirve un ejercicio y lo responde, para no repetir veinte líneas.
_ex_id = [0]


# Por defecto el ejercicio se sirve en el aparato de primer contacto del
# estudiante; se pasa explícito solo para el caso que importa, que es el de alguien
# que cambia de dispositivo a mitad de partida.
_PLAT = {p["id"]: p["platform"] for p in PLAYERS}


def servir(player_id: int, cuando: datetime, p_hat: float, template="t1_pow",
           status="answered", peeked=False, platform=None) -> int:
    _ex_id[0] += 1
    s.add(GameExercise(
        id=_ex_id[0], player_id=player_id, template_key=template,
        prompt_latex="x", expected_derivative="1",
        theta_at_serve=0.5, beta_at_serve=-1.0, p_hat=p_hat,
        status=status, peeked=peeked, platform=platform or _PLAT[player_id],
        created_at=cuando, answered_at=cuando if status != "served" else None))
    return _ex_id[0]


_at_id = [0]


def responder(ex: int, player_id: int, cuando: datetime, correcto: bool,
              intento=1, parse_ok=True, ms=8000) -> None:
    _at_id[0] += 1
    s.add(GameAttempt(
        id=_at_id[0], exercise_id=ex, player_id=player_id,
        attempt_number=intento if parse_ok else intento - 1,
        parse_ok=parse_ok, is_correct=correcto, response_ms=ms, xp_awarded=25 if correcto else 0,
        theta_before=0.4 if (parse_ok and intento == 1) else None,
        theta_after=0.5 if (parse_ok and intento == 1) else None,
        created_at=cuando))


# p1: 12 respuestas el día 0, todas correctas menos una. Dos sesiones: las
# primeras 10 seguidas, y las últimas 2 más de 30 minutos después.
for i in range(10):
    ex = servir(1, T(0, 14, i), 0.75)
    responder(ex, 1, T(0, 14, i), correcto=(i != 3))
for i in range(2):
    ex = servir(1, T(0, 16, i), 0.75)
    responder(ex, 1, T(0, 16, i), correcto=True)

# p1 además miró la tabla en un ejercicio y acertó: NO tiene que contar en P1.
ex_peek = servir(1, T(0, 16, 30), 0.30, peeked=True)
responder(ex_peek, 1, T(0, 16, 30), correcto=True)

# p1 también escribió algo que el parser no entendió, y después acertó bien.
ex_parse = servir(1, T(0, 16, 40), 0.75)
responder(ex_parse, 1, T(0, 16, 40), correcto=False, parse_ok=False)
responder(ex_parse, 1, T(0, 16, 41), correcto=True)

# p2: 3 respuestas el día 1, dos correctas. Vuelve el día 3 con una más.
for i in range(3):
    ex = servir(2, T(1, 14, i), 0.60)
    responder(ex, 2, T(1, 14, i), correcto=(i != 2))
# La vuelta de p2 es desde la compu: el mismo estudiante en dos aparatos.
ex = servir(2, T(3, 14), 0.60, platform="desktop")
responder(ex, 2, T(3, 14), correcto=True)

# p3: una sola respuesta, y sigue jugando (partida abierta).
ex = servir(3, NOW - timedelta(hours=1), 0.85)
responder(ex, 3, NOW - timedelta(hours=1), correcto=True)

# p4: 5 respuestas, todas correctas, y un salteo de una difícil.
for i in range(5):
    ex = servir(4, T(2, 14, i), 0.72)
    responder(ex, 4, T(2, 14, i), correcto=True)
servir(4, T(2, 14, 30), 0.35, template="t5_ln_over_x", status="skipped")

# p5 vuelve: una respuesta en la semana de referencia, cuatro semanas después
# de su alta. Es lo único que lo hace contar como retenido.
ex = servir(5, T(0, 15), 0.70)
responder(ex, 5, T(0, 15), correcto=True)

# Bot: 50 respuestas que NO tienen que aparecer en ningún lado.
for i in range(50):
    ex = servir(9, T(0, 12, i % 60), 0.90)
    responder(ex, 9, T(0, 12, i % 60), correcto=True)

# ── Cafecito ─────────────────────────────────────────────────────────────────
# 4 impresiones sobre 2 personas, 1 click de 1 persona → CTR por persona = 50%.
for pid, cuando, trig in [(1, T(0, 15), "milestone"), (1, T(0, 15, 30), "milestone"),
                          (2, T(1, 15), "record"), (2, T(1, 15, 5), "record")]:
    s.add(GameCtaEvent(player_id=pid, cta="cafecito", action="impression",
                       placement=trig, solved=10, university="UBA", created_at=cuando))
s.add(GameCtaEvent(player_id=1, cta="cafecito", action="click", placement="milestone",
                   solved=10, university="UBA", created_at=T(0, 15, 1)))
s.add(GameCtaEvent(player_id=1, cta="share", action="impression", created_at=T(0, 15)))
s.add(GameCtaEvent(player_id=1, cta="share", action="click", created_at=T(0, 15, 2)))
# Un CTA del bot, que tampoco puede contar.
s.add(GameCtaEvent(player_id=9, cta="cafecito", action="click", created_at=T(0, 15)))

# Dos empujes con el MISMO tamaño y distinto origen: uno donado de verdad y uno
# que insertamos nosotros para probar. Es el par que fija la definición — el
# titular de ingresos tiene que contar el primero y no el segundo.
s.add(GameBoost(university="UBA", cafecitos=3, donor_name="Nico", source="cafecito",
                created_at=T(0, 14), expires_at=T(0, 14, 30)))
s.add(GameBoost(university="UTN", cafecitos=3, donor_name=None, source="manual",
                created_at=T(0, 18), expires_at=T(0, 18, 30)))
s.add(GameEvent(kind="boost", text="alguien invitó un cafecito", emoji="☕",
                university="UBA", created_at=T(0, 14)))
s.add(GameEvent(kind="climb", text="subió 4 puestos", emoji="🚀", created_at=T(0, 15)))

s.commit()

data = q.load(s)
weeks = q._weeks_back(WEEK, 4)

# ── 1 · Los bots no existen ──────────────────────────────────────────────────
print("\n— bots —")
check("los estudiantes sembrados se excluyen", len(data["players"]) == 5,
      f'({len(data["players"])} estudiantes)')
check("sus ejercicios también", all(e["player_id"] != 9 for e in data["exercises"]))
check("sus respuestas también", all(a["player_id"] != 9 for a in data["attempts"]))
check("sus CTA también", all(c["player_id"] != 9 for c in data["cta"]))
check("se informa cuántos se sacaron", data["_bots"] == 1)

# ── 2 · Qué es una respuesta ─────────────────────────────────────────────────
print("\n— respuestas —")
# p1: 12 + 1 mirada + 1 buena tras el fallo de parseo = 14 primeros intentos.
# p2: 4. p3: 1. p4: 5. p5: 1. Total 25 respuestas parseadas.
check("lo que no parsea no es respuesta", len(data["_answers"]) == 25,
      f'({len(data["_answers"])})')
check("el fallo de parseo sí queda registrado",
      sum(1 for a in data["attempts"] if not a["parse_ok"]) == 1)
# Los primeros intentos son la unidad de la curva de profundidad: el largo de
# una partida es cuántas derivadas DISTINTAS enfrentó, no cuántas veces tipeó.
# En este escenario nadie usó el segundo intento, así que coinciden — lo que se
# clava acá es que el fallo de parseo, que sí ocurrió, no cuenta como ninguno.
check("el largo de la partida se mide en primeros intentos",
      len(data["_firsts"]) == 25 and len(data["_firsts"]) == len(data["_answers"]),
      f'({len(data["_firsts"])} primeros intentos sobre {len(data["_answers"])} respuestas)')

# ── 3 · Titulares ──────────────────────────────────────────────────────────
print()
print("— titulares —")
h = {c["label"]: c for c in q.headline(data, weeks)}
# Los cuatro estudiantes de la semana, más nadie: el bot no cuenta y p3 respondió
# recién el lunes siguiente, así que su respuesta cae en la semana de al lado.
check("usuarios nuevos de la semana", h["Usuarios nuevos"]["value"] == 4,
      f'({h["Usuarios nuevos"]["value"]})')
# "Ingresó" se arma con toda huella fechada, no solo con el alta: los cuatro
# altas de la semana más p0, que es de antes y volvió a jugar.
check("los ingresos suman a los que ya existían y volvieron",
      h["Ingresos"]["value"] == 5, f'({h["Ingresos"]["value"]})')
check("y son personas distintas, no visitas",
      h["Ingresos"]["value"] >= h["Usuarios nuevos"]["value"])
check("se registran", h["Se registran"]["value"] == 50.0,
      f'({h["Se registran"]["value"]}%)')
# p0 es de una semana anterior y jugó en esta; los cuatro nuevos no cuentan acá
# por más que hayan jugado, porque su alta es de esta misma semana.
check("los retenidos son de otra semana", h["Usuarios retenidos"]["value"] == 1,
      f'({h["Usuarios retenidos"]["value"]})')
check("el titular de cafecitos no cuenta los grants a mano",
      h["Cafecitos"]["value"] == 3, f'({h["Cafecitos"]["value"]}, no 6)')
check("los reclutas son los que entraron por el link de otro",
      h["Reclutas"]["value"] == 1, f'({h["Reclutas"]["value"]})')
# Un recluta sobre el único jugador que existía antes de la semana.
check("la viralidad se divide por los que ya estaban",
      h["Coeficiente de viralidad"]["value"] == 1.0,
      f'({h["Coeficiente de viralidad"]["value"]})')
check("y se muestra con dos decimales", h["Coeficiente de viralidad"]["dec"] == 2)
# La primera tanda de p1 son las 9 correctas del bloque del día 0 (la décima cae
# más de media hora después); p2 3, p4 5. p3 no respondió en esta semana.
# p1 respondió 12 veces el día 0, pero las últimas dos son dos horas después:
# su primera tanda son las diez seguidas, con nueve aciertos. Si el corte no
# existiera daría 12 y este número mediría "cuánto jugó en total el día que
# entró", que es otra cosa.
check("la primera sesión corta en el primer hueco de media hora",
      q._correctas_de_la_primera_sesion(
          [a for a in data["_answers"] if a["player_id"] == 1]) == 9)
# Primeras tandas: p1 9, p2 2, p4 5, p3 1 (su tanda cae recién el lunes
# siguiente, pero el alta es de esta semana y la cohorte es por alta).
check("y la mediana es sobre los nuevos que llegaron a responder",
      h["Primera sesión"]["value"] == 3.5, f'({h["Primera sesión"]["value"]})')

# ── 4 · Embudo ───────────────────────────────────────────────────────────────
print("\n— embudo —")
f = q.funnel(data, WEEK)
pasos = {p["label"]: p["n"] for p in f["steps"]}
check("base = estudiantes de la cohorte", f["base"] == 4)
check("todos vieron una derivada", pasos["Vio una derivada"] == 4)
check("todos respondieron", pasos["Respondió"] == 4)
# El paso lleva el número del hito de producto, no un redondo: se pide carrera
# y universidad en la tercera, y el paso de al lado —«cargó universidad»— se lee
# contra los que llegaron a que se lo preguntaran.
check("el paso anterior a la universidad es el hito real",
      pasos[f"Llegó a {q.PEDIDO_PERFIL}"] == 3,
      f'({pasos[f"Llegó a {q.PEDIDO_PERFIL}"]})')
check("llegó a 10", pasos["Llegó a 10"] == 1)
check("volvió otro día", pasos["Volvió otro día"] == 1, f'({pasos["Volvió otro día"]})')
# Los pasos que no están anidados no pueden mostrar «% del paso anterior»: era
# de donde salía el «600% del paso anterior» que no quiere decir nada.
por_label = {p["label"]: p for p in f["steps"]}
check("los pasos anidados se leen contra el anterior",
      por_label[f"Llegó a {q.PEDIDO_PERFIL}"]["pct_prev"] is not None)
check("los que no lo están, no", por_label["Cargó universidad"]["pct_prev"] is None
      and por_label["Se registró"]["pct_prev"] is None
      and por_label["Volvió otro día"]["pct_prev"] is None)
check("y la cadena no se corta por ellos: «llegó a 25» sigue midiendo contra «llegó a 10»",
      por_label["Llegó a 25"]["pct_prev"] == 0.0)

# ── 5 · Profundidad ──────────────────────────────────────────────────────────
print("\n— profundidad —")
pr = q.profundidad(data, weeks, now=NOW)
# p3 está jugando ahora mismo: su partida NO entra.
check("las partidas abiertas no entran en la curva", pr["base"] == 3, f'(base {pr["base"]})')
check("y se informa cuántas quedaron afuera", pr["abiertos"] == 1)
curva = {c["k"]: c for c in pr["curva"]}
check("S(1) = 100%", curva[1]["pct"] == 100.0)
# Largos de las partidas cerradas: p1 = 14 (12 + la mirada + la de después del
# fallo de parseo), p2 = 4, p4 = 5.
check("S(4) los tiene a los tres", curva[4]["vivos"] == 3, f'({curva[4]["vivos"]})')
check("S(5) deja afuera a p2", curva[5]["vivos"] == 2, f'({curva[5]["vivos"]})')
check("S(6) deja solo a p1", curva[6]["vivos"] == 1)
# p1 tiene 14 primeros intentos parseados.
check("S(15) es cero", curva[15]["vivos"] == 0)
check("mediana de derivadas", pr["mediana"] == 5.0, f'({pr["mediana"]})')

# ── 6 · El desglose de la profundidad ──────────────────────────────────────
print()
print("— desglose —")
# El corte reparte a la MISMA gente en montones: no cambia quién entra ni cuánto
# aguantó cada uno. Si eso se rompiera, dos cortes contarían poblaciones
# distintas y compararlos no querría decir nada.
for c in q.CORTES:
    d = q.profundidad(data, weeks, now=NOW, corte=c)
    check(f"el corte «{c}» no cambia la base", d["base"] == pr["base"],
          f'({d["base"]} vs {pr["base"]})')
    check(f"ni el escalón del titular en «{c}»", d["peor_escalon"] == pr["peor_escalon"])

# Cinco partidas cerradas es el piso para tener línea propia. En el escenario
# solo p1, p2 y p4 están cerrados —tres— así que ninguna universidad ni ningún
# aparato llega, y el desglose queda deliberadamente vacío en vez de dibujar
# tres curvas de una persona.
uni = q.profundidad(data, weeks, now=NOW, corte="universidad")
check("un grupo con menos de cinco partidas no dibuja línea",
      uni["series"] == [], f'({[x["label"] for x in uni["series"]]})')
check("y se informa que el desglose no cubre nada", uni["cubiertos"] == 0)

# Con el piso bajado a uno aparecen, y las líneas suman exactamente el total:
# es la propiedad que hace que el desglose sea un reparto y no otro recorte.
q.MIN_BASE_SERIE = 1
try:
    uni = q.profundidad(data, weeks, now=NOW, corte="universidad")
    apa = q.profundidad(data, weeks, now=NOW, corte="aparato")
    coh = q.profundidad(data, weeks, now=NOW, corte="cohorte")
    check("por universidad salen UBA y UTN",
          {x["label"] for x in uni["series"]} == {"UBA", "UTN"},
          f'({[x["label"] for x in uni["series"]]})')
    check("cada línea lleva su sigla para poder pintarla con su color",
          all(x["clave"] for x in uni["series"]))
    check("las líneas del aparato suman el total",
          sum(x["base"] for x in apa["series"]) == apa["base"],
          f'({sum(x["base"] for x in apa["series"])} de {apa["base"]})')
    check("y van en el orden de la plataforma, no por tamaño",
          [x["label"] for x in apa["series"]] == ["Android", "Escritorio"],
          f'({[x["label"] for x in apa["series"]]})')
    check("las cohortes van de la más vieja a la más nueva",
          [x["label"] for x in coh["series"]]
          == sorted(x["label"] for x in coh["series"]))
    check("y no son más de tres", len(coh["series"]) <= q.MAX_COHORTES)
finally:
    q.MIN_BASE_SERIE = 5

check("un corte que no existe cae en el total",
      q.profundidad(data, weeks, now=NOW, corte="inventado")["corte"] == "total")

# La cohorte es la de la SEMANA ELEGIDA y no la ventana visible entera. Sin el
# corte por arriba, pedir una semana vieja devolvía una curva con gente que esa
# semana todavía no existía: p5 es de cuatro semanas antes y su cohorte es él
# solo, aunque después hayan entrado cuatro más.
vieja = q._weeks_back(WEEK - timedelta(weeks=4), 4)
pv = q.profundidad(data, vieja, now=NOW)
check("la cohorte es la de la semana elegida, no la ventana",
      pv["base"] == 1, f'({pv["base"]}, y la de {WEEK} tiene {pr["base"]})')
check("y no arrastra las altas posteriores",
      pv["base"] + pr["base"] == 4, f'({pv["base"]} + {pr["base"]})')

# «Por cohorte» sí trae otras camadas: la elegida y las dos anteriores. Es el
# único corte que NO parte la cohorte, y por eso sus líneas no suman el total.
q.MIN_BASE_SERIE = 1
try:
    tres = q.profundidad(data, q._weeks_back(WEEK - timedelta(weeks=2), 4),
                         now=NOW, corte="cohorte")
    check("por cohorte alcanza dos semanas para atrás",
          {x["clave"] for x in tres["series"]}
          == {(WEEK - timedelta(weeks=4)).isoformat()},
          f'({[x["clave"] for x in tres["series"]]})')
    lejos = q.profundidad(data, q._weeks_back(WEEK - timedelta(weeks=1), 4),
                          now=NOW, corte="cohorte")
    check("y una camada de tres semanas atrás ya no entra",
          all(x["clave"] != (WEEK - timedelta(weeks=4)).isoformat()
              for x in lejos["series"]),
          f'({[x["clave"] for x in lejos["series"]]})')
finally:
    q.MIN_BASE_SERIE = 5

# ── 7 · La página se arma ───────────────────────────────────────────────────
print("\n— render —")
payload = q.build(s, WEEK)
html = game_render.page(payload, token="tok")
check("la página se arma entera", len(html) > 10000, f"({len(html)} bytes)")
check("no quedó ningún None crudo en el HTML", "None" not in html)
check("lleva el papel cuadriculado del juego", "background-size:40px 40px" in html)
check("y el borde de las cajas del juego", "#38385a" in html)
check("enlaza el panel de Intervalo", "/panel/tok</a>" in html or "/panel/tok'" in html)
check("el data.json queda linkeado", "/panel/tok/dx/data.json" in html)

# Una semana sin nada tiene que armarse igual y no romperse por dividir por cero.
vacio = q.build(s, WEEK + timedelta(weeks=8))
html2 = game_render.page(vacio, token="tok")
check("una semana vacía no rompe el panel", len(html2) > 5000)

# Cada pestaña se arma sola y trae SU sección y ninguna otra: es lo que hace que
# el panel deje de ser un scroll.
titulos = {"titulares": "Titulares", "embudo": "Embudo de la partida",
           "profundidad": "Profundidad"}
for clave, _ in game_render.SECCIONES:
    h = game_render.page(q.build(s, WEEK), token="tok", seccion=clave)
    otros = [t for k, t in titulos.items() if k != clave]
    check(f"la pestaña «{clave}» trae su sección",
          f'>{titulos[clave]}</h2>' in h or titulos[clave] in h)
    check(f"y ninguna otra en «{clave}»",
          not any(f'<h2><b>' in h and t in h.split('<h2>')[-1] for t in otros)
          or h.count("<h2>") == 1,
          f'({h.count(chr(60) + "h2>")} secciones)')
    check(f"la pestaña «{clave}» queda marcada en la barra",
          f'<span class="cur">{titulos[clave].split()[0]}</span>' in h)

# Una pestaña inventada cae en la primera en vez de dar una página vacía.
h = game_render.page(q.build(s, WEEK), token="tok", seccion="inventada")
check("una pestaña que no existe cae en la primera", h.count("<h2>") == 1
      and "Titulares" in h)

# Los cuatro cortes tienen que armar la pestaña de profundidad, incluido el que
# se queda sin series: ahí el gráfico no se dibuja y la caja se cae si nadie lo
# previó.
for c in q.CORTES:
    h = game_render.page(q.build(s, WEEK, corte=c), token="tok", seccion="profundidad")
    check(f"la página se arma con el corte «{c}»", len(h) > 8000, f"({len(h)} bytes)")
    # El corte activo se dibuja como texto marcado y no como link: los otros
    # tres siguen siendo links, y el activo no puede llevar a sí mismo.
    activo = {"total": "Todos", "cohorte": "Por cohorte",
              "universidad": "Por universidad", "aparato": "Por aparato"}[c]
    check(f"y el corte «{c}» queda marcado en la barra",
          f'<span class="cur">{activo}</span>' in h)
    # Las tres barras conviven: cambiar de semana o de pestaña no puede perder
    # el desglose elegido, y elegir desglose no puede devolver a la primera
    # pestaña. Se verifica sobre los links, que es donde viaja el estado.
    if c != "total":
        check(f"y el corte «{c}» viaja en los links de semana y pestaña",
              h.count(f"&corte={c}") >= 3, f'({h.count(f"&corte={c}")} links)')
    check(f"los links de la barra de cortes conservan la pestaña con «{c}»",
          h.count("s=profundidad") >= 3, f'({h.count("s=profundidad")} links)')

s.close()

print()
if fallos:
    print(f"FALLARON {len(fallos)}: " + ", ".join(fallos))
    sys.exit(1)
print("todo ok")
