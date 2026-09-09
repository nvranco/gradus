"use client"

// La diapo que ofrece los recordatorios del juego.
//
// Solo aparece dentro de la app instalada (ver `puedeOfrecerNotificaciones`), y
// por eso es el segundo escalón: primero `pedido-instalar.tsx`, que en iOS es
// literalmente el prerrequisito —el push web no existe fuera de la pantalla de
// inicio—.
//
// A diferencia de la de instalar, esta SÍ pide algo: un permiso del sistema, que
// se quema para siempre si dicen que no. Por eso tiene botón de color, cuenta
// regresiva antes de poder saltearla y consume el cooldown compartido, como el
// cafecito y como reclutas.
//
// El copy es el de Intervalo («Activá los recordatorios»), con el horario elegido
// en la misma pantalla en vez de en una pantalla de ajustes: es el único dato que
// hace falta y preguntarlo después significa no preguntarlo nunca.

import { useEffect, useState } from "react"
import { BellIcon, ClockIcon } from "lucide-react"
import posthog from "posthog-js"

import { cn } from "@/lib/utils"

import {
  DEFAULT_REMINDER_TIME,
  REMINDER_TIME_OPTIONS,
  usePrenderAvisos,
} from "./UseAvisosDelJuego"
import { CLASE_ACCION_EN_EL_PIE, claseDeSalida, Salida, useCuentaRegresiva } from "./slide-salida"

// Lo mismo que espera la diapo de reclutas: el tiempo de leer antes de poder
// saltear. Ocho segundos y no diez porque acá hay menos para leer.
const ESPERA_S = process.env.NODE_ENV === "development" ? 0 : 8

const INDIGO = "#7e80f7"

export function PedidoNotificaciones({
  onContinue,
  slotSalida,
  slotAccion,
  fullBleed = false,
  className,
}: {
  onContinue: () => void
  slotSalida?: HTMLElement | null
  slotAccion?: HTMLElement | null
  fullBleed?: boolean
  className?: string
}) {
  const [hora, setHora] = useState(DEFAULT_REMINDER_TIME)
  const prender = usePrenderAvisos({ onSuccess: onContinue })
  const restante = useCuentaRegresiva(ESPERA_S)
  const listo = restante === 0

  useEffect(() => {
    posthog.capture("game_notify_hint_shown")
  }, [])

  const accion = (
    <button
      type="button"
      disabled={prender.isPending}
      onClick={() => prender.mutate(hora)}
      className={cn(
        slotAccion ? CLASE_ACCION_EN_EL_PIE : "mt-4 flex w-full items-center justify-center gap-2 rounded-md px-4 py-3 text-base font-semibold",
        "text-white disabled:opacity-60",
      )}
      style={{ backgroundColor: INDIGO }}
    >
      <BellIcon className="size-5" />
      {prender.isPending ? "Activando…" : "Activar"}
    </button>
  )

  return (
    <div
      className={cn(
        "flex min-h-0 flex-1 flex-col justify-center p-6 text-center",
        !fullBleed && "rounded-lg border",
        className,
      )}
      style={
        fullBleed
          ? undefined
          : {
              backgroundColor: `color-mix(in oklab, ${INDIGO} 12%, var(--card))`,
              borderColor: `color-mix(in oklab, ${INDIGO} 45%, transparent)`,
            }
      }
    >
      <div className="mx-auto w-full max-w-sm">
        <div className="mx-auto w-fit" style={{ color: INDIGO }}>
          <BellIcon className="size-8" />
        </div>
        <p className="mt-2 text-2xl font-medium">Activá los recordatorios</p>
        <p className="mt-3 leading-relaxed text-foreground/70">
          Te van a llegar a la hora que elijas: cuando alguien te pase en el
          ranking, cuando haya cafecito para tu universidad, o simplemente para
          no perder el ritmo.
        </p>

        {/* El horario se elige ACÁ y no después en configuración. Es el único
            dato que hace falta, y preguntarlo en otra pantalla significa no
            preguntarlo nunca: nadie entra a ajustes a completar algo que no
            sabe que quedó a medias. */}
        <label className="mt-5 flex items-center justify-center gap-2 text-foreground/70">
          <ClockIcon className="size-4" />
          <span>Recordarme a las</span>
          <select
            value={hora}
            onChange={(e) => setHora(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-foreground"
          >
            {REMINDER_TIME_OPTIONS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        {prender.isError && (
          <p className="mt-3 text-sm text-foreground/60">
            No se pudo activar. Puede que el navegador tenga las notificaciones
            bloqueadas.
          </p>
        )}

        {!slotAccion && accion}
      </div>

      {slotAccion && <Salida slot={slotAccion}>{accion}</Salida>}

      <Salida slot={slotSalida}>
        <button
          type="button"
          disabled={!listo}
          onClick={onContinue}
          className={claseDeSalida(Boolean(slotSalida))}
        >
          {listo ? "Ahora no" : `Ahora no (${restante})`}
        </button>
      </Salida>
    </div>
  )
}
