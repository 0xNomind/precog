// Precog — Hermes Desktop half.
// A status-bar chip with the number of mistakes Precog prevented, a lessons
// workspace (turn lessons on/off, forget them), and a toast whenever Precog
// learns a lesson or fixes a command before it runs. Talks only to its own
// backend (dashboard/plugin_api.py) through ctx.rest, and listens to its own
// plugin.precog.* events.
import { host, STATUSBAR_AREAS, PALETTE_AREA, Button, Badge, relativeTime } from '@hermes/plugin-sdk'
import { useEffect, useState, useCallback } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const muted = { color: 'var(--ui-text-tertiary)' }
const faint = { color: 'var(--ui-text-quaternary)' }
const mono = { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: '0.75rem' }

// One tiny shared store so the chip and the workspace stay in sync.
const listeners = new Set()
let summary = { saves: 0, lessons: 0, enabled: 0, week_failures: 0, recent_saves: [] }
function setSummary(next) {
  summary = next
  listeners.forEach(fn => fn(next))
}
function useSummary() {
  const [value, setValue] = useState(summary)
  useEffect(() => {
    listeners.add(setValue)
    return () => listeners.delete(setValue)
  }, [])
  return value
}

function Chip({ open }) {
  const s = useSummary()
  return jsx('button', {
    type: 'button',
    title: `Precog: ${s.saves} mistakes prevented · ${s.lessons} lessons`,
    onClick: open,
    className: 'px-1.5 text-[0.6875rem] hover:text-(--ui-text-secondary)',
    style: muted,
    children: `precog · ${s.saves} saved`
  })
}

function LessonRow({ lesson, onToggle, onForget }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1 py-2',
    style: { borderBottom: '1px solid var(--ui-stroke-secondary)', opacity: lesson.enabled ? 1 : 0.55 },
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('span', { style: faint, children: `#${lesson.id}` }),
          jsx('span', { className: 'flex-1 text-sm', children: lesson.text }),
          jsx(Badge, { variant: 'outline', children: lesson.project }),
          jsx(Badge, { variant: lesson.saves ? 'default' : 'outline', children: `saved ${lesson.saves}×` })
        ]
      }),
      lesson.fail_cmd
        ? jsxs('div', {
            className: 'flex flex-col gap-0.5 pl-6',
            children: [
              jsx('span', { style: { ...mono, ...muted }, children: `✗ ${lesson.fail_cmd.split('\n')[0]}` }),
              lesson.error ? jsx('span', { style: { ...mono, ...faint }, children: `  ${lesson.error}` }) : null,
              jsx('span', { style: { ...mono, ...muted }, children: `✓ ${lesson.fix_cmd.split('\n')[0]}` })
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-2 pl-6',
        children: [
          jsx('span', {
            className: 'flex-1 text-[0.6875rem]',
            style: faint,
            children: `${lesson.kind} · learned ${lesson.learned}× · applied ${lesson.applied}×` +
              (lesson.last_used_at ? ` · last used ${relativeTime(lesson.last_used_at * 1000)}` : '')
          }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => onToggle(lesson), children: lesson.enabled ? 'Turn off' : 'Turn on' }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => onForget(lesson), children: 'Forget' })
        ]
      })
    ]
  })
}

function LessonsView({ ctx, refresh }) {
  const s = useSummary()
  const [lessons, setLessons] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await ctx.rest('/lessons')
      setLessons(res.lessons || [])
      setError('')
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    }
    refresh()
  }, [ctx, refresh])

  useEffect(() => {
    load()
    const off = ctx.onEvent('plugin.precog.lesson.learned', () => load())
    const off2 = ctx.onEvent('plugin.precog.mistake.prevented', () => load())
    return () => { off(); off2() }
  }, [load, ctx])

  const toggle = async lesson => {
    await ctx.rest(`/lessons/${lesson.id}/enabled`, { method: 'POST', body: { enabled: !lesson.enabled } })
    load()
  }
  const forget = async lesson => {
    await ctx.rest(`/lessons/${lesson.id}/forget`, { method: 'POST' })
    load()
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-4',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-3',
        children: [
          jsx('div', { className: 'text-lg font-medium', children: 'Precog' }),
          jsx('div', { className: 'text-sm', style: muted, children: 'Hermes stops repeating its own mistakes.' })
        ]
      }),
      jsxs('div', {
        className: 'grid grid-cols-3 gap-2',
        children: [
          ['mistakes prevented', s.saves],
          ['lessons', `${s.enabled}/${s.lessons} on`],
          ['failed commands this week', s.week_failures]
        ].map(([label, value]) => jsxs('div', {
          key: label,
          className: 'flex flex-col rounded-md p-2',
          style: { border: '1px solid var(--ui-stroke-secondary)' },
          children: [
            jsx('span', { className: 'text-xl font-medium', children: String(value) }),
            jsx('span', { className: 'text-[0.6875rem]', style: muted, children: label })
          ]
        }))
      }),
      error ? jsx('div', { className: 'text-sm', style: { color: 'var(--ui-danger, #d33)' }, children: `Precog backend unavailable: ${error}. Is the precog plugin enabled in config.yaml?` }) : null,
      lessons === null
        ? jsx('div', { className: 'text-sm', style: muted, children: 'Loading…' })
        : lessons.length === 0
          ? jsx('div', {
              className: 'text-sm',
              style: muted,
              children: 'No lessons yet. When a command fails and a later one fixes it, Precog keeps the fix and applies it next time — in any session.'
            })
          : jsx('div', {
              className: 'flex flex-col',
              children: lessons.map(l => jsx(LessonRow, { key: l.id, lesson: l, onToggle: toggle, onForget: forget }))
            })
    ]
  })
}

export default {
  id: 'precog',
  name: 'Precog',
  register(ctx) {
    const refresh = async () => {
      try {
        setSummary(await ctx.rest('/summary'))
      } catch {
        /* backend off: keep the last value */
      }
    }

    // Main-area workspace tab on current Desktops (re-opening the same id re-fronts
    // it); a right-hand pane on older ones that lack host.openWorkspace.
    let fallbackPane = null
    const open = () => {
      if (typeof host.openWorkspace === 'function') {
        host.openWorkspace('precog-lessons', {
          title: 'Precog',
          minWidth: '420px',
          render: () => jsx(LessonsView, { ctx, refresh })
        })
        return
      }
      if (!fallbackPane) {
        fallbackPane = ctx.register({
          id: 'lessons',
          area: 'panes',
          title: 'Precog',
          data: { placement: 'right', width: '420px' },
          render: () => jsx(LessonsView, { ctx, refresh })
        })
      }
    }

    ctx.register({ id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(Chip, { open }) })
    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: { id: 'precog.open', label: 'Precog: show lessons', run: open }
    })

    ctx.onEvent('plugin.precog.lesson.learned', ({ payload }) => {
      refresh()
      if (payload && payload.created) {
        host.notify({ kind: 'info', title: 'Precog learned a lesson', message: payload.text || 'New lesson' })
      }
    })
    ctx.onEvent('plugin.precog.mistake.prevented', ({ payload }) => {
      refresh()
      const msg = payload ? `${payload.original} → ${payload.command}` : 'A known mistake was fixed before it ran'
      host.notify({ kind: 'success', title: 'Precog prevented a repeat mistake', message: msg })
      if (ctx.pet && ctx.pet.visible.get()) ctx.pet.say('Fixed a mistake before it happened', { id: 'saved', ttlMs: 6000 })
    })

    refresh()
    ctx.setInterval(refresh, 30000)
  }
}
