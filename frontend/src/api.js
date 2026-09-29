let token = "";
export async function bootstrap() {
  const result = await api("/bootstrap");
  token = result.token;
  return result;
}
export async function api(path, data) {
  const response = await fetch(
    "/api" + path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Video-Token": token,
          },
          body: JSON.stringify(data),
        },
  );
  const body = await response
    .json()
    .catch(() => ({ error: response.statusText }));
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}
export function videoUrl(pageLocation = location) {
  const protocol = pageLocation.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${pageLocation.host}/api/video?token=${encodeURIComponent(token)}`;
}
