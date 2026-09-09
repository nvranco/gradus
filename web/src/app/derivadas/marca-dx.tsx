import { readFile } from "node:fs/promises"
import { join } from "node:path"

// El "dx" con su barra de cuatro colores, dibujado una sola vez y usado por los
// tres lugares que necesitan una imagen de la marca del juego:
//
//   - icon.tsx           → el favicon de la pestaña (96 px, esquinas redondeadas)
//   - apple-icon.tsx     → lo que iOS agarra al "Agregar a inicio" (180 px, a sangre)
//   - app-icon/[medida]  → los PNG del manifest, que es lo que usa Android
//
// Antes esto vivía entero adentro de icon.tsx y era el único ícono del juego:
// al instalarlo en la pantalla de inicio quedaba el cuadradito "int" de
// Intervalo, o sea que instalabas una cosa y te quedaba otra. Ahora los tres
// salen del mismo dibujo, así que no pueden divergir.
//
// Se rasteriza al tamaño declarado (Satori no vectoriza): una serifa resuelta
// directamente en 32 px queda con los remates rotos, por eso el favicon se
// dibuja a 96 y el navegador lo baja él, que interpola mucho mejor.

const BAR_COLORS = ["#e8e8ea", "#2a62c4", "#8d31b7", "#7e451f"]

export const FONDO = "#131324"
export const TINTA = "#F6F8FC"

// Proporciones medidas sobre el ícono de Intervalo (public/intervalo-icon-1024.png),
// todas relativas al lado para que escalen solas.
const FUENTE = 43 / 96
const RADIO = 18 / 96 // ~19% del lado
// La barra va más gruesa que en el wordmark de pantalla (.17em contra .12em):
// es la misma corrección óptica que hace el Wordmark de la app a tamaño chico,
// porque un subrayado a escala exacta desaparece en un ícono.
const BARRA = 0.17
const HUECO = 0.14
// El centrado del flex alinea la CAJA, no la tinta: con line-height 1 la fuente
// igual reserva aire sobre la mayúscula, así que la tinta quedaba 4 px más abajo
// que el centro a 96 px. Medido contra el ícono de Intervalo, que sí está
// ópticamente centrado.
const SUBIR = 4 / 96

export function fuenteNotoSerif() {
  return readFile(join(process.cwd(), "src/app/derivadas/noto-serif-600.ttf"))
}

/** Opciones de la marca.
 *
 * `redondeado` es para el favicon, donde el cuadradito con esquinas ES la forma
 * del ícono. Los de la pantalla de inicio van a sangre: iOS y Android aplican su
 * propia máscara, y unas esquinas transparentes abajo de la máscara se ven como
 * un ícono más chico que el resto de la grilla.
 *
 * `escala` achica el dibujo dentro del mismo lienzo. Lo necesita el ícono
 * `maskable` de Android, donde el lanzador puede recortar hasta un círculo del
 * 80% del lado: al 100% la barra queda pisando el borde de ese recorte.
 */
export function MarcaDx({
  lado,
  redondeado = false,
  escala = 1,
}: {
  lado: number
  redondeado?: boolean
  escala?: number
}) {
  const fuente = Math.round(lado * FUENTE * escala)
  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: FONDO,
        borderRadius: redondeado ? Math.round(lado * RADIO) : 0,
      }}
    >
      {/* `alignItems: stretch` hace que la barra mida exactamente lo que mide la
          palabra, igual que en el logo: el ancho lo fija el texto. */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "stretch",
          gap: Math.round(fuente * HUECO),
          marginTop: -Math.round(lado * SUBIR * escala),
        }}
      >
        <div
          style={{
            fontFamily: "Noto Serif",
            fontSize: fuente,
            fontWeight: 600,
            color: TINTA,
            lineHeight: 1,
          }}
        >
          dx
        </div>
        <div
          style={{
            display: "flex",
            height: Math.round(fuente * BARRA),
            borderRadius: Math.max(2, Math.round(lado * 0.02)),
            overflow: "hidden",
          }}
        >
          {BAR_COLORS.map((c) => (
            <div key={c} style={{ flex: 1, background: c }} />
          ))}
        </div>
      </div>
    </div>
  )
}
