import type { ReactNode } from "react"
import type { Platform } from "@/lib/platform/detect"
import { MoreVerticalIcon, ShareIcon, SquarePlusIcon } from "lucide-react"

// `text` es ReactNode y no string para poder resaltar el nombre exacto del
// control que hay que tocar (ver «Compartir»).
export type Step = { text: ReactNode; icon?: ReactNode }

// Lo que hay que buscar en la pantalla del navegador: el nombre del control, sin
// las comillas, que son puntuación de la oración y no están en ningún menú.
// Solo negrita: el índigo lo tiene "pantalla de inicio" en el encabezado del
// pane, y repetirlo cuatro veces más abajo dejaba de destacar nada.
function Control({ children }: { children: ReactNode }) {
  return <strong className="font-semibold">{children}</strong>
}

const ICON_CLS = "size-4"

// Último paso, común a mobile: sin volver a abrirla desde el inicio el usuario
// se queda en la pestaña del navegador y cree que no pasó nada. En escritorio no
// aplica, ahí la instalación ya deja la app abierta.
//
// Nombra el producto porque son DOS: el minijuego se instala como app aparte,
// con su propio ícono y su propio nombre en la pantalla de inicio (ver
// public/derivadas.webmanifest). Decirle "abrí Intervalo" a quien acaba de
// agregar "dx" lo manda a buscar un ícono que puede no tener.
const reopenStep = (producto: string): Step => ({
  text: `Cerrá tu navegador y abrí ${producto} desde tu pantalla de inicio.`,
})

const PLATFORM_STEPS: Record<Platform, Step[]> = {
  ios: [
    {
      text: (
        <>
          Tocá el botón <Control>Compartir</Control>
        </>
      ),
      icon: <ShareIcon className={ICON_CLS} />,
    },
    {
      // El share sheet de iOS muestra ese ícono a la derecha de la fila; el
      // menú de Chrome en Android no, así que ahí el paso va sin ícono.
      text: (
        <>
          Elegí «<Control>Agregar a inicio</Control>»
        </>
      ),
      icon: <SquarePlusIcon className={ICON_CLS} />,
    },
    {
      text: (
        <>
          Confirmá tocando «<Control>Agregar</Control>».
        </>
      ),
    },
  ],
  android: [
    {
      text: (
        <>
          Abrí el <Control>menú</Control>
        </>
      ),
      icon: <MoreVerticalIcon className={ICON_CLS} />,
    },
    {
      text: (
        <>
          Elegí «<Control>Agregar a la pantalla principal</Control>».
        </>
      ),
    },
    {
      text: (
        <>
          Confirmá tocando «<Control>Agregar</Control>».
        </>
      ),
    },
  ],
  desktop: [
    {
      text: "En Chrome o Edge, tocá el ícono de instalar en la barra de direcciones y confirmá.",
    },
  ],
}

// Los pasos como dato: quien los muestra decide la maqueta. Hoy el único que los
// dibuja es install-hint-pane.tsx, como párrafos numerados.
export function getInstallSteps(platform: Platform, producto = "Intervalo"): Step[] {
  const steps = PLATFORM_STEPS[platform]
  return platform === "desktop" ? steps : [...steps, reopenStep(producto)]
}
