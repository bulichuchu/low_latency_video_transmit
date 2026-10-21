import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FrameMatcher,
  Playout,
  PLAYOUT_MAX_MS,
  ageLimit,
  avcCodec,
} from "../src/media/matcher.js";
const config = {
  streams: 2,
  max_age_ms: 100,
  sync_mode: "aligned",
  sync_wait_ms: 8,
  sync_tolerance_ms: 18,
};
function frame(stream, capture, epoch = 1) {
  const f = {
    meta: { stream, capture_ms: capture, epoch },
    capture,
    decoded: capture,
    closed: false,
  };
  f.frame = { close: () => (f.closed = true) };
  return f;
}
test("pairs within tolerance, releases superseded and expired native frames", () => {
  const matcher = new FrameMatcher(config),
    old = frame(0, 10),
    a = frame(0, 30),
    b = frame(1, 35);
  matcher.add(old);
  matcher.add(a);
  matcher.add(b);
  assert.deepEqual(matcher.poll(40), [a, b]);
  assert.equal(old.closed, true);
  const expired = frame(0, 45);
  matcher.add(expired);
  assert.deepEqual(matcher.poll(160), []);
  assert.equal(expired.closed, true);
});
test("missing camera waits only its budget and next poll makes progress", () => {
  const matcher = new FrameMatcher(config),
    a = frame(0, 10);
  matcher.add(a);
  assert.deepEqual(matcher.poll(12), []);
  assert.deepEqual(matcher.poll(20), [a]);
  assert.deepEqual(matcher.poll(30), []);
});
test("latest mode does not wait; max six decoded resources; epoch resets", () => {
  const matcher = new FrameMatcher({
      ...config,
      sync_mode: "latest",
      rtp_playout: "min",
    }),
    items = Array.from({ length: 10 }, (_, i) => frame(0, i));
  items.forEach((x) => matcher.add(x));
  assert.equal(matcher.queues[0].length, 6);
  assert.deepEqual(matcher.poll(10), [items.at(-1)]);
  assert.ok(items.slice(0, -1).every((x) => x.closed));
  const a = frame(0, 20),
    b = frame(0, 25, 2);
  matcher.add(a);
  matcher.add(b);
  assert.ok(a.closed);
  matcher.clear();
  assert.ok(b.closed);
});
test("derives AVC profile/constraints/level from SPS for both Annex B start codes", () => {
  assert.equal(
    avcCodec(new Uint8Array([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x1f, 0xff])),
    "avc1.42c01f",
  );
  assert.equal(
    avcCodec(new Uint8Array([0, 0, 1, 0x67, 0x64, 0, 0x28, 0xff])),
    "avc1.640028",
  );
  assert.equal(avcCodec(new Uint8Array([0, 0, 1, 0x65, 1, 2, 3, 4])), null);
});

test("frame age limit grows by two round trips on a long path", () => {
  assert.equal(ageLimit(config, { net_rtt_ms: null }), 100);
  assert.equal(ageLimit(config, {}), 100);
  assert.equal(ageLimit(config, { net_rtt_ms: 35 }), 170); // NetBird over the internet
  assert.equal(ageLimit(config, { net_rtt_ms: 900 }), 400); // at most 2 x 150 ms
  const m = new FrameMatcher(config);
  const late = frame(0, 0);
  late.meta.net_rtt_ms = 35;
  m.add(late);
  m.poll(150);
  assert.equal(late.closed, false); // 150 ms old: within 100 + 2 x 35
});

// A frame captured at `capture` (sender clock, here = page clock) that reached
// the page `transit` ms later, on a path with this round trip.
function arrived(stream, capture, transit, rtt = 40) {
  const f = frame(stream, capture);
  f.capture = null; // no clock mapping needed for the playout buffer
  f.decoded = capture + transit;
  f.meta.net_rtt_ms = rtt;
  return f;
}
test("latest mode shows a late frame instead of keeping an older one", () => {
  const m = new FrameMatcher({ ...config, sync_mode: "latest" });
  const late = frame(0, 0);
  late.meta.net_rtt_ms = 35;
  m.add(late);
  assert.deepEqual(m.poll(900), [late]); // 900 ms old, beyond 100 + 2 x 35
  assert.equal(late.closed, false);
});
// Plays arrivals through a matcher at 60 Hz; returns the frames shown.
function play(matcher, frames) {
  const shown = [];
  let i = 0;
  const end = frames.at(-1).decoded + 300;
  for (let now = 0; now < end; now += 1000 / 60) {
    while (i < frames.length && frames[i].decoded <= now)
      matcher.add(frames[i++]);
    shown.push(...matcher.poll(now));
  }
  return shown;
}
test("playout buffer shows frames that arrive in pairs at their own cadence", () => {
  const live = { ...config, streams: 1, sync_mode: "latest", display_fps: 60 };
  // Every even frame waits for the odd one (as over the dog's Wi-Fi uplink):
  // pairs arrive together 33 ms apart.
  const pairs = () =>
    Array.from({ length: 240 }, (_, i) =>
      arrived(0, i * 33.3, i % 2 ? 40 : 73.3),
    );
  const frames = pairs(),
    m = new FrameMatcher(live);
  const shown = new Set(play(m, frames));
  assert.ok(frames.slice(30).every((f) => shown.has(f))); // none replaced
  assert.ok(Math.abs(m.buffer(0) - 33.3) < 0.5, `buffer ${m.buffer(0)}`);
  const unbuffered = new Set(
    play(new FrameMatcher({ ...live, rtp_playout: "min" }), pairs()),
  );
  assert.ok(unbuffered.size <= 121); // without it: one of each pair
});
test("after a stall the newest due frame shows; nothing is held over 100 ms", () => {
  const live = { ...config, streams: 1, sync_mode: "latest", display_fps: 60 };
  const frames = Array.from({ length: 150 }, (_, i) =>
    arrived(0, i * 33.3, i % 5 === 4 ? 70 : 40),
  );
  const t = 150 * 33.3,
    stall = Array.from({ length: 6 }, (_, i) =>
      arrived(0, t + i * 33.3, (5 - i) * 33.3 + 40),
    ); // a 200 ms stall: six frames arrive together
  const after = Array.from({ length: 10 }, (_, i) =>
    arrived(0, t + (6 + i) * 33.3, 40),
  );
  const m = new FrameMatcher(live),
    all = [...frames, ...stall, ...after];
  const shown = play(m, all);
  assert.ok(Math.abs(m.buffer(0) - 30) < 0.5, `buffer ${m.buffer(0)}`);
  assert.ok(shown.includes(stall.at(-1)) && !shown.includes(stall[0]));
  assert.ok(after.every((f) => shown.includes(f)));
  assert.ok(all.every((f) => f.due <= f.decoded + PLAYOUT_MAX_MS + 1e-9));
});
test("playout buffer: on only on a long path, rises at once, falls slowly", () => {
  const p = new Playout();
  assert.equal(p.observe(1000, 960, 3), 1000); // LAN: shown at once
  assert.equal(p.on, false);
  p.observe(1033, 993, 40);
  assert.equal(p.on, true);
  p.observe(1066, 1026, 10); // stays on above 8 ms
  assert.equal(p.on, true);
  p.observe(1100, 1060, 7);
  assert.equal(p.on, false);
  const q = new Playout();
  let t = 0;
  for (let i = 0; i < 30; i++, t += 33.3) q.observe(t + 40, t, 40);
  assert.ok(Math.abs(q.delay) < 1e-9);
  // 1.3 s with every third frame 150 ms late: the margin rises as soon as a
  // tenth of the last 4 s is late, capped.
  for (let i = 0; i < 40; i++, t += 33.3) {
    q.observe(t + (i % 3 === 2 ? 190 : 40), t, 40);
    if (i === 20) assert.equal(q.delay, PLAYOUT_MAX_MS);
  }
  // Calm: the late frames leave the 4 s window, then the margin falls 20 ms/s.
  for (let i = 0; i < 150; i++, t += 33.3) q.observe(t + 40, t, 40);
  assert.ok(q.delay > 50 && q.delay < 80, `delay ${q.delay}`);
  for (let i = 0; i < 120; i++, t += 33.3) q.observe(t + 40, t, 40);
  assert.ok(Math.abs(q.delay) < 1e-9);
});
test("playout buffer can be turned off and restarts with a new sender", () => {
  const off = new FrameMatcher({
    ...config,
    streams: 1,
    sync_mode: "latest",
    rtp_playout: "min",
  });
  const f = arrived(0, 0, 40);
  off.add(f);
  assert.equal(f.due, undefined);
  assert.equal(off.buffer(0), null);
  const m = new FrameMatcher({ ...config, streams: 1, sync_mode: "latest" });
  m.add(arrived(0, 0, 40));
  m.add(arrived(0, 33, 140));
  const next = arrived(0, 66, 40);
  next.meta.epoch = 2;
  m.add(next);
  assert.equal(m.playouts[0].history.length, 1);
  const aligned = new FrameMatcher(config);
  assert.equal(aligned.buffered, false);
  // A frame without a known round trip keeps the buffer as it was (review 10-21).
  const unknown = arrived(0, 99, 40);
  unknown.meta.epoch = 2;
  unknown.meta.net_rtt_ms = null;
  m.add(unknown);
  assert.equal(m.playouts[0].on, true);
});
