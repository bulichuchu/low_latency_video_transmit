// Robot dog control: key mapping and labels. The server (video_demo/dog_control.py)
// turns motion into protocol axes and only sends what the robot's state allows.

// The page sends the keys it holds whatever the robot's state; the server maps
// them (dog_control.py DogClient.set_motion). Walking: W/S forward/back, A/D
// sideways, Q/E turn. In place: W/S look up/down, A/D or Q/E lean the head,
// R/F raise/lower the body, Z/C turn the body (the remote's H + right stick).
export const DRIVE_KEYS = new Set([
  "KeyW",
  "KeyA",
  "KeyS",
  "KeyD",
  "KeyQ",
  "KeyE",
  "KeyR",
  "KeyF",
  "KeyZ",
  "KeyC",
]);
export const PAD_KEYS = [
  { code: "KeyQ", label: "Q", walk: "左转", head: "左探头" },
  { code: "KeyW", label: "W", walk: "前进", head: "抬头" },
  { code: "KeyE", label: "E", walk: "右转", head: "右探头" },
  { code: "KeyA", label: "A", walk: "左移", head: "左探头" },
  { code: "KeyS", label: "S", walk: "后退", head: "低头" },
  { code: "KeyD", label: "D", walk: "右移", head: "右探头" },
];
// In-place mode only.
export const BODY_KEYS = [
  { code: "KeyR", label: "R", head: "升高" },
  { code: "KeyF", label: "F", head: "降低" },
  { code: "KeyZ", label: "Z", head: "左转身" },
  { code: "KeyC", label: "C", head: "右转身" },
];
export const NO_KEYS = { forward: 0, left: 0, turn: 0, twist: 0, height: 0 };

export function motionFromKeys(keys, scale) {
  const axis = (plus, minus) =>
    (keys.has(plus) ? 1 : 0) - (keys.has(minus) ? 1 : 0);
  const s = Math.min(1, Math.max(0, Number(scale) || 0));
  const v = (x) => Math.round(x * s * 1000) / 1000 + 0; // + 0: no -0
  return {
    forward: v(axis("KeyW", "KeyS")),
    left: v(axis("KeyA", "KeyD")),
    turn: v(axis("KeyQ", "KeyE")),
    twist: axis("KeyZ", "KeyC"),
    height: axis("KeyR", "KeyF"),
  };
}

// Text inputs keep their keys; sliders, buttons and the page drive the robot.
export function isTyping(target) {
  if (!target || target.isContentEditable) return Boolean(target);
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  return (
    tag === "INPUT" &&
    !["range", "checkbox", "radio", "button", "submit"].includes(target.type)
  );
}

// Full-stick speeds per level (SDK SetSpeed): forward m/s, sideways m/s, turn rad/s.
const LIMITS = {
  slow: [1, 0.5, 1.5],
  medium: [2, 0.5, 1.5],
  high: [3, 0.5, 1.5],
};
export function speedLimits(level, scale) {
  const limit = LIMITS[level];
  return (
    limit && {
      forward: limit[0] * scale,
      left: limit[1] * scale,
      turn: limit[2] * scale,
    }
  );
}

export const COMMAND_LABELS = {
  "action/stand_up": "站立",
  "action/crawl": "匍匐",
  "action/lie_down": "卧倒",
  "action/locked": "锁定",
  "action/slim": "过窄门",
  "action/gait_walk": "步态行走",
  "action/dsb": "过挡鼠板",
  "action/climb": "爬高台",
  "action/new1new": "扭一扭",
  reverse_head_tail: "头尾交换",
  "knee_mode/same_direction": "同膝",
  "knee_mode/medial_facing": "内对膝",
  "mode/general": "通用模式",
  "mode/in_place": "原地模式",
  "mode/stair": "登阶模式",
  "mode/navigation": "导航模式",
  "mode/follow": "跟随模式",
  "mode/track": "追踪模式",
  "speed/low": "低速档",
  "speed/medium": "中速档",
  "speed/high": "高速档",
  "fill_light/front_light_on": "开前灯",
  "fill_light/front_light_off": "关前灯",
  "fill_light/back_light_on": "开后灯",
  "fill_light/back_light_off": "关后灯",
  "fill_light/light_auto_work_on": "开自动补光",
  "fill_light/light_auto_work_off": "关自动补光",
  "emergency/stop": "急停",
  "emergency/recover": "解除急停",
  connect: "连接",
  disconnect: "断开",
  take_control: "获取控制权",
  release_control: "释放控制权",
  camera_bitrate: "相机码率",
};
// Rows of the panel: [label, [[command, button text], ...]].
export const COMMAND_ROWS = [
  [
    "姿态",
    [
      ["action/stand_up", "站立"],
      ["action/crawl", "匍匐"],
      ["action/lie_down", "卧倒"],
      ["action/locked", "锁定"],
    ],
  ],
  [
    "模式",
    [
      ["mode/general", "通用", "general"],
      ["mode/in_place", "原地", "in_place"],
      ["mode/stair", "登阶", "stair"],
      ["mode/navigation", "导航", "nav"],
      ["mode/follow", "跟随", "follow"],
      ["mode/track", "追踪", "track"],
    ],
  ],
  [
    "特殊姿态",
    [
      ["action/slim", "过窄门"],
      ["action/gait_walk", "步态行走"],
      ["action/dsb", "过挡鼠板"],
      ["action/climb", "爬高台"],
      ["action/new1new", "扭一扭"],
    ],
  ],
  [
    "机身",
    [
      ["reverse_head_tail", "头尾交换"],
      ["knee_mode/same_direction", "同膝"],
      ["knee_mode/medial_facing", "内对膝"],
    ],
  ],
];
// These need a second click within 3 s; the text says why.
export const RISKY = {
  "action/climb": "爬高台是特技：至少两格电、没有加装负载、周围 2 米内无人",
  "action/new1new": "扭一扭是特技：周围 2 米内无人",
  reverse_head_tail: "头尾交换后机器狗以原来的尾部为前，W 会朝原来的尾部走",
  "knee_mode/same_direction": "切换膝关节朝向，机器狗会调整腿部姿态",
  "knee_mode/medial_facing": "切换膝关节朝向，机器狗会调整腿部姿态",
  "mode/navigation": "导航模式需要已建好的地图，本页在导航模式下不发移动",
};
export const HEAD_DIRECTIONS = { head: "头在前", tail: "尾在前" };
// battery.power_supply_status1/2 (protocol 1.2.0).
export const POWER_SUPPLY = {
  0: "未知",
  1: "充电中",
  2: "放电中",
  3: "接电未充",
  4: "已充满",
};
export const KNEES = { same: "同膝", medial_facing: "内对膝" };
export const MOTION_STATES = {
  stand_up: "站立中",
  lie_down: "卧倒",
  crawl: "匍匐",
  locked: "锁定",
  sport: "站立 · 通用",
  walk: "站立 · 通用", // firmware 0.3.2's name for sport
  in_place: "站立 · 原地",
  stair: "站立 · 登阶",
  climb: "爬高台",
  dsb: "挡鼠板",
  nav: "导航",
  slim: "过窄门",
  gait_walk: "步态行走",
  new1new: "扭一扭",
  tuoluo: "陀螺",
  jump: "跳跃",
  handstand: "双足站立",
  backflip: "后空翻",
  follow: "跟随",
  track: "追踪",
};
export const MODES = {
  general: "通用",
  in_place: "原地",
  nav: "导航",
  stair: "登阶",
  follow: "跟随",
  track: "追踪",
};
export const SPEED_LEVELS = {
  slow: "低速",
  medium: "中速",
  high: "高速",
  nav: "导航",
};
export const CONTROL_SOURCES = {
  0: "无人控制",
  1: "遥控器 APP",
  2: "SDK",
  3: "外部协议",
};
export const FAULT_LEVELS = { 0: "正常", 1: "警告", 2: "错误", 3: "严重" };
