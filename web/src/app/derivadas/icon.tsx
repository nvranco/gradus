import { ImageResponse } from "next/og"

import { fuenteNotoSerif, MarcaDx } from "./marca-dx"

// Favicon propio de /derivadas: el mismo cuadradito redondeado del ícono de
// Intervalo (public/intervalo-icon-1024.png) pero con "dx" adentro en vez de
// "int". Al vivir en esta carpeta y no en app/, Next se lo pone SOLO a esta ruta.
//
// El dibujo está en marca-dx.tsx, compartido con el ícono de la pantalla de
// inicio: son la misma marca y tienen que salir del mismo lugar.

export const size = { width: 96, height: 96 }
export const contentType = "image/png"

export default async function Icon() {
  return new ImageResponse(<MarcaDx lado={size.width} redondeado />, {
    ...size,
    fonts: [
      { name: "Noto Serif", data: await fuenteNotoSerif(), weight: 600, style: "normal" },
    ],
  })
}
