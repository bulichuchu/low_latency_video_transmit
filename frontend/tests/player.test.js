import { test } from "node:test";
import assert from "node:assert/strict";
import { VideoPlayer } from "../src/media/player.js";
import { DisplayPacer } from "../src/media/pacer.js";

function setup(t) {
  const old = {
    document: globalThis.document,
    VideoDecoder: globalThis.VideoDecoder,
    EncodedVideoChunk: globalThis.EncodedVideoChunk,
    requestAnimationFrame: globalThis.requestAnimationFrame,
  };
  const time = { now: 0 };
  t.mock.method(performance, "now", () => time.now);
  globalThis.document = { hidden: false };
  globalThis.EncodedVideoChunk = class {
    constructor(data) {
      Object.assign(this, data);
    }
  };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.VideoDecoder = class {
    constructor(callbacks) {
      this.callbacks = callbacks;
      this.state = "unconfigured";
      this.decodeQueueSize = 0;
    }
    configure() {
      this.state = "configured";
    }
    close() {
      this.state = "closed";
    }
    decode(chunk) {
      this.callbacks.output({
        timestamp: chunk.timestamp,
        displayWidth: 1280,
        displayHeight: 720,
        close() {},
      });
    }
  };
  t.after(() => {
    for (const [key, value] of Object.entries(old)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  });
  const p = Object.create(VideoPlayer.prototype);
  const messages = [],
    draws = [0, 0];
  Object.assign(p, {
    decoders: new Map(),
    keyRequests: new Map(),
    counters: {},
    visible: {},
    telemetry: [],
    clock: [],
    send: (m) => messages.push(m),
    onError: (e) => assert.fail(e),
    canvasFor: (s) => ({
      width: 1280,
      height: 720,
      getContext: () => ({ drawImage: () => draws[s]++ }),
    }),
    onStats: () => {},
    stopped: false,
  });
  p.control({
    type: "config",
    streams: 2,
    display_fps: 60,
    max_age_ms: 100,
    sync_mode: "latest",
    sync_wait_ms: 8,
    sync_tolerance_ms: 18,
  });
  return { p, time, messages, draws };
}
function packet(stream, id, key = false, epoch = 1) {
  const meta = new TextEncoder().encode(
    JSON.stringify({
      stream,
      frame_id: id,
      key,
      epoch,
      capture_ms: (id * 1000) / 60,
      host_capture_ms: null,
    }),
  );
  const bytes = new Uint8Array([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x1f, 0xff]);
  const result = new Uint8Array(4 + meta.length + bytes.length);
  new DataView(result.buffer).setUint32(0, meta.length);
  result.set(meta, 4);
  result.set(bytes, 4 + meta.length);
  return result.buffer;
}
test("a gap ending at an IDR resumes both streams without asking the bridge to pause again", (t) => {
  const { p, messages, draws, time } = setup(t);
  for (const s of [0, 1]) {
    p.packet(packet(s, 0, true));
    p.packet(packet(s, 1));
  }
  p.packet(packet(0, 10, true)); // Recovered IDR after missing P frames.
  p.packet(packet(0, 11));
  p.packet(packet(1, 2));
  assert.equal(messages.filter((m) => m.type === "key").length, 0);
  assert.equal(p.counters[0].resets.frame_gap, 1);
  assert.equal(p.counters[1].dropped, 0);
  time.now = 20;
  p.tick(time.now);
  assert.deepEqual(draws, [1, 1]);
  assert.equal(p.decoders.get(0).next, 12);
});
test("a gap at a P frame requests an IDR, suppresses repeated requests, and resumes at the received IDR", (t) => {
  const { p, messages, time } = setup(t);
  p.packet(packet(0, 0, true));
  p.packet(packet(0, 2));
  p.packet(packet(0, 3));
  assert.equal(messages.length, 1);
  assert.equal(messages[0].reason, "frame_gap");
  time.now = 220;
  p.packet(packet(0, 4));
  assert.equal(messages.length, 2); // Bounded retry if a response is lost.
  p.packet(packet(0, 10, true));
  p.packet(packet(0, 11));
  assert.equal(messages.length, 2);
  assert.equal(p.decoders.get(0).next, 12);
});
test("rounded 60 Hz callbacks display two 60 fps streams without a half-rate throttle", (t) => {
  const { p, time, draws } = setup(t);
  for (let n = 0; n < 600; n++) {
    time.now = Math.round(((n * 1000) / 60) * 10) / 10;
    for (const s of [0, 1]) p.packet(packet(s, n, n === 0));
    p.tick(time.now);
  }
  assert.deepEqual(draws, [600, 600]);
  assert.equal(p.matcher.drops, 0);
});
test("pacing respects a 30 fps cap at 60/120 Hz and skips backlog after a pause", () => {
  for (const hz of [60, 120]) {
    const p = new DisplayPacer(30);
    let draws = 0;
    for (let n = 0; n < hz * 10; n++) {
      const now = Math.round(((n * 1000) / hz) * 10) / 10;
      if (p.due(now)) {
        p.submitted(now);
        draws++;
      }
    }
    assert.equal(draws, 300);
    assert.ok(p.due(20000));
    p.submitted(20000);
    assert.equal(p.due(20001), false);
  }
});
test("receive/decode/display counters report separate stages and reset each interval", (t) => {
  const { p, time } = setup(t);
  p.packet(packet(0, 0, true));
  p.packet(packet(0, 1));
  p.tick(0);
  let report;
  p.onStats = (r) => {
    report = r;
  };
  time.now = 500;
  p.report();
  assert.equal(report.streams[0].receiveFps, 4);
  assert.equal(report.streams[0].decodeFps, 4);
  assert.equal(report.streams[0].fps, 2);
  time.now = 1000;
  p.report();
  assert.equal(report.streams[0].receiveFps, 0);
  assert.equal(report.streams[0].decodeFps, 0);
  assert.equal(report.streams[0].fps, 0);
});
