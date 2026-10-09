// Preserve stream order even when an older sender has no device labels.
export function streamTitles(names, count) {
  const labels = Array.from({ length: count }, (_, index) => {
    const name = Array.isArray(names) ? names[index] : null;
    return typeof name === "string" ? name.trim().slice(0, 120) : "";
  });
  return labels.map((name, index) => {
    if (!name) return `摄像头 ${index + 1}`;
    return labels.filter((label) => label === name).length > 1
      ? `${name} · 第 ${index + 1} 路`
      : name;
  });
}
