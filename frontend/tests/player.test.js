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
function packet(stream, id, key = false, epoch = 1, timing = {}) {
  const meta = new TextEncoder().encode(
    JSON.stringify({
      stream,
      frame_id: id,
      key,
      epoch,
      capture_ms: (id * 1000) / 60,
      host_capture_ms: null,
      ...timing,
    }),
  );
  const bytes = new Uint8Array([0, 0, 0, 1, 0x67, 0x42, 0xc0, 0x1f, 0xff]);
  const result = new Uint8Array(4 + meta.length + bytes.length);
  new DataView(result.buffer).setUint32(0, meta.length);
  result.set(meta, 4);
  result.set(bytes, 4 + meta.length);
  return result.buffer;
}
test("clock offset uses the lowest-RTT recent sample and ignores slow or invalid replies", (t) => {
  const { p, time } = setup(t);
  for (let i = 0; i < 3; i++) {
    time.now = 1100 + i * 100;
    p.control({
      type: "pong",
      t1: time.now - 12,
      t2: 2000 + i * 100,
      t3: 2002 + i * 100,
    });
  }
  assert.equal(p.clockUncertainty, 5); // 12ms total minus 2ms service processing.
  assert.equal(p.clockOffset, 907);
  time.now = 1500;
  p.control({ type: "pong", t1: 998, t2: 3000, t3: 3002 }); // 500ms: too slow to fit clocks.
  assert.equal(p.clockUncertainty, 5);
  time.now = 1600;
  for (const invalid of [
    { t1: "1500", t2: 2000, t3: 2002 },
    { t1: 1590, t2: 2000, t3: 1999 },
    { t1: 1601, t2: 2000, t3: 2001 },
    { t1: 1590, t2: 2000, t3: 2100 },
  ])
    p.control({ type: "pong", ...invalid });
  assert.equal(p.clockUncertainty, 5);
  assert.equal(p.clockOffset, 907);
  // Valid as of the latest pong, not the best sample's own (older) time.
  assert.equal(p.clockSampleAt, 1600);
  p.reset();
  assert.equal(p.clockOffset, null);
});
test("sensor timestamp uses the browser clock offset and keeps camera latency separate", (t) => {
  const { p, time } = setup(t);
  time.now = 1000;
  Object.assign(p, {
    clockOffset: 200,
    clockUncertainty: 1,
    clockSampleAt: 1000,
  });
  p.packet(
    packet(0, 0, true, 1, {
      host_capture_ms: 1180,
      uncertainty_ms: 2,
      host_sensor_capture_ms: 1150,
      sensor_status: "ready",
      sensor_clock_uncertainty_ms: 2.005,
    }),
  );
  p.tick(1000);
  assert.equal(p.counters[0].latency, 20);
  assert.equal(p.counters[0].sensorLatency, 50);
  assert.equal(p.counters[0].sensorUncertainty, 3.005);
  assert.equal(p.telemetry[0].sensor_latency_ms, 50);
  time.now = 6100;
  let report;
  p.onStats = (value) => (report = value);
  p.report();
  assert.equal(report.streams[0].sensorLatency, null);
  assert.equal(report.streams[0].sensorStatus, "clock_sync");
});
test("missing clock, future timestamp and unsupported source never use dequeue time as sensor time", (t) => {
  const { p, time } = setup(t);
  time.now = 1000;
  p.packet(
    packet(0, 0, true, 1, {
      sensor_status: "ready",
      host_sensor_capture_ms: 1150,
    }),
  );
  p.tick(1000);
  assert.equal(p.counters[0].sensorStatus, "clock_sync");
  assert.equal(p.counters[0].sensorLatency, null);
  Object.assign(p, {
    clockOffset: 200,
    clockUncertainty: 1,
    clockSampleAt: 1000,
  });
  time.now = 1020;
  p.packet(
    packet(0, 1, false, 1, {
      sensor_status: "ready",
      host_sensor_capture_ms: 1300,
    }),
  );
  p.tick(1020);
  assert.equal(p.counters[0].sensorStatus, "invalid");
  assert.equal(p.counters[0].sensorLatency, null);
  time.now = 1040;
  p.packet(
    packet(0, 2, false, 1, {
      sensor_status: "unsupported",
      host_capture_ms: 1210,
    }),
  );
  p.tick(1040);
  assert.equal(p.counters[0].latency, 30);
  assert.equal(p.counters[0].sensorLatency, null);
});
test("latency and its steps follow the displayed frame, including fallback and stalls", (t) => {
  const { p, time } = setup(t);
  let report;
  p.onStats = (value) => (report = value);
  time.now = 1000;
  Object.assign(p, {
    clockOffset: 200,
    clockUncertainty: 1,
    clockSampleAt: 1000,
  });
  p.packet(
    packet(0, 0, true, 1, {
      host_capture_ms: 1164.8,
      host_sensor_capture_ms: 1139.5,
      sensor_status: "ready",
      uncertainty_ms: 2,
      sensor_clock_uncertainty_ms: 2.005,
    }),
  );
  p.tick(1000);
  time.now = 1005.8;
  p.report();
  let timing = report.streams[0].timing;
  assert.equal(timing.origin, "camera");
  assert.equal(timing.latency.toFixed(1), "60.5");
  assert.equal(timing.steps.camera.toFixed(1), "25.3");
  // Without a forward time or sender step the rest stays one remote step.
  assert.equal(timing.steps.network.toFixed(1), "35.2");
  assert.equal(timing.steps.viewer, null);
  assert.equal(timing.steps.sender, null);

  // Receiving a different time source does not relabel the visible old frame.
  time.now = 1020;
  p.packet(
    packet(0, 1, false, 1, {
      host_capture_ms: 1200,
      sensor_status: "unsupported",
      uncertainty_ms: 2,
    }),
  );
  p.report();
  assert.equal(report.streams[0].timing.origin, "camera");
  assert.equal(report.streams[0].sensorStatus, "ready");
  p.tick(1020);
  p.report();
  timing = report.streams[0].timing;
  assert.equal(timing.origin, "application");
  assert.equal(timing.latency, 20);
  assert.equal(timing.steps.camera, null);
  assert.equal(timing.steps.network, 20);
  assert.equal(report.streams[0].sensorLatency, null);
  assert.equal(report.streams[0].sensorStatus, "unsupported");
  time.now = 1520;
  p.report();
  assert.equal(report.streams[0].timing.latency, 20); // A stall keeps the last frame's values.
  assert.equal(report.streams[0].fps, 0);
});
test("expired clocks and reconnects cannot reuse stale unified timing", (t) => {
  const { p, time } = setup(t);
  let report;
  p.onStats = (value) => (report = value);
  time.now = 1000;
  Object.assign(p, {
    clockOffset: 200,
    clockUncertainty: 1,
    clockSampleAt: 1000,
  });
  p.packet(
    packet(0, 0, true, 1, {
      host_capture_ms: 1180,
      host_sensor_capture_ms: 1150,
      sensor_status: "ready",
    }),
  );
  p.tick(1000);
  time.now = 6100;
  p.report();
  assert.equal(report.streams[0].timing.origin, null);
  assert.equal(report.streams[0].timing.latency, null);
  assert.equal(report.streams[0].timing.steps, null);
  assert.equal(report.streams[0].latency, null);
  assert.equal(report.streams[0].sensorStatus, "clock_sync");
  p.reset();
  Object.assign(p, { clockSampleAt: 6100, clockOffset: 200 });
  p.report();
  assert.equal(report.streams[0].timing.latency, null);
  assert.equal(report.streams[0].timing.origin, null);
  assert.equal(report.streams[0].timing.steps, null);
});
test("ordered steps add up to the total; only the cross-machine split is adjusted", (t) => {
  const { p, time } = setup(t);
  let report;
  p.onStats = (value) => (report = value);
  Object.assign(p, {
    clockOffset: 200,
    clockUncertainty: 1,
    clockSampleAt: 1000,
  });
  const frame = (id, forwarded) =>
    packet(0, id, id === 0, 1, {
      host_sensor_capture_ms: 1150 + id * 100, // page clock 950
      host_capture_ms: 1164 + id * 100, // 964
      sender_ms: 7,
      host_sent_ms: forwarded + id * 100,
      sensor_status: "ready",
    });
  time.now = 1000; // received and decoded (synchronous decoder)
  p.packet(frame(0, 1194)); // forwarded at 994
  time.now = 1012;
  p.tick(time.now);
  p.report();
  const { latency, steps } = report.streams[0].timing;
  assert.equal(latency, 62);
  assert.deepEqual(steps, {
    camera: 14,
    sender: 7,
    network: 23,
    viewer: 6,
    decode: 0,
    display: 12,
  });
  const sum = (s) => Object.values(s).reduce((a, b) => a + (b ?? 0), 0);
  assert.equal(sum(steps), latency);
  // A clock estimate that puts the forward before the encode completes moves
  // the deficit to the viewer step; the total and the other steps are kept.
  time.now = 1100;
  p.packet(frame(1, 1170)); // forwarded 1 ms "before" capture + sender
  time.now = 1112;
  p.tick(time.now);
  p.report();
  const skewed = report.streams[0].timing;
  assert.equal(skewed.steps.network, 0);
  assert.equal(skewed.steps.viewer, 29);
  assert.equal(sum(skewed.steps), skewed.latency);
});
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
