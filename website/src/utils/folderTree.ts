import type { ChatFolder } from '../types'

/** Breadcrumb separator — matches the server-side folder_breadcrumb (U+203A). */
export const FOLDER_PATH_SEP = ' › '

export interface OrderedFolder {
  readonly folder: ChatFolder
  /** Ancestor names root→parent (excludes the folder itself). Empty for root folders. */
  readonly ancestors: readonly string[]
  /** Depth in the tree (0 for root folders). Equals ancestors.length. */
  readonly depth: number
  /** Full ancestry path root→leaf, e.g. "Parent › Child". Equals the name for root folders. */
  readonly path: string
}

/**
 * The one order siblings are drawn in: stored `order`, then name as a tie-break
 * (the store permits duplicate order values, so a comparator without the second
 * key would leave the sequence to array position and shuffle on refetch).
 *
 * Both halves exist to agree with the Python reader, because `chat_folder_tree`
 * is what an agent picks a `before`/`after` anchor from and a sequence that
 * differs from the sidebar's makes the anchor wrong.
 *
 * `?? 0` is load-bearing, not defensive. A row written before the field existed
 * carries no `order`, and `GET /api/chat/folders` returns rows verbatim — so
 * `a.order - b.order` would be `NaN`, which is falsy, and the whole comparison
 * would silently fall through to a name-only order. 0 is what
 * `_chat_folder_order` coerces a missing key to.
 *
 * The name tie-break compares LOWERCASED code points rather than calling
 * `localeCompare`. Two reasons, one per side: the host locale would order the
 * same two folders differently for two people (the i18n gate's rule), and
 * ICU collation would not match `_chat_folder_siblings`, which sorts on
 * `lower()` and therefore by code point.
 *
 * `String(x ?? '')` mirrors that reader's `str(f.get("name") or "")`. A folder
 * row is persisted JSON, so `name` can be absent or non-string, and reaching
 * `.toLowerCase()` on that throws inside a comparator — which takes the whole
 * sidebar down, not just the one row.
 *
 * Exported because a folder's position is something an agent can set
 * (`chat_folder_move`'s `before`/`after`), so every surface that draws siblings
 * has to read it the same way — a render path that skips this comparator shows
 * a sequence the person never chose.
 */
export const bySidebarOrder = (a: ChatFolder, b: ChatFolder): number => {
  const byOrder = folderOrder(a) - folderOrder(b)
  if (byOrder !== 0) return byOrder
  // `||`, not `??`: Python reads the name as `str(name or '')`, so a persisted
  // `false` or `0` empties there. `??` would keep it and compare "false"/"0",
  // putting the anchor at a different visible gap than the tool reported.
  const an = String(a.name || '').toLowerCase()
  const bn = String(b.name || '').toLowerCase()
  return an < bn ? -1 : an > bn ? 1 : 0
}

/**
 * A folder's `order` as a finite number, matching Python's
 * `int(folder.get("order") or 0)` for every value JSON can hold.
 *
 * The store is read with a bare `JSON.parse` and never schema-checked, so
 * `order` can arrive as a string, `null`, a bool, or `1e999` (→ `Infinity`).
 * Python catches its coercion failures and reads 0; without the finite check a
 * junk value here becomes `NaN`, and a comparator returning `NaN` leaves the
 * sequence unspecified rather than merely odd — the two sides would then
 * disagree about which gap an anchor names.
 */
const folderOrder = (f: ChatFolder): number => {
  const n = Number(f.order ?? 0)
  return Number.isFinite(n) ? n : 0
}

/**
 * Flatten folders into pre-order (tree) sequence so children sit directly under
 * their parent, siblings sorted by `order` then name. Each entry carries its
 * ancestor names (for breadcrumb rendering) and depth (for indentation).
 * Orphans (parent_id pointing at a missing folder) are treated as roots.
 * Cycle/depth guarded.
 *
 * Shared by the folder pickers (move-to-folder submenu, new-chat-in-folder)
 * so the indented tree ordering stays identical everywhere.
 */
export function orderFoldersWithPaths(folders: readonly ChatFolder[]): OrderedFolder[] {
  const byId = new Map(folders.map(f => [f.id, f]))
  const childrenOf = (pid: string) =>
    folders
      .filter(f => {
        const parent = f.parent_id && byId.has(f.parent_id) ? f.parent_id : ''
        return parent === pid
      })
      .sort(bySidebarOrder)

  const out: OrderedFolder[] = []
  const walk = (folder: ChatFolder, ancestors: string[], visited: Set<string>) => {
    if (visited.has(folder.id) || ancestors.length > 20) return
    visited.add(folder.id)
    out.push({
      folder,
      ancestors: [...ancestors],
      depth: ancestors.length,
      path: [...ancestors, folder.name].join(FOLDER_PATH_SEP),
    })
    for (const child of childrenOf(folder.id)) walk(child, [...ancestors, folder.name], visited)
  }
  const visited = new Set<string>()
  for (const root of childrenOf('')) walk(root, [], visited)
  // Safety net: surface any folder the walk missed (e.g. a cycle root) so no
  // destination silently disappears from the picker.
  for (const f of folders) if (!visited.has(f.id)) out.push({ folder: f, ancestors: [], depth: 0, path: f.name })
  return out
}

/**
 * Collect a folder's id plus every descendant id (children, grandchildren, …).
 * Used to keep re-parenting acyclic: a folder may not move into itself or any
 * folder inside its own subtree. O(N): one pass builds a parent→children
 * index, then a BFS visits only the subtree; the visited set doubles as the
 * result and guarantees termination on corrupt parent_id cycles.
 */
export function collectFolderSubtreeIds(folders: readonly ChatFolder[], rootId: string): Set<string> {
  const childrenOf = new Map<string, string[]>()
  for (const f of folders) {
    if (!f.parent_id) continue
    const siblings = childrenOf.get(f.parent_id)
    if (siblings) siblings.push(f.id)
    else childrenOf.set(f.parent_id, [f.id])
  }
  const out = new Set<string>([rootId])
  const queue: string[] = [rootId]
  for (let i = 0; i < queue.length; i++) {
    for (const child of childrenOf.get(queue[i]) ?? []) {
      if (!out.has(child)) {
        out.add(child)
        queue.push(child)
      }
    }
  }
  return out
}
