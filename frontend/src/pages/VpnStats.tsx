import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Wifi, WifiOff, Users, LogIn, Download, RefreshCw, AlertTriangle } from "lucide-react";
import { vpn, ApiError } from "@/api";
import type { VpnUserStatus, VpnSummary } from "@/api";
import TopBar from "@/components/TopBar";
import Card from "@/components/Card";
import Button from "@/components/Button";
import DataTable, { type Column } from "@/components/DataTable";
import { TableSkeleton } from "@/components/Skeleton";
import PageTransition from "@/components/PageTransition";
import { useToast } from "@/components/Toast";

const POLL_MS = 30000;

const EMPTY_SUMMARY: VpnSummary = {
  total_users: 0,
  online_now: 0,
  total_logins: 0,
  most_active_userid: null,
  most_active_logins: 0,
};

// Compact "time since" label for a VPN last-seen timestamp (e.g. "3m ago").
function formatLastSeen(iso: string | null): string {
  if (!iso) return "Never";
  const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function VpnStats() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [users, setUsers] = useState<VpnUserStatus[]>([]);
  const [summary, setSummary] = useState<VpnSummary>(EMPTY_SUMMARY);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  const load = useCallback(async () => {
    try {
      const r = await vpn.overview();
      setEnabled(r.enabled);
      setUsers(r.users);
      setSummary(r.summary ?? EMPTY_SUMMARY);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Failed to read VPN status");
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll on an interval while the page is open.
  useEffect(() => {
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      await load();
      if (!stopped) timer.current = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      stopped = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load]);

  const handleExport = async () => {
    setExporting(true);
    try {
      const blob = await vpn.report();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `vpn-activity-${new Date().toISOString().slice(0, 10)}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast("error", err instanceof ApiError ? err.detail : "PDF export failed");
    } finally {
      setExporting(false);
    }
  };

  const columns: Column<VpnUserStatus>[] = [
    {
      key: "name",
      label: "Name / Email",
      sortable: true,
      sortValue: (u) => u.full_name.toLowerCase(),
      render: (u) => (
        <div>
          <div className="text-[15px] text-text-primary">{u.full_name}</div>
          <div className="text-xs text-text-muted">{u.email}</div>
        </div>
      ),
    },
    {
      key: "userid",
      label: "UserID",
      render: (u) => (
        <span className="text-[15px] font-mono text-text-secondary">{u.ludus_userid}</span>
      ),
    },
    {
      key: "range",
      label: "Range",
      render: (u) => (
        <span className="text-[15px] font-mono text-text-secondary">{u.range_id || "-"}</span>
      ),
    },
    {
      key: "session",
      label: "Range Group",
      sortable: true,
      sortValue: (u) => u.session_name.toLowerCase(),
      render: (u) => (
        <button
          onClick={(e) => {
            e.stopPropagation();
            navigate(`/sessions/${u.session_id}`);
          }}
          className="text-[15px] text-accent-info hover:text-accent-success transition-colors"
        >
          {u.session_name}
        </button>
      ),
    },
    {
      key: "vpn",
      label: "VPN",
      sortable: true,
      sortValue: (u) => (u.online ? 1 : 0),
      render: (u) => (
        <div className="flex items-center gap-1.5">
          <span
            className={`inline-block h-2 w-2 rounded-full ${u.online ? "bg-accent-success" : "bg-text-muted"}`}
          />
          <span className={`text-xs ${u.online ? "text-accent-success" : "text-text-muted"}`}>
            {u.online ? "Online" : "Offline"}
          </span>
        </div>
      ),
    },
    {
      key: "logins",
      label: "Logins",
      sortable: true,
      sortValue: (u) => u.login_count,
      render: (u) => <span className="text-[15px] text-text-primary">{u.login_count}</span>,
    },
    {
      key: "last_seen",
      label: "Last Seen",
      sortable: true,
      sortValue: (u) => (u.last_seen ? new Date(u.last_seen).getTime() : 0),
      render: (u) => (
        <span
          className="text-xs text-text-secondary"
          title={u.last_seen ? new Date(u.last_seen).toLocaleString() : "Never connected"}
        >
          {formatLastSeen(u.last_seen)}
        </span>
      ),
    },
  ];

  const offline = Math.max(0, summary.total_users - summary.online_now);
  const stats = [
    { label: "Online", value: summary.online_now, icon: Wifi, accent: "text-accent-success" },
    { label: "Offline", value: offline, icon: WifiOff, accent: "text-text-muted" },
    { label: "Provisioned Users", value: summary.total_users, icon: Users, accent: "text-accent-info" },
    { label: "Total Logins", value: summary.total_logins, icon: LogIn, accent: "text-accent-warning" },
  ];

  return (
    <>
      <TopBar
        breadcrumbs={[{ label: "VPN Activity" }]}
        actions={
          enabled && !error ? (
            <Button
              variant="secondary"
              icon={<Download />}
              onClick={handleExport}
              disabled={exporting || users.length === 0}
            >
              {exporting ? "Exporting..." : "Export PDF"}
            </Button>
          ) : undefined
        }
      />

      <PageTransition className="p-4 md:p-8 space-y-6">
        <div>
          <h1 className="text-2xl md:text-[32px] leading-tight font-bold text-text-primary">
            VPN Activity
          </h1>
          <p className="text-[15px] text-text-secondary mt-1">
            Live WireGuard connection status and login history for every provisioned user
          </p>
        </div>

        {!loading && !enabled && (
          <Card className="flex items-start gap-3 p-5 border-accent-warning/30 bg-accent-warning/5">
            <AlertTriangle className="h-5 w-5 text-accent-warning shrink-0 mt-0.5" />
            <div>
              <p className="text-[15px] font-medium text-text-primary">VPN tracking is disabled</p>
              <p className="text-sm text-text-secondary mt-1">
                Set <code className="font-mono">VPN_TRACKING_ENABLED=true</code> and configure{" "}
                <code className="font-mono">VPN_WG_MODE</code> (and the SSH settings for a remote
                Ludus host) on the backend, then restart it.
              </p>
            </div>
          </Card>
        )}

        {!loading && enabled && error && (
          <Card className="flex items-start gap-3 p-5 border-accent-danger/30 bg-accent-danger/5">
            <AlertTriangle className="h-5 w-5 text-accent-danger shrink-0 mt-0.5" />
            <div>
              <p className="text-[15px] font-medium text-text-primary">Could not read VPN status</p>
              <p className="text-sm text-text-secondary mt-1">{error}</p>
              <p className="text-xs text-text-muted mt-1">
                Check that the backend can run <code className="font-mono">wg show</code> on the
                Ludus host (SSH reachable, key valid, permission to read the interface).
              </p>
            </div>
          </Card>
        )}

        {enabled && !error && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {stats.map((s) => (
              <Card key={s.label} variant="stat" className="flex items-start justify-between">
                <div>
                  <p className="text-[13px] font-medium uppercase tracking-wider text-text-secondary">
                    {s.label}
                  </p>
                  <p className={`text-[28px] font-bold leading-none mt-2 ${s.accent}`}>{s.value}</p>
                </div>
                <s.icon className={`h-6 w-6 ${s.accent} opacity-60`} />
              </Card>
            ))}
          </div>
        )}

        {loading ? (
          <TableSkeleton rows={6} cols={7} />
        ) : enabled && !error ? (
          <DataTable
            columns={columns}
            data={users}
            keyExtractor={(u) => u.student_id}
            searchable
            searchPlaceholder="Search users..."
            searchFilter={(u, q) =>
              [u.full_name, u.email, u.ludus_userid, u.session_name]
                .join(" ")
                .toLowerCase()
                .includes(q.toLowerCase())
            }
            pageSize={15}
            emptyState={
              <div className="flex flex-col items-center justify-center py-12">
                <RefreshCw className="h-10 w-10 text-text-muted mb-3" />
                <p className="text-text-secondary">No provisioned users yet</p>
                <p className="text-sm text-text-muted">
                  Users appear here once their ranges are deployed and a VPN config is issued.
                </p>
              </div>
            }
          />
        ) : null}
      </PageTransition>
    </>
  );
}
