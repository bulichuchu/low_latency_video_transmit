import { test } from "node:test";
import assert from "node:assert/strict";
import { FrameMatcher, avcCodec } from "../src/media/matcher.js";
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
  const matcher = new FrameMatcher({ ...config, sync_mode: "latest" }),
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
