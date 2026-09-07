// SpielOS OpenCode adapter — one plugin file for every host generation.
//
// Contract resilience, on purpose (host versions must never matter to the
// owner):
//
// - OpenCode V2 validates `default` as an object with `id` plus a `setup`
//   (or `effect`) function — that shape is what we export.
// - A server may be started from a different folder than the session it
//   serves: never trust `ctx.location.directory` alone. Resolve the home
//   per request from the session directory, then `.agents/company`, then
//   a flat source checkout (`company/`), then the plugin's own home.
//   This file has zero imports on purpose: a fresh home has no node_modules.

type Any = Record<string, any>

// The clean core parks live actions as one kind of owner attention.
const REPORTABLE = new Set(["owner_input_required"])

const homeAt = (directory: string): Promise<boolean> => {
  if (typeof directory !== "string" || !directory) return Promise.resolve(false)
  return Promise.all([
    Bun.file(`${directory}/.agents/company/__main__.py`).exists(),
    Bun.file(`${directory}/company/__main__.py`).exists(),
  ]).then(([vendored, flat]) => vendored || flat).catch(() => false)
}

// Candidate homes for one request, most specific first.
const homeCandidates = (
  sessionDirectory: string | undefined,
  pluginHome: string,
): string[] => {
  const out: string[] = []
  const push = (value: string | undefined) => {
    if (typeof value === "string" && value.trim() && !out.includes(value)) {
      out.push(value)
    }
  }
  push(sessionDirectory)
  push(pluginHome)
  push(process.env.SPIELOS_HOME)
  push(process.cwd())
  return out
}

const companyRunner = (
  sessionDirectoryOf: () => string | undefined,
  pluginHome: string,
) => {
  const runners = new Map<string, (args: string[]) => Promise<Any> | null>()

  const runnerFor = async (
    home: string,
  ): Promise<(args: string[]) => Promise<Any> | null> => {
    const cached = runners.get(home)
    if (cached !== undefined) return cached
    if (!(await homeAt(home))) {
      runners.set(home, null)
      return null
    }
    const vendored = `${home}/.agents`
    const pythonPath = process.env.PYTHONPATH
      ? `${vendored}:${process.env.PYTHONPATH}`
      : vendored
    const run = async (args: string[]): Promise<Any> => {
      const child = Bun.spawn({
        cmd: ["python3", "-B", "-m", "company", ...args],
        cwd: home,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          ...process.env,
          PYTHONDONTWRITEBYTECODE: "1",
          PYTHONPATH: pythonPath,
        },
      })
      const [code, stdout, stderr] = await Promise.all([
        child.exited,
        new Response(child.stdout).text(),
        new Response(child.stderr).text(),
      ])
      if (code !== 0) {
        const detail = stderr.trim().split("\n").at(-1) || `exit ${code}`
        throw new Error(`company command failed: ${detail}`)
      }
      return JSON.parse(stdout)
    }
    runners.set(home, run)
    return run
  }

  const resolve = async (): Promise<
    ((args: string[]) => Promise<Any>) | null
  > => {
    for (const candidate of homeCandidates(sessionDirectoryOf(), pluginHome)) {
      const run = await runnerFor(candidate)
      if (run) return run
    }
    return null
  }

  return async (args: string[]): Promise<Any> => {
    const run = await resolve()
    if (!run) throw new Error("no SpielOS home found for this session")
    return run(args)
  }
}

const formatNotification = (item: Any): string => {
  const payload = item.payload || {}
  const lines = [
    `SpielOS attention · ${item.kind}`,
    `Goal: ${payload.goal?.name || item.goal_id}`,
  ]
  if (payload.message) lines.push(`Message: ${payload.message}`)
  const next = payload.required_user_action
  if (next) lines.push(`Next: ${next}`)
  return lines.join("\n")
}

const CONTEXT_FAILURE_NOTICE =
  "SpielOS context unavailable for this request. Do not search the repository " +
  "or guess company state. Run the read-only command " +
  "`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.agents python3 -B -m company status` " +
  "once, tell the owner that host context injection failed, and report this " +
  "diagnostic"

const pushSystem = (system: Any[], text: string): void => {
  // The system parts are strings in some builds and objects in others; push
  // the shape the existing entries already use.
  const sample = system.find((part) => typeof part !== "undefined")
  if (typeof sample === "string") system.push(text)
  else system.push({ type: "text", text })
}

// Directory of the session a request belongs to, when the host exposes it.
const sessionDirectoryFrom = (event: Any): string | undefined =>
  event?.directory
  ?? event?.location?.directory
  ?? event?.session?.directory
  ?? event?.properties?.directory
  ?? event?.properties?.info?.directory
  ?? undefined

const setup = async (ctx: Any): Promise<(() => void) | undefined> => {
  // The plugin ships at <home>/.opencode/plugins/, so its own folder names
  // the home even when the server was started somewhere else.
  const pluginDirectory: string =
    (typeof import.meta.dir === "string" && import.meta.dir)
    || ctx?.location?.directory
    || process.cwd()
  const pluginHome = pluginDirectory
    .replace(/\/\.opencode\/plugins\/?$/, "")
    .replace(/\/\.opencode\/?$/, "")

  let currentSessionDirectory: string | undefined
  const rememberSession = (value: string | undefined): void => {
    if (typeof value === "string" && value.trim()) {
      currentSessionDirectory = value
    }
  }
  const runCompany = companyRunner(() => currentSessionDirectory, pluginHome)

  const refreshSessionDirectory = async (sessionID: string): Promise<void> => {
    try {
      const session = await ctx?.session?.get?.({ path: { id: sessionID } })
      rememberSession(
        session?.data?.directory ?? session?.directory ?? undefined,
      )
    } catch {
      // Directory resolution is best effort; candidates still cover it.
    }
  }

  // ---- V2: inject the read-only projection on every model request -------
  try {
    await ctx.session.hook("context", async (event: Any) => {
      const system = event?.system
      if (!Array.isArray(system)) return
      rememberSession(sessionDirectoryFrom(event))
      try {
        const projection = await runCompany([
          "context", "--owner", "director", "--json",
        ])
        if (typeof projection?.context !== "string" || !projection.context) {
          throw new Error("empty context projection")
        }
        pushSystem(system, projection.context)
      } catch (error) {
        const detail = error instanceof Error ? error.message : "unknown host error"
        pushSystem(system, `${CONTEXT_FAILURE_NOTICE}: ${detail}`)
      }
    })
  } catch (error) {
    console.error("[spielos] context hook unavailable", error)
  }

  // ---- Idle: surface pending attention, ack only after delivery ---------
  const controller = new AbortController()
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({
        signal: controller.signal,
      }) as Any) {
        if (event?.type !== "session.idle") continue
        const sessionID =
          event?.properties?.sessionID ?? event?.properties?.id
        if (!sessionID) continue
        rememberSession(sessionDirectoryFrom(event))
        await refreshSessionDirectory(sessionID)
        try {
          const rows = (await runCompany([
            "notifications", "list", "--status", "pending",
            "--limit", "20", "--json",
          ])) as Any[]
          for (const item of rows.filter((row) => REPORTABLE.has(row.kind))) {
            try {
              await ctx.session.synthetic({
                sessionID,
                text: formatNotification(item),
              })
            } catch {
              break // delivery failed: leave it pending for the next idle
            }
            await runCompany(["notifications", "ack", item.id, "--json"])
          }
        } catch {
          // Persistence is the fallback. Failed delivery remains pending.
        }
      }
    } catch {
      // stream aborted during shutdown; nothing to clean up
    }
  })()
  return () => controller.abort()
}

// Default export satisfies the V2 schema (id + setup).
const plugin = { id: "spielos-notifications", setup }

export default plugin
