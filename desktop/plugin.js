import { host, useQuery, useValue, gatewayActivationEpoch, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const page = '/rh-preflight'
const CREDIT = "rh-preflight by quotum with HEY Research Lab"

function humanResponse(row) {
  if (['pass', 'note', 'block'].includes(row.disposition)) return 'not requested'
  const responses = {
    once: 'approved once; execution not recorded',
    session: 'approved for session; execution not recorded',
    always: 'approved always; execution not recorded',
    deny: 'denied',
    timeout: 'timed out',
    cancelled: 'cancelled',
  }
  return Object.hasOwn(responses, row.decision) ? responses[row.decision] : 'not recorded'
}

const PROJECTS = ['HEY', 'Priors', 'Sentinelhood', 'HoodLock', 'Trnch', 'Agent Wormhole', 'Hookr', 'Toll', 'Borro', 'Sherwood', 'OffRouter']
const OUTCOME = { go: '#15803d', ask: '#b45309', stop: '#b91c1c', note: '#4b5563' }
const BUCKET = {
  answered: { color: '#15803d', label: 'answered' },
  other: { color: 'rgba(127, 127, 127, 0.55)', label: 'not for this target' },
  silent: { color: '#b45309', label: 'silent' },
  off: { color: 'rgba(127, 127, 127, 0.22)', label: 'off' },
}

function answerStyle(answer) {
  const label = String(answer ?? '').replace(/^if Hermes swapped this now: /, '').split(' · ')[0]
  const color = Object.hasOwn(OUTCOME, label) ? OUTCOME[label] : null
  return color ? { display: 'inline-block', padding: '4px 10px', borderRadius: '6px', background: color, color: '#fff' } : undefined
}

// Coverage is our own deterministic line; anything else yields no buckets.
function buckets(coverage) {
  const parts = String(coverage ?? '').split(' · ')
  if (!/^\d+ sources$/.test(parts[0] ?? '')) return null
  const state = {}
  for (const part of parts.slice(1)) {
    const named = part.match(/^(?:\d+ not for this target|silent|off): (.+)$/)
    if (!named) continue
    const kind = part.startsWith('silent') ? 'silent' : part.startsWith('off') ? 'off' : 'other'
    for (const item of named[1].split(', ')) {
      const entry = item.match(/^(.+?)(?: \((.+)\))?$/)
      if (entry && PROJECTS.includes(entry[1])) state[entry[1]] = { kind, reason: entry[2] }
    }
  }
  return PROJECTS.map(name => ({ name, ...(state[name] ?? { kind: 'answered' }) }))
}

function coverageStyle(list) {
  if (!list) return undefined
  const step = 100 / list.length
  const stops = list.flatMap(({ kind }, i) => [
    `${BUCKET[kind].color} ${(i * step).toFixed(3)}%`, `${BUCKET[kind].color} ${((i + 1) * step - 0.6).toFixed(3)}%`,
    `transparent ${((i + 1) * step - 0.6).toFixed(3)}%`, `transparent ${((i + 1) * step).toFixed(3)}%`,
  ])
  return { paddingBottom: '14px', backgroundImage: `linear-gradient(90deg, ${stops.join(', ')})`, backgroundSize: '100% 6px', backgroundRepeat: 'no-repeat', backgroundPosition: 'left bottom' }
}

function sourceList(card) {
  let list
  if (Object.hasOwn(card, 'sources')) {
    if (!Array.isArray(card.sources) || !card.sources.every(source =>
      source && typeof source === 'object' && !Array.isArray(source) &&
      ['HEY Research Lab', ...PROJECTS.slice(1)].includes(source.name) &&
      ['answered', 'not_for_target', 'silent', 'off'].includes(source.bucket) &&
      (source.reason === null || typeof source.reason === 'string') &&
      (source.link === null || typeof source.link === 'string' && source.link.startsWith('https://'))
    )) return null
    list = card.sources.map(({ name, bucket, reason, link }) => ({
      name, kind: bucket === 'not_for_target' ? 'other' : bucket, reason, link,
    }))
  } else {
    list = buckets(card.coverage ?? card.card.split('\n')[1])
  }
  if (!list) return null
  return jsx('ul', {
    className: 'mt-3 grid gap-1',
    'aria-label': `Sources for ${card.target}`,
    children: list.map(({ name, kind, reason, link }) => {
      const href = kind === 'answered' ? link : null
      return jsxs('li', {
        style: { display: 'flex', alignItems: 'center', gap: '8px' },
        children: [
          jsx('span', { 'aria-hidden': true, style: { width: '10px', height: '10px', borderRadius: '50%', flex: 'none', background: BUCKET[kind].color } }),
          jsx('span', { style: { minWidth: '9.5em' }, children: name === 'HEY' ? 'HEY Research Lab' : name }),
          jsx('span', { className: 'text-(--ui-text-secondary)', children: reason ? `${BUCKET[kind].label} · ${reason}` : BUCKET[kind].label }),
          href ? jsx('a', { href, className: 'underline underline-offset-2', 'aria-label': `${name} source page`, children: 'source' }) : null,
        ],
      }, name)
    }),
  })
}

function useChecks(ctx) {
  const profile = useValue(host.state.profile)
  const gateway = useValue(host.state.gateway)
  return useQuery({
    queryKey: ['rh-preflight', 'checks', profile, gateway, gatewayActivationEpoch()],
    queryFn: () => ctx.rest('/checks'),
    refetchInterval: 15000,
    retry: false,
  })
}

function Chip({ ctx }) {
  const { data, isError } = useChecks(ctx)
  const label = isError ? 'rh-preflight unavailable' : data
    ? `checked ${data.today.checked} · asked ${data.today.asked} today`
    : 'rh-preflight loading'
  return jsx('button', {
    type: 'button',
    onClick: () => host.navigate(page),
    className: 'px-2 text-xs text-(--ui-text-secondary) focus-visible:outline focus-visible:outline-(--ui-accent)',
    'aria-label': `${label}. Open rh-preflight checks`,
    children: label,
  })
}

function Checks({ ctx }) {
  const query = useChecks(ctx)
  const rows = query.data?.checks ?? []
  return jsxs('main', {
    className: 'h-full overflow-auto p-6 text-sm text-(--ui-text-primary)',
    style: { fontFamily: 'inherit' },
    children: [
      jsx('h1', { className: 'mb-2 text-2xl font-semibold', children: 'rh-preflight' }),
      jsxs('p', { className: 'mb-2 text-(--ui-text-secondary)', children: [CREDIT, ' · ', jsx('a', { href: 'https://heyresearch.xyz', className: 'underline underline-offset-2', children: 'heyresearch.xyz' })] }),
      jsx('p', { className: 'mb-6 text-(--ui-text-secondary)', children: '11 sources: HEY Research Lab plus ten Robinhood Chain projects. HEY Research Lab supplies a builder record with links to what HEY read. rh-preflight decides go / ask / stop under your rules; each check shows the recorded human response.' }),
      jsx('button', {
        type: 'button', onClick: () => query.refetch(), disabled: query.isFetching,
        className: 'mb-6 rounded border border-(--ui-stroke-secondary) px-3 py-2 focus-visible:outline focus-visible:outline-(--ui-accent) disabled:opacity-50',
        children: query.isFetching ? 'Refreshing checks' : 'Refresh checks',
      }),
      query.isError ? jsx('p', { role: 'alert', children: 'The local check history could not be read. Enable the agent half in this profile, then refresh checks.' })
        : query.isLoading ? jsx('p', { role: 'status', children: 'Loading local checks' })
          : rows.length === 0 ? jsx('p', { children: 'No checks recorded yet. Checks appear after a detected contract action.' })
            : jsx('ol', {
              className: 'space-y-6',
              children: rows.map(row => jsxs('li', {
                className: 'border-t border-(--ui-stroke-secondary) pt-4',
                children: [
                  ...row.cards.map(card => jsxs('section', {
                    className: 'mb-3 break-words',
                    style: { overflowWrap: 'anywhere' },
                    children: [
                      jsx('h2', { className: 'text-sm font-semibold', children: card.answer ?? card.card.split('\n')[0], style: answerStyle(card.answer ?? card.card.split('\n')[0]) }),
                      jsx('p', { className: 'mt-1 text-(--ui-text-secondary)', children: card.coverage ?? card.card.split('\n')[1], style: coverageStyle(buckets(card.coverage ?? card.card.split('\n')[1])) }),
                      jsx('p', { className: 'mt-1 text-(--ui-text-secondary)', children: `target: ${card.target}` }),
                      jsxs('details', {
                        className: 'mt-2',
                        children: [
                          jsx('summary', {
                            className: 'cursor-pointer focus-visible:outline focus-visible:outline-(--ui-accent)',
                            'aria-label': `Source details for ${card.target}`,
                            children: 'Source details',
                          }),
                          jsx('p', {
                            className: 'mt-2 whitespace-pre-wrap leading-relaxed',
                            children: card.answer ? card.card.split('\n').slice(2).join('\n') : card.card,
                          }),
                        ],
                      }),
                      sourceList(card),
                    ],
                  }, card.target)),
                  jsx('p', { className: 'mt-1 text-(--ui-text-secondary)', children: `${row.tool_name}${row.cards.length > 1 ? ` · ${row.cards.length} checked targets` : ''} · ${row.mode} · ${row.time}` }),
                  jsx('p', { className: 'mt-1 text-(--ui-text-secondary)', children: `human response: ${humanResponse(row)}` }),
                ],
              }, row.id)),
            }),
    ],
  })
}

export default {
  id: 'rh-preflight',
  name: 'rh-preflight',
  defaultEnabled: false,
  register(ctx) {
    ctx.registerMany([
      { id: 'checks', area: ROUTES_AREA, data: { path: page }, render: () => jsx(Checks, { ctx }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: page, label: 'rh-preflight', codicon: 'list-flat' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 125, render: () => jsx(Chip, { ctx }) },
    ])
  },
}
