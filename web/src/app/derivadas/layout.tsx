import type { Metadata } from "next"

import { appleStartupImages } from "@/lib/ios-splash"

// Metadata propia del minijuego: el link se comparte masivamente por WhatsApp
// y el preview (title/description/OG) es parte del gancho. La imagen OG la
// resuelve el archivo opengraph-image.png de esta carpeta.

// El texto del preview, uno solo para los tres lugares: la pestaña, WhatsApp y
// Twitter tienen que decir exactamente lo mismo, y con tres literales sueltos la
// forma segura de que dejen de coincidir era editar uno.
//
// Dos oraciones separadas por un salto de línea: la primera dice qué es y para
// qué sirve, la segunda invita. Juntas en un párrafo, la invitación se leía como
// la cola de la explicación.
//
// Ojo con el salto: WhatsApp lo respeta en la descripción del preview, pero no
// todos los clientes lo hacen —varios colapsan los blancos del `og:description`
// y muestran las dos oraciones seguidas—. El texto está escrito para que se
// entienda igual en una sola línea.
const DESCRIPCION =
  "Memorizá todas las derivadas y llegá mejor preparado a tus parciales con este minijuego.\n¡Vení a bancar a tu universidad!"

// Solo la marca, también en el preview. La pestaña es un renglón de 15
// caracteres y "· Derivadas" se comía la mitad sin decir nada que el ícono no
// diga ya; en la tarjeta de WhatsApp pasa lo mismo con el logo al lado, que ya
// es el operador de derivada.
const TITULO = "Intervalo"

export const metadata: Metadata = {
  title: TITULO,
  description: DESCRIPCION,
  // Manifest PROPIO del minijuego, que pisa al de la app para esta ruta.
  //
  // El de la raíz (app/manifest.ts) tiene `start_url: "/"`, así que quien
  // agregaba el juego a su pantalla de inicio se quedaba con un ícono que abría
  // Intervalo: instalaba una cosa y le quedaba otra. Este arranca en
  // `/derivadas`.
  //
  // Es un archivo estático en `public/` y no otro `manifest.ts`: la convención
  // de archivo de Next solo vale en la raíz de `app`, un manifest por ruta se
  // enlaza con este campo.
  //
  // Sin `scope`: el default lo deriva de `start_url` sacándole el último
  // segmento, o sea "/", que es lo que ya había. Acotarlo a "/derivadas" haría
  // que cualquier link fuera del juego saliera del contenedor instalado, y no
  // hay motivo para estrenar ese comportamiento acá.
  //
  // El `id` distinto es lo que hace que el navegador las trate como dos apps y
  // no pise una instalación con la otra.
  manifest: "/derivadas.webmanifest",
  // iOS NO lee el manifest para "Agregar a inicio": se lleva el
  // `apple-touch-icon` de la página abierta (lo pone apple-icon.tsx) y este
  // título. Sin esto el juego quedaba en la pantalla de inicio llamándose
  // "Intervalo", al lado del Intervalo de verdad y con el mismo nombre.
  //
  // Va el objeto entero y no solo `title` porque la metadata de una ruta
  // REEMPLAZA la del layout raíz campo por campo: dejando solo el título se
  // perdían `capable` (que es lo que hace que abra sin barra de Safari) y las
  // pantallas de arranque. Las splash son un navy liso sin logo, así que sirven
  // igual para los dos productos.
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "dx",
    startupImage: appleStartupImages,
  },
  openGraph: {
    type: "website",
    url: "https://www.intervalo.xyz/derivadas",
    title: TITULO,
    description: DESCRIPCION,
  },
  twitter: {
    card: "summary_large_image",
    title: TITULO,
    description: DESCRIPCION,
  },
}

export default function GameLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // `data-game` es lo que engancha la regla de globals.css que le saca la serif
  // a los títulos: acá adentro todo el texto es la misma sans.
  return (
    <div data-game className="min-h-dvh bg-background">
      {children}
    </div>
  )
}
