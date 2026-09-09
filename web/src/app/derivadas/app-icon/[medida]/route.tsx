import { ImageResponse } from "next/og"

import { fuenteNotoSerif, MarcaDx } from "../../marca-dx"

// Los PNG del manifest, que es lo que usa Android al instalar el juego.
//
// Son una ruta y no archivos en `public/`: la marca ya se dibuja en JSX para el
// favicon y para iOS, y tener además tres PNG generados por un script obliga a
// acordarse de volver a correrlo cada vez que se toca el dibujo. Acá los tres
// tamaños salen del mismo componente, así que no pueden quedar viejos.
//
// `maskable` va a sangre y un poco más chico: el lanzador de Android puede
// recortar el ícono hasta un círculo del 80% del lado, y al 100% la barra de
// colores queda justo pisando ese borde.

export const dynamic = "force-static"

const MEDIDAS = {
  "192": { lado: 192, redondeado: true, escala: 1 },
  "512": { lado: 512, redondeado: true, escala: 1 },
  maskable: { lado: 512, redondeado: false, escala: 0.8 },
} as const

export function generateStaticParams() {
  return Object.keys(MEDIDAS).map((medida) => ({ medida }))
}

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ medida: string }> },
) {
  const { medida } = await params
  const conf = MEDIDAS[medida as keyof typeof MEDIDAS]
  if (!conf) return new Response("no existe", { status: 404 })

  return new ImageResponse(
    <MarcaDx lado={conf.lado} redondeado={conf.redondeado} escala={conf.escala} />,
    {
      width: conf.lado,
      height: conf.lado,
      fonts: [
        { name: "Noto Serif", data: await fuenteNotoSerif(), weight: 600, style: "normal" },
      ],
    },
  )
}
