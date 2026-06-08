// Shevchenko autoresearch — real-time dashboard relay.
// Dependency-free. The pod POSTs a JSON snapshot to /ingest every ~10s;
// the browser polls /api/data and renders. Single in-memory snapshot is enough:
// if the dyno restarts, the pod re-pushes within one interval.

const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = process.env.PORT || 3000;
const SECRET = process.env.INGEST_SECRET || "dev-secret";

let snapshot = { updated: null, experiments: [], summary: {}, running: {}, samples: [] };
let lastIngest = 0;

const INDEX = fs.readFileSync(path.join(__dirname, "public", "index.html"), "utf8");

function send(res, code, body, type = "application/json") {
  res.writeHead(code, {
    "Content-Type": type,
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

const server = http.createServer((req, res) => {
  const url = req.url.split("?")[0];

  if (req.method === "POST" && url === "/ingest") {
    if (req.headers["x-secret"] !== SECRET) return send(res, 401, '{"error":"unauthorized"}');
    let buf = "";
    req.on("data", (c) => {
      buf += c;
      if (buf.length > 5_000_000) req.destroy(); // 5MB guard
    });
    req.on("end", () => {
      try {
        snapshot = JSON.parse(buf);
        lastIngest = Date.now();
        send(res, 200, '{"ok":true}');
      } catch (e) {
        send(res, 400, '{"error":"bad json"}');
      }
    });
    return;
  }

  if (url === "/api/data") {
    const age = lastIngest ? Math.round((Date.now() - lastIngest) / 1000) : null;
    return send(res, 200, JSON.stringify({ ...snapshot, _ageSeconds: age, _stale: age != null && age > 40 }));
  }

  if (url === "/healthz") return send(res, 200, '{"ok":true}', "text/plain");

  if (url === "/" || url === "/index.html") return send(res, 200, INDEX, "text/html; charset=utf-8");

  send(res, 404, '{"error":"not found"}');
});

server.listen(PORT, () => console.log(`shevchenko-dash listening on :${PORT}`));
