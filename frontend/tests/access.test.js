import { test } from "node:test";
import assert from "node:assert/strict";
import { videoUrl } from "../src/api.js";

test("HTTPS UI uses WSS on the same host and port", () => {
  assert.equal(
    videoUrl({ protocol: "https:", host: "qnbot-macmini.qnbot.net:8765" }),
    "wss://qnbot-macmini.qnbot.net:8765/api/video?token=",
  );
});

test("local HTTP and SSH-forwarded UI retain WS on the forwarded port", () => {
  assert.equal(
    videoUrl({ protocol: "http:", host: "127.0.0.1:18765" }),
    "ws://127.0.0.1:18765/api/video?token=",
  );
});
