<template>
  <q-card v-if="picked.length" class="ed" flat bordered>
    <div class="ed-head">
      <q-input v-if="one && !nodes.attached" v-model="nameDraft" dense borderless class="ed-name"
               input-class="ed-name-input" @keyup.enter="rename" @blur="rename" />
      <span v-else class="ed-title">{{ one ? one.name : `${picked.length} nodes` }}</span>
      <span v-if="one" class="ed-id">#{{ one.id }}</span>
      <q-space />
      <q-btn flat dense round size="sm" :icon="matDeleteOutline" color="grey-6"
             :aria-label="one ? 'Remove this node' : 'Remove these nodes'" @click="$emit('remove', names)">
        <q-tooltip>{{ one ? 'Remove this node from the nodeset…' : `Remove these ${picked.length} nodes from the nodeset…` }}</q-tooltip>
      </q-btn>
      <q-btn flat dense round size="sm" aria-label="Close" @click="nodes.select(null)">
        <span class="ed-x">×</span>
        <q-tooltip>Deselect</q-tooltip>
      </q-btn>
    </div>
    <q-separator />

    <template v-if="one?.live">
      <div class="ed-live">
        <div><span>status</span><b :class="'st-' + one.status">{{ one.status }}</b></div>
        <div><span>firmware</span><b :class="{ 'ed-none': !one.firmware }">{{
          one.firmware ? (one.deviceName ?? one.firmware) : 'none: no .firmware(…) names it' }}</b></div>
        <div><span>role</span><b>{{ one.liveRole ?? '—' }}</b></div>
        <div><span>radio</span><b>{{ liveRadio }}</b></div>
        <div v-if="one.stale"><span>losses</span><b class="ed-stale">moved: its row is being recomputed</b></div>
        <div v-if="heard.length"><span>heard by</span><b class="ed-heard">
          <span v-for="[name, level] in heard" :key="name">{{ name }} {{ level.toFixed(0) }}&thinsp;dBm</span>
        </b></div>
      </div>
      <div class="ed-actions">
        <q-btn v-if="one.web" flat dense no-caps size="sm" label="Web UI" @click="$emit('web', one.name)" />
        <q-btn flat dense no-caps size="sm" label="Console" @click="$emit('console', one.name)" />
        <q-btn flat dense no-caps size="sm" label="Reset" @click="sim.resetNode(one.name)">
          <q-tooltip>Presses reset: the process restarts, its state is untouched</q-tooltip>
        </q-btn>
        <q-btn flat dense no-caps size="sm" label="Factory reset" @click="$emit('factory', [one.name])">
          <q-tooltip>Wipes its state and sets it up again</q-tooltip>
        </q-btn>
      </div>
      <q-separator />
    </template>

    <div class="ed-body">
      <div class="ed-sub">Radio</div>
      <div class="ed-row">
        <q-input :model-value="common(n => n.max_dbm ?? null) ?? ''"
                 :placeholder="differs(n => n.max_dbm ?? null) ? MULTI : String(CHIP_MAX_DBM)"
                 type="number" step="any" :min="CHIP_MIN_DBM" :max="FEM_MAX_DBM"
                 dense outlined clearable label="max power (dBm)" class="col"
                 :hint="powerHint"
                 @change="(v: string | number | null) => setMaxDbm(v)"
                 @clear="setMaxDbm(null)" />
      </div>
      <div class="ed-sub">Antenna</div>
      <div class="ed-row">
        <q-select :model-value="common(n => n.antenna.type) ?? null" :options="antennaOptions" dense outlined
                  label="antenna" class="col" emit-value map-options
                  :placeholder="differs(n => n.antenna.type) ? MULTI : ''"
                  @update:model-value="(v: string) => v && setAntennaType(v)">
          <template #option="scope">
            <q-item v-bind="scope.itemProps">
              <q-item-section avatar><div class="ed-pic" v-html="scope.opt.svg" /></q-item-section>
              <q-item-section>
                <q-item-label>{{ scope.opt.label }}</q-item-label>
                <q-item-label caption>{{ scope.opt.caption }}</q-item-label>
              </q-item-section>
            </q-item>
          </template>
        </q-select>
      </div>
      <AimDial v-if="directional && beam" :azimuth="picked[0]!.antenna.azimuth_deg ?? 0"
               :elevation="picked[0]!.antenna.elevation_deg ?? 0" :hbw="beam.hbw_deg ?? 60" :vbw="beam.vbw_deg"
               :mixed="{ az: differs(n => n.antenna.azimuth_deg ?? 0), el: differs(n => n.antenna.elevation_deg ?? 0) }"
               @aim="aim" />
      <div v-if="one" class="ed-row">
        <q-input :model-value="round(one.lat, 7)" type="number" step="any" dense outlined label="latitude"
                 class="col" @change="(v: string | number | null) => set({ lat: Number(v) })" />
        <q-input :model-value="round(one.lon, 7)" type="number" step="any" dense outlined label="longitude"
                 class="col" @change="(v: string | number | null) => set({ lon: Number(v) })" />
      </div>
      <!-- Heights in one unit, metres above sea level, so they compare: the
           antenna's, and beside it the terrain's and the roof's, either of
           which puts the antenna there with a click. -->
      <div class="ed-row ed-height">
        <q-input :model-value="heightShown" :placeholder="heightPlaceholder" type="number" step="any"
                 dense outlined label="height (m)" class="ed-height-in"
                 :hint="aboveGround" @change="(v: string | number | null) => setHeight(v)" />
        <div class="ed-refs">
          <a v-if="terrainShown !== null" class="ed-ref" @click="toTerrain">
            terrain {{ terrainShown }}
            <q-tooltip>Put the antenna on the ground here</q-tooltip>
          </a>
          <a v-if="roofShown !== null" class="ed-ref" @click="toRoof">
            roof {{ roofShown }}
            <q-tooltip>Put the antenna on this building's roof</q-tooltip>
          </a>
          <span v-if="terrainShown === null" class="ed-dim">…</span>
        </div>
      </div>
      <div class="ed-dim">All heights above sea level.</div>

      <div class="ed-sub">Tags</div>
      <div class="ed-tags">
        <q-chip v-for="[tag, count] in tagCounts" :key="tag" dense clickable
                :outline="count < picked.length" :color="count === picked.length ? 'deep-purple-6' : undefined"
                text-color="white" removable @click="nodes.tag(names, tag, true)"
                @remove="nodes.tag(names, tag, false)">
          {{ tag }}<span v-if="!one && count < picked.length" class="ed-some">&nbsp;{{ count }}/{{ picked.length }}</span>
          <q-tooltip>{{ count < picked.length ? 'Click: on every one' : '' }} × takes it off every one</q-tooltip>
        </q-chip>
        <q-input v-model="newTag" dense borderless placeholder="add a tag" class="ed-newtag"
                 @keyup.enter="addTag" />
      </div>

      <!-- A pair's extra loss is set in its inspector, opened by clicking its
           link line; here, the ones this node has. -->
      <template v-if="one && myOffsets.length">
        <div class="ed-sub">Extra losses</div>
        <div v-for="o in myOffsets" :key="o.between.join()" class="ed-offset">
          <a class="ed-offset-to" @click="$emit('inspect', o.between[0] === one.name ? o.between[1] : o.between[0])">
            to {{ o.between[0] === one.name ? o.between[1] : o.between[0] }}</a>:
          {{ o.db > 0 ? '+' : '' }}{{ o.db }} dB
          <span v-if="o.note" class="ed-dim">{{ o.note }}</span>
          <q-btn flat dense round size="xs" aria-label="Take it away" @click="nodes.setOffset(o.between[0], o.between[1], 0)">
            ×<q-tooltip>Take this extra loss away</q-tooltip>
          </q-btn>
        </div>
      </template>
    </div>
  </q-card>
</template>

<script setup lang="ts">
/* The selected nodes: one, or many at once. Every field shows the value the
 * nodes share, or `<multiple values>` where they differ, and a field changed
 * here is changed on every selected node and no other field with it. A tag
 * is on all of them (filled) or some (outlined, with how many); clicking it
 * puts it on every one, its × takes it off every one, and the other tags
 * stay as they are. Attached to a running simulation the edits go to its
 * simd, and one node shows how its station is doing. */
import { computed, ref, watch } from 'vue'
import { matDeleteOutline } from '@quasar/extras/material-icons'
import { useNodes, type HeightFrom, type NodeFields, type NodeView } from '../stores/nodes'
import { useSim } from '../stores/sim'
import { useGeodata } from '../stores/geodata'
import { useCatalog } from '../stores/catalog'
import { placeAt, type Place } from '../lib/roof'
import AimDial from './AimDial.vue'
import { CHIP_MAX_DBM, CHIP_MIN_DBM, FEM_MAX_DBM } from '../lib/boards'

defineEmits<{
  inspect: [other: string]; remove: [names: string[]]
  console: [name: string]; web: [name: string]; factory: [names: string[]]
}>()

const MULTI = '<multiple values>'

const nodes = useNodes()
const sim = useSim()
const ground = useGeodata()
const catalog = useCatalog()
const nameDraft = ref('')
const newTag = ref('')

const picked = computed<NodeView[]>(() => nodes.selection.map(n => nodes.byName[n]).filter((n): n is NodeView => !!n))
const names = computed(() => picked.value.map(n => n.name))
const one = computed(() => (picked.value.length === 1 ? picked.value[0]! : null))

watch(() => one.value?.name, (n) => { nameDraft.value = n ?? '' })

function common<T>(get: (n: NodeView) => T): T | undefined {
  const all = picked.value.map(get)
  return all.every(v => JSON.stringify(v) === JSON.stringify(all[0])) ? all[0] : undefined
}
function differs<T>(get: (n: NodeView) => T): boolean { return picked.value.length > 1 && common(get) === undefined }

void catalog.ensureAntennas()
const antennaOptions = computed(() => catalog.antennas.map(a => ({
  value: a.type, label: `${a.label}, ${a.peak_dbi > 0 ? '+' : ''}${a.peak_dbi} dBi`,
  caption: a.description, svg: a.svg ?? '',
})))
/** Whether every selected node's antenna is aimed: then its aim is edited here. */
const directional = computed(() => picked.value.length > 0 && picked.value.every(
  n => catalog.antennaByType[n.antenna.type]?.kind === 'directional'))

/** What the maximum power means: an SX1262 on its own, or behind a front end above 22 dBm. */
const powerHint = `When empty, a virtual SX1262 with a max power of ${CHIP_MAX_DBM} dBm is assumed. `
  + `When set between ${CHIP_MAX_DBM + 1} and ${FEM_MAX_DBM} dBm, the node behaves as if it has `
  + 'a GC1109 front end amp, like a Heltec V4 board.'

/** The maximum power at the antenna connector on every selected node; empty clears it. */
function setMaxDbm(raw: string | number | null) {
  const v = raw === '' || raw === null ? null : Number(raw)
  if (v !== null && (!Number.isFinite(v) || v < CHIP_MIN_DBM || v > FEM_MAX_DBM)) return
  nodes.setMany(names.value, { max_dbm: v })
}

/** The first selected node's pattern, whose beam the aim dials draw. */
const beam = computed(() => (picked.value[0] ? catalog.antennaByType[picked.value[0].antenna.type] : undefined))

/** A new type on every selected node; a directional one keeps an aim it had. */
function setAntennaType(type: string) {
  for (const n of picked.value) {
    const aimed = catalog.antennaByType[type]?.kind === 'directional'
    nodes.setMany([n.name], { antenna: aimed
      ? { type, azimuth_deg: n.antenna.azimuth_deg ?? 0, elevation_deg: n.antenna.elevation_deg ?? 0 }
      : { type } })
  }
}
/** One figure of the aim on every selected node, each keeping the other. */
function aim(change: { azimuth_deg?: number; elevation_deg?: number }) {
  for (const n of picked.value) nodes.setMany([n.name], { antenna: { ...n.antenna, ...change } })
}
const myOffsets = computed(() => nodes.offsets.filter(o => one.value && o.between.includes(one.value.name)))
const tagCounts = computed<[string, number][]>(() => {
  const count = new Map<string, number>()
  for (const n of picked.value) for (const t of n.tags) count.set(t, (count.get(t) ?? 0) + 1)
  return [...count.entries()].sort((a, b) => a[0].localeCompare(b[0]))
})
const liveRadio = computed(() => {
  const n = one.value
  if (!n?.freq) return '—'
  return `${(n.freq / 1e6).toFixed(3)} MHz  SF${n.sf}  ${(n.bw ?? 0) / 1000} kHz`
})
const heard = computed<[string, number][]>(() => {
  const levels = one.value ? sim.levels[one.value.name] : undefined
  return levels ? Object.entries(levels).sort((a, b) => b[1] - a[1]) : []
})

function set(fields: NodeFields) { nodes.setMany(names.value, fields) }

/* ── height ──
 * A nodeset keeps each antenna's height above the ground under it; here it
 * is shown above sea level, beside the terrain and the roof at the node, so
 * the three read in one unit. Synthetic ground is flat at 0 m, and has no
 * buildings. */
const places = ref<Record<string, Place | null>>({})
/* Asked again every few seconds while the sidecar does not know the roofs yet. */
const placeTick = ref(0)
let placeTries = 0
let placeTimer: ReturnType<typeof setTimeout> | null = null
watch(() => picked.value.map(n => `${n.name}@${n.lat},${n.lon}`).join(';') + `|${ground.current?.name}|${placeTick.value}`,
      (_, before) => {
        if (before !== undefined && !before.endsWith(`|${placeTick.value - 1}`)) placeTries = 0
        for (const n of picked.value) {
          const key = n.name
          if (!ground.isPack) { places.value[key] = { ground: 0, roof: null }; continue }
          const base = ground.sidecar
          if (!base) continue
          const [x, y] = ground.frame.toXY(n.lat, n.lon)
          const at = `${n.lat},${n.lon}`
          void placeAt(base, x, y).then((p) => {
            const now = nodes.byName[key]
            if (now && `${now.lat},${now.lon}` === at) places.value[key] = p
            if (p?.roofUnknown && !placeTimer && placeTries++ < 20) {
              placeTimer = setTimeout(() => { placeTimer = null; placeTick.value++ }, 3000)
            }
          })
        }
      }, { immediate: true })

function r1(v: number) { return Math.round(v * 10) / 10 }
/** A node's antenna above sea level, once its terrain is known. */
function absolute(n: NodeView): number | null {
  const p = places.value[n.name]
  return p ? r1(p.ground + n.height_m) : null
}
const heightKnown = computed(() => picked.value.every(n => places.value[n.name]))
const heightShown = computed(() => {
  if (!heightKnown.value) return ''
  const v = common(absolute)
  return v === undefined || v === null ? '' : v
})
const heightPlaceholder = computed(() => (!heightKnown.value ? '…' : differs(absolute) ? MULTI : ''))
const aboveGround = computed(() => {
  const v = common(n => n.height_m)
  return v === undefined ? 'mixed above ground' : `${r1(v)} m above ground`
})
const terrainShown = computed(() => {
  if (!heightKnown.value) return null
  const v = common(n => r1(places.value[n.name]!.ground))
  return v === undefined ? 'of each' : `${v} m`
})
const roofShown = computed(() => {
  if (!heightKnown.value) return null
  const onRoof = picked.value.filter(n => places.value[n.name]!.roof !== null)
  if (!onRoof.length) return null
  if (one.value) return `${r1(places.value[one.value.name]!.roof!)} m`
  return `of each (${onRoof.length} of ${picked.value.length} on one)`
})

/** Each selected node's antenna at `to(its place)` metres above sea level, when that gives one. */
function setAbsolute(to: (p: Place) => number | null, from: HeightFrom) {
  for (const n of picked.value) {
    const p = places.value[n.name]
    const v = p ? to(p) : null
    if (!p || v === null) continue
    nodes.setMany([n.name], { height_m: Math.max(0, r1(v - p.ground)), height_from: from })
  }
}

function setHeight(raw: string | number | null) {
  const v = Number(raw)
  if (raw === '' || raw === null || !Number.isFinite(v)) return
  // A typed figure is a measurement: it is no longer the roof's or the raster's.
  setAbsolute(() => v, 'measured')
}
function toTerrain() { setAbsolute(p => p.ground, 'measured') }
function toRoof() { setAbsolute(p => p.roof, 'roof') }

function round(v: number, places: number) { return Math.round(v * 10 ** places) / 10 ** places }

function rename() {
  const n = one.value
  const to = nameDraft.value.trim()
  if (!n || to === n.name) return
  if (!/^[a-z0-9][a-z0-9-]*$/.test(to) || !nodes.rename(n.name, to)) nameDraft.value = n.name
}

function addTag() {
  const tag = newTag.value.trim().toLowerCase()
  if (!/^[a-z0-9][a-z0-9-]*$/.test(tag)) return
  nodes.tag(names.value, tag, true)
  newTag.value = ''
}
</script>

<style scoped>
.ed { width: 360px; background: #1b1f26; border-color: #2b313b; max-height: calc(100vh - 140px); overflow-y: auto; }
.ed-head { display: flex; align-items: center; gap: 8px; padding: 2px 6px 2px 12px; }
.ed-name { font-weight: 600; }
.ed-name :deep(.ed-name-input) { font-weight: 600; color: #fff; }
.ed-title { font-weight: 600; padding: 8px 0; }
.ed-id { color: #6b7280; font: 11px ui-monospace, monospace; }
.ed-x { font-size: 18px; line-height: 1; color: #9ca3af; }
.ed-body { padding: 8px 10px; display: flex; flex-direction: column; gap: 8px; }
.ed-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.ed-pic { width: 28px; height: 28px; color: #cbd5e1; }
.ed-pic :deep(svg) { width: 100%; height: 100%; }
.ed-none { color: #f59e0b; font-family: inherit; }
.ed-height { align-items: flex-start; flex-wrap: nowrap; }
.ed-height-in { width: 140px; }
.ed-height-in :deep(.q-field__messages) { white-space: nowrap; }
.ed-refs { display: flex; flex-direction: column; gap: 2px; padding-top: 4px; flex: 1 1 auto; min-width: 0; }
.ed-ref { font-size: 11px; color: var(--q-primary); cursor: pointer; white-space: nowrap; }
.ed-ref:hover { text-decoration: underline; }
.ed-dim { font-size: 11px; color: #6b7280; }
.ed-note { font-size: 11px; color: #fbbf24; line-height: 1.4; }
.ed-sub { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 4px; }
.ed-tags { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; }
.ed-some { font-size: 10px; opacity: 0.8; }
.ed-newtag { width: 110px; font-size: 12px; }
.ed-offset { font-size: 12px; color: #fca5a5; display: flex; align-items: center; gap: 6px; }
.ed-offset-to { color: var(--q-primary); cursor: pointer; }
.ed-offset-to:hover { text-decoration: underline; }
.ed-actions { display: flex; flex-wrap: wrap; gap: 2px; padding: 4px 6px; }
.ed-live { padding: 8px 12px; font-size: 12px; }
.ed-live > div { display: flex; gap: 10px; padding: 2px 0; }
.ed-live span { color: #6b7280; width: 62px; flex: none; }
.ed-live b { font-weight: 500; font-family: ui-monospace, monospace; }
.ed-heard { display: flex; flex-direction: column; min-width: 0; }
.ed-heard span { white-space: nowrap; width: auto; color: inherit; }
.ed-stale { color: #f59e0b; }
.st-up { color: #e5e7eb; }
.st-starting, .st-setup { color: #f59e0b; }
.st-stopped { color: #6b7280; }
.st-restarting { color: #ef4444; }
</style>
