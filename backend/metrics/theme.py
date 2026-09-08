"""CSS y helpers de presentación compartidos entre los dos paneles.

Antes duplicados a mano entre `metrics/render.py` (Intervalo) y
`metrics/game_render.py` (Derivemos), con estética distinta: Intervalo era
plano y Derivemos tenía la piel del juego (papel cuadriculado, cajas de radio
8). El usuario pidió que Intervalo adoptara esa piel, y una vez que las dos
comparten piel, mantener el CSS duplicado deja de tener sentido — cualquier
cambio de tema hay que hacerlo dos veces o los paneles vuelven a divergir.

`BASE_CSS` es, en lo esencial, la hoja de estilos que ya tenía Derivemos:
es la identidad que se adoptó. Lo que NO vive acá es intencional: los
helpers cuya estructura de datos es específica de un panel (`_cohort_table`
con heatmap en Intervalo; las celdas de MathML/plantilla en Derivemos)
quedan en su propio render.py, porque unificarlos significaría inventarles
un caso de uso común que no existe.
"""
from __future__ import annotations

import base64
from pathlib import Path

from . import charts as ch
from .charts import esc, num

# Los tokens salen del tema del front (web/src/app/globals.css) para que los
# paneles y el juego sean literalmente el mismo color y no dos azules
# parecidos. `--surface` existe además de `--card` porque charts.py lo usa
# para el relleno de los puntos huecos; apuntan al mismo valor a propósito.
BASE_CSS = """
:root{
  --bg:#131324; --card:#1a1a2a; --surface:#1a1a2a; --surface-2:#22223a;
  --border:#38385a; --fg:#f6f8fc; --muted:#a4b3c6; --grid:#26263f;
  --indigo:#5457e5; --indigo-soft:#8b8df0; --violet:#9b2fc9; --blue:#1b63d6;
  --brown:#b4652a;
  --ok:#22c55e; --warn:#f59e0b; --bad:#f97316;
}
*{box-sizing:border-box}
body{margin:0;color:var(--fg);
  background-color:var(--bg);
  /* Papel cuadriculado: el mismo GRID_BG_STYLE del juego, traducido a CSS. */
  background-image:linear-gradient(rgba(255,255,255,0.03) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(255,255,255,0.03) 1px,transparent 1px);
  background-size:40px 40px;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:15px;line-height:1.55;-webkit-text-size-adjust:100%}
.wrap{max-width:1180px;margin:0 auto;padding:20px 24px 72px;
  display:flex;flex-direction:column;gap:12px}
a{color:var(--indigo-soft);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--indigo-soft);outline-offset:2px;border-radius:6px}

/* La caja. Es LA pieza que se copia del juego: radio 8, borde #38385a,
   superficie #1a1a2a. Todo lo demás del panel vive adentro de una de estas. */
.box{border:1px solid var(--border);background:var(--card);border-radius:8px;padding:18px 20px}

/* Cabecera: una sola caja de ancho completo, con la marca a la izquierda y el
   selector de semana a la derecha. Eran dos —la de al lado tenía los totales y
   el link al otro panel—, pero un contador que ya está en cada sección y un
   link que ahora vive en la marca no justificaban partir la fila. */
header.top{display:block}
/* 55 px es el alto EXACTO de la cabecera de /derivadas, medido sobre la página
   en producción: ahí lo fijan los botones de la barra, que acá no están. Sin
   esto la caja se encoge al alto de la marca y el logo —que es el mismo dibujo
   y el mismo cuerpo de letra— se lee más chico por vivir en una caja más baja. */
header.top .box{min-height:55px;padding:10px 16px;display:flex;align-items:center;
  justify-content:space-between;gap:12px;flex-wrap:wrap}
/* La marca de Intervalo tal como está en la cabecera de /derivadas: la palabra
   con su subrayado de cuatro tramos (web/src/app/derivadas/game-logo.tsx). Las
   proporciones son las de allá y por eso van en `em` —separación 0,16 y barra
   0,12 del cuerpo de letra—: así el dibujo es el mismo a cualquier tamaño.
   El cuerpo de letra es el mismo de allá (17 px) y la fuente también: es lo
   único que el panel trae de afuera (ver FUENTE_MARCA). Los cuatro colores
   están resueltos en web/src/app/derivadas/icon.tsx. */
.brand{display:inline-flex;flex-direction:column;align-items:stretch;gap:.16em;
  font-family:'Noto Serif',Georgia,'Times New Roman',Times,serif;font-weight:600;
  font-size:17px;line-height:1;color:var(--fg);text-decoration:none}
.brand:hover{text-decoration:none}
.brand .bar{display:flex;height:.12em;border-radius:2px;overflow:hidden}
.brand .bar i{flex:1}
.weeknav{display:flex;gap:6px;align-items:center;font-size:13px;flex-wrap:wrap}
.weeknav a,.weeknav .cur{padding:3px 9px;border-radius:6px;border:1px solid var(--border)}
.weeknav .cur{background:var(--indigo);color:#fff;border-color:var(--indigo);font-weight:600}
/* Barra de secciones: índice en el panel de Intervalo, pestañas en el del juego.
   El activo se PINTA, no se despoja: comparte caja con los links —mismo alto,
   mismo borde, mismo redondeo— y solo cambia de relleno. Sin la caja, el
   seleccionado se leía como texto suelto entre dos botones, que es lo contrario
   de lo que tiene que comunicar. Es el mismo trato que la semana en curso. */
nav.jump{display:flex;gap:6px;flex-wrap:wrap;font-size:12px}
nav.jump a,nav.jump .cur{border:1px solid var(--border);border-radius:6px;
  padding:3px 9px}
nav.jump a{color:var(--muted);background:var(--card)}
nav.jump a:hover{color:var(--fg);text-decoration:none;border-color:var(--indigo)}
nav.jump .cur{background:var(--indigo);color:#fff;border-color:var(--indigo);
  font-weight:600}
/* La barra de cortes de un gráfico: los mismos chips que tenía el índice de
   secciones, que ya no existe. Un control del panel se ve igual viva donde
   viva, y el activo se marca como la semana en curso del selector de arriba. */
.cortes{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12px;
  margin:0 0 12px}
.cortes .sub{margin:0 4px 0 0}
.cortes a,.cortes .cur{border-radius:6px;padding:3px 9px;border:1px solid var(--border)}
.cortes a{color:var(--muted);background:var(--card)}
.cortes a:hover{color:var(--fg);text-decoration:none;border-color:var(--indigo)}
.cortes .cur{background:var(--indigo);color:#fff;border-color:var(--indigo);font-weight:600}

h2{font-size:12.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
  font-weight:700;margin:26px 0 10px;display:flex;gap:10px;align-items:baseline}
h2 b{color:var(--indigo-soft);font-variant-numeric:tabular-nums}
h3{font-size:14.5px;margin:0 0 10px;font-weight:650}
section{display:flex;flex-direction:column;gap:12px}

.grid{display:grid;gap:12px}
/* min(Npx,100%): con auto-fit un minmax fijo mantiene el track aunque el
   contenedor sea más angosto, y la caja se sale de la pantalla en un celular. */
.g2{grid-template-columns:repeat(auto-fit,minmax(min(420px,100%),1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr))}

.kpi .label{color:var(--muted);font-size:12.5px}
.kpi .row{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin-top:2px}
.kpi .val{font-size:31px;font-weight:750;letter-spacing:-.03em;
  font-variant-numeric:tabular-nums;line-height:1.05}
.kpi .hint{color:var(--muted);font-size:11.5px;margin-top:6px}
.chip{font-size:11.5px;font-weight:650;padding:2px 8px;border-radius:6px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.chip.up{background:rgba(34,197,94,.16);color:#5ee08a}
.chip.down{background:rgba(249,115,22,.16);color:#fb9a5c}
.chip.flat{background:var(--surface-2);color:var(--muted)}

.note{color:var(--muted);font-size:12.5px;margin:10px 0 0;
  border-left:2px solid var(--border);padding-left:11px}
.note b{color:var(--fg);font-weight:600}
.sub{color:var(--muted);font-size:13px;margin:0}
.big{font-size:26px;font-weight:750;letter-spacing:-.03em;
  font-variant-numeric:tabular-nums;line-height:1.1}

.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:7px;border-bottom:1px solid var(--border);white-space:nowrap}
th:first-child,td:first-child{padding-left:0;text-align:left}
th:last-child,td:last-child{padding-right:0}
th{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.04em;
  text-transform:uppercase}
tbody tr:last-child td{border-bottom:0}
td.dim{color:var(--muted)}

/* Chip de universidad: el mismo del ranking del juego
   (web/src/components/university-tag.tsx). */
.tag{display:inline-flex;align-items:center;border:1px solid;border-radius:6px;
  padding:1px 7px;font-size:11.5px;font-weight:700}
.tag-plain{border-color:transparent;background:rgba(255,255,255,.1);
  color:rgba(246,248,252,.72);font-weight:600}
.pill{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;
  background:var(--surface-2);color:var(--muted);margin-left:6px}
.empty{color:var(--muted);font-style:italic;font-size:13px;margin:8px 0}
.warn{color:#fb9a5c}
.ok{color:#5ee08a}
footer{color:var(--muted);font-size:12px;margin-top:28px;padding-top:16px;
  border-top:1px solid var(--border)}
footer b{color:var(--fg)}
@media print{body{background:#fff;color:#111}}
"""


def delta_chip(d, suffix: str = "", dec: int = 1) -> str:
    if d is None:
        return '<span class="chip flat">sin base</span>'
    cls = "up" if d > 0 else ("down" if d < 0 else "flat")
    sign = "+" if d > 0 else ""
    # La diferencia entre dos porcentajes son puntos porcentuales: "-2,2%" sobre
    # un 7,1% se lee como una caída del 2% cuando cayó de 9,3 a 7,1.
    unit = " pp" if suffix == "%" else suffix
    return f'<span class="chip {cls}">{sign}{num(d, unit, dec)}</span>'


def kpi(c: dict) -> str:
    sfx = c["suffix"]
    dec = c.get("dec", 1)
    return (
        f'<div class="box kpi">'
        f'<div class="label">{esc(c["label"])}</div>'
        f'<div class="row"><div class="val">{num(c["value"], sfx, dec)}</div>'
        f'{ch.spark(c["series"])}</div>'
        f'<div class="row" style="margin-top:8px">{delta_chip(c["delta"], sfx, dec)}'
        f'<span class="hint">vs. semana anterior</span></div>'
        f'<div class="hint">{esc(c["hint"])}</div></div>')


def table(cols: list[str], rows: list[list], empty: str = "sin datos") -> str:
    if not rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(
            f"<td>{c if isinstance(c, str) and c.startswith('<') else esc(c)}</td>" for c in r)
        + "</tr>" for r in rows)
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def box(title: str, body: str, note: str = "") -> str:
    h = f"<h3>{esc(title)}</h3>" if title else ""
    n = f'<p class="note">{note}</p>' if note else ""
    return f'<div class="box">{h}{body}{n}</div>'


def section(n: int, title: str, body: str, sub: str = "", anchor: str = "") -> str:
    a = f' id="{esc(anchor)}"' if anchor else ""
    s = f'<p class="sub">{sub}</p>' if sub else ""
    return f'<section{a}><h2><b>{n}</b>{esc(title)}</h2>{s}{body}</section>'
# Los cuatro tramos del subrayado de la marca, en orden. Resueltos en
# web/src/app/derivadas/icon.tsx: el front los calcula con `mixWithLegendBg`
# sobre los colores de cinturón, así que este es el resultado y no la fórmula —
# si allá cambian, hay que venir a copiarlos de nuevo.
BELT_BAR = ("#e8e8ea", "#2a62c4", "#8d31b7", "#7e451f")


# ── Identidad de la pestaña ────────────────────────────────────────────────

# El favicon es EL ARCHIVO de producción, no una versión dibujada acá: es el PNG
# de 96 px que sirve /derivadas/icon (web/src/app/derivadas/icon.tsx, que lo
# rasteriza con Satori). Se bajó de la app y se guardó al lado de este módulo.
#
# El primer intento fue redibujarlo en SVG con las mismas medidas, y no alcanzó:
# la serifa del sistema no es la Noto Serif, el subrayado cae distinto y en la
# barra de pestañas los dos íconos se veían como dos marcas parecidas en vez de
# la misma. A 16 px no hay margen para "casi".
#
# Va embebido como data URI y no como archivo servido porque el panel se sirve
# desde FastAPI, que no tiene rutas de estáticos: una ruta nueva para dos kilos
# sería más código que el ícono.
#
# Si el ícono de la app cambia, este queda viejo. Se actualiza bajándolo de
# nuevo: la URL sale del `<link rel="icon">` de https://www.intervalo.xyz/derivadas.
FAVICON_PNG = Path(__file__).with_name("favicon-dx.png")

FAVICON_LINK = (
    "<link rel='icon' type='image/png' sizes='96x96' href='data:image/png;base64,"
    + base64.b64encode(FAVICON_PNG.read_bytes()).decode("ascii")
    + "'>"
)

# La Noto Serif de la app, solo para la marca de la cabecera. Es la única cosa
# que el panel pide afuera, y es a propósito: con la serifa del sistema el
# wordmark del panel y el del juego se leían como dos logos distintos, que es
# justo lo que compartir la piel quería evitar. Si la fuente no carga, la pila
# de abajo la reemplaza y el dibujo sigue en pie.
FUENTE_MARCA = (
    "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
    "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?"
    "family=Noto+Serif:wght@600&display=swap'>"
)
