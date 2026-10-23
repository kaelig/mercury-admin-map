// The sweep's parent-first dispatch queue (KTD10). A state is handed out only once the state it
// replays through has its raw file written; the child of an unreachable parent comes back marked
// `blocked` with the root cause, so the sweep writes it as blocked-by-parent without replaying it,
// and the mark passes down to every descendant in turn.
//
//   const q = new Queue(ids, parentOf, { done, onIdle });
//   for (let item; (item = await q.next());) { ...; q.finish(item.id, { unreachable, rootCause }); }
//
// `ids` are the states to sweep, in dispatch order; `parentOf(id)` is the state `id` replays through,
// or null. `done` maps states that already have a valid raw file to their outcome, so a resumed
// sweep releases their children. An outcome is {unreachable, rootCause}: rootCause is the state the
// failure started at (the state itself, or its parent's root cause when it was blocked).
//
// hold(id) parks a dispatched state without an outcome: it no longer counts as in flight. When
// nothing is ready or in flight, onIdle(heldIds) may return [[id, outcome], ...] to settle held
// states, which can release more work. stop() hands every waiting worker null.

export class Queue {
  constructor(ids, parentOf, { done = {}, onIdle = null } = {}) {
    this.outcomes = new Map(Object.entries(done));
    this.children = new Map();
    this.ready = [];
    this.inflight = new Set();
    this.held = new Set();
    this.waiters = [];
    this.stopped = false;
    this.onIdle = onIdle;
    const todo = new Set(ids);
    // A parent chain that never leaves `todo` loops: release those states as if they had no parent.
    const loops = (id) => {
      let p = parentOf(id);
      for (let n = 0; p && todo.has(p); n++) {
        if (n > todo.size) return true;
        p = parentOf(p);
      }
      return false;
    };
    for (const id of ids) {
      const p = parentOf(id);
      if (p && todo.has(p) && !loops(id)) {
        if (!this.children.has(p)) this.children.set(p, []);
        this.children.get(p).push(id);
      } else {
        this.ready.push(this.item(id, p && this.outcomes.has(p) ? p : null));
      }
    }
  }

  item(id, parent) {
    const o = parent ? this.outcomes.get(parent) : null;
    return { id, blocked: o && o.unreachable ? o.rootCause : null };
  }

  // The next state to sweep, {id, blocked}, or null once nothing is left (or the queue stopped).
  next() {
    return new Promise((resolve) => {
      this.waiters.push(resolve);
      this.pump();
    });
  }

  settle(id, outcome) {
    this.outcomes.set(id, outcome);
    this.inflight.delete(id);
    this.held.delete(id);
    for (const child of this.children.get(id) || []) this.ready.push(this.item(child, id));
    this.children.delete(id);
  }

  finish(id, outcome) {
    this.settle(id, outcome);
    this.pump();
  }

  hold(id) {
    this.inflight.delete(id);
    this.held.add(id);
    this.pump();
  }

  stop() {
    this.stopped = true;
    this.ready = [];
    this.pump();
  }

  // States never handed out: waiting on a parent, or ready when the queue stopped.
  get pending() {
    let n = this.ready.length;
    for (const ids of this.children.values()) n += ids.length;
    return n;
  }

  pump() {
    while (this.waiters.length) {
      if (this.stopped) {
        this.waiters.shift()(null);
      } else if (this.ready.length) {
        const item = this.ready.shift();
        this.inflight.add(item.id);
        this.waiters.shift()(item);
      } else if (this.inflight.size) {
        return;
      } else {
        const settled = this.held.size && this.onIdle ? this.onIdle([...this.held]) || [] : [];
        if (!settled.length) {
          this.waiters.shift()(null);
          continue;
        }
        for (const [id, outcome] of settled) this.settle(id, outcome);
      }
    }
  }
}
