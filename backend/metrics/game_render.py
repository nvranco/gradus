"""Armado del HTML del panel del minijuego.

Misma cocina que `metrics/render.py` —una sola página, oscura, sin JS, con los
SVG generados en el server— y **otra piel**: acá el formato de contenedores es el
de la versión de escritorio de `/derivadas`. Eso significa cosas concretas y no
un parecido de familia:

  - el papel cuadriculado de fondo (`GRID_BG_STYLE` del front, 40 px, blanco al
    3%), que es lo que hace que las cajas floten en vez de estar pegadas;
  - las cajas con el mismo radio, borde y superficie que las del juego
    (`rounded-lg border border-border bg-card`, o sea 8 px, `#38385a`, `#1a1a2a`);
  - la cabecera partida en dos, ancha a la izquierda y angosta a la derecha,
    igual que el bloque de marca + identidad del juego;
  - `gap` de 12 px entre cajas, que es el `gap-3` de Tailwind que usa el layout.

Es deliberado: el panel se mira inmediatamente después de jugar, y que las dos
pantallas compartan la caja hace que se lean como el mismo producto. Lo que NO se
copia es el ancho —el juego vive en 61,8rem porque tiene una sola columna de
contenido y acá hay tablas— ni la altura fija: un panel scrollea.

Las secciones están numeradas y en el orden en que conviene leerlas: cuánta
gente entra, cuánto aguanta, y recién después de dónde salió y con qué la juega.

El panel llegó a tener once secciones —el diagnóstico del Elo, la tabla de
plantillas, el embudo del cafecito, la fricción del teclado— y se recortó a
seis. Lo que se fue no estaba mal medido: era instrumentación del motor, que se
mira cuando se está tocando el motor y no todos los días. Las consultas se
borraron con la sección, no se dejaron colgadas alimentando un `data.json` que
nadie lee (`git log` las tiene si hacen falta de nuevo).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import charts as ch
from . import theme
from .charts import esc, num
from .game_queries import (
    FIRST_WEEK, PEDIDO_CAFECITO, PEDIDO_PERFIL, PEDIDO_REGISTRO,
)

# El grueso del CSS es el mismo que Intervalo (ver metrics/theme.py) — es la
# piel de la que se copió en primer lugar. Acá solo quedan las reglas que no
# tienen sentido fuera del juego: la celda monoespaciada de `template_key` y
# el tamaño del MathML de los ejemplos.
CSS = theme.BASE_CSS

# Los colores de marca de cada universidad, reusados del panel de Intervalo en
# vez de copiar la lista: backend/universities.py ya advierte de las tres copias
# que hay dando vueltas, y una cuarta sería la que se olvida de actualizarse.
# Son los mismos del chip que el jugador ve en su ranking, que es lo que hace
# que una línea del desglose se reconozca sin leer la leyenda.
from .render import UNIVERSITY_COLOR  # noqa: E402

# Helpers de presentación compartidos con el panel de Intervalo — ver
# metrics/theme.py. Los alias locales evitan reescribir las llamadas ya
# existentes en este archivo.
_chip = theme.delta_chip
_kpi = theme.kpi
_table = theme.table
_box = theme.box
_section = theme.section


def _pct_txt(v) -> str:
    return "—" if v is None else num(v, "%")


# Las tres pestañas del panel, en el orden en que conviene leerlas: cuánta
# gente hay, dónde se cae, y cuánto aguanta la que se queda.
#
# Son PESTAÑAS y no una página larga porque las tres se miran de a una: no hay
# ninguna lectura que necesite el embudo y la curva de profundidad a la vez, y
# scrollear entre ellas obligaba a acordarse del número de arriba mientras se
# busca el de abajo.
#
# La pestaña viaja en la URL (`?s=`) y no en un `:target` ni en un radio con
# CSS. Cuesta una recarga, pero deja el estado entero —semana, pestaña y
# desglose— en un link que se puede compartir y que sobrevive al botón de
# atrás. Con el estado del lado del navegador, cambiar el desglose —que sí
# recarga, porque cambia los datos— devolvía a la primera pestaña.
SECCIONES: tuple[tuple[str, str], ...] = (
    ("titulares", "Titulares"),
    ("embudo", "Embudo"),
    ("profundidad", "Profundidad"),
)
SECCION_POR_DEFECTO = SECCIONES[0][0]


def week_of_today() -> date:
    from .queries import local_date, week_start
    return week_start(local_date(datetime.utcnow()))


# ── Página ───────────────────────────────────────────────────────────────────

def page(p: dict, *, token: str, seccion: str = SECCION_POR_DEFECTO) -> str:
    m = p["meta"]
    claves = [c for c, _ in SECCIONES]
    seccion = seccion if seccion in claves else SECCION_POR_DEFECTO
    week = date.fromisoformat(m["week"])
    labels = m["labels"]
    semanas = [date.fromisoformat(w) for w in m["weeks"]]

    out: list[str] = ["<div class='wrap'>"]

    # Cabecera: dos cajas, ancha + angosta, igual que el header del juego.
    today = week_of_today()
    corte_actual = p["profundidad"]["corte"]
    nav = []
    for i in range(4, -1, -1):
        # Sin bajar del piso del panel: ofrecer semanas anteriores a la difusión
        # es ofrecer ceros estructurales con forma de historia.
        w = today - timedelta(weeks=i)
        if w < FIRST_WEEK:
            continue
        lab = w.strftime("%d/%m")
        if w == week:
            nav.append(f'<span class="cur">{lab}</span>')
            continue
        q = f"?w={w.isoformat()}"
        if seccion != SECCION_POR_DEFECTO:
            q += f"&s={seccion}"
        if corte_actual != "total":
            q += f"&corte={corte_actual}"
        nav.append(f'<a href="/panel/{esc(token)}/dx{q}">{lab}</a>')
    # La marca lleva el link al panel de Intervalo. Antes eso vivía en una
    # segunda caja a la derecha; al sacarla, el logo se queda con el trabajo que
    # hace en cualquier cabecera —volver a la casa— en vez de perderse la única
    # forma de saltar de un panel al otro.
    out.append(
        "<header class='top'><div class='box'>"
        f"<a class='brand' href='/panel/{esc(token)}' title='Panel de Intervalo'>"
        "intervalo"
        "<span class='bar'>" + "".join(
            f"<i style='background:{c}'></i>" for c in theme.BELT_BAR) +
        "</span></a>"
        f"<div class='weeknav'>{''.join(nav)}</div>"
        "</div></header>")

    def link(*, s: str | None = None, corte: str | None = None) -> str:
        """La URL del panel cambiando UNA cosa y dejando el resto como está.

        Es lo que hace que las tres barras convivan: elegir semana no pierde la
        pestaña, y elegir desglose no devuelve a la primera."""
        s = s if s is not None else seccion
        corte = corte if corte is not None else p["profundidad"]["corte"]
        q = f"?w={week.isoformat()}"
        if s != SECCION_POR_DEFECTO:
            q += f"&s={s}"
        if corte != "total":
            q += f"&corte={corte}"
        return f"/panel/{esc(token)}/dx{q}"

    tabs = "".join(
        f'<span class="cur">{esc(t)}</span>' if c == seccion
        else f'<a href="{link(s=c)}">{esc(t)}</a>'
        for c, t in SECCIONES)
    out.append(f"<nav class='jump'>{tabs}</nav>")

    # Las tres se arman siempre y se muestra una. Armarlas cuesta unos SVG que
    # nadie va a ver, y a cambio el archivo se sigue leyendo en el orden del
    # panel en vez de partirse en tres ramas con el cuerpo de cada sección
    # colgando de un `if`.
    cabecera = "".join(out)
    paneles: dict[str, str] = {}

    # ── 0 · Titulares ────────────────────────────────────────────────────────
    out = []
    out.append(_section(
        0, f"Titulares · semana del {labels[-1]}",
        f'<div class="grid g4">{"".join(_kpi(c) for c in p["headline"])}</div>',
        sub="El sparkline son las semanas visibles."))
    paneles["titulares"] = "".join(out)

    # ── 1 · Embudo ───────────────────────────────────────────────────────────
    out = []
    f = p["funnel"]
    def nota(s: dict) -> str:
        if s["pct_prev"] is not None:
            return f'{num(s["pct_prev"], "%")} del paso anterior'
        # Los pasos que no están anidados se leen contra la cohorte: es el único
        # denominador que significa algo para ellos.
        if not s["cadena"] and s["pct_base"] is not None:
            return f'{num(s["pct_base"], "%")} de la cohorte'
        return ""

    rows = [{"label": s["label"], "value": s["n"], "note": nota(s)} for s in f["steps"]]
    out.append(_section(
        1, "Embudo de la partida",
        # Barras más finas y más juntas que el default: diez pasos con el aire
        # de siempre pedían media pantalla de scroll para leer una lista que se
        # entiende de un vistazo.
        _box("", ch.hbars(rows, colors=["var(--indigo)"], bar_h=18, gap=6),
             note="<b>Arranca en «abrió el juego»</b> y no en «vio el link»: la fila del estudiante "
                  "se crea en la primera carga de la página, así que todo lo anterior —cuánta "
                  "gente vio el mensaje de WhatsApp, cuánta tocó y no llegó a cargar— solo lo "
                  "sabe PostHog. Preferimos que el embudo empiece tarde y sea cierto."
                  "<br><br><b>«Cargó universidad» y «se registró» no son parte de la "
                  "cadena</b>, y por eso se leen contra la cohorte y no contra el paso de "
                  "arriba. Están puestos donde el juego los pide —carrera y universidad en la "
                  f"derivada <b>{PEDIDO_PERFIL}</b>, el registro en la <b>{PEDIDO_REGISTRO}</b>— "
                  "para poder compararlos con la gente que llegó hasta ahí. Alguien que viene "
                  "de Intervalo cuenta en los dos desde el minuto cero, sin haber derivado "
                  "nada."),
        sub=f"Cohorte de los {num(f['base'])} estudiantes que abrieron el juego en la semana del "
            f"{labels[-1]}, seguida hasta hoy.",
        anchor="embudo"))
    paneles["embudo"] = "".join(out)

    # ── 2 · Profundidad ──────────────────────────────────────────────────────
    out = []
    pr = p["profundidad"]
    corte = pr["corte"]
    # Las cohortes son el MISMO indicador en momentos distintos, así que van en
    # un solo tono con la vieja apagada y la nueva al frente (charts.ramp): un
    # color por semana las haría leer como categorías separadas. Universidad y
    # aparato sí son categorías, y ahí cada una lleva su color —el de marca en
    # el caso de las universidades, que es el del chip del ranking—.
    mono = corte == "cohorte"

    def color_de(serie: dict) -> str | None:
        if corte == "universidad":
            return UNIVERSITY_COLOR.get(serie["clave"] or "")
        return None

    series = [{
        "label": f'{s["label"]} (n={s["base"]})' if corte != "total" else "Siguen jugando",
        "color": color_de(s),
        "values": [c["pct"] for c in s["curva"]],
        "tips": [f'{s["label"]}: {c["vivos"]} de {s["base"]} llegaron a responder '
                 f'{c["k"]} derivadas ({_pct_txt(c["pct"])}).\n'
                 f'De los que llegaron a la {c["k"]}, {_pct_txt(c["abandono"])} no hizo '
                 f'la siguiente.' for c in s["curva"]],
        # Menos de 10 partidas vivas: el porcentaje se mueve entero con una
        # persona y se dibuja punteado para que no se lea como tendencia.
        "weak": [c["vivos"] < 10 for c in s["curva"]],
    } for s in pr["series"]]

    peor = pr["peor_escalon"]
    peor_txt = (f'El escalón más grande del tramo 1–20 está en la derivada '
                f'<b>#{peor["k"]}</b>: de los {peor["vivos"]} que llegaron ahí, '
                f'{_pct_txt(peor["abandono"])} no hizo la siguiente.'
                if peor else "Todavía no hay base para señalar un escalón.")
    resumen = (f'Mediana <b>{num(pr["mediana"])}</b> derivadas · p90 '
               f'<b>{num(pr["p90"])}</b>. ' if pr["base"] else
               'Todavía no hay ninguna partida cerrada en esta ventana. ')
    if corte == "total":
        alcance = ""
    elif not series:
        alcance = ("<br><br><b>El desglose quedó vacío</b>: ningún grupo llega a las cinco "
                   "partidas cerradas que hacen falta para merecer su propia línea, así que "
                   "no hay nada que dibujar sin dibujar ruido.")
    elif corte == "cohorte":
        # Acá las líneas NO parten la cohorte: son otras camadas. Decir «cubren
        # X de Y» sería mentir sobre lo que se está mirando.
        alcance = ('<br><br><b>Cada línea es una camada distinta</b>, no un pedazo de esta: '
                   'la de la semana elegida y las dos anteriores, cada una seguida desde su '
                   'propio día uno. Por eso no suman — se comparan.')
    else:
        afuera = pr["base"] - pr["cubiertos"]
        alcance = (f'<br><br><b>Las líneas parten esta misma cohorte</b>: cubren '
                   f'{num(pr["cubiertos"])} de sus {num(pr["base"])} partidas cerradas'
                   + (f', y quedan {num(afuera)} afuera — quien no cargó ese dato, y los grupos '
                      f'con menos de cinco partidas, que dibujarían el ruido de cuatro personas '
                      f'con la misma tinta que la tendencia de cuarenta.' if afuera else "."))

    if not series:
        grafico = '<p class="empty">todavía no hay partidas cerradas en esta ventana</p>'
    else:
        grafico = ch.lines(series, [str(c["k"]) for c in pr["curva"]], suffix="%",
                           height=340, y_max=100, legend=corte != "total", mono=mono)

    # El selector va pegado al gráfico que gobierna, y no arriba de la página:
    # es lo único que cambia, así que si vive lejos hay que acordarse de que
    # existe. Son links y no un `<select>` porque el panel no tiene JavaScript —
    # y de yapa cada corte queda con URL propia, así que se puede compartir o
    # abrir dos en dos pestañas para compararlos al mismo tiempo.
    selector = "".join(
        f'<span class="cur">{esc(t)}</span>' if c == corte
        else f'<a href="{link(corte=c)}">{esc(t)}</a>'
        for c, t in [("total", "Todos"), ("cohorte", "Por cohorte"),
                     ("universidad", "Por universidad"), ("aparato", "Por aparato")])
    cuerpo = (f"<div class='cortes'><span class='sub'>Desglose</span>{selector}</div>"
              + grafico)

    out.append(_section(
        2, "Profundidad",
        _box("Cuántos siguen jugando en la derivada k", cuerpo,
             note=resumen + peor_txt + alcance
                  + "<br><br>Solo entran <b>partidas cerradas</b> (nadie que haya "
                    "respondido en las últimas 24 h): quien está jugando ahora todavía puede "
                    "sumar derivadas, y contarlo hundiría la cola por reloj y no por "
                    f'comportamiento. De esta cohorte quedaron afuera {num(pr["abiertos"])} '
                    f'partidas abiertas.'
                    "<br><br>El tramo punteado es donde quedan menos de diez partidas vivas: "
                    "ahí el porcentaje se mueve entero con una persona y no conviene leer la "
                    "forma."),
        sub=f"La misma cohorte del embudo —los que abrieron el juego en la semana del "
            f"{labels[-1]}— seguida hasta hoy. Es la métrica del juego: el Elo, el ranking y "
            f"el cafecito existen para mover esta curva.",
        anchor="profundidad"))
    paneles["profundidad"] = "".join(out)

    out = [cabecera, paneles[seccion]]
    out.append(
        f"<footer>Generado {esc(m['generated_at'])} · zona {esc(m['tz'])} · "
        f"semana del {esc(labels[-1])}. "
        f"<a href='/panel/{esc(token)}/dx/data.json?w={week.isoformat()}'>data.json</a>"
        f"</footer></div>")

    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex,nofollow'>"
        "<title>Intervalo — Dashboard</title>"
        f"{theme.FAVICON_LINK}{theme.FUENTE_MARCA}"
        f"<style>{CSS}</style></head><body>{''.join(out)}</body></html>")
