"use client"

// La diapo que ofrece agregar el juego a la pantalla de inicio.
//
// Es la única de las que interrumpen que NO pide nada: no hay plata, ni un
// mensaje que mandar, ni una cuenta que crear. Por eso tampoco tiene botón de
// color ni cuenta regresiva —las dos cosas existen para que un pedido se llegue
// a leer antes de poder saltearlo, y acá no hay nada que decidir—: se lee y se
// sale de un toque.
//
// La acción de verdad pasa AFUERA de la app, en el menú del navegador, así que
// lo único que puede hacer esta pantalla es explicar cómo. Los pasos son los de
// Intervalo (`InstallHintPane`), no una copia: cambian por sistema operativo y
// tenerlos en dos lugares es la forma segura de que uno quede viejo.
//
// Antes esto era un LINK adentro de la diapo de registro (`InstalarLinea`), que
// sale en la derivada 12: ahí sigue el 15% de la cohorte. Como diapo propia en
// la 5 le habla al 46%.

import { useEffect } from "react"
import posthog from "posthog-js"

import { InstallHintPane } from "@/components/install-hint-pane"
import { cn } from "@/lib/utils"
import { getPlatform, isStandalone, usePlatform } from "@/lib/platform/detect"

import { claseDeSalida, Salida } from "./slide-salida"

/** El nombre con el que el juego queda en la pantalla de inicio.
 *  Mantener igual al `short_name` de public/derivadas.webmanifest y al
 *  `appleWebApp.title` de layout.tsx: es el ícono que la persona va a buscar. */
export const NOMBRE_INSTALADO = "dx"

/** ¿Tiene sentido ofrecerlo en este aparato?
 *
 * No es un hook porque lo llama el ladder de las dos plataformas, que corre
 * dentro de un callback y no en el cuerpo de un componente. Las tres condiciones
 * importan:
 *   · en escritorio no se ofrece: no es de donde viene esta gente, ni donde un
 *     ícono en la pantalla de inicio significa algo;
 *   · ya instalada, no hay nada que ofrecer —y es además el "ya accionó" que
 *     apaga el pedido para siempre sin necesidad de anotar nada—;
 *   · en el servidor no hay `navigator`, así que esto solo se llama desde el
 *     cliente. */
export function puedeOfrecerInstalar(): boolean {
  if (typeof window === "undefined") return false
  return getPlatform() !== "desktop" && !isStandalone()
}

export function PedidoInstalar({
  onContinue,
  slotSalida,
  fullBleed = false,
  className,
}: {
  onContinue: () => void
  slotSalida?: HTMLElement | null
  /** Gemelo del de cafecito-panel.tsx: solo lo manda el teléfono, donde el
   *  fondo lo pinta la pantalla entera. En escritorio sigue siendo la card. */
  fullBleed?: boolean
  className?: string
}) {
  const platform = usePlatform()

  useEffect(() => {
    posthog.capture("game_install_hint_shown", { platform })
    // Una sola vez por aparición: `platform` se resuelve en el primer efecto y
    // no vuelve a cambiar mientras la diapo está montada.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col justify-center p-6",
        !fullBleed && "rounded-lg border",
        className,
      )}
    >
      <InstallHintPane
        producto={NOMBRE_INSTALADO}
        descripcion={
          <>
            Agregá {NOMBRE_INSTALADO} a tu{" "}
            {/* chart-5 y no primary: el mismo índigo pero más claro, que sobre
                el fondo oscuro resalta bastante más. Igual que en Intervalo. */}
            <strong className="font-semibold text-chart-5">
              pantalla de inicio
            </strong>{" "}
            para tener una mejor experiencia y poder establecer recordatorios
            para practicar.
          </>
        }
      />

      <Salida slot={slotSalida}>
        <button
          type="button"
          onClick={() => {
            posthog.capture("game_install_hint_dismiss", { platform })
            onContinue()
          }}
          className={claseDeSalida(Boolean(slotSalida))}
        >
          Entendido
        </button>
      </Salida>
    </div>
  )
}
