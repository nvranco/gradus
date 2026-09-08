"""Armado del HTML del panel.

Una sola página, oscura, sin JS. El orden de las secciones es el mismo del
reporte semanal (docs/reports/FORMATO.md) a propósito: la idea es que armar el
reporte del domingo sea leer el panel de arriba a abajo, no rearmar el hilo.

La numeración de secciones no es decorativa — mapea 1:1 contra las del PDF.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import charts as ch
from . import theme
from .charts import esc, num
from .queries import FIRST_WEEK

# El grueso del CSS es compartido con el panel de Derivemos — ver
# metrics/theme.py, que es la piel adoptada. Acá solo queda lo que no tiene
# sentido fuera de Intervalo: la tabla de cohorte con heatmap y las celdas de
# copy de push largo.
CSS = theme.BASE_CSS + """
/* Tablas de cohorte: más aire y números más grandes, porque son el corte que
   más se mira y ocupan el ancho completo. */
table.big{font-size:14px}
table.big th,table.big td{padding:11px 10px}
table.big td{border-radius:4px}
table.big td.ent{font-size:14px;color:var(--fg)}
.emo{margin-right:7px;font-size:15px}

/* Descripción y ejemplo del copy dentro de la celda: la fila necesita respirar
   en varias líneas, así que acá sí se permite el salto. */
td .sub2{color:var(--muted);font-size:11.5px;font-weight:400}
td .ej{color:var(--indigo-soft);font-size:11.5px;font-style:italic}
td:has(.ej){white-space:normal;max-width:420px;line-height:1.45;padding:10px 7px}

/* Toggles de cohorte del gráfico de retención: botones de checkbox, sin JS.
   Cada <input> vive escondido y su <label> hace de botón — el prender/apagar
   real de las líneas del SVG lo hacen las reglas :has() que arma render.py
   junto al gráfico, indexadas por cohorte. */
.chart-head{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:baseline;justify-content:space-between}
.chart-head h3{margin:0}
.ret-toggles{display:flex;flex-wrap:wrap;gap:6px}
.ret-toggles input{position:absolute;opacity:0;pointer-events:none}
.rt-btn{cursor:pointer;user-select:none;font-size:11.5px;font-weight:650;
  padding:3px 10px;border-radius:999px;border:1px solid var(--border);color:var(--muted)}
.rt-chk:checked + .rt-btn{background:var(--indigo);border-color:var(--indigo);color:#fff}
.rt-chk.rt-combine:checked + .rt-btn-combine{background:var(--violet);border-color:var(--violet)}
"""

# Helpers de presentación compartidos con el panel de Derivemos — ver
# metrics/theme.py. Los alias locales evitan reescribir las llamadas ya
# existentes en este archivo.
_delta_chip = theme.delta_chip
_kpi = theme.kpi
_table = theme.table


# Colores de marca de las universidades, espejo de UNIVERSITY_TAGS del front
# (web/src/lib/university-tags.ts). Es la TERCERA copia de esta lista — el
# docstring de backend/universities.py ya advierte de las otras dos. Acá van
# solo las que tienen tag propia; el resto cae en el chip gris genérico, igual
# que hace <UniTag/>.
UNIVERSITY_COLOR = {
    "UBA": "#4F76E0", "UTN": "#EC4869", "UNSAM": "#4D90F2", "UNLP": "#21B8AE",
    "UNC": "#4A63D6", "UNR": "#D742A0", "UNL": "#29CBD9", "UNT": "#9AA7B8",
    "UNS": "#2E8FE0", "UADE": "#E3A73C", "ITBA": "#2C7DBE", "UNLaM": "#3FAE5C",
}

# Los mismos emojis que ve el usuario, para que el panel y la app nombren las
# cosas igual: carreras de onboarding-wizard.tsx (CAREERS) y cursos de COURSES.
CAREER_LABEL = {
    "E": ("⚙️", "Ingeniería"), "S": ("🔬", "Ciencia"),
    "T": ("🤖", "Tecnología"), "M": ("📐", "Matemática"),
    "Otra": ("✦", "Otra"),
}
COURSE_LABEL = {
    "analisis": ("📈", "Análisis"), "algebra": ("🧮", "Álgebra"),
    "probabilidad": ("🎲", "Probabilidad"),
}

# Los mismos emojis que el usuario toca al responder la micro-encuesta
# (web/.../survey-pane.tsx, SURVEY_QUESTIONS). Repetirlos acá hace que el
# gráfico se lea sin traducir: la barra 🥱 es la misma cara que vio en la app.
SURVEY_EMOJI = {
    "aburrido": "🥱", "justo": "🙂", "interesante": "💡",
    "muy_facil": "😴", "muy_dificil": "🤯",
}
SURVEY_TEXT = {
    "aburrido": "Aburrido", "justo": "Justo", "interesante": "Interesante",
    "muy_facil": "Muy fácil", "muy_dificil": "Muy difícil",
}
D_ORDER = ["aburrido", "justo", "interesante"]
A_ORDER = ["muy_facil", "justo", "muy_dificil"]

# `justo` aparece en los dos canales con distinto significado, así que el emoji
# del medio se resuelve por canal: 🙂 en D (ni aburrido ni interesante) y 👌 en A
# (la dificultad estuvo bien). Ver el comentario de models.ExerciseFeedback.
SURVEY_EMOJI_A = dict(SURVEY_EMOJI, justo="👌")

# Qué dice cada copy de push. Los nombres de categoría son internos
# ("personal_best", "podium") y no significan nada de un vistazo, así que la
# tabla muestra para qué sirve cada uno y un ejemplo real del texto.
#
# Los ejemplos están copiados de backend/notification_copy.py; las llaves marcan
# lo que se rellena por usuario. El peso nominal es CATEGORY_WEIGHTS del mismo
# archivo, para poder contrastar la mezcla real contra la esperada.
PUSH_COPY = {
    "practice": ("Recordatorio de repasar, sin gancho particular",
                 "¡Vení a repasar! Tus ejercicios te esperan 🦾", 15),
    "university": ("Cuánto XP le aportó a su universidad",
                   "Sumaste {xp} XP para la {uni} esta semana ¿Seguimos? 🎓", 20),
    "social": ("Cuántos compañeros de su universidad ya repasaron hoy",
               "{n} compañeros de la {uni} ya repasaron hoy. ¿Vos? 🎓", 15),
    "ranking": ("Alguien lo pasó en el ranking",
                "Alguien te pasó en el ranking. ¿Lo dejás así? 🤼", 15),
    "podium": ("Qué tan cerca está de entrar al podio",
               "Estás a {xp} XP del top {n} del ranking. ¡Dale que se puede! 🏅", 15),
    "reactivation": ("Hace días que no entra",
                     "Hace {n} días que no practicás. ¿Volvemos? 👀", 10),
    "personal_best": ("Su récord de ejercicios en un día",
                      "Tu mejor racha de ejercicios en un día fue {n}. ¿La superás hoy? 🚀", 10),
}

# Avisos de EVENTO (notification_copy.EVENT_VARIANTS): no entran a
# CATEGORY_WEIGHTS ni a la rotación de arriba — salen porque pasó algo (alguien
# donó un cafecito, un recluta estudió), no por sorteo, así que no tienen un
# peso nominal que contrastar. Tabla separada de PUSH_COPY por eso, y no una
# fila más ahí con un peso inventado.
EVENT_PUSH_COPY = {
    "recruit": ("Un recluta suyo estudió y le generó XP",
                "@fulano entró por tu link y te sumó 8 XP hoy 🪖"),
    "cafecito": ("Alguien invitó un cafecito para su universidad",
                 "Alguien de la UBA invitó un cafecito. Tenés ×1,5 por 24 h ☕"),
}


def curso_label(slug: str) -> str:
    emo, name = COURSE_LABEL.get(slug, ("", slug))
    return f"{emo} {name}".strip()


def _uni_chip(sigla: str) -> str:
    """El chip de universidad del ranking: color de marca sobre su propio fondo
    translúcido, o gris si la universidad entró por «Otra»."""
    color = UNIVERSITY_COLOR.get(sigla)
    if not color:
        return (f'<span class="tag tag-plain">{esc(sigla)}</span>')
    return (f'<span class="tag" style="color:{color};border-color:{color}99;'
            f'background:{color}33">{esc(sigla)}</span>')


def _emoji_label(pair: tuple[str, str] | None, fallback: str) -> str:
    if not pair:
        return esc(fallback)
    return f'<span class="emo">{pair[0]}</span>{esc(pair[1])}'


# Escala de calor para las celdas de porcentaje. La intensidad es relativa a la
# COLUMNA, no absoluta: lo que interesa es cuál origen rinde mejor que cuál, y
# una escala fija de 0 a 100 dejaría todas las celdas casi iguales cuando los
# valores viven apretados en una banda angosta.
def _heat_cell(v, lo: float, hi: float, dim: bool) -> str:
    if v is None:
        return '<td class="dim">—</td>'
    if dim:
        # Denominador chico: el porcentaje es ruido y pintarlo lo haría pasar
        # por señal. Se muestra el número, sin color.
        return f'<td class="dim">{num(v, "%")}</td>'
    t = 0.0 if hi <= lo else (v - lo) / (hi - lo)
    alpha = round(0.10 + 0.42 * t, 3)
    return (f'<td style="background:rgba(126,128,247,{alpha});'
            f'border-radius:4px">{num(v, "%")}</td>')


# Por debajo de esta base, una tasa de vuelta no es señal. Las filas con menos
# quedan sin color y con el número atenuado.
HEAT_MIN_BASE = 8


def _cohort_table(rows: list[dict], head: str, kind: str) -> str:
    """Tabla de cohorte a ancho completo, con chips/emojis y calor por columna."""
    if not rows:
        return '<p class="empty">todavía no hay usuarios con este dato</p>'

    def label(r: dict) -> str:
        if kind == "uni":
            return _uni_chip(r["label"])
        if kind == "carrera":
            return _emoji_label(CAREER_LABEL.get(r["label"]), r["label"])
        if kind == "curso":
            return _emoji_label(COURSE_LABEL.get(r["label"]), r["label"])
        return esc(r["label"])

    cols = ["estudio", "volvio", "dos_dias"]
    rango = {}
    for c in cols:
        vals = [r[c] for r in rows if r[c] is not None and r["base"] >= HEAT_MIN_BASE]
        rango[c] = (min(vals), max(vals)) if vals else (0.0, 0.0)

    body = []
    for r in rows:
        dim = r["base"] < HEAT_MIN_BASE
        celdas = "".join(_heat_cell(r[c], *rango[c], dim=dim) for c in cols)
        body.append(
            f'<tr><td class="ent">{label(r)}</td>'
            f'<td>{r["n"]}</td><td>{r["base"]}</td>{celdas}</tr>')

    head_html = "".join(f"<th>{esc(c)}</th>" for c in
                        [head, "n", "base", "estudió", "volvió", "2+ días"])
    nota = ("" if all(r["base"] >= HEAT_MIN_BASE for r in rows) else
            f'<p class="note">Las filas atenuadas tienen menos de {HEAT_MIN_BASE} '
            f'personas en la base: el porcentaje es ruido, no señal.</p>')
    return (f'<div class="scroll"><table class="big"><thead><tr>{head_html}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>{nota}')


_COHORT_NOTE = (
    '<p class="note">«estudió» es sobre el total del corte —conversión—; '
    '<b>«volvió» y «2+ días» son sobre la base</b>, los que llegaron a terminar '
    'una sesión. El usuario a retener es el que ya usó el producto: medir la '
    'vuelta sobre el total mezcla dos problemas distintos y no deja distinguir '
    'un origen que trae gente que no arranca de uno que trae gente que arranca '
    'y no vuelve.</p>')


def _flojo(pt: dict, base: int) -> bool:
    """Un punto de la curva es «flojo» cuando menos de la mitad de la cohorte
    llegó a vivir ese día.

    La curva se apoya en `obs`, que se achica con k porque dentro de una misma
    semana la gente se activa en días distintos. Eso está bien —lo contrario
    contaría como «no volvió» a quien todavía no llegó a esa fecha— pero deja
    una cola sostenida por poquísima gente: en la cohorte del 10/08 el D+13 son
    4 personas de 23, y dibujado con el mismo trazo firme que el D+1 se lee
    como un derrumbe en vez de como ruido.

    La mitad y no un mínimo absoluto para que el umbral escale con el tamaño de
    la cohorte: 12 personas son cola en una tanda de 95 y son casi todo en una
    de 23."""
    return pt["pct"] is not None and pt["obs"] * 2 < base


_section = theme.section


# ── Página ───────────────────────────────────────────────────────────────────

def page(p: dict, *, token: str) -> str:
    m = p["meta"]
    week = date.fromisoformat(m["week"])
    labels = m["labels"]

    # Navegación de semanas: la actual y las cuatro anteriores, sin bajar de
    # FIRST_WEEK (ver su comentario: antes de eso las cohortes son de 1 y 2
    # personas). No hay "siguiente" más allá de hoy — una semana futura solo
    # puede mostrar ceros y se lee como una caída.
    today_week = week_of_today()
    nav = []
    for i in range(4, -1, -1):
        w = today_week - timedelta(weeks=i)
        if w < FIRST_WEEK:
            continue
        lab = f"{w.strftime('%d/%m')}"
        if w == week:
            nav.append(f'<span class="cur">{lab}</span>')
        else:
            nav.append(f'<a href="/panel/{esc(token)}?w={w.isoformat()}">{lab}</a>')

    jump = "".join(
        f'<a href="#{a}">{esc(t)}</a>'
        for a, t in [("embudo", "Embudo"), ("cohortes", "Cohortes"), ("producto", "Producto"),
                     ("encuestas", "Encuestas"), ("push", "Push"),
                     ("mails", "Mails"), ("cafecito", "Cafecito"), ("reclutas", "Reclutas")])

    out = [
        "<div class='wrap'>",
        "<header class='top'>",
        "<div class='box'><div class='brand'>intervalo</div>",
        f"<div class='weeknav'>{''.join(nav)}</div></div>",
        f"<div class='box'><span class='sub'>{m['usuarios']} usuarios en la base</span>"
        # El minijuego tiene su propio panel: mismo token, otro vocabulario.
        # Se enlaza desde acá para que no haya que acordarse de la URL.
        f"<a href='/panel/{esc(token)}/dx'>dx →</a></div>",
        "</header>",
        f"<nav class='jump'>{jump}</nav>",
    ]

    # 0 · Titulares
    out.append(_section(
        0, f"Titulares · semana del {labels[-1]}",
        f'<div class="grid g4">{"".join(_kpi(c) for c in p["headline"])}</div>',
        sub="Cada tarjeta es sobre la <b>cohorte de alta de esa semana</b>, seguida hasta hoy. "
            "El sparkline son las tres semanas visibles."))

    # 1 · Embudo
    f = p["funnel"]
    rows = [{"label": s["label"], "value": s["n"],
             "note": "" if s["pct_prev"] is None else f'{num(s["pct_prev"], "%")} del paso anterior'}
            for s in f["steps"]]
    out.append(_section(
        1, "Embudo de la cohorte", ch.hbars(rows, colors=["var(--indigo)"]) +
        '<p class="note"><b>Altas</b> son las cuentas que vio esta base. Las crea Clerk, y el '
        'escalón Clerk → backend (273 → 233 en la semana del 18/08) no es medible desde acá, así '
        'que el embudo arranca un paso más adelante.</p>'
        '<p class="note"><b>Llegó al home</b> es el escalón que separa dos fallas distintas: '
        'trabarse en la autenticación, y llegar a la app y no tocar «empezar». Lo marca el '
        'endpoint que el home llama en cada carga. Para las cohortes anteriores al 24/08 es una '
        '<b>cota inferior</b>: se reconstruyó de quien tiene sesiones o zona horaria guardada, y '
        'el resto no dejó rastro.</p>'
        '<p class="note"><b>Instaló y abrió la PWA</b> va después de terminar una sesión y no '
        'antes: acá nadie instala para conocer el producto, instala porque ya lo usó y quiere '
        'volver más cómodo. Es la señal que alimenta la curva de retención de más abajo.</p>'
        '<p class="note"><b>Volvió otro día</b> es haber estudiado en dos días distintos, sin '
        'pedir que sean consecutivos, y se cuenta contra el <b>primer día que estudió</b> cada uno '
        'y no contra su alta. Antes había un paso más —volver justo al día siguiente— pero eso es '
        'más estricto que lo que el producto promete: la promesa es la repetición espaciada, no la '
        'racha diaria. A diferencia del PDF, que cortaba las sesiones al domingo, la cohorte se '
        'sigue <b>hasta hoy</b>.</p>',
        anchor="embudo"))

    # 2 · Cohortes
    r = p["retencion"]

    def tip(label: str, pt: dict, base: int, *, combinada: bool = False) -> str:
        """Tooltip de un punto, escrito como frase.

        El eje dice «D+3» y eso no significa nada solo, así que cada tooltip
        traduce el k, da el numerador y el denominador con nombre, y aclara por
        qué el denominador no es toda la cohorte.

        `combinada=True` es el mismo tooltip para la curva que fusiona las
        cohortes: mismo texto, sujeto distinto ("la base combinada" en vez de
        "la cohorte del 17/08")."""
        k, n, obs = pt["k"], pt["n"], pt["obs"]
        cuando = ("el mismo día que instalaron" if k == 0 else
                  "un día después de instalar la PWA" if k == 1 else
                  f"{k} días después de instalar la PWA")
        if combinada:
            cab = f'Todas las cohortes combinadas  ·  D+{k}'
            suj_cap, suj = "La base combinada", "la base combinada"
        else:
            cab = f'Cohorte del {label}  ·  D+{k}'
            suj_cap, suj = "La cohorte", "la cohorte"
        # A diferencia de la curva vieja (donde D+0 daba 100% por construcción,
        # porque el ancla y el evento medido eran la misma sesión), acá instalar
        # y estudiar son eventos distintos: D+0 mide, honestamente, cuánta gente
        # estudió el mismo día que instaló — puede no ser 100%.
        if pt["pct"] is None:
            return (
                f'{cab}\n\n'
                f'Todavía no hay a quién medir: nadie de {suj} llegó a cumplir '
                f'{k} días desde que instaló la PWA.')
        falta = base - obs
        # Por qué el denominador no es la cohorte entera, dicho con el número
        # que falta: "83 de 95" no explica nada, "las otras 12 todavía no
        # llegaron a ese día" sí.
        porque = (
            f'{suj_cap} son {base} personas, pero {falta} todavía no cumplieron '
            f'{k} días desde que instalaron, así que su D+{k} no pasó todavía. '
            f'Meterlas en el denominador las contaría como «no volvió» y '
            f'hundiría la curva por calendario, no por comportamiento.'
            if falta else
            f'Acá el denominador es {suj} entera: las {base} ya cumplieron '
            f'{k} días desde que instalaron.')
        cola = ('\n\nTramo punteado: menos de la mitad de la base llegó a este día, '
                'así que el porcentaje se mueve mucho con pocos casos.'
                if _flojo(pt, base) else '')
        return (
            f'{cab}\n\n'
            f'{n} de {obs} personas volvieron a estudiar {cuando}  ({num(pt["pct"], "%")}).\n\n'
            f'{porque}{cola}')

    ret_series = [{
        "label": f'{c["label"]} (n={c["n"]})',
        "values": [pt["pct"] for pt in c["points"]],
        "tips": [tip(c["label"], pt, c["n"]) for pt in c["points"]],
        "weak": [_flojo(pt, c["n"]) for pt in c["points"]],
    } for c in r["cohortes"]]
    # Serie extra al final del array: mismo índice que su <g class="cht-sN">
    # (ver charts.lines/_legend), así que el CSS de abajo la puede prender y
    # apagar por posición sin tocar charts.py. Arranca oculta (ver toggle_css)
    # — "Combinar" es la excepción, no la vista por default.
    n_coh = len(r["cohortes"])
    ret_series.append({
        "label": f'Todas combinadas (n={r["n_combinada"]})',
        "values": [pt["pct"] for pt in r["combinada"]],
        "tips": [tip("", pt, r["n_combinada"], combinada=True) for pt in r["combinada"]],
        "weak": [_flojo(pt, r["n_combinada"]) for pt in r["combinada"]],
    })

    # Botones de cohorte + "Combinar", en CSS puro (sin JS: ver el docstring
    # del módulo). Cada checkbox vive antes que el <svg> en el DOM pero fuera
    # de su árbol, así que las reglas cuelgan de :has() en el contenedor común
    # (.ret-box) en vez del combinador `~`, que no cruza el div de botones.
    toggles, hide_rules, combine_hide = "", "", ""
    if n_coh:
        toggles = "".join(
            f'<input type="checkbox" id="rt{i}" class="rt-chk" checked>'
            f'<label for="rt{i}" class="rt-btn">{esc(c["label"])}</label>'
            for i, c in enumerate(r["cohortes"]))
        toggles += ('<input type="checkbox" id="rtc" class="rt-chk rt-combine">'
                    '<label for="rtc" class="rt-btn rt-btn-combine">Combinar</label>')
        hide_rules = "".join(
            f'.ret-box:has(#rt{i}:not(:checked)) .cht-s{i}{{display:none}}'
            for i in range(n_coh))
        combine_hide = "".join(
            f'.ret-box:has(#rtc:checked) .cht-s{i}{{display:none}}' for i in range(n_coh))
    toggle_css = (
        f'<style>.ret-box .cht-s{n_coh}{{display:none}}{hide_rules}{combine_hide}'
        f'.ret-box:has(#rtc:checked) .cht-s{n_coh}{{display:inline}}</style>')

    co = p["cohortes"]
    body = [
        f'<div class="box ret-box"><div class="chart-head"><h3>Retención diaria por cohorte '
        f'semanal</h3><div class="ret-toggles">{toggles}</div></div>{toggle_css}',
        # mono: son la misma métrica en semanas distintas, no categorías. Un
        # solo tono con la más vieja apagada ordena la lectura.
        # Más alto que el resto: con 14 puntos y la curva pegada al piso a
        # partir de D+2, en 220px las series se superponen y no se distinguen.
        # Más alto todavía desde que hay toggles: con menos líneas activas a
        # la vez conviene que cada una se lea grande.
        #
        # Sin y_max fijo: con la retención anclada en la primera sesión, D+0
        # daba 100% por construcción y forzar el techo a 100 tenía sentido.
        # Anclada en la instalación, nada garantiza que la curva se acerque a
        # 100 (la nota de abajo explica por qué), así que un techo fijo
        # desperdicia la mitad del gráfico. Mismo criterio que el resto de los
        # gráficos de porcentaje del panel (ver ch.vbars en la sección de
        # Producto): el techo se calcula del dato real.
        ch.lines(ret_series, [f"D+{k}" for k in range(r["horizon"] + 1)], mono=True,
                 height=460),
        '<p class="note"><b>D+0 es el día que instaló y abrió la PWA, no el del alta</b>. A '
        'diferencia de la curva anterior —donde el ancla era la primera sesión, así que D+0 daba '
        '100% por construcción—, acá instalar y estudiar son eventos distintos: <b>D+0 no está '
        'garantizado en 100%</b>, mide honestamente cuánta gente estudió el mismo día que instaló. '
        'Anclado en la instalación, cada k mide una sola cosa: cuántos siguen volviendo a estudiar '
        'k días después de haber instalado.</p>'
        '<p class="note">El 100% son <b>los que instalaron y abrieron la PWA</b>, no los que se '
        'dieron de alta ni los que solo estudiaron: instalar es un compromiso mayor que registrarse '
        'o incluso que terminar una sesión, y meter en el denominador a quien nunca instaló '
        'mezclaría «convertir a estudiar» —que ya mide el embudo— con «convertir a hábito '
        'instalado», que es lo que esta curva aísla. «Volver» en cada día sigue siendo terminar una '
        'sesión ese día: instalar no es el objetivo, es el medio. La cohorte sigue siendo la semana '
        'de alta, así que se comparan tandas de usuarios aunque el reloj de cada uno arranque '
        'cuando instaló.</p>'
        '<p class="note"><b>El denominador de cada día no es la cohorte entera</b>, y por eso el '
        'n de la leyenda no coincide con el del tooltip: son solo los que ya vivieron ese día. '
        'Dentro de una misma semana cada uno instala un día distinto, así que quien instaló '
        'anteayer todavía no puede tener un D+5 — y contarlo como «no volvió» hundiría '
        'la curva por calendario y no por comportamiento. Es también por eso que cada línea termina '
        'en un k distinto. <b>Donde la línea va punteada y el punto hueco</b>, menos de la mitad de '
        'la cohorte llegó a ese día: el dato existe pero se mueve mucho con pocos casos. '
        '<b>Pasá el mouse por un punto</b> para ver el detalle.</p>'
        '</div>',
        # A ancho completo y una debajo de la otra: son el corte principal de la
        # semana y en media pantalla no entraban sin scrollear.
        f'<div class="box"><h3>Por universidad</h3>'
        f'{_cohort_table(co["universidad"], "Universidad", "uni")}</div>',
        f'<div class="box"><h3>Por carrera</h3>'
        f'{_cohort_table(co["carrera"], "Carrera", "carrera")}</div>',
        f'<div class="box"><h3>Por curso</h3>'
        f'{_cohort_table(co["curso"], "Curso", "curso")}</div>',
    ]
    body.append(_COHORT_NOTE)
    out.append(_section(2, "Cohortes", "".join(body),
                        sub="El corte que más importa esta semana: quién vuelve, partido por de "
                            "dónde vino.", anchor="cohortes"))

    # 3 · Producto
    pr = p["producto"]
    cur = pr["cursos"]

    # Accuracy y abandono lado a lado, por curso. Son las dos caras de lo mismo
    # —si un curso cuesta más, se abandona más— y el gráfico existe para poder
    # cruzarlas de un vistazo.
    grupos = [curso_label(c["curso"]) for c in cur]
    series = [
        {"label": "Accuracy (P1)", "values": [c["p1"] for c in cur]},
        {"label": "Abandono repaso", "values": [c["main_abandono"] for c in cur]},
        {"label": "Abandono práctica", "values": [c["practice_abandono"] for c in cur]},
    ]
    ses_rows = [[f'{curso_label(r["curso"])} · {r["modo"]}', r["iniciadas"],
                 r["terminadas"], num(r["pct"], "%")] for r in pr["sesiones"]]
    p1_rows = [{"label": r["label"], "value": r["p1"], "note": f'n={r["n"]}'} for r in pr["p1_skill"]]
    sr = pr["sin_respuesta"]

    out.append(_section(
        3, "Producto",
        f'<div class="box"><h3>Accuracy y abandono por curso</h3>'
        + ch.vbars(grupos, series, suffix="%", height=250, width=900)
        + f'<p class="note"><b>Accuracy</b> = P1, aciertos al primer intento '
          f'(<code>quality_score = 5</code>); global {num(pr["p1_global"], "%")} sobre '
          f'{pr["respuestas"]} respuestas. <code>is_correct</code> no sirve para esto: cuenta hasta '
          f'el tercer intento y da ~93% en todos lados. <b>Abandono</b> = sesiones iniciadas que '
          f'nunca se terminaron. Se leen juntas: un curso con accuracy baja y abandono alto tiene '
          f'un problema de dificultad; uno con accuracy alta y abandono alto lo tiene en otro '
          f'lado.</p>'
          f'<p class="note">Aparte quedan las sesiones que se abren y no resuelven <b>ningún</b> '
          f'ejercicio — {sr.get("main", 0)} en repaso y {sr.get("practice", 0)} en práctica. No '
          f'están en el abandono de arriba a propósito: quien corta en el sexto ejercicio se cansó, '
          f'quien corta en el cero nunca arrancó, y son dos problemas distintos.</p></div>'
        '<div class="grid g2">'
        f'<div class="box"><h3>Sesiones por curso y modo</h3>'
        f'{_table(["Curso · modo", "Iniciadas", "Terminadas", "%"], ses_rows)}'
        f'<p class="note">Duración mediana de las terminadas: '
        + " · ".join(f'{k} {num(v)} min' for k, v in pr["duracion"].items())
        + '. <code>duration_seconds</code> está muerta; esto es '
          '<code>finished_at − started_at</code>.</p></div>'
        f'<div class="box"><h3>Accuracy por habilidad</h3>'
        + ch.hbars(p1_rows, suffix="%", label_w=70, width=520)
        + f'<p class="note">La banda de calibración es {pr["banda"][0]}–{pr["banda"][1]}%: sale de '
          f'cruzar los votos de la encuesta de dificultad contra el comportamiento real. Por '
          f'encima, el ítem está blando; por debajo, duro.</p></div>'
        '</div>',
        anchor="producto"))

    # 4 · Encuestas
    e = p["encuestas"]
    mix_rows = [[r["canal"], r["shown"], r["answered"], num(r["tasa"], "%"),
                 num(r["real"], "%"), f'{r["nominal"]}%'] for r in e["mix"]]

    def encuesta_chart(rows: list[dict], order: list[str], emoji: dict, vacio: str) -> str:
        """Barras agrupadas por curso, una serie por respuesta posible."""
        if not rows:
            return f'<p class="empty">{esc(vacio)}</p>'
        grupos = [curso_label(r["curso"]) for r in rows]
        series = [{"label": f'{emoji[v]} {SURVEY_TEXT[v]}',
                   "values": [r["valores"][v] for r in rows]} for v in order]
        return ch.vbars(grupos, series, height=230, width=900)

    out.append(_section(
        4, "Micro-encuestas",
        f'<div class="box"><h3>Mezcla de canales</h3>'
        f'{_table(["Canal", "Mostradas", "Respondidas", "Tasa", "Real", "Nominal"], mix_rows)}'
        '<p class="note">D (interés) es el canal norte, A (dificultad) queda como calibración y B '
        '(explicación) es el más chico. La mezcla real va a estar siempre más cargada a D/A: B '
        'solo loguea impresión si la persona abre «¿Por qué?». <b>No compensar subiendo el peso '
        'de B.</b></p></div>'
        f'<div class="box"><h3>💡 Interés (canal D) por curso</h3>'
        + encuesta_chart(e["d_por_curso"], D_ORDER, SURVEY_EMOJI,
                         "Todavía sin respuestas: el canal D se desplegó el 24/08 y las reglas "
                         "anti-fatiga lo muestran como máximo una vez por sesión.")
        + '<p class="note">Si un curso concentra los 🥱, el problema es de ese contenido y no del '
          'mazo entero — que es justo lo que el total escondía.</p></div>'
        f'<div class="box"><h3>👌 Dificultad (canal A) por curso</h3>'
        + encuesta_chart(e["a_por_curso"], A_ORDER, SURVEY_EMOJI_A,
                         "sin respuestas en la ventana")
        + '<p class="note">Ojo: «justo» existe en los dos canales y significa cosas distintas —acá '
          'la dificultad estuvo bien, en D ni aburrido ni interesante—. Cualquier corte por valor '
          'tiene que filtrar el canal.</p></div>'
        + f'<p class="note">{e["reportes"]} reporte(s) de contenido (canal C) en la ventana.</p>',
        anchor="encuestas"))

    # 5 · Re-enganche: push
    rg = p["reenganche"]
    enviadas_tot = sum(r["enviadas"] for r in rg["por_categoria"]) or 1
    cat_rows = []
    for r in rg["por_categoria"]:
        if r["categoria"] in EVENT_PUSH_COPY:
            desc, ejemplo = EVENT_PUSH_COPY[r["categoria"]]
            nominal = "evento"
        else:
            desc, ejemplo, peso = PUSH_COPY.get(r["categoria"], ("—", "—", 0))
            nominal = f"{peso}%"
        cat_rows.append([
            f'<b>{esc(r["categoria"])}</b><br><span class="sub2">{esc(desc)}</span>'
            f'<br><span class="ej">{esc(ejemplo)}</span>',
            r["enviadas"], num(100 * r["enviadas"] / enviadas_tot, "%"), nominal,
            r["abiertas"], num(r["ctr"], "%")])
    out.append(_section(
        5, "Re-enganche · push",
        '<div class="grid g4">'
        + "".join(f'<div class="box kpi"><div class="label">{esc(l)}</div>'
                  f'<div class="val">{num(v)}</div></div>'
                  for l, v in [("Suscripciones push", rg["subs"]),
                               ("Con notificación activa", rg["activos"]),
                               ("Enviadas", rg["enviadas"]),
                               ("Abiertas", rg["abiertas"])])
        + '</div>'
        f'<div class="box"><h3>Por categoría de copy</h3>'
        f'{_table(["Copy", "Enviadas", "Real", "Nominal", "Abiertas", "CTR"], cat_rows, empty="sin envíos en la ventana")}'
        f'<p class="note">CTR global {num(rg["ctr"], "%")}. <b>Real</b> es qué porción de los '
        f'envíos se llevó cada copy y <b>nominal</b> el peso que tiene asignado en '
        f'<code>notification_copy.py</code>: si se separan mucho, hay variantes que casi nunca '
        f'aplican y el reparto efectivo no es el que se configuró. Si «con notificación activa» '
        f'queda muy por debajo de «suscripciones push», volvió el bug de persistencia de '
        f'<code>notify_enabled</code>.</p>'
        f'<p class="note"><b>«recruit» y «cafecito» son avisos de EVENTO</b>, no de la rotación de '
        f'arriba: salen porque alguien reclutado estudió o alguien donó, con cupo propio '
        f'(<code>notify_events_count</code>), así que no compiten por el lugar del recordatorio '
        f'diario. No tienen «nominal» porque no se sortean — la variante la decide el hecho.</p>'
        f'</div>',
        anchor="push"))

    # 6 · Re-enganche: email
    em = p["emails"]
    mail_rows = [[f'{r["tipo"]}', r["desc"], r["enviados"], r["activados"],
                  num(r["pct"], "%")] for r in em["tipos"]]
    out.append(_section(
        6, "Re-enganche · mails de ciclo de vida",
        '<div class="grid g4">'
        + "".join(f'<div class="box kpi"><div class="label">{esc(l)}</div>'
                  f'<div class="val">{num(v, sfx)}</div><div class="hint">{esc(h)}</div></div>'
                  for l, v, sfx, h in [
                      ("Enviados", em["enviados"], "", "en la ventana visible"),
                      ("Activaron", em["activados"], "",
                       f'estudiaron dentro de {em["ventana_dias"]} días'),
                      ("Tasa de activación", em["pct"], "%", "sobre los enviados"),
                      ("Bajas", em["bajas"], "", f'de {em["usuarios"]} usuarios')])
        + '</div>'
        f'<div class="box"><h3>Por copy</h3>'
        f'{_table(["Copy", "A quién va", "Enviados", "Activaron", "Tasa"], mail_rows, empty="sin envíos en la ventana")}'
        '<p class="note"><b>Activar</b> = terminar una sesión dentro de los '
        f'{em["ventana_dias"]} días siguientes al envío. Es lo más cerca de «el mail funcionó» que '
        'se puede medir con lo que hay. Dos salvedades al leerlo: <b>«streak» y «reclutas_semanal» '
        'no se comparan con los otros</b> —van a alguien que viene de una racha activa o de '
        'reclutas que rindieron esta semana, así que su tasa arranca alta por selección y esa '
        'gente volvía igual—, y <b>no hay grupo de control</b>: todo el que '
        'califica recibe el mail, así que esto es una tasa bruta, no un efecto causal. Para saber '
        'cuánto aporta el mail habría que dejar un holdout sin mandar.</p>'
        '<p class="note"><b>Aperturas: no están.</b> Resend las conoce pero no llegan a esta base; '
        'necesitan un webhook (<code>email.opened</code>) contra un endpoint nuevo. Lo que sí '
        'quedó instrumentado hoy son los <b>clicks</b>: los botones ahora llevan '
        '<code>?utm_source=email&amp;utm_campaign=&lt;copy&gt;</code>, así que PostHog los separa '
        'por copy a partir de los envíos de esta semana.</p></div>',
        anchor="mails"))

    # 7 · Cafecito
    ca = p["cafecito"]
    out.append(_section(
        7, "Cafecito",
        '<div class="grid g4">'
        + "".join(f'<div class="box kpi"><div class="label">{esc(l)}</div>'
                  f'<div class="val">{num(v)}</div><div class="hint">{esc(h)}</div></div>'
                  for l, v, h in [
                      ("Empujes reales", ca["empujes"], "donaciones, sin grants a mano"),
                      ("Cafecitos donados", ca["cafecitos"], "en la ventana visible"),
                      ("XP que generó", ca["xp_generada"], "en respuestas de Intervalo"),
                      ("Mail de agradecimiento", ca["mails_enviados"],
                       f'de {ca["vencidos"]} empujes ya vencidos')])
        + '</div>'
        f'<p class="note">El embudo completo del cartel —impresiones, clicks, CTR por disparador— '
        f'vive en el <a href="/panel/{esc(token)}/dx">panel de dx</a>, que es donde '
        f'se dispara. Acá solo lo que le pasa a Intervalo cuando alguien dona: cuánta XP repartió '
        f'el empuje en esta app (<code>answers.xp_from_boost</code>, sumada UNA vez por respuesta '
        f'y no por empuje, para no duplicar si dos se solapan) y si el mail que le cuenta al '
        f'donante qué hizo su empuje ya salió — sale recién cuando el empuje VENCE, no cuando se '
        f'acredita, porque recién ahí el número está cerrado. <b>«Empujes»</b> cuenta solo '
        f'donaciones reales: los grants a mano que insertamos para probar la mecánica quedan '
        f'afuera.</p>',
        anchor="cafecito"))

    # 8 · Reclutas
    rc = p["reclutas"]
    k_rows = [[w["label"], w["nuevos"], w["reclutados"],
              num(w["pct_reclutados"], "%"), w["base"], num(w["k"])] for w in rc["semanas"]]
    top_rows = [[f'@{esc(t["alias"])}', _uni_chip(t["university"]) if t["university"] else "—",
                t["reclutas"], t["xp"]] for t in rc["top"]]
    out.append(_section(
        8, "Reclutas",
        '<div class="grid g4">'
        + "".join(f'<div class="box kpi"><div class="label">{esc(l)}</div>'
                  f'<div class="val">{num(v, sfx)}</div><div class="hint">{esc(h)}</div></div>'
                  for l, v, sfx, h in [
                      ("Reclutados", rc["total_reclutados"], "", "jugadores que entraron por un link"),
                      ("Del total de jugadores", rc["pct_reclutados"], "%",
                       f'sobre {rc["total_jugadores"]}'),
                      ("K de la última semana",
                       rc["semanas"][-1]["k"] if rc["semanas"] else None, "",
                       "reclutas nuevos / base que ya existía"),
                      ("Top reclutador", rc["top"][0]["xp"] if rc["top"] else None, "",
                       f'XP de @{rc["top"][0]["alias"]}' if rc["top"] else "todavía nadie reclutó")])
        + '</div>'
        f'<div class="box"><h3>Coeficiente de viralidad por semana</h3>'
        f'{_table(["Semana", "Nuevos", "Reclutados", "% reclutados", "Base previa", "K"], k_rows)}'
        '<p class="note"><b>K</b> = reclutas nuevos de la semana sobre los jugadores que YA '
        'EXISTÍAN antes de esa semana — «cuántos usuarios nuevos trae, en promedio, cada usuario '
        'que ya estaba». No es la fórmula completa de Lean Startup (invitaciones × conversión): no '
        'sabemos cuántos links se mandaron, solo cuántos prendieron. <b>K &gt; 1 es crecimiento '
        'que se sostiene solo</b>; por debajo, el link ayuda pero no alcanza como único canal.</p>'
        '</div>'
        f'<div class="box"><h3>Top reclutadores</h3>'
        f'{_table(["Reclutador", "Universidad", "Reclutas", "XP ganada"], top_rows, empty="todavía nadie reclutó")}'
        '<p class="note">De SIEMPRE y no de la ventana visible — como el ranking del juego: no '
        'tendría sentido resetear a quien lleva meses trayendo gente solo porque esta semana no '
        'reclutó a nadie nuevo. Un solo nivel: los reclutas de tus reclutas no suman acá (ver '
        '<code>game/referrals.py</code>).</p></div>',
        anchor="reclutas"))

    out.append(
        '<footer>'
        f'<p>Generado {esc(m["generated_at"])} · zona horaria {esc(m["tz"])} · '
        f'{m["usuarios"]} usuarios en la base · semanas de lunes a domingo.</p>'
        '<p><b>Definiciones.</b> Sesión = modo <code>main</code> o <code>practice</code>; las de '
        '<code>onboarding</code> (el ejercicio de prueba del alta) y <code>test</code> (QA) no '
        'cuentan — contarlas fue lo que infló el «97% completó una sesión» durante dos semanas. '
        'Terminada = <code>finished_at</code> no nulo. P1 = <code>quality_score = 5</code>. '
        'Cohorte = semana de alta del usuario. Volver = terminó más de una sesión.</p>'
        '<p>Este panel es de solo lectura y no expone datos personales. El link es secreto: '
        'rotarlo es cambiar <code>DASHBOARD_TOKEN</code> en Railway.</p>'
        '</footer></div>')

    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'>"
        f"<title>Panel · intervalo</title><style>{CSS}</style></head>"
        f"<body>{''.join(out)}</body></html>")


def week_of_today() -> date:
    from datetime import datetime

    from .queries import AR_OFFSET, week_start
    return week_start((datetime.utcnow() + AR_OFFSET).date())
