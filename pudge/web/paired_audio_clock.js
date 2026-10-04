'use strict';
(() => {
  const now = () => performance.now();
  // A separate clock per surface prevents the sidebar and LN reader from
  // modifying each other's transport intent or DOM-dependent mora map.
  function createClock() {
    let position = 0, at = now(), playing = false, speed = 1, initialized = false;
    let startup = null, correction = 0;
    const current = () => {
      const elapsed = Math.min(2, Math.max(0, now() - at) / 1000);
      return Math.max(0, position + (playing ? elapsed * speed + correction * (1 - Math.exp(-elapsed / .3)) : 0));
    };
    return {
      current,
      reset(state = {}) {
        position = Math.max(0, Number(state.position || 0));
        at = now(); speed = Number(state.speed || 1); playing = !!state.playing;
        initialized = true; startup = playing ? position : null; correction = 0;
      },
      reconcile(state = {}) {
        const backend = Math.max(0, Number(state.position || 0));
        const local = current(), drift = backend - local;
        const nextPlaying = !!state.playing;
        correction = 0;
        if (!initialized || !nextPlaying || !playing || Math.abs(drift) > 1.5) position = backend;
        else if (startup != null && backend <= startup + .03) position = backend;
        else { position = local; correction = drift > 0 ? Math.min(drift, .45) * .35 : 0; }
        if (startup != null && backend > startup + .03) startup = null;
        if (!playing && nextPlaying) startup = backend;
        speed = Number(state.speed || 1); playing = nextPlaying; at = now(); initialized = true;
        return position;
      },
    };
  }
  const pathCache = new WeakMap();
  function pathFor(state) {
    const anchor = state?.anchor_window || {};
    if (pathCache.has(anchor)) return pathCache.get(anchor);
    const rows = Array.isArray(anchor.points) ? anchor.points : Array.isArray(anchor.path) ? anchor.path : [];
    const path = rows.map(row => ({...row, time:Number(row.time), offset:Number(row.offset)}))
      .filter(row => Number.isFinite(row.time) && Number.isFinite(row.offset)).sort((a, b) => a.time - b.time);
    if (path.length >= 2) { pathCache.set(anchor, path); return path; }
    return [{time:Number(anchor.left_time), offset:Number(anchor.left_offset)},
      {time:Number(anchor.right_time), offset:Number(anchor.right_offset)}]
      .filter(row => Number.isFinite(row.time) && Number.isFinite(row.offset));
  }
  function activityDuration(activity, start, end) {
    return activity.reduce((total, row) => {
      const left = Number(row.start ?? row[0]), right = Number(row.end ?? row[1]);
      return total + (Number.isFinite(left) && Number.isFinite(right)
        ? Math.max(0, Math.min(end, right) - Math.max(start, left)) : 0);
    }, 0);
  }
  function offsetAtTime(state, time) {
    const fallback = Number(state?.chapter_char_offset_exact ?? state?.chapter_char_offset);
    const path = pathFor(state), anchor = state?.anchor_window || {};
    if (path.length < 2 || !Number.isFinite(time)) return fallback;
    if (time <= path[0].time) return path[0].offset;
    if (time >= path[path.length - 1].time) return path[path.length - 1].offset;
    const rightIndex = path.findIndex(row => row.time >= time);
    const left = path[rightIndex - 1], right = path[rightIndex];
    if (right.time <= left.time || right.offset < left.offset) return fallback;
    let ratio = (time - left.time) / (right.time - left.time);
    if (anchor.activity_clock === true && right.wall_clock_from_previous !== true && Array.isArray(anchor.activity)) {
      const duration = activityDuration(anchor.activity, left.time, right.time);
      if (duration >= Math.max(.04, (right.time - left.time) * .025)) {
        ratio = activityDuration(anchor.activity, left.time, time) / duration;
      }
    }
    return left.offset + (right.offset - left.offset) * Math.max(0, Math.min(1, ratio));
  }
  globalThis.PudgePairedAudioClock = {createClock, offsetAtTime, pathFor};
})();
