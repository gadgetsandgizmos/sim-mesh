import { defineStore } from 'pinia'
import { request } from '../lib/front'
import { useSim } from './sim'
import { useCatalog, type NodesetRow } from './catalog'
import { DEFAULT_ANTENNA, type Antenna } from '../lib/antennas'
import type { OtherNode } from '../lib/marks'

/* The Nodes tab: the nodeset on its geodata, the selection, and every edit.
 *
 * Standalone, nothing is shown until a geodata is chosen (chooseGeodata);
 * the one chosen last in this browser is chosen again when the page loads.
 * Then the tab is a **list** of the nodesets with a node on it (`sets`),
 * beside a map that only shows: the nodesets checked in the list (`checked`)
 * are loaded as their files stand (`viewed`) and drawn there together, each
 * in its colour, their nodes off the geodata left out. Save selection as
 * writes one new nodeset of the checked ones, earlier in the list first
 * (nodeset_merge). **Opening** one leaves the list for the map that edits
 * it: `nodeset`, whose nodes are the selection's, the editor's, the links'
 * and the coverage's, and which Save writes; its nodes off the geodata are
 * put `aside` and written back on Save. Closing it is the list again.
 *
 * Standalone, the nodeset being edited is the page's own until it is saved:
 * every edit is made here, and Save sends the whole file's mapping to the
 * front (`nodeset_save {name, data}`), which checks it and writes it.
 * Attached to a running simulation, what is shown is that run's nodeset, as
 * its simd streams it, and every edit is a message to that simd, which
 * changes the run's own copy; Save as keeps it as a nodeset of the store.
 * Either way the page reads one list of nodes (`list`) and edits through one
 * set of actions, so the map, the editor and the tags panel do not care which. */

export type HeightFrom = 'measured' | 'roof' | 'raster' | 'assumed'

/** The tag of a node with no radio: it has no coverage and no links. Every
 *  other node's radio is the one scripts/globals.py gives (catalog.globals). */
export const NO_RADIO = 'no-radio'

/** One node as a nodeset file holds it. A role is a tag (transport, router,
 *  repeater); the radio is the scripts'. */
export interface NodeRecord {
  id: number
  lat: number
  lon: number
  height_m: number
  height_from: HeightFrom
  /** Its maximum power at the antenna connector, in dBm, when it states one
   *  (lib/boards.ts: 22 when it does not). */
  max_dbm?: number
  antenna: Antenna
  tags: string[]
}

export interface Offset { between: [string, string]; db: number; note?: string }

/** A pair's loss stated outright (nodeset.py). The page draws and edits
 *  none: it carries them through a rename, drops a removed node's, and
 *  hands the rest back on a save. */
export interface Link { between: [string, string]; loss_db: number; back_db?: number; note?: string }

/** A nodeset as the front and simd describe it (nodeset.Nodeset.as_dict). */
export interface NodesetData {
  name: string | null
  dirty: boolean
  geometry_hash?: string
  nodes: Record<string, NodeRecord>
  offsets: Offset[]
  /** Only when the file states some. */
  links?: Link[]
}

/** One node as the page shows it: its record, and when a simulation runs it, how it is. */
export interface NodeView extends NodeRecord {
  name: string
  live: boolean
  status?: string
  liveRole?: string | null
  stale?: boolean
  /** Attached: its firmware's base. */
  kind?: string | null
  web?: boolean
  /** Attached: the firmware its script's rules give it, and what that build is called. */
  firmware?: string | null
  deviceName?: string | null
  freq?: number
  sf?: number
  bw?: number
}

/** The fields an edit can change, flat. */
export interface NodeFields {
  id?: number
  lat?: number
  lon?: number
  height_m?: number
  height_from?: HeightFrom
  /** null: the node states none. */
  max_dbm?: number | null
  antenna?: Antenna
  tags?: string[]
}

export type SelectMode = 'replace' | 'add' | 'toggle' | 'remove'

/** One row of the Nodes tab's list: a nodeset with a node on the geodata,
 *  how many it has and how many of them stand on it. */
export interface SetRow {
  name: string; nodes: number; inside: number
  /** The nodeset's size on disk, with its own setup script. */
  bytes: number | null
}

type Bbox = [number, number, number, number]

function within(bbox: Bbox | null, n: { lat: number; lon: number }): boolean {
  if (!bbox) return true
  const [lon0, lat0, lon1, lat1] = bbox
  return n.lat >= lat0 && n.lat <= lat1 && n.lon >= lon0 && n.lon <= lon1
}

/** Where the geodata chosen last is kept, per browser, to be chosen again
 *  when the page is loaded while it is still installed. */
const CHOSEN_KEY = 'sim-mesh.geodata'

/** The geodata chosen last in this browser, if any. */
export function chosenBefore(): string | null {
  try { return localStorage.getItem(CHOSEN_KEY) } catch { return null }
}

/** The colours the list's nodesets are drawn in, by their place in it. */
export const SET_COLOURS = ['#f472b6', '#34d399', '#60a5fa', '#fbbf24', '#c084fc', '#f87171', '#2dd4bf', '#fb923c']

/** Before a simulation is started from a nodeset by name: when it is the
 *  one the Nodes tab is editing, with changes not saved, save them, since a
 *  simulation runs the file. The error, or null. */
export async function saveIfEditing(name: string | null): Promise<string | null> {
  const nodes = useNodes()
  if (!name || nodes.attached || nodes.nodeset?.name !== name || !nodes.nodeset.dirty) return null
  return await nodes.save()
}

export const useNodes = defineStore('nodes', {
  state: () => ({
    nodeset: null as NodesetData | null,
    /** The nodes of the nodeset being edited that stand outside the geodata:
     *  not loaded, and written back as they were on Save. */
    aside: {} as Record<string, NodeRecord>,
    /** The geodata the Nodes tab stands on, by name: none until one is
     *  chosen, on the Geodata tab or the Nodes tab's own list (chooseGeodata). */
    geodata: null as string | null,
    selection: [] as string[],
    /** The two nodes the pair inspector shows. */
    pair: null as [string, string] | null,
    /** The list: the nodesets with a node on the geodata. */
    sets: [] as SetRow[],
    /** The ones checked in the list, which its map shows. */
    checked: new Set<string>(),
    /** The checked nodesets, as their files stand. */
    viewed: {} as Record<string, NodesetData>,
  }),

  getters: {
    attached: (): boolean => useSim().attached,
    /** The chosen geodata's extent, [lon0, lat0, lon1, lat1]. */
    bbox(): Bbox | null {
      return useCatalog().geodata.find(g => g.name === this.geodata)?.bbox ?? null
    },
    /** The nodeset on show: the run's when attached, else the one being edited. */
    data(): NodesetData | null { return this.attached ? useSim().nodeset : this.nodeset },
    open(): boolean { return this.data !== null },
    dirty(): boolean { return this.data?.dirty ?? false },
    list(): NodeView[] {
      const sim = useSim()
      if (this.attached) {
        const records = sim.nodeset?.nodes ?? {}
        return sim.nodeList.map(n => ({
          ...(records[n.name] ?? {
            id: n.id, lat: n.lat, lon: n.lon, height_m: n.height_m, height_from: n.height_from,
            max_dbm: n.max_dbm, antenna: n.antenna, tags: n.tags,
          }),
          id: n.id, lat: n.lat, lon: n.lon, height_m: n.height_m,
          antenna: n.antenna ?? { type: DEFAULT_ANTENNA },
          tags: n.tags ?? [], name: n.name, live: true, status: n.status, liveRole: n.role,
          stale: n.stale, kind: n.base, web: n.web, firmware: n.firmware,
          deviceName: n.device_name,
          freq: n.freq, sf: n.sf, bw: n.bw,
        }))
      }
      return Object.entries(this.nodeset?.nodes ?? {})
        .map(([name, r]) => ({ ...r, name, live: false }))
        .sort((a, b) => a.id - b.id)
    },
    byName(): Record<string, NodeView> {
      return Object.fromEntries(this.list.map(n => [n.name, n]))
    },
    names(): string[] { return this.list.map(n => n.name) },
    /** Every tag with how many nodes carry it. */
    tags(): [string, number][] {
      const count = new Map<string, number>()
      for (const n of this.list) for (const t of n.tags) count.set(t, (count.get(t) ?? 0) + 1)
      return [...count.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    },
    offsets(): Offset[] { return this.data?.offsets ?? [] },
    /** The lowest id no node has. */
    nextId(): number {
      const taken = new Set([...this.list, ...Object.values(this.aside)].map(n => n.id))
      let id = 1
      while (taken.has(id)) id++
      return id
    },
    /** The open nodeset's name; '' for one not saved yet, null for none. */
    active(): string | null {
      if (!this.nodeset) return null
      return this.nodeset.name ?? ''
    },
    colourOf(): (set: string) => string {
      const order = this.sets.map(l => l.name)
      return (set: string) => SET_COLOURS[Math.max(0, order.indexOf(set)) % SET_COLOURS.length]!
    },
    /** The checked nodesets, in the list's order. */
    checkedNames(): string[] { return this.sets.map(s => s.name).filter(n => this.checked.has(n)) },
    /** The checked nodesets' nodes on the geodata, for the list's map. */
    viewedNodes(): OtherNode[] {
      const out: OtherNode[] = []
      for (const set of this.checkedNames) {
        const data = this.viewed[set]
        if (!data) continue
        const colour = this.colourOf(set)
        for (const [name, n] of Object.entries(data.nodes)) {
          if (within(this.bbox, n)) out.push({ layer: set, name, lat: n.lat, lon: n.lon, colour })
        }
      }
      return out
    },
  },

  actions: {
    /* ── selection ── */
    select(name: string | null, mode: SelectMode = 'replace') {
      if (name === null) { if (mode === 'replace') this.selection = []; return }
      this.selectMany([name], mode)
    },
    selectMany(names: string[], mode: SelectMode = 'replace') {
      const now = new Set(mode === 'replace' ? [] : this.selection)
      for (const n of names) {
        if (mode === 'remove') now.delete(n)
        else if (mode === 'toggle' && now.has(n)) now.delete(n)
        else now.add(n)
      }
      this.selection = this.names.filter(n => now.has(n))
    },
    selectTag(tag: string, mode: 'add' | 'remove') {
      this.selectMany(this.list.filter(n => n.tags.includes(tag)).map(n => n.name), mode)
    },

    /* ── edits: the run's when attached, else the page's own ── */
    touched(_geometry: boolean) {
      if (!this.nodeset) return
      this.nodeset.dirty = true
    },

    /** A nodeset to edit: the one open, or a new unsaved one when none is. */
    ensureOpen() {
      if (!this.attached && !this.nodeset) this.adopt({ name: null, dirty: false, nodes: {}, offsets: [] })
    },

    place(name: string, lat: number, lon: number, fields: NodeFields = {}): boolean {
      this.ensureOpen()
      if (this.byName[name] || (!this.attached && this.aside[name])) return false
      const record = {
        ...(fields.antenna ? { antenna: fields.antenna } : {}),
        ...(typeof fields.max_dbm === 'number' ? { max_dbm: fields.max_dbm } : {}),
        ...(fields.height_m !== undefined ? { height_m: fields.height_m } : {}),
        ...(fields.height_from ? { height_from: fields.height_from } : {}),
        ...(fields.tags ? { tags: fields.tags } : {}),
      }
      if (this.attached) {
        useSim().addNode(name, lat, lon, record)
      } else {
        if (!this.nodeset) return false
        this.nodeset.nodes[name] = {
          id: fields.id ?? this.nextId, lat, lon, height_m: fields.height_m ?? 2,
          height_from: fields.height_from ?? 'assumed',
          ...(typeof fields.max_dbm === 'number' ? { max_dbm: fields.max_dbm } : {}),
          antenna: fields.antenna ?? { type: DEFAULT_ANTENNA }, tags: fields.tags ?? [],
        }
        this.touched(true)
      }
      this.selection = [name]
      return true
    },

    move(name: string, lat: number, lon: number, settle: boolean) {
      if (this.attached) { useSim().moveNode(name, lat, lon, settle); return }
      const n = this.nodeset?.nodes[name]
      if (!n || !settle) return
      n.lat = lat
      n.lon = lon
      this.touched(true)
    },

    /** The same fields on every node named; a field not given is left alone. */
    setMany(names: string[], fields: NodeFields) {
      if (!Object.keys(fields).length) return
      if (this.attached) {
        for (const name of names) useSim().setNode(name, fields as Record<string, unknown>)
        return
      }
      const geometry = fields.lat !== undefined || fields.lon !== undefined || fields.height_m !== undefined
      for (const name of names) {
        const n = this.nodeset?.nodes[name]
        if (!n) continue
        const { antenna, max_dbm, ...rest } = fields
        Object.assign(n, rest)
        if (antenna !== undefined) n.antenna = { ...antenna }
        if (max_dbm === null) delete n.max_dbm
        else if (max_dbm !== undefined) n.max_dbm = max_dbm
      }
      this.touched(geometry)
    },

    /** A tag onto every node named, or off it, their other tags untouched. */
    tag(names: string[], tag: string, on: boolean) {
      for (const name of names) {
        const now = this.byName[name]?.tags ?? []
        const next = on ? (now.includes(tag) ? now : [...now, tag]) : now.filter(t => t !== tag)
        if (next !== now && next.join() !== now.join()) this.setMany([name], { tags: next })
      }
    },

    /** A node's name is its reference everywhere, so a rename carries its offsets and links. */
    rename(name: string, to: string): boolean {
      const ns = this.nodeset
      if (this.attached || !ns?.nodes[name] || ns.nodes[to] || this.aside[to] || !to) return false
      const nodes: Record<string, NodeRecord> = {}
      for (const [k, v] of Object.entries(ns.nodes)) nodes[k === name ? to : k] = v
      ns.nodes = nodes
      for (const o of [...ns.offsets, ...(ns.links ?? [])]) o.between = o.between.map(n => (n === name ? to : n)) as [string, string]
      this.selection = this.selection.map(n => (n === name ? to : n))
      if (this.pair) this.pair = this.pair.map(n => (n === name ? to : n)) as [string, string]
      this.touched(true)
      return true
    },

    remove(names: string[]) {
      if (this.attached) { for (const n of names) useSim().removeNode(n) }
      else if (this.nodeset) {
        for (const n of names) delete this.nodeset.nodes[n]
        this.nodeset.offsets = this.nodeset.offsets.filter(o => !o.between.some(e => names.includes(e)))
        if (this.nodeset.links) this.nodeset.links = this.nodeset.links.filter(l => !l.between.some(e => names.includes(e)))
        this.touched(true)
      }
      this.selection = this.selection.filter(n => !names.includes(n))
      if (this.pair?.some(n => names.includes(n))) this.pair = null
    },

    /** The dB added between two nodes; 0 removes it. */
    setOffset(a: string, b: string, db: number, note = '') {
      if (this.attached) { useSim().setOffset(a, b, db, note); return }
      const ns = this.nodeset
      if (!ns) return
      const rest = ns.offsets.filter(o =>
        !((o.between[0] === a && o.between[1] === b) || (o.between[0] === b && o.between[1] === a)))
      ns.offsets = db ? [...rest, { between: [a, b], db, ...(note ? { note } : {}) }] : rest
      this.touched(false)
    },

    /* ── files, through the front ── */
    /** A nodeset to edit: its nodes on the geodata loaded, the rest put aside. */
    adopt(data: NodesetData) {
      const aside: Record<string, NodeRecord> = {}
      for (const [name, n] of Object.entries(data.nodes)) {
        n.antenna = n.antenna?.type ? n.antenna : { type: DEFAULT_ANTENNA }
        n.tags = n.tags ?? []
        if (!within(this.bbox, n)) { aside[name] = n; delete data.nodes[name] }
      }
      data.offsets = data.offsets ?? []
      this.nodeset = data
      this.aside = aside
      this.selection = []
      this.pair = null
    },

    /** The nodeset file's mapping, as nodeset_save takes it: the nodes put
     *  aside go back in as they were. */
    fileData(): Record<string, unknown> {
      const d = this.data!
      return { nodes: this.attached ? d.nodes : { ...this.aside, ...d.nodes }, offsets: d.offsets,
               ...(d.links?.length ? { links: d.links } : {}) }
    },

    /** Another geodata to stand on: whatever was open on the one before is
     *  closed. The page asks first about unsaved edits. */
    async chooseGeodata(name: string | null) {
      if (name === this.geodata) return
      this.geodata = name
      try {
        if (name) localStorage.setItem(CHOSEN_KEY, name); else localStorage.removeItem(CHOSEN_KEY)
      } catch { /* private window */ }
      this.close()
      this.viewed = {}
      this.sets = []
      this.checked = new Set()
      await this.loadSets()
      // The list starts with every nodeset checked, all of them on its map.
      this.checked = new Set(this.sets.map(s => s.name))
      await this.loadViewed()
    },

    /** The nodeset being edited closed, unsaved edits and all: the list again. */
    close() {
      this.nodeset = null
      this.aside = {}
      this.selection = []
      this.pair = null
    },

    /** The edits dropped: a saved nodeset back as its file stands, an unsaved one gone. */
    async discard() {
      const name = this.nodeset?.name
      if (name) await this.openSet(name)
      else this.close()
    },

    /** A nodeset gone for good; when it is the one being edited, nothing is. */
    async deleteNodeset(name: string): Promise<string | null> {
      const r = await request('nodeset_delete', { name })
      if (!r.ok) return r.error ?? 'could not delete it'
      this.forget([name])
      void useCatalog().refreshGeodata()
      return null
    },

    /** Nodesets deleted elsewhere (with the geodata they stood on) let go of:
     *  closed when one is being edited, off the list and the map. */
    forget(names: string[]) {
      if (this.nodeset?.name && names.includes(this.nodeset.name)) this.close()
      for (const name of names) delete this.viewed[name]
      this.sets = this.sets.filter(l => !names.includes(l.name))
      this.checked = new Set([...this.checked].filter(n => !names.includes(n)))
    },

    async save(): Promise<string | null> {
      if (this.attached) return 'a running simulation\'s nodeset is kept with Save as'
      if (!this.nodeset?.name) return 'the nodeset has no name yet: Save as'
      return this.saveAs(this.nodeset.name, false)
    },
    async saveAs(name: string, fresh = true): Promise<string | null> {
      if (!this.data) return 'no nodeset open'
      const r = await request(fresh ? 'nodeset_save_as' : 'nodeset_save', { name, data: this.fileData() })
      if (!r.ok) return r.error ?? 'could not save it'
      if (!this.attached && this.nodeset) {
        const saved = r.nodeset as NodesetData
        this.nodeset.name = saved.name
        this.nodeset.dirty = false
        this.nodeset.geometry_hash = saved.geometry_hash
        delete this.viewed[saved.name ?? '']
        await this.loadSets()
      }
      return null
    },
    /** A new nodeset from an import source (nodeset_import), opened. */
    async importNodes(fields: Record<string, unknown>): Promise<string | null> {
      const r = await request('nodeset_import', { ...fields, geodata: this.geodata })
      if (!r.ok) return r.error ?? 'could not import it'
      await this.openData(r.nodeset as NodesetData)
      return null
    },

    /* ── the list ── */

    /** The list for the geodata: every nodeset with a node on it, by name,
     *  and the one open even with none. A nodeset whose file changed size is
     *  loaded again for the map; the checked ones not loaded yet are loaded. */
    async loadSets() {
      if (!this.geodata) { this.sets = []; return }
      const r = await request('nodeset_list', { geodata: this.geodata })
      if (!r.ok) return
      const rows = (r.nodesets as (NodesetRow & { inside?: number })[]).filter(n => !n.error)
      const open = this.active
      const sets = rows.filter(n => n.inside || n.name === open)
        .map(n => ({ name: n.name, nodes: n.nodes ?? 0, inside: n.inside ?? 0, bytes: n.bytes ?? null }))
      const before = new Map(this.sets.map(s => [s.name, s.bytes]))
      for (const name of Object.keys(this.viewed)) {
        const now = sets.find(s => s.name === name)
        if (!now || now.bytes !== before.get(name)) delete this.viewed[name]
      }
      this.sets = sets
      const here = new Set(sets.map(s => s.name))
      if ([...this.checked].some(n => !here.has(n))) this.checked = new Set([...this.checked].filter(n => here.has(n)))
      await this.loadViewed()
    },

    /** The checked nodesets not loaded yet, loaded for the map. */
    async loadViewed() {
      await Promise.all(this.checkedNames.filter(n => !this.viewed[n]).map(async (name) => {
        const r = await request('nodeset_open', { name })
        if (r.ok) this.viewed[name] = r.nodeset as NodesetData
      }))
    },

    /** A nodeset opened to edit, as its file stands. The page asks first
     *  about unsaved edits. */
    async openSet(name: string): Promise<string | null> {
      const r = await request('nodeset_open', { name })
      if (!r.ok) return r.error ?? 'could not open it'
      await this.openData(r.nodeset as NodesetData)
      return null
    },

    async openData(data: NodesetData) {
      this.adopt(data)
      await this.loadSets()
    },

    /** New: an empty nodeset by this name, opened. */
    async newNodeset(name: string): Promise<string | null> {
      const r = await request('nodeset_new', { name })
      if (!r.ok) return r.error ?? 'could not make it'
      await this.openData(r.nodeset as NodesetData)
      return null
    },

    /** Save selection as: the checked nodesets as their files stand, earlier
     *  in the list first, as one new nodeset, which is then the one checked. */
    async mergeAs(name: string): Promise<string | null> {
      await this.loadViewed()
      const layers = this.checkedNames.map(l => {
        const data = this.viewed[l]
        // What the map shows: each nodeset's nodes on the geodata.
        const nodes = data ? Object.fromEntries(Object.entries(data.nodes).filter(([, n]) => within(this.bbox, n))) : {}
        // An offset or a link comes along where both its nodes do; the merge
        // refuses one naming a node it has not, such as one off the geodata.
        const kept = new Set(Object.keys(nodes))
        const held: Offset[] = data?.offsets ?? []
        const offsets = held.filter(o => o.between.every(e => kept.has(e)))
        const stated: Link[] = data?.links ?? []
        const links = stated.filter(k => k.between.every(e => kept.has(e)))
        return { name: l, data: data ? { nodes, offsets, ...(links.length ? { links } : {}) } : null }
      }).filter(l => l.data)
      if (!layers.length) return 'no nodeset is checked'
      const r = await request('nodeset_merge', { name, layers })
      if (!r.ok) return r.error ?? 'could not save it'
      this.viewed[name] = r.nodeset as NodesetData
      this.checked = new Set([name])
      await this.loadSets()
      return null
    },

  },
})
