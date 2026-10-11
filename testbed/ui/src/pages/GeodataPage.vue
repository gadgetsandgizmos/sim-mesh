<template>
  <q-page class="gp">
    <template v-if="preview">
      <q-toolbar class="gp-bar">
        <q-btn flat dense no-caps label="← Geodata" @click="back" />
        <div class="gp-title">
          {{ preview }}
          <span class="gp-sub">{{ describe(ground.current) }}</span>
        </div>
        <q-space />
        <q-btn flat dense no-caps label="Export zip" type="a" :href="`/api/geodata/export?name=${encodeURIComponent(preview)}`"
               :download="`${preview}.zip`" />
        <PlaceSearch @go="p => map?.goTo(p.x, p.y, 1500)" />
        <DisplayMenu view="geodata" />
      </q-toolbar>
      <div class="gp-map">
        <GroundMap ref="map" :display="display.geodata" view-key="ground" />
      </div>
    </template>

    <BuildView v-else-if="building" @back="building = false" @started="building = false" />

    <div v-else class="gp-body">
      <div class="tab-head">
        <div class="tab-heading">Installed geodata packs</div>
        <q-space />
        <q-btn flat dense no-caps color="primary" label="New synthetic" @click="creating = true" />
        <q-btn flat dense no-caps color="primary" label="Build" @click="building = true" />
        <q-btn flat dense no-caps color="primary" label="Import zip" @click="importing = true" />
      </div>
      <div class="gp-text">
        The ground nodes stand on: a pack (terrain, clutter, buildings, roads,
        places, with ITU-R P.1812 over the real profile), downloaded pre-built,
        built here from public sources, or synthetic ground at 0°, 0° whose degrees
        are metres at a nautical mile to the minute. Open one to work on it: the
        Nodes tab shows the nodesets with a node on it (counted here), and nothing
        before one is chosen.
      </div>
      <SelectBar :sel="sel">
        <template #default="{ keys }">
          <q-btn flat dense no-caps size="sm" :icon="matDeleteOutline" label="Delete selection"
                 @click="askDelete(keys)" />
        </template>
      </SelectBar>
      <table class="gp-table tab-flow">
        <thead><tr><th class="gp-check"></th><th>Geodata</th><th>What</th><th>Extent</th><th>Nodesets</th>
          <th class="col-size">Size</th><th class="col-act"></th></tr></thead>
        <tbody>
          <tr v-if="catalog.build" class="gp-building">
            <td></td>
            <td><span class="gp-name">{{ catalog.build.name }}</span></td>
            <td colspan="5">
              <div class="gp-build">
                <span v-if="catalog.build.state === 'failed'" class="gp-bad">
                  build failed: {{ catalog.build.error }}
                </span>
                <span v-else>building… {{ progress(catalog.build) }}</span>
                <q-space />
                <q-btn flat dense no-caps size="sm"
                       :label="catalog.build.state === 'failed' ? 'Dismiss' : 'Cancel'" @click="cancelBuild" />
              </div>
              <q-linear-progress v-if="catalog.build.state !== 'failed'" :value="fraction(catalog.build) ?? 0"
                                 :indeterminate="fraction(catalog.build) === null" color="primary" class="q-mt-xs" />
            </td>
          </tr>
          <tr v-for="g in catalog.geodata" :key="g.name"
              :class="{ 'gp-row': !g.error, 'gp-chosen': g.name === nodes.geodata, 'gp-picked': sel.has(g.name) }"
              :title="g.error ? '' : 'Click to open this geodata'" @click="!g.error && choose(g.name)">
            <td class="gp-check" @click.stop>
              <q-checkbox dense size="xs" :model-value="sel.has(g.name)" @update:model-value="sel.toggle(g.name)" />
            </td>
            <td>
              <span class="gp-name">{{ g.name }}</span>
              <span v-if="g.name === nodes.geodata" class="gp-sub">chosen</span>
              <div v-if="g.from_index" class="gp-sub">from {{ g.from_index.index }}</div>
              <div v-if="g.error" class="gp-bad">{{ g.error }}</div>
            </td>
            <td>{{ describe(g) }}</td>
            <td class="mono">{{ extent(g) }}</td>
            <td class="mono">{{ g.error ? '' : g.nodesets ?? '…' }}</td>
            <td class="col-size mono">{{ sizeText(g.bytes) }}</td>
            <td class="col-act" @click.stop>
              <q-btn flat dense round size="sm" :icon="matEdit" @click="askRename(g.name)">
                <q-tooltip>Rename</q-tooltip>
              </q-btn>
              <q-btn flat dense round size="sm" :icon="matDeleteOutline" @click="askDelete([g.name])">
                <q-tooltip>Delete, with its pack</q-tooltip>
              </q-btn>
            </td>
          </tr>
        </tbody>
      </table>

      <IndexOffers kind="geodata" @open="choose" />

      <div class="tab-head tab-section">
        <div class="tab-heading">Geodata sources</div>
        <q-space />
        <q-btn flat dense no-caps label="Refresh" @click="loadSources" />
      </div>
      <div class="gp-text">
        Data sources that geodata packs are built from, cached so you don't need
        to download things twice. Cached data can be freely deleted after a pack
        is built.
      </div>
      <div class="gp-sources">
        <div class="gp-sources-list">
        <div v-if="point" class="gp-at">
          <span>
            at {{ point.lat.toFixed(4) }}° {{ point.lat >= 0 ? 'N' : 'S' }},
            {{ point.lon.toFixed(4) }}° {{ point.lon >= 0 ? 'E' : 'W' }}:
            {{ shownSources.length }} source{{ shownSources.length === 1 ? '' : 's' }} with data there
          </span>
          <q-space />
          <q-btn flat dense no-caps size="sm" label="All sources" @click="clearPoint" />
        </div>
        <table class="gp-table tab-flow">
          <thead><tr><th>Source</th><th>Licence</th><th class="num">Cached</th><th></th></tr></thead>
          <tbody>
            <tr v-for="s in shownSources" :key="s.source"
                :class="{ 'gp-row': s.map, 'gp-picked': s.source === shownSource }"
                @click="s.map && showSource(s.source)">
              <td>
                <span class="gp-what">{{ keepUnits(s.what) }}</span>
                <q-tooltip v-if="s.holds" anchor="center right" self="center left" max-width="340px"
                           class="gp-holds">{{ keepUnits(s.holds) }}</q-tooltip>
                <div class="gp-sub">
                  <span class="mono">{{ s.source }}</span><template v-if="s.where"> · {{ s.where }}</template><template
                    v-if="s.own"> · yours</template>
                </div>
                <div v-if="s.layers" class="gp-sub">
                  {{ Object.entries(s.layers).map(([l, p]) => `${l} ${p}`).join(', ') }}
                </div>
              </td>
              <td class="gp-sub">{{ s.licence }}</td>
              <td class="num mono">
                {{ sizeText(s.bytes) }}
                <div v-if="point" class="gp-sub">{{ point.at[s.source]?.cached ? 'this spot cached' : 'this spot not cached' }}</div>
              </td>
              <td class="gp-act" @click.stop>
                <q-btn flat dense round size="sm" :icon="matDeleteOutline" :disable="!s.bytes"
                       @click="askClear(s)">
                  <q-tooltip>Empty this source's cache</q-tooltip>
                </q-btn>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
        <div class="gp-source-side">
          <SlippyMap ref="sourceMap" class="gp-source-map" :shapes="sourceShapes"
                     view-key="sim-mesh.sourcesmap" :pin="point ? [point.lon, point.lat] : null"
                     @pick="pickPoint" />
          <div class="gp-sub gp-source-note">
            <template v-if="!shownSource">Click a source to see where it has data, and what of it is
              cached; click the map to list only the sources with data at that spot.</template>
            <template v-else-if="!sourceArea">asking…</template>
            <template v-else>
              <div class="gp-fit" title="show all of it on the map" @click="fitTo('covers')">
                <span class="gp-key gp-key-covers" /> where it has data: {{ sourceArea.covers_from ?? 'not known here' }}
              </div>
              <div :class="{ 'gp-fit': sourceArea.cached }"
                   :title="sourceArea.cached ? 'show all of it on the map' : ''"
                   @click="sourceArea.cached && fitTo('cached')">
                <span class="gp-key gp-key-cached" /> cached here:
                {{ sourceArea.cached_files ? `${sourceArea.cached_files} file${sourceArea.cached_files === 1 ? '' : 's'}` : 'nothing' }}
              </div>
              <div v-if="sourceArea.error" class="gp-bad">{{ sourceArea.error }}</div>
            </template>
          </div>
        </div>
      </div>
    </div>

    <q-dialog v-model="importing">
      <q-card style="min-width: 420px">
        <q-card-section class="text-subtitle2">Import a zip</q-card-section>
        <q-card-section class="column q-gutter-sm">
          <q-file v-model="file" dense outlined label="zip" accept=".zip,application/zip"
                  @update:model-value="nameFromZip" />
          <q-input v-model="name" dense outlined label="geodata name"
                   hint="lower-case letters, digits and hyphens" />
          <div class="text-caption text-grey-6">
            A sim-mesh geodata pack, as Export zip writes one, or a bare planner pack
            (its manifest.json at the top or inside one directory), which becomes
            geodata by this name. Nodes are never ground: a pack's
            Nodes layer is left out.
          </div>
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
          <q-btn flat no-caps color="primary" label="Import" :loading="busy"
                 :disable="!file || !name.trim()" @click="doImport" />
        </q-card-actions>
      </q-card>
    </q-dialog>

    <q-dialog v-model="creating">
      <q-card style="min-width: 380px">
        <q-card-section class="text-subtitle2">New synthetic ground</q-card-section>
        <q-card-section class="column q-gutter-sm">
          <q-input v-model="name" dense outlined label="name" />
          <q-input v-model.number="exponent" type="number" step="0.1" dense outlined
                   label="path-loss exponent" hint="2 free space, 2.7 suburban, 3.5 built-up" />
          <q-input v-model.number="extentKm" type="number" dense outlined label="extent (km across)" />
        </q-card-section>
        <q-card-actions align="right">
          <q-btn flat no-caps label="Cancel" v-close-popup />
          <q-btn flat no-caps color="primary" label="Create" :disable="!name.trim()" @click="create" />
        </q-card-actions>
      </q-card>
    </q-dialog>
  </q-page>
</template>

<script setup lang="ts">
/* Three sections. The ground there is, with the three ways of making or
 * bringing more: New synthetic, Build (its own view, BuildView, and then a
 * row here while it builds), and Import zip; a click on a row opens that
 * geodata on its own, with no nodes on it, and from there Export zip takes
 * it elsewhere; the checkboxes choose rows for what is done to several.
 * What the listed indexes offer pre-built (IndexOffers). And the build's
 * sources, with what each holds in the cache. */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useQuasar } from 'quasar'
import { matDeleteOutline, matEdit } from '@quasar/extras/material-icons'
import GroundMap from '../components/GroundMap.vue'
import DisplayMenu from '../components/DisplayMenu.vue'
import PlaceSearch from '../components/PlaceSearch.vue'
import BuildView from '../components/BuildView.vue'
import IndexOffers from '../components/IndexOffers.vue'
import SlippyMap from '../components/SlippyMap.vue'
import { boxGeometry, type Geometry, type MapShape } from '../lib/mapshape'
import SelectBar from '../components/SelectBar.vue'
import { useSelection } from '../lib/selection'
import { NBSP, keepUnits, sizeText } from '../lib/size'
import { useCatalog, type BuildRow, type GeodataInfo, type SourceRow } from '../stores/catalog'
import { zipGeodataName } from '../lib/zip'
import { useGeodata } from '../stores/geodata'
import { useDisplay } from '../stores/display'
import { useNodes } from '../stores/nodes'
import { useSim } from '../stores/sim'
import { request, upload } from '../lib/front'
import { whenSaved } from '../lib/unsaved'

const catalog = useCatalog()
const ground = useGeodata()
const display = useDisplay()
const nodes = useNodes()
const sim = useSim()
const quasar = useQuasar()
const map = ref<InstanceType<typeof GroundMap>>()
const preview = ref<string | null>(null)
const importing = ref(false)
const creating = ref(false)
const file = ref<File | null>(null)
const name = ref('')
const exponent = ref(2.7)
const extentKm = ref(20)
const busy = ref(false)
const building = ref(false)
const sel = useSelection(() => catalog.geodata.map(g => g.name))
const sources = ref<SourceRow[]>([])

onMounted(() => { void loadSources() })

/* One source on the map beside the list: where it has data, tinted (the
 * world for a worldwide one), and what of it is in the cache, filled. */
interface SourceArea {
  source: string; worldwide: boolean; covers: Geometry; covers_from: string | null
  cached: Geometry; cached_files: number; error: string | null
}
const WORLD: [number, number, number, number] = [-180, -84, 180, 84]
const shownSource = ref<string | null>(null)
const sourceArea = ref<SourceArea | null>(null)
const sourceMap = ref<InstanceType<typeof SlippyMap>>()
const sourceShapes = computed<MapShape[]>(() => {
  const a = sourceArea.value
  if (!a) return []
  return [
    { geometry: a.worldwide ? boxGeometry(WORLD) : a.covers,
      fill: 'rgba(59, 130, 246, 0.10)', stroke: 'rgba(59, 130, 246, 0.7)', width: 1 },
    { geometry: a.cached, fill: 'rgba(234, 88, 12, 0.35)', stroke: 'rgba(234, 88, 12, 0.9)', width: 1 },
  ]
})

/* A click on the map: the sources with data at that spot, and whether that
 * part of each is cached. The list then shows those alone (the caches with
 * no area to them, the sources' lists and the map tiles, drop out) until
 * All sources. */
interface PointSources { lon: number; lat: number; at: Record<string, { has: boolean; cached: boolean }> }
const point = ref<PointSources | null>(null)
const shownSources = computed(() => point.value
  ? sources.value.filter(s => point.value!.at[s.source]?.has)
  : sources.value)

async function pickPoint([lon, lat]: [number, number]) {
  const r = await request('geodata_sources_at', { lon, lat })
  if (!r.ok) { tell(r.error); return }
  point.value = { lon, lat, at: r.sources as PointSources['at'] }
  if (shownSource.value && !point.value.at[shownSource.value]?.has) {
    shownSource.value = null
    sourceArea.value = null
  }
}

function clearPoint() { point.value = null }

async function showSource(source: string) {
  shownSource.value = source
  sourceArea.value = null
  const r = await request('geodata_source_map', { source })
  if (shownSource.value !== source) return
  if (!r.ok) { tell(r.error); shownSource.value = null; return }
  sourceArea.value = r as unknown as SourceArea
  await nextTick()
  // The cache where there is some, else where the source has data.
  fitTo(sourceArea.value.cached ? 'cached' : 'covers')
}

/** The map on all of where the source has data (the world for a worldwide
 *  one), or on all of what is cached. */
function fitTo(what: 'covers' | 'cached') {
  const a = sourceArea.value, m = sourceMap.value
  if (!a || !m) return
  const box = what === 'cached' ? m.extentOf([a.cached])
    : a.worldwide ? null : m.extentOf([a.covers])
  m.fitBox(box ?? WORLD)
}

async function loadSources() {
  const r = await request('geodata_sources')
  if (r.ok) sources.value = r.sources as SourceRow[]
}

function askClear(s: SourceRow) {
  quasar.dialog({
    title: `Empty ${s.source}`,
    message: `${sizeText(s.bytes)} of ${s.what} goes; a build that needs it fetches it again.`,
    ok: { label: 'Empty', color: 'negative', flat: true, noCaps: true },
    cancel: { flat: true, noCaps: true }, persistent: true,
  }).onOk(async () => {
    const r = await request('geodata_source_clear', { source: s.source })
    tell(r.ok ? null : r.error)
    await loadSources()
  })
}

function describe(g: GeodataInfo | null) {
  if (!g || g.error) return ''
  if (g.kind === 'pack') return `pack, EPSG:${g.crs_epsg ?? '?'}${g.layers?.length ? ` · ${g.layers.length} layers` : ''}`
  return `synthetic, ${g.terrain ?? 'flat'}, exponent ${g.exponent}`
}

function extent(g: GeodataInfo) {
  if (g.error || !g.bbox) return ''
  if (g.kind !== 'pack') return `${((g.extent_m ?? 0) / 1000).toFixed(0)}${NBSP}km square at 0°, 0°`
  const [lon0, lat0, lon1, lat1] = g.bbox
  return `${lat0.toFixed(3)}…${lat1.toFixed(3)} N, ${lon0.toFixed(3)}…${lon1.toFixed(3)} E`
}

/* Opening a geodata chooses it: it is the ground the Nodes tab works on.
 * Another one leaves the nodeset being edited, so the page asks first about
 * unsaved edits. */
function choose(n: string) {
  whenSaved(quasar, () => { void show(n) })
}

/* The nodesets on it and its sidecar are asked for together: neither waits
 * for the other. */
async function show(n: string) {
  preview.value = n
  const [, error] = await Promise.all([nodes.chooseGeodata(n), ground.open(n)])
  if (error) quasar.notify({ type: 'negative', message: error, timeout: 6000 })
}

function back() { preview.value = null }

function tell(error: string | null | undefined, done?: string) {
  if (error) quasar.notify({ type: 'negative', message: error, timeout: 6000 })
  else if (done) quasar.notify({ type: 'positive', message: done, timeout: 4000 })
}

/* Renaming or deleting the chosen geodata leaves the nodeset being edited too. */
function askRename(n: string) {
  const go = () => quasar.dialog({
    title: `Rename ${n}`, message: 'Lower-case letters, digits and hyphens.',
    prompt: { model: n, type: 'text' }, cancel: true,
  }).onOk(async (to: string) => {
    to = to.trim()
    if (!to || to === n) return
    const r = await request('geodata_rename', { name: n, to })
    if (!r.ok) { tell(r.error); return }
    await catalog.refreshGeodata()
    if (nodes.geodata === n) { await nodes.chooseGeodata(null); await show(to) }
    tell(null, `renamed to ${to}`)
  })
  if (nodes.geodata === n) whenSaved(quasar, go)
  else go()
}

/* "run a" or "runs a, b"; nothing for none. */
function named(one: string, many: string, names: string[]) {
  return names.length ? `${names.length === 1 ? one : many} ${names.join(', ')}` : ''
}

/* The geodata named deleted after one confirmation, which says everything
 * that goes with it: the runs, snapshots and other geodata on its packs (the
 * simulations on them stopped), and the nodesets left on no geodata. */
async function askDelete(names: string[]) {
  const plan = await request('geodata_delete_plan', { names })
  if (!plan.ok) { tell(plan.error); return }
  const runs = plan.runs as string[], running = plan.running as string[]
  const also = [
    named('run', 'runs', runs) + (running.length
      ? ` (${named('simulation', 'simulations', running)} stopped first)` : ''),
    named('snapshot', 'snapshots', plan.snapshots as string[]),
    named('geodata', 'geodata', plan.geodata as string[]),
    named('nodeset', 'nodesets', plan.nodesets as string[])
      + ((plan.nodesets as string[]).length ? ', which stand on no other geodata' : ''),
  ].filter(Boolean)
  const size = catalog.geodata.filter(g => names.includes(g.name)).reduce((t, g) => t + (g.bytes ?? 0), 0)
  const go = () => quasar.dialog({
    title: names.length === 1 ? `Delete ${names[0]}` : `Delete ${names.length} geodata`,
    message: `Delete ${names.join(', ')}, packs included (${sizeText(size)})?`
      + (also.length ? ` This also deletes ${also.join('; ')}.` : '')
      + ' This cannot be undone.',
    ok: { label: 'Delete', color: 'negative', flat: true, noCaps: true },
    cancel: { flat: true, noCaps: true }, persistent: true,
  }).onOk(async () => {
    const r = await request('geodata_delete', { names })
    if (r.ok) {
      const deleted = r.deleted as { geodata: string[], nodesets: string[] }
      if (nodes.geodata && deleted.geodata.includes(nodes.geodata)) {
        await nodes.chooseGeodata(null); preview.value = null; await ground.open(null)
      }
      nodes.forget(deleted.nodesets)
    }
    sel.none()
    await catalog.refreshGeodata()
    void catalog.refreshNodesets()
    void catalog.refreshIndexes()
    tell(r.ok ? null : r.error, 'deleted')
  })
  if (nodes.geodata && names.includes(nodes.geodata)) whenSaved(quasar, go)
  else go()
}

function mb(bytes: number) { return `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)}${NBSP}MB` }

/* A build's row: its step, and how far into it. Downloading counts bytes
 * where every host said a size, files where one did not; compiling counts
 * the compiler's steps. */
function progress(b: BuildRow) {
  const step = b.step ?? b.state
  if (b.state === 'fetching' && b.total) {
    const bytes = b.of ? ` (${mb(b.fetched)} of ${mb(b.of)})` : b.fetched ? ` (${mb(b.fetched)})` : ''
    return `${step} ${Math.min(b.done + 1, b.total)}/${b.total}${bytes}`
  }
  if (b.state === 'compiling' && b.total) return `${step} ${b.done + 1}/${b.total}`
  return step
}

function fraction(b: BuildRow): number | null {
  if (b.state === 'fetching' && b.of) return Math.min(1, b.fetched / b.of)
  if (b.state === 'fetching' && b.total) return b.done / b.total
  if (b.state === 'compiling' && b.total) return b.done / b.total
  return null
}

async function cancelBuild() {
  await fetch('/api/geodata/build', { method: 'DELETE' })
  if (catalog.build?.state === 'failed') catalog.build = null
}

async function nameFromZip(f: File | null) {
  if (!f) return
  const given = await zipGeodataName(f)
  if (given && file.value === f) name.value = given
}

/* A click on the tab is the list, whatever was on show. */
watch(() => sim.geodataList, () => { preview.value = null })

/* Coming back to this tab puts the preview's ground back on the map, and
 * the nodeset counts are asked again. */
watch(() => sim.view, (v) => {
  if (v !== 'geodata') return
  void catalog.refreshGeodata()
  void loadSources()
  if (preview.value) void ground.open(preview.value)
})

/* A build that ends has filled the cache. */
watch(() => catalog.build, (b) => { if (!b) void loadSources() })

async function doImport() {
  if (!file.value) return
  busy.value = true
  const r = await upload('/api/geodata/import', name.value.trim(), file.value)
  busy.value = false
  if (!r.ok) { quasar.notify({ type: 'negative', message: r.error ?? 'refused', timeout: 8000 }); return }
  importing.value = false
  file.value = null
  await catalog.refreshGeodata()
  choose(name.value.trim())
}

async function create() {
  const r = await request('geodata_new', {
    name: name.value.trim(),
    synthetic: { terrain: 'flat', exponent: exponent.value, extent_m: extentKm.value * 1000 },
  })
  if (!r.ok) { quasar.notify({ type: 'negative', message: r.error ?? 'refused', timeout: 6000 }); return }
  creating.value = false
  await catalog.refresh()
}
</script>

<style scoped>
.gp { display: flex; flex-direction: column; height: 100%; }
.gp-bar { min-height: 38px; gap: 16px; padding-left: 4px; background: #171b21; border-bottom: 1px solid #262c35; flex: none; }
.gp-title { font-size: 14px; font-weight: 500; padding: 0 10px; }
.gp-sub { font-size: 12px; font-weight: 400; color: #6b7280; padding-left: 8px; }
.gp-map { position: relative; flex: 1 1 auto; min-height: 0; }
/* The whole width: the paragraphs keep their own reading width, the tables
 * and the sources' map take the rest, and the scroll bar is the window's edge. */
.gp-body { padding: 16px 20px 32px; overflow-y: auto; flex: 1 1 auto; min-height: 0; }
.gp-text { font-size: 12px; color: #9ca3af; line-height: 1.5; margin-bottom: 12px; max-width: 760px; }
.gp-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.gp-table th {
  text-align: left; font-weight: 500; font-size: 11px; color: #6b7280;
  padding: 4px 10px; border-bottom: 1px solid #262c35;
}
.gp-table td { padding: 8px 10px; border-bottom: 1px solid #1f242c; vertical-align: top; }
.gp-name { font-weight: 500; }
.gp-row { cursor: pointer; }
.gp-row .gp-name, .gp-row .gp-what { color: var(--q-primary); }
.gp-row:hover td { background: #1b2028; }
.gp-chosen td { background: #1a2130; }
.gp-act { white-space: nowrap; text-align: right; width: 1%; }
.gp-table .num { text-align: right; white-space: nowrap; }
.gp-check { width: 28px; padding-left: 2px !important; padding-right: 0 !important; }
.gp-picked td { background: #172030; }
.gp-sources { display: flex; gap: 16px; align-items: flex-start; }
/* A narrow page: the sources' map above their list. */
@media (max-width: 640px) {
  .gp-body { padding: 12px 16px 24px; }
  .gp-sources { flex-direction: column-reverse; align-items: stretch; }
  .gp-source-side { position: static; min-width: 0; }
  .gp-source-map { height: 50vh; }
}
.gp-sources-list { flex: 1 1 55%; min-width: 0; }
.gp-at { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #d1d5db;
  background: #1a2130; border-radius: 3px; padding: 2px 4px 2px 10px; margin-bottom: 4px; }
.gp-source-side { flex: 1 1 45%; min-width: 280px; position: sticky; top: 0; }
.gp-source-map { height: min(60vh, 520px); border: 1px solid #262c35; border-radius: 4px; }
.gp-source-note { margin-top: 6px; line-height: 1.7; }
.gp-what { border-bottom: 1px dotted #4b5563; cursor: help; }
.gp-fit { cursor: pointer; color: var(--q-primary); border-radius: 3px; padding: 0 4px; margin-left: -4px; width: fit-content; }
.gp-fit:hover { background: #1b2028; color: #d1d5db; }
.gp-key { display: inline-block; width: 12px; height: 10px; border-radius: 2px; vertical-align: -1px; }
.gp-key-covers { background: rgba(59, 130, 246, 0.35); border: 1px solid rgba(59, 130, 246, 0.8); }
.gp-key-cached { background: rgba(234, 88, 12, 0.5); border: 1px solid rgba(234, 88, 12, 0.9); }
.gp-bad { font-size: 11px; color: #fca5a5; }
.gp-building td { background: #161a20; }
.gp-build { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #d1d5db; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
</style>
