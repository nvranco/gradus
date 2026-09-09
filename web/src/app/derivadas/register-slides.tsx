"use client"

// Slides de registro del minijuego, en el orden que pide el producto:
// hito 1 (usuario enganchado) → carrera y universidad, IDÉNTICAS a las del
// onboarding (components/onboarding-fields.tsx); hito 2 → registro con Google
// con el gancho de elegir tu @username. Todo skippeable ("Ahora no").

import { useEffect, useRef, useState } from "react"
import { useSignIn } from "@clerk/nextjs"
import { useQueryClient } from "@tanstack/react-query"
import posthog from "posthog-js"
import { Button } from "@/components/ui/button"
import { CareerSelect, UniversityGrid } from "@/components/onboarding-fields"
import { readOnboarding, saveOnboarding } from "@/lib/onboarding/storage"
import { canonicalUniversity } from "@/lib/university-tags"
import { cn } from "@/lib/utils"
import { useSfx } from "@/lib/audio/useSfx"
import { ApiError, unwrap } from "@/lib/api/client"
import { ArrowUp } from "lucide-react"
import { XpDots } from "@/components/xp-dots"
import { ALL_SCOPE } from "@/components/leaderboard-chrome"
import { VERDE } from "./cafecito-cta"
import { KeyCap } from "./exercise-card"
import { colorDeCafe, levelColor } from "./game-colors"
import { Salida, claseDeSalida } from "./slide-salida"
import { SlideFlip } from "./slide-flip"
import { SlideHorizontal } from "./slide-horizontal"
import { enCampoDeTexto, useTeclas } from "./teclas"
import { useGameApi } from "./UseGameApi"
import { useGameLeaderboard, useGameRecruits } from "./UseGameLeaderboard"
import { gameKeys, type GamePlayer } from "./UseGamePlayer"

const ctaCls =
  "h-[var(--cta-h)] w-full rounded-md bg-white text-black hover:bg-white/90 hover:text-black"

// Las slides están dibujadas para una columna de teléfono (`max-w-md`, el mismo
// ancho del onboarding). En el panel de escritorio, que es bastante más ancho,
// hay que acotarlas y centrarlas: si no, la grilla 2×2 de carreras y los chips
// de universidad se estiran y quedan deformes.
const panelCls = "mx-auto flex min-h-0 w-full max-w-md flex-1 flex-col gap-6"
const bodyCls = "flex min-h-0 flex-1 flex-col justify-center overflow-y-auto py-6"

// El ámbar del café, en su punto MÁS ENCENDIDO — el mismo con el que se ve la
// barra del slider de cafecitos cuando está al máximo (`colorPara`/
// `colorDeCafe` en 1, ver cafecito-panel.tsx/game-colors.ts). No el `AMBAR`
// fijo de game-colors.ts (el punto MEDIO de esa rampa): acá hace falta el
// extremo más vivo, no el del medio.
const AMBAR_MAXIMO = colorDeCafe(1)

const DESIRED_ALIAS_KEY = "intervalo:game:desired-alias"

export function readDesiredAlias(): string | null {
  try {
    return window.localStorage.getItem(DESIRED_ALIAS_KEY)
  } catch {
    return null
  }
}

export function clearDesiredAlias() {
  try {
    window.localStorage.removeItem(DESIRED_ALIAS_KEY)
  } catch {}
}

// Persistir carrera/universidad: al jugador (backend) y como prefill del
// onboarding de Intervalo (localStorage, solo si no había nada — semántica
// register_once). El puente es pasivo: el juego no linkea a Intervalo.
export function ProfileSlides({
  onDone,
  onSkip,
  slotSalida,
}: {
  onDone: (data: { career: string; university: string }) => void
  onSkip: () => void
  // Dónde dibujar los botones de cada pantalla: el pie de la columna, AFUERA
  // de la caja — misma idea que cafecito/reclutas, el registro desde
  // Configuración y "Elegí tu @" (slide-salida.tsx), para que la caja mida lo
  // mismo que la del ejercicio en vez de comerse la columna entera. Solo lo
  // manda `desktop-layout.tsx`; en el teléfono los botones se quedan adentro,
  // donde siempre estuvieron.
  slotSalida?: HTMLElement | null
}) {
  const sfx = useSfx()
  const api = useGameApi()
  const [phase, setPhase] = useState<"career" | "university">("career")
  const [career, setCareer] = useState("")
  const [university, setUniversity] = useState("")
  const [universityOther, setUniversityOther] = useState("")
  const [showOther, setShowOther] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const [saving, setSaving] = useState(false)

  const finish = async (chosenUniversity: string) => {
    if (saving) return
    setSaving(true)
    sfx.continue()
    try {
      await api.PATCH("/game/derivemos/me", {
        body: { career, university: chosenUniversity },
      })
    } catch {
      // Sin drama: el juego sigue; la próxima vuelta lo reintenta.
    }
    if (readOnboarding() === null && career && chosenUniversity) {
      saveOnboarding({
        name: "",
        career,
        university: chosenUniversity,
        course: "analisis",
      })
    }
    posthog.capture("game_register_completed", { slide: "profile" })
    onDone({ career, university: chosenUniversity })
  }

  const confirmOther = () => {
    const value = canonicalUniversity(universityOther)
    if (!value) return
    void finish(value)
  }

  // Las dos preguntas del perfil son dos pantallas, y cambiar de pantalla en
  // este juego es el pase de cada aparato. Antes la segunda reemplazaba a la
  // primera sin más y parecía que la página se hubiera recargado sola.
  //
  // Y es el pase DEL APARATO, no siempre el volteo: estas dos caras viven
  // adentro de UNA sola diapo del teléfono, así que el `slideSeq` de
  // mobile-flow.tsx no cambia entre carrera y universidad y el deslizamiento de
  // afuera no ocurre. Con `SlideFlip` en los dos lados, «¿Dónde?» era la única
  // pantalla del teléfono que aparecía con un fundido en medio de un juego que
  // se mueve entero de costado. `slotSalida` —que ya distingue escritorio en
  // todo este archivo— elige cuál va.
  //
  // Los botones viven AFUERA del `SlideFlip`, y no —como antes— uno por fase
  // adentro de cada cara: `AnimatePresence` cruza las dos caras durante la
  // transición (las dos están montadas a la vez, ver slide-flip.tsx), así que
  // con un botón por cara y `slotSalida` puesto, las dos intentarían
  // portalizar al MISMO nodo del pie a la vez y se verían superpuestas un
  // instante. Afuera, el botón no cruza con nada: cambia de golpe con
  // `phase`, que es del padre y no de la cara.
  const cara =
    phase === "career" ? (
      <div className={bodyCls}>
        <CareerSelect
          value={career}
          onSelect={(v) => {
            sfx.select()
            setCareer(v)
          }}
        />
      </div>
    ) : (
      <div className={bodyCls}>
        <UniversityGrid
          university={university}
          showOther={showOther}
          otherValue={universityOther}
          onOtherChange={setUniversityOther}
          onPick={(u) => {
            sfx.select()
            setUniversity(u)
            setShowOther(false)
          }}
          onSelectOther={() => {
            sfx.select()
            setUniversity("")
            setShowOther(true)
          }}
          onConfirmOther={confirmOther}
          onPickSuggestion={(key) => {
            sfx.select()
            setUniversityOther(key)
            inputRef.current?.focus()
          }}
          inputRef={inputRef}
        />
      </div>
    )

  return (
    <div className={panelCls}>
      {slotSalida ? (
        <SlideFlip slide={phase} className="flex min-h-0 flex-1 flex-col">
          {cara}
        </SlideFlip>
      ) : (
        // Sin `flex`: `SlideHorizontal` es una grilla de una celda (así apila
        // las dos caras en el mismo lugar sin sacarlas del flujo), y ponerle
        // `flex` acá le pisaría el `display`.
        <SlideHorizontal llave={phase} className="min-h-0 flex-1">
          {cara}
        </SlideHorizontal>
      )}
      <Salida slot={slotSalida}>
        {/* En el pie (escritorio), Continuar y Ahora no van UNO AL LADO DEL
            OTRO —la misma fila que Revisar/¿Por qué?/Saltear en el
            ejercicio—, con Continuar quedándose con el ancho que sobra. En el
            teléfono (sin `slotSalida`) siguen apilados, como siempre. */}
        <div className={slotSalida ? "flex w-full items-stretch gap-2" : "flex flex-col gap-2"}>
          {phase === "career" ? (
            <Button
              size="lg"
              className={cn(ctaCls, slotSalida && "flex-1")}
              disabled={!career}
              onClick={() => {
                sfx.continue()
                posthog.capture("game_register_slide_shown", { slide: "university" })
                setPhase("university")
              }}
            >
              Continuar
            </Button>
          ) : showOther ? (
            <Button
              size="lg"
              className={cn(ctaCls, slotSalida && "flex-1")}
              disabled={!universityOther.trim() || saving}
              onClick={confirmOther}
            >
              Continuar
            </Button>
          ) : (
            <Button
              size="lg"
              className={cn(ctaCls, slotSalida && "flex-1")}
              disabled={!university || saving}
              onClick={() => void finish(university)}
            >
              Continuar
            </Button>
          )}
          <button
            type="button"
            onClick={onSkip}
            className={
              slotSalida
                ? cn(claseDeSalida(true), "w-auto shrink-0")
                : "py-2 text-sm text-muted-foreground"
            }
          >
            Ahora no
          </button>
        </div>
      </Salida>
    </div>
  )
}

function GoogleIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden>
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
      <path fill="#FBBC05" d="M5.84 14.1c-.22-.66-.35-1.36-.35-2.1s.13-1.44.35-2.1V7.07H2.18A10.97 10.97 0 0 0 1 12c0 1.77.43 3.45 1.18 4.93l3.66-2.83z" />
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z" />
    </svg>
  )
}

// Resaltado de "esta fila sos vos": el mismo que usa game-ranking.tsx (no
// exportado de ahí, así que se repite el mismo literal en vez de importarlo).
const MINE_ROW_CLASS = "bg-primary/10 ring-primary/30"

/** La fila propia del ranking, en miniatura: mismo formato que una fila de
 *  verdad (game-ranking.tsx :: Row) —puesto, @ con el color de su nivel,
 *  flecha de cuánto subió, XP— para que esta pantalla muestre el progreso
 *  con el mismo lenguaje visual que el ranking de al lado, en vez de
 *  repetirlo en prosa. */
function FilaPropia({
  player,
  delta,
}: {
  player: GamePlayer
  // El mismo `rank_delta` que ya trae la fila de verdad (game-ranking.tsx),
  // no una cuenta propia: se probó restando contra `climbFrom` —lo que subió
  // ESTE acierto puntual— y daba un número menor al de la fila real, que
  // acumula desde el último pulso. Dos números "cuánto subiste" distintos en
  // la misma pantalla es peor que uno solo, así que esta fila lee la MISMA
  // fuente en vez de inventar la propia (ver el `useGameLeaderboard` en
  // `RegisterSlide`).
  delta: number
}) {
  const rank = player.rank ?? null
  return (
    <ul className="w-full max-w-xs">
      <li className={cn("flex items-center gap-3 rounded-lg px-4 py-3 ring-1 ring-foreground/10", MINE_ROW_CLASS)}>
        {rank !== null && (
          <span className="w-4 shrink-0 text-center text-sm font-semibold tabular-nums text-muted-foreground">
            {rank}
          </span>
        )}
        <span
          className="min-w-0 flex-1 truncate text-left text-sm font-medium"
          style={{ color: levelColor(player.level) }}
        >
          {player.alias}
        </span>
        {delta > 0 && (
          <span
            className="inline-flex shrink-0 items-center gap-0.5 text-xs font-medium tabular-nums text-green-400"
            aria-label={`subió ${delta} puestos`}
          >
            <ArrowUp size={12} />
            {delta}
          </span>
        )}
        <span className="inline-flex shrink-0 items-center gap-1 text-sm font-semibold tabular-nums">
          {player.xp}
          <XpDots className="size-[0.85em]" />
        </span>
      </li>
    </ul>
  )
}

// Cuánto mide la ventana del login: ni tan chica que Google recorte su propio
// diseño, ni tan grande que parezca la pestaña principal escondiéndose atrás.
const VENTANA_ANCHO = 480
const VENTANA_ALTO = 640

/** Abre la ventanita del login, centrada sobre esta. `null` si el navegador la
 *  bloqueó — ahí quien llama sigue con el redirect de toda la vida.
 *
 *  Tiene que llamarse SIN ningún `await` antes, en el mismo gesto del click:
 *  los navegadores solo dejan abrir una ventana nueva sin bloquearla dentro
 *  del stack síncrono de un gesto del usuario. */
function abrirVentanaDeGoogle(): Window | null {
  const left = window.screenX + Math.max(0, (window.outerWidth - VENTANA_ANCHO) / 2)
  const top = window.screenY + Math.max(0, (window.outerHeight - VENTANA_ALTO) / 2)
  return window.open(
    "",
    "intervalo-google",
    `width=${VENTANA_ANCHO},height=${VENTANA_ALTO},left=${left},top=${top}`,
  )
}

// Hito 2: registro con Google. El gancho es el @ propio: el guest ve su alias
// autogenerado y el input para elegir el definitivo; el alias deseado queda en
// localStorage y se aplica al volver del OAuth (ver applyDesiredAlias).
//
// Y si además ya reclutó a alguien, el gancho cambia por uno mucho más fuerte:
// la XP que sus reclutas le vienen dando. Es la diferencia entre pedir fe —
// «registrate para elegir tu nombre»— y cobrar una deuda que ya existe y tiene
// número. La diapo de reclutar sale a las diez resueltas y esta a las doce, así
// que quien compartió y le funcionó llega acá con algo concreto que perder.
export function RegisterSlide({
  player,
  onSkip,
  onOpenPrivacy,
  desdeConfiguracion = false,
  slotSalida,
  popup = false,
  keyboard = false,
}: {
  player: GamePlayer
  onSkip: () => void
  onOpenPrivacy?: () => void
  // Se llega acá también tocando "Usuario" en Configuración, para un invitado
  // que no puede elegir su @ sin cuenta (settings-panel.tsx). Ahí la persona
  // ya sabe quién es —lo está mirando del otro lado, en el ranking— así que
  // repetir "sos fulano, puesto tanto" es ruido, y sobra "Ahora no": la
  // tuerca de la cabecera, siempre a la vista, ya es la vuelta atrás. El botón
  // de Google y el link de privacidad se quedan adentro de la caja, como
  // siempre; lo que cambia es el pie (ver `slotSalida`).
  desdeConfiguracion?: boolean
  // Dónde dibujar el botón que cierra esta pantalla: el pie de la columna,
  // AFUERA de la caja — la misma idea que cafecito/reclutas
  // (slide-salida.tsx), para que la caja mida lo mismo que la del ejercicio
  // en vez de comerse la columna entera. Con `desdeConfiguracion`, ese botón
  // dice "Guardar" y hace lo mismo que el de Google de más arriba; en la
  // variante de hito dice "Ahora no". Solo lo manda `desktop-layout.tsx` —en
  // el teléfono todo se queda adentro de la caja, como siempre.
  slotSalida?: HTMLElement | null
  // Solo escritorio: en vez de irse de la pestaña a Google y volver, el login
  // corre en una ventanita aparte y esta pestaña no se mueve de `/derivadas`
  // en ningún momento. En el teléfono no hay ventanas que abrir, así que ahí
  // se sigue con el redirect de toda la vida (ver `autenticarConVentana`).
  popup?: boolean
  // Atajos de teclado, solo escritorio (mismo criterio que cafecito/reclutas):
  // Enter dispara "Continuar con Google", Alt+Enter dispara "Ahora no". Nunca
  // se manda junto con `desdeConfiguracion` — ahí no hay "Ahora no" y "Guardar"
  // no tiene atajo propio.
  keyboard?: boolean
}) {
  const { signIn } = useSignIn()
  const api = useGameApi()
  const teclas = useTeclas()
  const queryClient = useQueryClient()

  // El mismo `rank_delta` que ya trae la fila de verdad del ranking
  // individual sin acotar (game-ranking.tsx), para que `FilaPropia` diga
  // exactamente lo mismo que la fila de al lado y no una cuenta propia con
  // otro criterio. Misma clave de caché que ese ranking (`ALL_SCOPE` en las
  // dos), así que si ya está cargado esto no pide nada nuevo al servidor.
  const miEntrada = useGameLeaderboard(
    { university: ALL_SCOPE, career: ALL_SCOPE },
    true,
  ).data?.pages[0]?.entries.find((e) => e.is_current_player)
  const [desired, setDesired] = useState("")
  const [authPending, setAuthPending] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)
  const [savingAlias, setSavingAlias] = useState(false)

  // Del mismo caché que usan la diapo de reclutar y el ranking: si ya se pidió
  // en esta sesión, esto no suma ni un pedido.
  const { data } = useGameRecruits(true)
  const entries = data?.entries ?? []
  const xp = entries.reduce((total, r) => total + r.xp_given, 0)
  // Solo cuando hay algo que cobrar. Con reclutas que todavía no aportaron
  // nada, «ya te dieron 0 XP» sería peor que no decir nada.
  const reclutas = xp > 0 ? { xp, gente: entries.length } : null

  // "Guardar", para quien no quiere registrarse pero sí cambiar el @: es el
  // mismo PATCH que ya usa `settings-panel.tsx`, no el registro con Google.
  //
  // El backend lo resuelve solo (`patch_me`, backend/game/router.py): un
  // invitado que TODAVÍA no gastó su única edición gratis (`alias_is_generated`)
  // guarda igual que cualquiera; si ya la gastó, devuelve 403 y ahí el error
  // señala lo que el botón de Google, siempre a la vista al lado, resuelve.
  async function guardarAlias() {
    if (savingAlias) return
    const value = desired.trim()
    if (!value || value === player.alias) {
      onSkip()
      return
    }
    setSavingAlias(true)
    setAuthError(null)
    try {
      const updated = unwrap(await api.PATCH("/game/derivemos/me", { body: { alias: value } }))
      queryClient.setQueryData(gameKeys.me, updated)
      // El ranking de al lado también muestra el @: sin esto seguiría con el
      // viejo hasta el próximo refresco por su cuenta.
      queryClient.invalidateQueries({ queryKey: gameKeys.leaderboard })
      posthog.capture("game_alias_edited", { via: "settings_register" })
      onSkip()
    } catch (err) {
      setAuthError(
        err instanceof ApiError ? err.message : "No se pudo guardar.",
      )
    }
    setSavingAlias(false)
  }

  // Misma coreografía que el wizard (create + sso), con el retorno apuntando
  // al juego: /sso-callback?next=/derivadas y de ahí de vuelta acá.
  //
  // Con `popup`, la única diferencia es ESA ventana: `sso()` acepta una
  // (`SignInFutureSSOParams.popup`) y navega A ELLA en vez de a esta pestaña,
  // así que `/sso-callback` corre adentro de la ventanita —ahí se cierra sola
  // (ver sso-callback/page.tsx)— y esta pestaña nunca se mueve de `/derivadas`.
  // Si el navegador bloqueó la ventana, sigue el redirect de toda la vida.
  async function authenticateWithGoogle() {
    if (!signIn || authPending) return
    setAuthPending(true)
    setAuthError(null)
    posthog.capture("game_register_slide_shown", { slide: "google_tap" })

    // Antes que nada y sin ningún `await` en el medio: `window.open` solo
    // escapa al bloqueador de ventanas emergentes dentro del gesto síncrono
    // del click.
    const ventana = popup ? abrirVentanaDeGoogle() : null

    try {
      const cleaned = desired.trim().toLowerCase().replace(/^@/, "")
      if (cleaned) window.localStorage.setItem(DESIRED_ALIAS_KEY, cleaned)
    } catch {}

    const origin = window.location.origin
    const callbackUrl = `${origin}/sso-callback?next=/derivadas`
    const completeUrl = `${origin}/derivadas`

    const created = await signIn.create({
      strategy: "oauth_google",
      redirectUrl: callbackUrl,
      actionCompleteRedirectUrl: completeUrl,
    })
    if (created.error) return failGoogleSso(created.error)

    const { error } = await signIn.sso({
      strategy: "oauth_google",
      redirectUrl: completeUrl,
      redirectCallbackUrl: callbackUrl,
      ...(ventana ? { popup: ventana } : {}),
    })
    if (error) return failGoogleSso(error)

    if (ventana) {
      // Lo que haya pasado adentro de la ventanita ya corrió: si el login
      // terminó, esto activa la sesión en ESTA pestaña (`finalize`, la señal
      // que usan `useUser`/`useAuth` para enterarse). Si quedó a mitad —la
      // persona cerró la ventana antes de terminar—, no hay nada que activar
      // y el recargado de abajo vuelve a dejar todo como un invitado más.
      if (signIn.status === "complete") {
        await signIn.finalize().catch(() => {})
      }
      window.location.assign("/derivadas")
      return
    }

    if (!signIn.firstFactorVerification.externalVerificationRedirectURL) {
      failGoogleSso({ code: "no_external_verification_redirect" })
    }
  }

  function failGoogleSso(error: { code: string }) {
    // Sesión ya activa: no hay OAuth que correr; recargar alcanza para que el
    // bootstrap linkee al guest con la cuenta.
    if (error.code === "session_exists") {
      window.location.assign("/derivadas")
      return
    }
    console.error("Google SSO error", error)
    setAuthPending(false)
    setAuthError("No pudimos conectar con Google. Probá de nuevo.")
  }

  // Enter dentro del campo del @ lo maneja el propio `input` (ver más abajo):
  // `enCampoDeTexto` lo reconoce como campo de texto a propósito (teclas.ts) y
  // por eso un listener en `document` nunca lo vería. Alt+Enter sí llega acá
  // siempre, esté el campo enfocado o no —igual que Saltear en el ejercicio—,
  // porque nada en un `<input>` de HTML le da un uso especial a esa combinación.
  //
  // Las dos funciones van por ref y no directo en las dependencias —mismo
  // mecanismo que `reclutarRef` en reclutas-panel.tsx—: son closures nuevas en
  // cada render, y sin esto el listener se sacaría y se pondría de nuevo todo
  // el tiempo en vez de vivir una sola vez mientras `keyboard` no cambie.
  const onSkipRef = useRef(onSkip)
  const authenticateWithGoogleRef = useRef(authenticateWithGoogle)
  useEffect(() => {
    onSkipRef.current = onSkip
    authenticateWithGoogleRef.current = authenticateWithGoogle
  })
  useEffect(() => {
    if (!keyboard) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter") return
      if (e.altKey) {
        e.preventDefault()
        onSkipRef.current()
        return
      }
      if (enCampoDeTexto(e.target)) return
      e.preventDefault()
      void authenticateWithGoogleRef.current()
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [keyboard])

  return (
    <div className="mx-auto flex min-h-0 w-full max-w-md flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-4 text-center">
        {desdeConfiguracion ? (
          <h2 className="text-2xl font-bold">Elegí tu @</h2>
        ) : reclutas ? (
          <>
            {/* El número en el título y no en el cuerpo: es una deuda concreta
                que ya existe, y es lo único de esta pantalla que la persona no
                sabía. */}
            <h2 className="text-2xl font-bold">
              Ya te dieron{" "}
              <span className="tabular-nums" style={{ color: VERDE }}>
                {reclutas.xp} XP
              </span>
            </h2>
            <p className="text-sm leading-relaxed text-muted-foreground">
              {reclutas.gente === 1
                ? "Alguien entró por tu link y suma para vos."
                : `${reclutas.gente} personas entraron por tu link y suman para vos.`}
              <br />
              Sin cuenta, todo eso vive solo en este navegador.
            </p>
          </>
        ) : (
          <>
            {/* Ya eligió su @ antes (en "Elegí tu @" o en el registro de
                Configuración) — repetírselo acá sería pisar el gancho de la
                pantalla anterior. Este hito ya no vende el @: vende la cuenta,
                así que el cuerpo cuenta cuánto lleva jugado y qué gana
                registrándose, en vez de quién es. El puesto ya no va en el
                párrafo: lo muestra `FilaPropia`, con el mismo lenguaje visual
                que el ranking de al lado. */}
            <h2 className="text-2xl font-bold">¡Guardá tu progreso!</h2>
            <p className="text-sm leading-relaxed text-muted-foreground">
              Ya llevás <span className="text-foreground">{player.exercises_correct}</span>{" "}
              {player.exercises_correct === 1 ? "derivada resuelta" : "derivadas resueltas"}.
            </p>
            <FilaPropia player={player} delta={miEntrada?.rank_delta ?? 0} />
            <p className="text-sm leading-relaxed text-foreground/90">
              Registrándote podés{" "}
              <span className="font-semibold" style={{ color: VERDE }}>
                reclutar gente
              </span>{" "}
              y llevarte parte de la{" "}
              <span className="font-semibold" style={{ color: VERDE }}>
                XP
              </span>{" "}
              que generen, o{" "}
              <span className="font-semibold" style={{ color: AMBAR_MAXIMO }}>
                donar cafecitos
              </span>{" "}
              para{" "}
              <span className="font-semibold" style={{ color: AMBAR_MAXIMO }}>
                multiplicar el XP
              </span>{" "}
              de tu universidad.
            </p>
          </>
        )}
        {/* El @ ya se eligió antes de llegar acá (en "Elegí tu @" o en el
            registro de Configuración) — en las dos variantes de hito no hay
            nada que elegir, así que el campo no existe. Solo la variante de
            Configuración lo necesita: ahí SÍ se puede estar cambiando el @. */}
        {desdeConfiguracion && (
        <div className="flex w-full max-w-xs items-center gap-1 rounded-md border border-[#7e80f7] bg-white/5 px-3">
          <span className="text-lg text-muted-foreground">@</span>
          <input
            type="text"
            value={desired}
            onChange={(e) =>
              setDesired(e.target.value.toLowerCase().replace(/[^a-z0-9._]/g, ""))
            }
            onKeyDown={(e) => {
              if (keyboard && e.key === "Enter" && !e.altKey) {
                e.preventDefault()
                void authenticateWithGoogle()
              }
            }}
            placeholder={player.alias}
            maxLength={15}
            className="h-[52px] w-full bg-transparent text-foreground outline-none"
          />
        </div>
        )}
        {authError && <p className="text-sm text-orange-300">{authError}</p>}
        {/* Con `slotSalida` (escritorio): en la variante de Configuración el
            de Google se queda adentro —es otra acción, crear la cuenta, no
            una manera distinta de "Guardar"— y solo aparece para quien
            todavía es invitado. En la de hito, el de Google se fue al pie
            (ver `Salida` más abajo): al lado de Ahora no, no adentro de la
            caja. Acá adentro, en esa variante, solo queda el link de
            privacidad. */}
        {slotSalida && (
          <div className="flex w-full max-w-xs flex-col gap-2">
            {desdeConfiguracion && player.is_guest && (
              <Button
                size="lg"
                className={ctaCls}
                disabled={!signIn || authPending}
                onClick={() => void authenticateWithGoogle()}
              >
                <GoogleIcon className="mr-2 size-4" />
                {authPending ? "Conectando…" : "Continuar con Google"}
                {keyboard && <KeyCap>{teclas.enter}</KeyCap>}
              </Button>
            )}
            {onOpenPrivacy && (
              <button
                type="button"
                onClick={onOpenPrivacy}
                className="text-center text-xs leading-relaxed text-foreground/45 underline underline-offset-2 transition-colors hover:text-foreground/70"
              >
                ¿Qué pasa con mis datos?
              </button>
            )}
          </div>
        )}
      </div>
      <Salida slot={slotSalida}>
        {desdeConfiguracion ? (
          <Button
            size="lg"
            className={cn(ctaCls, slotSalida && "w-full")}
            disabled={savingAlias}
            onClick={() => void guardarAlias()}
          >
            {savingAlias ? "Guardando…" : "Guardar"}
          </Button>
        ) : slotSalida ? (
          // Continuar con Google y Ahora no, uno al lado del otro — la misma
          // fila que Revisar/¿Por qué?/Saltear en el ejercicio (y la misma
          // idea que el pie de ProfileSlides, acá arriba): Google se queda
          // con el ancho que sobra, Ahora no mide lo suyo.
          <div className="flex w-full items-stretch gap-2">
            <Button
              size="lg"
              className={cn(ctaCls, "flex-1")}
              disabled={!signIn || authPending}
              onClick={() => void authenticateWithGoogle()}
            >
              <GoogleIcon className="mr-2 size-4" />
              {authPending ? "Conectando…" : "Continuar con Google"}
              {keyboard && <KeyCap>{teclas.enter}</KeyCap>}
            </Button>
            <button
              type="button"
              onClick={onSkip}
              className={cn(claseDeSalida(true), "w-auto shrink-0")}
            >
              Ahora no
              {keyboard && <KeyCap>{teclas.altEnter}</KeyCap>}
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <Button
              size="lg"
              className={ctaCls}
              disabled={!signIn || authPending}
              onClick={() => void authenticateWithGoogle()}
            >
              <GoogleIcon className="mr-2 size-4" />
              {authPending ? "Conectando…" : "Continuar con Google"}
            </Button>
            <button
              type="button"
              onClick={onSkip}
              className="py-2 text-sm text-muted-foreground"
            >
              Ahora no
            </button>
            {onOpenPrivacy && (
              <button
                type="button"
                onClick={onOpenPrivacy}
                className="text-center text-xs leading-relaxed text-foreground/45 underline underline-offset-2 transition-colors hover:text-foreground/70"
              >
                ¿Qué pasa con mis datos?
              </button>
            )}
          </div>
        )}
      </Salida>
    </div>
  )
}

// Al volver del OAuth: el bootstrap ya linkeó guest→user (o /link explícito);
// acá se aplica el @ que la persona eligió antes de irse a Google.
export function useApplyDesiredAlias() {
  const api = useGameApi()
  return async (player: GamePlayer | null) => {
    if (!player || player.is_guest) return null
    const desired = readDesiredAlias()
    if (!desired || desired === player.alias) {
      clearDesiredAlias()
      return null
    }
    try {
      const updated = unwrap(
        await api.PATCH("/game/derivemos/me", { body: { alias: desired } }),
      )
      clearDesiredAlias()
      posthog.capture("game_alias_edited", { via: "register" })
      return updated
    } catch {
      // 409 (tomado) o red: se descarta el deseo; el alias derivado del
      // username de Google queda como definitivo.
      clearDesiredAlias()
      return null
    }
  }
}
