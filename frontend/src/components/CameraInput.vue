<script setup>
import { computed, ref, watch } from "vue";
const props = defineProps({ record: Object, disabled: Boolean });
const emit = defineEmits(["change"]);
const enabled = ref(false),
  automatic = ref(true),
  modeIndex = ref(0),
  fps = ref(30);
const width = ref(1280),
  height = ref(720),
  pixel = ref(""),
  near = ref(200),
  far = ref(6000);
const mode = computed(() => props.record.modes?.[modeIndex.value]);
watch(
  mode,
  (value) => {
    if (value) fps.value = Math.min(value.max_fps, Math.max(value.min_fps, 30));
  },
  { immediate: true },
);
watch(
  [enabled, automatic, modeIndex, fps, width, height, pixel, near, far],
  () => {
    const settings = { device: props.record.device };
    if (!automatic.value) {
      Object.assign(
        settings,
        mode.value
          ? {
              width: mode.value.width,
              height: mode.value.height,
              pixel_format: mode.value.pixel_format,
              fps: fps.value,
            }
          : { width: width.value, height: height.value, fps: fps.value },
      );
      if (!mode.value && pixel.value) settings.pixel_format = pixel.value;
    }
    if (props.record.sdk_stream === "depth")
      Object.assign(settings, {
        depth_min_mm: near.value,
        depth_max_mm: far.value,
      });
    emit("change", props.record.device, enabled.value ? settings : null);
  },
);
</script>
<template>
  <div class="camera-card" :class="{ selected: enabled }">
    <label class="camera-title"
      ><input v-model="enabled" type="checkbox" :disabled="disabled" /><span>{{
        record.name
      }}</span
      ><span class="tag">{{ record.sdk_stream || "USB / UVC" }}</span></label
    >
    <div v-if="enabled" class="camera-options">
      <label class="check"
        ><input
          v-model="automatic"
          type="checkbox"
          :disabled="disabled"
        />自动匹配输出画质</label
      >
      <template v-if="!automatic">
        <div v-if="record.modes?.length" class="fields two">
          <label
            >设备支持的采集模式<select v-model="modeIndex" :disabled="disabled">
              <option v-for="(m, i) in record.modes" :key="i" :value="i">
                {{ m.width }} × {{ m.height }} · {{ m.pixel_format }} ·
                {{ +m.min_fps.toFixed(3) }}–{{ +m.max_fps.toFixed(3) }} fps
              </option>
            </select></label
          >
          <label
            >采集帧率<input
              v-model.number="fps"
              :disabled="disabled"
              type="number"
              :min="mode?.min_fps || 1"
              :max="mode?.max_fps || 120"
              step="any"
          /></label>
        </div>
        <div v-else class="fields four">
          <label
            >宽<input
              v-model.number="width"
              type="number"
              min="64"
              max="4096"
              :disabled="disabled" /></label
          ><label
            >高<input
              v-model.number="height"
              type="number"
              min="64"
              max="2160"
              :disabled="disabled" /></label
          ><label
            >FPS<input
              v-model.number="fps"
              type="number"
              min="1"
              max="120"
              :disabled="disabled" /></label
          ><label
            >像素格式<input
              v-model="pixel"
              placeholder="后端默认"
              :disabled="disabled"
          /></label>
        </div>
      </template>
      <p class="hint">
        {{
          record.modes?.length
            ? "采集模式来自设备查询；输出分辨率和帧率在下方统一设置。"
            : "未取得结构化模式列表。可手动填写，实际支持情况在启动时验证。"
        }}
      </p>
      <div v-if="record.sdk_stream === 'depth'" class="fields two">
        <label
          >近端 / mm<input
            v-model.number="near"
            :disabled="disabled"
            type="number" /></label
        ><label
          >远端 / mm<input
            v-model.number="far"
            :disabled="disabled"
            type="number"
        /></label>
        <p class="hint">深度灰度预览，不能作为原始深度测距。</p>
      </div>
    </div>
  </div>
</template>
