// Runtime settings for the Stock desk UI. Edit to match where Grafana is reachable
// from the browser; the other paths go through the nginx proxy (deploy/ui/nginx.conf).
window.STOCK_DESK_CONFIG = {
  grafanaUrl: "http://localhost:3000",
  // apiBase: "/api", promBase: "/prom", tempoBase: "/tempo", chartsBase: "/charts",
  // opsRefreshMs: 15000, historyTurns: 6,
};
