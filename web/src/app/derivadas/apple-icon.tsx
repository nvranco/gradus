import { ImageResponse } from "next/og"

import { fuenteNotoSerif, MarcaDx } from "./marca-dx"

// Lo que iOS se lleva al "Agregar a inicio".
//
// Safari NO lee el manifest para eso: agarra el `apple-touch-icon` de la página
// que está abierta y el `apple-mobile-web-app-title` del `<head>`. O sea que
// tener los íconos declarados en derivadas.webmanifest alcanza para Android y
// no para iPhone, que es de donde viene la mitad del juego. Sin este archivo, el
// juego se instalaba con el ícono "int" de Intervalo.
//
// A sangre y sin esquinas redondeadas: iOS le aplica su propia máscara, y un
// PNG que ya viene con las esquinas transparentes se ve más chico que el resto
// de la grilla.

export const size = { width: 180, height: 180 }
export const contentType = "image/png"

export default async function AppleIcon() {
  return new ImageResponse(<MarcaDx lado={size.width} />, {
    ...size,
    fonts: [
      { name: "Noto Serif", data: await fuenteNotoSerif(), weight: 600, style: "normal" },
    ],
  })
}
