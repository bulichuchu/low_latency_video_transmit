<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { bootstrap } from "./api";
import SenderPanel from "./components/SenderPanel.vue";
import ReceiverPanel from "./components/ReceiverPanel.vue";
const page = ref(location.hash.includes("receiver") ? "receiver" : "sender"),
  ready = ref(false),
  error = ref(""),
  sdkRoot = ref("");
const senderSeen = ref(page.value === "sender"),
  receiverSeen = ref(page.value === "receiver");
function route() {
  page.value = location.hash.includes("receiver") ? "receiver" : "sender";
  if (page.value === "sender") senderSeen.value = true;
  else receiverSeen.value = true;
}
function receiver() {
  location.hash = "/receiver";
}
onMounted(async () => {
  window.addEventListener("hashchange", route);
  try {
    const b = await bootstrap();
    sdkRoot.value = b.sdk_root;
    ready.value = true;
  } catch (e) {
    error.value = e.message;
  }
});
onUnmounted(() => window.removeEventListener("hashchange", route));
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#/sender"
        ><span class="brand-icon">↗</span>
        <div>VIDEO FLOW<small>低延迟多路视频回传</small></div></a
      >
      <div class="nav-label">工作台</div>
      <nav>
        <a href="#/sender" :class="{ active: page === 'sender' }"
          ><span>↗</span>发送端<small>TX</small></a
        ><a href="#/receiver" :class="{ active: page === 'receiver' }"
          ><span>↙</span>接收端<small>RX</small></a
        >
      </nav>
      <div class="sidebar-note">
        <i></i> 本机控制服务
        <p>局域网 H.264 / UDP<br />Vue + WebCodecs</p>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <span>视频传输 / {{ page === "sender" ? "发送控制" : "接收监看" }}</span
        ><span class="local-tag">LOCAL WORKSPACE</span>
      </header>
      <main>
        <div v-if="error" class="error">无法连接本机服务：{{ error }}</div>
        <p v-if="!ready && !error">正在连接本机服务…</p>
        <template v-if="ready"
          ><div v-show="page === 'sender'">
            <SenderPanel
              v-if="senderSeen"
              :sdk-root="sdkRoot"
              @receiver-started="receiver"
            />
          </div>
          <div v-show="page === 'receiver'">
            <ReceiverPanel v-if="receiverSeen" /></div
        ></template>
      </main>
      <footer>
        VIDEO FLOW <span>关闭网页不停止传输，请使用对应端的停止按钮。</span>
      </footer>
    </div>
  </div>
</template>
