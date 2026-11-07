/**
 * dsh-blender-soft — implicit-geometry exploration for DSH.
 *
 * One tool, no dependency on the upstream dsh-blender-plugin.  The contract is
 * deliberately narrow: propose / measure / refine.  There is no pass/fail and
 * no delivery block in this layer; a separate commit step may choose to gate.
 */
import { spawn, spawnSync } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-blender-soft'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const RUNNER = path.join(HERE, 'runtime', 'blender_runner.py')
const MAX_STDOUT = 200_000

const PARAMS = {
  type: 'object',
  required: ['op'],
  properties: {
    op: {
      type: 'string',
      enum: ['propose', 'energy', 'refine', 'doctor'],
      description: 'propose = free shape; energy = values + gradient; refine = scheduled soft adjustment; doctor = locate Blender.'
    },
    genome: {
      type: 'object',
      description: 'SDF genome: {bounds, resolution, knobs, primitives, build}. Numeric fields may be "@knob".'
    },
    terms: {
      type: 'array',
      items: { type: 'object' },
      description: 'Energy terms: {id, weight, schedule:{start,end}, target, tol, q, axis, center}.'
    },
    tunables: {
      type: 'array',
      items: { type: 'string' },
      description: 'Knob names the refiner may move. Defaults to every declared knob.'
    },
    steps: { type: 'integer', description: 'Refine steps (2..40, default 10).' },
    lr: { type: 'number', description: 'Max normalized knob step per refine step (default 0.08).' },
    mode: { type: 'string', enum: ['soft', 'hard'], description: 'soft = scheduled descent; hard = one late jump (A/B control).' },
    t: { type: 'number', description: 'Schedule position 0..1 used by propose/energy (default: propose 0, energy 1).' },
    seed: { type: 'integer', description: 'Sampling seed; only used with jitter>0.' },
    jitter: { type: 'number', description: 'Uniform knob jitter fraction of range before generation (default 0).' },
    gradient: { type: 'boolean', description: 'Return finite-difference gradients (energy defaults true).' },
    out_dir: { type: 'string', description: 'Artifact directory; default is a temp run directory.' },
    attach: { type: 'boolean', description: 'Attach preview PNGs (default true).' }
  }
}

const OUTPUT = {
  type: 'object',
  properties: { text: { type: 'string' } }
}

function render(_args, value) {
  const blocks = [{ type: 'text', text: String((value && value.text) || '') }]
  if (value && value.image) blocks.push({ type: 'image', attachment: value.image })
  if (value && Array.isArray(value.images)) {
    for (const ref of value.images) blocks.push({ type: 'image', attachment: ref })
  }
  return blocks
}

function dirs(p) {
  try {
    return fs.readdirSync(p, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name)
  } catch {
    return []
  }
}

function findBlender(config) {
  const candidates = []
  if (config && typeof config.blenderPath === 'string') candidates.push([config.blenderPath, 'config.blenderPath'])
  if (process.env.BLENDER_PATH) candidates.push([process.env.BLENDER_PATH, 'BLENDER_PATH'])
  for (const d of dirs('D:/Blender').filter((x) => /^blender-.*-windows-x64$/i.test(x)).sort().reverse()) {
    candidates.push([path.join('D:/Blender', d, 'blender.exe'), 'D:/Blender'])
  }
  for (const base of ['C:/Program Files/Blender Foundation', 'C:/Program Files/Blender']) {
    for (const d of dirs(base).sort().reverse()) candidates.push([path.join(base, d, 'blender.exe'), base])
    candidates.push([path.join(base, 'blender.exe'), base])
  }
  candidates.push(['blender', 'PATH'])
  for (const [candidate, source] of candidates) {
    try {
      if (candidate === 'blender') {
        const r = spawnSync('where', ['blender'], { encoding: 'utf8', timeout: 8000, windowsHide: true })
        const line = String(r.stdout || '').split(/\r?\n/).map((x) => x.trim()).find((x) => /blender\.exe$/i.test(x))
        if (line && fs.existsSync(line)) return { path: line, source: 'where' }
      } else if (fs.existsSync(candidate)) {
        return { path: candidate, source }
      }
    } catch {
      /* keep looking */
    }
  }
  return null
}

function normalize(raw) {
  const args = raw && typeof raw === 'object' ? raw : {}
  const op = String(args.op || 'propose').toLowerCase()
  if (!['propose', 'energy', 'refine', 'doctor'].includes(op)) throw new Error('op must be propose|energy|refine|doctor')
  const ignored = []
  const known = new Set(['op', 'genome', 'terms', 'tunables', 'steps', 'lr', 'mode', 't', 'seed', 'jitter', 'gradient', 'out_dir', 'attach'])
  for (const k of Object.keys(args)) if (!known.has(k)) ignored.push(k)

  let genome = args.genome
  if (typeof genome === 'string') genome = JSON.parse(genome)
  let terms = args.terms
  if (typeof terms === 'string') terms = JSON.parse(terms)
  if (terms === undefined || terms === null) terms = []
  if (!Array.isArray(terms)) throw new Error('terms must be an array')

  const num = (v, d, lo, hi) => {
    const n = Number(v)
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : d
  }
  return {
    op,
    genome: genome && typeof genome === 'object' ? genome : null,
    terms,
    tunables: Array.isArray(args.tunables) ? args.tunables.map(String)
      : typeof args.tunables === 'string' ? args.tunables.split(',').map((s) => s.trim()).filter(Boolean)
        : undefined,
    steps: Math.round(num(args.steps, 10, 2, 40)),
    lr: num(args.lr, 0.08, 0.005, 0.6),
    mode: args.mode === 'hard' ? 'hard' : 'soft',
    t: args.t === undefined ? undefined : num(args.t, 1, 0, 1),
    seed: Math.round(num(args.seed, 0, 0, 2 ** 31)),
    jitter: num(args.jitter, 0, 0, 0.5),
    gradient: args.gradient === undefined ? undefined : !!args.gradient,
    out_dir: typeof args.out_dir === 'string' && args.out_dir.trim() ? args.out_dir.trim() : undefined,
    attach: args.attach === undefined ? true : !!args.attach,
    ignored
  }
}

function runBlender(exe, jobPath, timeoutMs, signal) {
  return new Promise((resolve) => {
    const started = Date.now()
    let stdout = ''
    let stderr = ''
    let settled = false
    const child = spawn(exe, ['-b', '--factory-startup', '--python', RUNNER, '--', jobPath], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1' }
    })
    const cap = (cur, chunk) => {
      const next = cur + chunk
      return next.length > MAX_STDOUT ? next.slice(next.length - MAX_STDOUT) : next
    }
    child.stdout.on('data', (d) => { stdout = cap(stdout, String(d)) })
    child.stderr.on('data', (d) => { stderr = cap(stderr, String(d)) })
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL') } catch { /* ignore */ }
      if (!settled) {
        settled = true
        resolve({ code: -1, stdout, stderr, ms: Date.now() - started, timedOut: true })
      }
    }, Math.max(10_000, timeoutMs))
    const onAbort = () => {
      try { child.kill('SIGKILL') } catch { /* ignore */ }
      if (!settled) {
        settled = true
        clearTimeout(timer)
        resolve({ code: -2, stdout, stderr, ms: Date.now() - started, aborted: true })
      }
    }
    if (signal) {
      if (signal.aborted) onAbort()
      else signal.addEventListener('abort', onAbort, { once: true })
    }
    child.on('error', (err) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve({ code: -3, stdout, stderr: stderr + '\n' + String(err && err.message), ms: Date.now() - started })
    })
    child.on('close', (code) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
      resolve({ code, stdout, stderr, ms: Date.now() - started })
    })
  })
}

function fmtNum(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v)
  const a = Math.abs(v)
  if (a >= 10_000) return v.toFixed(0)
  if (a >= 100) return v.toFixed(1)
  if (a >= 1) return v.toFixed(2)
  return v.toFixed(4)
}

function termLines(report) {
  const out = []
  for (const t of (report && report.terms) || []) {
    const target = Array.isArray(t.target) ? t.target.join('×') : t.target
    const measured = Array.isArray(t.measured) ? t.measured.join('×') : t.measured
    const tgt = t.target === null || t.target === undefined ? '' : ` · target ${target}${t.unit ? ' ' + t.unit : ''}`
    out.push(`  ${String(t.id).padEnd(10)} ${String(measured).padStart(9)} ${t.unit || ''}${tgt} · raw ${t.raw} · w_eff ${t.effective_weight} · ${t.state}`)
  }
  return out.join('\n')
}

function historyLines(history) {
  const rows = (history || []).map((h) => {
    const thk = h.terms && h.terms.thickness !== undefined ? h.terms.thickness : '—'
    return `  t=${String(h.t).padEnd(5)} Δ=${h.max_delta_norm === null ? '—' : h.max_delta_norm} drift=${h.chamfer_mm === null ? '—' : h.chamfer_mm}mm thk=${thk}`
  })
  return rows.join('\n')
}

function summary(res, args) {
  const lines = []
  const g = res.report && res.report.geometry
  const phase = res.phase || 'explore'
  lines.push(`blender_soft · ${res.op}${res.mode ? ' (' + res.mode + ')' : ''} · phase=${phase} · gate=${(res.gate || {}).mode || 'none'}`)
  lines.push(`genome ${String(res.genome_hash || '').slice(0, 12)} · lineage ${String(res.lineage_hash || '').slice(0, 12)} · ${res.elapsed_s || '?'}s`)
  if (g) {
    lines.push(`geometry ${g.verts} verts · ${g.faces} faces · closed=${g.closed} · euler=${g.euler} · volume ${fmtNum(g.volume_mm3)} mm³`)
    lines.push(`bbox ${(g.size_mm || []).map((x) => fmtNum(x)).join(' × ')} mm · components ${g.components}`)
  }
  if (res.report) {
    lines.push(`total weighted energy ${res.report.total} at t=${res.report.t}`)
    lines.push(termLines(res.report))
  }
  if (res.history) {
    lines.push('trajectory:')
    lines.push(historyLines(res.history))
  }
  if (res.gradient && res.gradient.total) {
    const pairs = Object.entries(res.gradient.total).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 4)
    lines.push('gradient (top): ' + pairs.map(([k, v]) => `${k}=${v}`).join(' · '))
  }
  if (res.signals && res.signals.length) {
    lines.push('signals (feedback, not permission):')
    for (const s of res.signals.slice(0, 5)) lines.push(`  · ${s.term}: ${s.suggestion}`)
  }
  if (res.notes && res.notes.length) {
    lines.push('notes: ' + res.notes.slice(0, 4).join(' | '))
  }
  if (res.artifacts) {
    const a = res.artifacts
    lines.push(`artifacts ${Array.isArray(a.previews) ? a.previews.length : 0} preview(s)${a.glb ? ' · ' + a.glb : ''}`)
  }
  if (args && args.ignored && args.ignored.length) lines.push(`ignored keys: ${args.ignored.join(', ')}`)
  lines.push('No verdict was produced. Measure here, decide in your commit step.')
  return lines.join('\n')
}

async function attachPreviews(ctx, previews, mode) {
  const out = []
  let why = ''
  try {
    const att = ctx.get('attachments')
    if (!att) return { images: out, why: 'host has no attachments service' }
    const media = (att.imageLimits && att.imageLimits.mediaTypes) || []
    if (!media.includes('image/png')) return { images: out, why: 'host image/png limit excludes PNG' }
    let wanted = Array.isArray(previews) ? previews.slice() : []
    if (mode === 'refine' && wanted.length > 2) wanted = [wanted[0], wanted[wanted.length - 1]]
    else wanted = wanted.slice(-1)
    for (const p of wanted) {
      if (!p || !p.path || !fs.existsSync(p.path)) continue
      const data = fs.readFileSync(p.path)
      const ref = await att.saveImage({
        data,
        mediaType: 'image/png',
        name: `blender-soft-${p.label || 'preview'}-${Date.now()}-${out.length}.png`
      })
      if (ref) out.push(ref)
    }
  } catch (e) {
    why = String((e && e.message) || e)
  }
  return { images: out, why }
}

function toolDefinition(ctx, config) {
  return {
    name: 'blender_soft',
    description:
      'Implicit-geometry exploration in headless Blender. Builds an SDF genome, ' +
      'meshes it with surface nets, and reports soft constraint energies + gradients. ' +
      'It NEVER returns pass/fail and NEVER blocks; gates belong to a separate commit step. ' +
      'op=propose (free shape), op=energy (values + finite-difference gradient at schedule t), ' +
      'op=refine (after the free shape exists, a smooth schedule moves only the tunables you expose; ' +
      'mode=hard is an A/B control that applies all weight in one late jump), op=doctor (find Blender). ' +
      'genome={bounds:[[x,y,z],[x,y,z]], resolution, knobs:{name:{value,min,max}}, ' +
      'primitives:[{id,type,center,rotate,mirror,radius|radii|size|round|a|b|height|major_radius|minor_radius}], ' +
      'build:[{op:union|subtract|intersect, ids:[...], k}]}. A numeric field may be "@knob". ' +
      'Primitive types: sphere, ellipsoid, box, capsule, cylinder, torus. ' +
      'terms=[{id:thickness|symmetry|curvature|volume|size|closed|components, weight, ' +
      'schedule:{start,end}, target, tol, q, axis, center}]. ' +
      'Returns previews + GLB + a trajectory; no verdict. Use a commit/audit path for delivery.',
    parameters: PARAMS,
    output: { schema: OUTPUT, render },
    isConcurrencySafe: () => false,
    timeoutMs: 360_000,
    async execute(raw, exec) {
      let args
      try {
        args = normalize(raw)
      } catch (e) {
        return { text: `blender_soft: ${String((e && e.message) || e)}`, ok: false }
      }
      const found = findBlender(config)
      if (args.op === 'doctor') {
        const v = found
          ? spawnSync(found.path, ['--version'], { encoding: 'utf8', timeout: 20_000, windowsHide: true })
          : null
        const version = v && v.stdout ? String(v.stdout).split(/\r?\n/)[0] : null
        const text = [
          'blender_soft · doctor',
          found ? `blender ${found.path} (${found.source})` : 'blender NOT FOUND',
          version ? `version ${version}` : 'version unavailable',
          'This tool needs a Blender >= 4.x executable; set config.blenderPath or BLENDER_PATH to override.'
        ].join('\n')
        return { text, ok: !!found }
      }
      if (!args.genome) {
        return { text: 'blender_soft: genome is required for propose/energy/refine. Run op=doctor to locate Blender.', ok: false }
      }
      if (!found) {
        return { text: 'blender_soft: Blender not found. Install Blender 4.x/5.x or set config.blenderPath / BLENDER_PATH. Run op=doctor for the search paths.', ok: false }
      }
      if (!fs.existsSync(RUNNER)) return { text: `blender_soft: runner missing at ${RUNNER}`, ok: false }

      const runId = `${Date.now().toString(36)}-${randomBytes(4).toString('hex')}`
      const dir = args.out_dir || path.join(os.tmpdir(), 'dsh-blender-soft', runId)
      fs.mkdirSync(dir, { recursive: true })
      const job = {
        op: args.op,
        genome: args.genome,
        terms: args.terms,
        tunables: args.tunables,
        steps: args.steps,
        lr: args.lr,
        mode: args.mode,
        seed: args.seed,
        jitter: args.jitter,
        out_dir: dir
      }
      if (args.t !== undefined) job.t = args.t
      if (args.gradient !== undefined) job.gradient = args.gradient
      const jobPath = path.join(dir, 'job.json')
      fs.writeFileSync(jobPath, JSON.stringify(job), 'utf8')

      const budget = Math.min(340_000, args.steps > 12 ? 340_000 : 300_000)
      const r = await runBlender(found.path, jobPath, budget, exec && exec.signal)
      const tag = (r.stdout.match(/^SOFT_RESULT (.+)$/m) || [])[1]
      const errTag = (r.stdout.match(/^SOFT_ERROR (.+)$/m) || [])[1]
      if (r.aborted) return { text: 'blender_soft: cancelled by the host.', ok: false }
      if (r.timedOut) return { text: `blender_soft: Blender exceeded ${budget} ms; artifacts may be in ${dir}`, ok: false }
      if (!tag) {
        const tail = (r.stderr || r.stdout || '').slice(-1800)
        return {
          text: `blender_soft: run failed (exit ${r.code})${errTag ? '\n' + errTag : ''}\n${tail}\njob: ${jobPath}\nout: ${dir}`,
          ok: false
        }
      }
      const resultPath = tag.trim()
      if (!fs.existsSync(resultPath)) return { text: `blender_soft: result marker ${resultPath} missing`, ok: false }
      const res = JSON.parse(fs.readFileSync(resultPath, 'utf8'))
      if (res.ok === false) return { text: `blender_soft: ${String(res.error || 'failed').slice(-1800)}`, ok: false }

      let images = []
      let why = ''
      if (args.attach) {
        const got = await attachPreviews(ctx, (res.artifacts && res.artifacts.previews) || [], args.op)
        images = got.images
        why = got.why
      }
      let text = summary(res, args)
      if (why) text += `\n(preview attach skipped: ${why})`
      const value = { text, ok: true, phase: res.phase, artifacts: res.artifacts }
      if (images.length) {
        value.image = images[0]
        if (images.length > 1) value.images = images.slice(1)
      }
      return value
    }
  }
}

export function apply(ctx, config) {
  const mount = (inner) => {
    let dispose = null
    try {
      dispose = inner.tools.register(toolDefinition(ctx, config))
    } catch (e) {
      ctx.logger?.error?.(`dsh-blender-soft: cannot register blender_soft: ${String(e && e.stack || e)}`)
      return
    }
    ctx.effect?.(() => () => {
      try { dispose() } catch { /* best effort */ }
    }, 'dsh-blender-soft tools')
  }
  if (typeof ctx.inject === 'function') ctx.inject(['tools'], mount)
  else mount(ctx)
}
